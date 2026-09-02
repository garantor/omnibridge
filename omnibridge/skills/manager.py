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

import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
from typing import Dict, List, Optional
import yaml
from pydantic import BaseModel, Field


# The repository the running system reads and writes. It sits at the project root
# rather than inside the package so a checkout's skills are visible as data, not as
# installed code.
LOCAL_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
    dest_chain_ref: Optional[str] = None
    token: str
    bridge_protocol: str
    router_address: str
    source_token_address: Optional[str] = None
    dest_token_address: Optional[str] = None
    cctp_destination_domain: Optional[int] = None

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
    # than enumerated in advance.
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

        alias_set = {_norm(a) for a in self.aliases if a}
        src_names = {_norm(self.source_chain)} | alias_set
        dst_names = {_norm(self.dest_chain)} | alias_set
        return _norm(source_chain) in src_names and _norm(dest_chain) in dst_names


class Skill(BaseModel):
    metadata: SkillMetadata
    content: str
    file_path: Optional[str] = None


class SkillManager:
    """Manages discovery, loading, verification, and persistence of SKILL.md modules."""

    def __init__(self, repository_path: Optional[str] = None):
        if repository_path:
            self.repo_dir = Path(repository_path)
        else:
            self.repo_dir = Path(__file__).resolve().parent / "repository"

        self.repo_dir.mkdir(parents=True, exist_ok=True)

    def _parse_skill_file(self, file_path: Path) -> Optional[Skill]:
        """Parses a SKILL.md file with YAML frontmatter."""
        try:
            raw_text = file_path.read_text(encoding="utf-8")
            pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
            match = re.search(pattern, raw_text, re.DOTALL)
            if not match:
                return None

            data = yaml.safe_load(match.group(1))
            if not data or "name" not in data:
                return None

            metadata = SkillMetadata(**data)
            return Skill(metadata=metadata, content=match.group(2), file_path=str(file_path))
        except Exception as e:
            print(f"[SkillManager] Warning: failed to parse {file_path}: {e}")
            return None

    def list_skills(self) -> List[Skill]:
        """Lists all registered skills in the repository and local skills directory."""
        skills = []
        search_dirs = [
            self.repo_dir,
            Path.cwd() / "skills",
            Path(__file__).resolve().parent / "repository",
        ]
        seen_names = set()

        for sdir in search_dirs:
            if not sdir.exists():
                continue
            for root, dirs, files in os.walk(sdir):
                # Retained revisions are history, not candidates.
                dirs[:] = [d for d in dirs if d != "revisions"]
                for file in files:
                    if file.lower() == "skill.md":
                        parsed = self._parse_skill_file(Path(root) / file)
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

    @staticmethod
    def _render(metadata: SkillMetadata, markdown_body: str) -> str:
        header = yaml.dump(metadata.model_dump(exclude_none=True), sort_keys=False)
        return f"---\n{header}---\n\n{markdown_body.strip()}\n"

    def _retain_revision(self, file_path: Path) -> Optional[str]:
        """
        Copy the current SKILL.md aside before it is replaced.

        Human approval is a good filter, not an infallible one, so every overwrite
        stays reversible without depending on the repository being under git.
        """
        if not file_path.exists():
            return None
        revisions_dir = file_path.parent / "revisions"
        revisions_dir.mkdir(parents=True, exist_ok=True)
        archived = revisions_dir / f"SKILL.{_utc_stamp()}.md"
        shutil.copy2(file_path, archived)
        return str(archived.relative_to(file_path.parent))

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

        file_path.write_text(self._render(metadata, markdown_body), encoding="utf-8")
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
        path.write_text(self._render(metadata, skill.content), encoding="utf-8")
        return str(path)
