"""
Skill Manager for persistent procedural cross-chain execution modules (SKILL.md).

A skill is procedural memory: a route that was discovered, simulated and approved
once, recorded so the second identical request never reaches the model. It is a
shortcut past *discovery*, never past the guardrails.

Two properties matter more than convenience here, and both are about not laundering
errors into the cache:

  - A skill is demoted, never overwritten, on failure. A transient RPC error must not
    be able to replace a known-good route with whatever a cold search turned up.
  - Every write retains the revision it replaced, so a bad patch that clears both the
    simulation gate and the human gate is still reversible.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator

from omnibridge.memory.store import VersionedStore, utc_stamp, utc_today


# The repository the running system reads and writes. It sits at the project root
# rather than inside the package so a checkout's skills are visible as data, not as
# installed code.
LOCAL_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


# Kept under their old names for anything that imported them from here.
_utc_today = utc_today
_utc_stamp = utc_stamp


def _norm(value: Optional[str]) -> str:
    return (value or "").lower().strip()


class SkillMetadata(BaseModel):
    name: str
    description: str
    source_chain: str
    source_chain_id: int
    dest_chain: str
    # Optional because a destination that is not EIP-155 addressable has no chain id,
    # and inventing one is exactly the failure `dest_chain_ref` exists to avoid.
    dest_chain_id: Optional[int] = None
    # A structured reference, not a string: {"scheme": "cctp_domain", "value": "6"}.
    # Every other carrier of this field — RouteProposal, RouteQuote.extra_params, the
    # approval payload — has always held the dict, and the scheme is the whole point
    # (a CCTP domain of 6 and an EIP-155 id of 6 are different chains). Typing it as a
    # string here meant every successful agent route raised a validation error while
    # saving its skill — after the transaction had already been broadcast.
    dest_chain_ref: Optional[Dict[str, Any]] = None
    token: str
    bridge_protocol: str
    router_address: str
    source_token_address: Optional[str] = None
    dest_token_address: Optional[str] = None
    cctp_destination_domain: Optional[int] = None

    @field_validator("dest_chain_ref", mode="before")
    @classmethod
    def _accept_a_bare_reference(cls, value: Any) -> Any:
        """
        Take a hand-written or legacy string without refusing to load the skill.

        The scheme is unrecoverable from a bare value, so it is recorded as unknown
        instead of guessed — a reader can tell the difference between "this is a CCTP
        domain" and "this is a number somebody wrote down".
        """
        if isinstance(value, str):
            return {"scheme": "unknown", "value": value}
        return value

    # Network family. A mainnet skill must never answer a testnet request; before this
    # existed, substring matching let "base" serve "base sepolia".
    network: str = "mainnet"

    # What makes a skill replayable without a curated template name (Rule B). With the
    # signature and its parameter types recorded, a cache hit re-encodes through the
    # same generic path an agent proposal takes.
    function_signature: Optional[str] = None
    param_types: List[str] = Field(default_factory=list)
    # Stored with this request's amount and addresses replaced by placeholders
    # (CalldataExecutor.PLACEHOLDERS), so a replay encodes the caller's transfer and
    # not a frozen copy of whichever one happened to be verified first.
    param_values: List[str] = Field(default_factory=list)
    # The ERC-20 approval this route needs, if any. A replay without it rebuilds a
    # single-transaction plan and reverts on allowance exactly as a cold route would.
    spend: Optional[Dict[str, str]] = None

    # The strings that have actually resolved to this route, earned at save time rather
    # than enumerated in advance. Kept per role, because a route has a direction.
    #
    # These used to be one flat list consulted for both ends, which made every skill
    # match its own reverse: an Arbitrum -> Base route carried the aliases
    # {arbitrum, arbitrum one, base}, and a Base -> Arbitrum request found both names in
    # that one pool and replayed it. The router in a replayed reverse route is on the
    # wrong chain, and only a later chain-id check happened to catch it.
    source_aliases: List[str] = Field(default_factory=list)
    dest_aliases: List[str] = Field(default_factory=list)
    #: Retained for skills written before the split, and for display. Never consulted
    #: for matching: a shared pool cannot say which end a name belonged to.
    aliases: List[str] = Field(default_factory=list)

    # Provenance. A skill with no recent successful verification is a hypothesis, and
    # the manifest should be able to say so.
    verified: bool = True
    verification_count: int = 0
    last_verified: Optional[str] = None
    last_failure: Optional[str] = None
    last_failure_at: Optional[str] = None
    sources: List[str] = Field(default_factory=list)
    discovered_by: Optional[str] = None
    revisions: List[str] = Field(default_factory=list)

    rpc_fallbacks: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)

    def matches(self, source_chain: str, dest_chain: str, token: str, network: str) -> bool:
        """Exact match on normalised names or recorded aliases. No substrings."""
        if _norm(self.network) != _norm(network):
            return False
        if self.token.upper().strip() != token.upper().strip():
            return False

        # Each end is matched against its own names only. A skill written before the
        # split has no per-role aliases, so it matches on the recorded chain names
        # alone — narrower than before, and the narrow direction is the safe one.
        src_names = {_norm(self.source_chain)} | {_norm(a) for a in self.source_aliases if a}
        dst_names = {_norm(self.dest_chain)} | {_norm(a) for a in self.dest_aliases if a}
        return _norm(source_chain) in src_names and _norm(dest_chain) in dst_names


class Skill(BaseModel):
    metadata: SkillMetadata
    content: str
    file_path: Optional[str] = None


class SkillManager(VersionedStore):
    """Manages discovery, loading, verification, and persistence of SKILL.md modules."""

    document_name = "SKILL.md"

    def __init__(self, repository_path: Optional[str] = None):
        # An explicit repository means *only* that repository. Without this, a manager
        # pointed at a temporary directory still searched the working tree's ./skills
        # and the packaged repository, so a real run that saved a route made unrelated
        # tests fail — and, worse, a caller that asked for an isolated store silently
        # got the machine's live one as well.
        self.isolated = repository_path is not None

        if repository_path:
            self.repo_dir = Path(repository_path)
        else:
            self.repo_dir = Path(__file__).resolve().parent / "repository"

        self.repo_dir.mkdir(parents=True, exist_ok=True)

    def _parse_skill_file(self, file_path: Path) -> Optional[Skill]:
        """Parses a SKILL.md file with YAML frontmatter."""
        try:
            parsed = self.parse_document(file_path)
            if parsed is None:
                return None
            data, body = parsed
            if "name" not in data:
                return None

            metadata = SkillMetadata(**data)
            return Skill(metadata=metadata, content=body, file_path=str(file_path))
        except Exception as e:
            print(f"[SkillManager] Warning: failed to parse {file_path}: {e}")
            return None

    def list_skills(self) -> List[Skill]:
        """Lists all registered skills in the repository and local skills directory."""
        skills = []
        search_dirs = [self.repo_dir] if self.isolated else [
            self.repo_dir,
            Path.cwd() / "skills",
            Path(__file__).resolve().parent / "repository",
        ]
        seen_names = set()

        # Retained revisions are history, not candidates; iter_documents skips them.
        for path in self.iter_documents(search_dirs):
            parsed = self._parse_skill_file(path)
            if parsed and parsed.metadata.name not in seen_names:
                seen_names.add(parsed.metadata.name)
                skills.append(parsed)
        return skills

    def find_skill(
        self,
        source_chain: str,
        dest_chain: str,
        token: str,
        network: str = "mainnet",
        include_unverified: bool = False,
    ) -> Optional[Skill]:
        """
        Find the cached route for exactly this request, or None.

        Matching is exact on normalised names and recorded aliases. It used to be
        substring-based in both directions, so a skill for "base" answered a request
        for "base sepolia" — a mainnet router returned for a testnet transfer, which
        simulates fine and sends real funds to the wrong network.

        A demoted skill is not returned. It failed its last verification, so serving
        it is serving a known-bad route.
        """
        for skill in self.list_skills():
            if not skill.metadata.matches(source_chain, dest_chain, token, network):
                continue
            if not skill.metadata.verified and not include_unverified:
                continue
            return skill
        return None

    def _skill_dir(self, name: str, target_dir: Optional[str] = None) -> Path:
        return Path(target_dir) if target_dir else self.repo_dir / name

    @classmethod
    def _render(cls, metadata: SkillMetadata, markdown_body: str) -> str:
        return cls.render_document(metadata.model_dump(exclude_none=True), markdown_body)

    def _retain_revision(self, file_path: Path) -> Optional[str]:
        """Copy the current SKILL.md aside before it is replaced. See VersionedStore."""
        return self.retain_revision(file_path)

    def save_skill(
        self,
        metadata: SkillMetadata,
        markdown_body: str,
        target_dir: Optional[str] = None,
    ) -> str:
        """
        Write a skill, retaining whatever it replaced.

        The verification counter carries forward from the existing revision, so a
        re-verified route accumulates evidence instead of resetting to zero each time.
        """
        dest_dir = self._skill_dir(metadata.name, target_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        file_path = dest_dir / "SKILL.md"

        with self.locked(file_path):
            existing = self._parse_skill_file(file_path) if file_path.exists() else None
            archived = self._retain_revision(file_path)

            if existing is not None:
                metadata.revisions = list(existing.metadata.revisions)
                metadata.verification_count = max(
                    metadata.verification_count, existing.metadata.verification_count
                )
            if archived:
                metadata.revisions = [*metadata.revisions, archived]
            if metadata.verified:
                metadata.verification_count += 1
                metadata.last_verified = metadata.last_verified or _utc_today()

            self.write_atomic(file_path, self._render(metadata, markdown_body))
        return str(file_path)

    def mark_unverified(self, skill: Skill, reason: str) -> str:
        """
        Demote a skill that failed verification. Never rewrite it.

        This is the difference between a system that learns and one that launders its
        own errors: a failure records *that* the route stopped working, and the
        replacement lands only once it has itself cleared simulation and the human
        gate. Overwriting here would let a transient RPC error replace a known-good
        route with whatever a cold search turned up.
        """
        if not skill.file_path:
            raise ValueError(f"Skill {skill.metadata.name!r} has no file path to demote.")

        metadata = skill.metadata.model_copy(deep=True)
        metadata.verified = False
        metadata.last_failure = reason
        metadata.last_failure_at = _utc_today()

        path = Path(skill.file_path)
        with self.locked(path):
            # A demotion is a write like any other, so it keeps what it replaced. It
            # used to rewrite in place, which left no record of the verified version.
            archived = self._retain_revision(path)
            if archived:
                metadata.revisions = [*metadata.revisions, archived]
            self.write_atomic(path, self._render(metadata, skill.content))
        return str(path)
