"""
Storage for protocol cards: `protocols/<protocol_id>/CARD.md`.

Built on the same VersionedStore as skills, so a card gets the same guarantees: every
write retains what it replaced, writes are atomic, and read-modify-write is locked.

Two things are stricter than for skills, because a card is shared by every route of its
protocol and read by every later run:

  - A write is refused if the card changed on disk since it was read (its
    `card_version` no longer matches). Two runs learning different things about CCTP
    at once must not silently drop one of them. `update()` does the read-modify-write
    under the lock and is the method later steps use.
  - Every source on every entry must be on the documentation allowlist or a known block
    explorer. A card is not a place to launder a citation that discovery would reject.
"""

from pathlib import Path
from typing import Callable, List, Optional

from omnibridge.agent.contract import allowed_domains
from omnibridge.agent.provenance import check_sources
from omnibridge.cards.models import (
    ENTRY_KINDS,
    Card,
    CardDocument,
    ChangeRecord,
    Provenance,
)
from omnibridge.memory.store import VersionedStore, utc_today

#: Where the running system reads and writes cards. At the project root, beside skills/,
#: so a checkout's cards are visible as data rather than as installed code.
LOCAL_CARDS_DIR = Path(__file__).resolve().parents[2] / "protocols"


class CardWriteRefused(Exception):
    """A card write was refused. The message says why; nothing was written."""


class AmbiguousProtocol(Exception):
    """A name matches more than one card. Refusing to pick one."""


def _norm(value: Optional[str]) -> str:
    return (value or "").lower().strip()


class CardManager(VersionedStore):
    document_name = "CARD.md"

    def __init__(self, repository_path: Optional[str] = None):
        self.repo_dir = Path(repository_path) if repository_path else LOCAL_CARDS_DIR
        self.repo_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ reading

    def _card_path(self, protocol_id: str) -> Path:
        return self.repo_dir / protocol_id / self.document_name

    def _parse(self, file_path: Path) -> Optional[CardDocument]:
        try:
            parsed = self.parse_document(file_path)
            if parsed is None:
                return None
            data, body = parsed
            return CardDocument(card=Card(**data), body=body, file_path=str(file_path))
        except Exception as exc:
            # A card that fails validation is not served. Said, not swallowed.
            print(f"[CardManager] Warning: failed to parse {file_path}: {exc}")
            return None

    def list_cards(self) -> List[CardDocument]:
        return [doc for doc in map(self._parse, self.iter_documents([self.repo_dir])) if doc]

    def load(self, protocol_id: str) -> Optional[CardDocument]:
        path = self._card_path(protocol_id)
        return self._parse(path) if path.exists() else None

    def resolve_protocol(self, name: str) -> Optional[str]:
        """
        A card id for this name, matched exactly on id, name or alias. No substrings.

        Raises AmbiguousProtocol when two cards claim the name: picking one is how a
        request for one protocol's contracts gets answered with another's.
        """
        key = _norm(name)
        if not key:
            return None
        hits = sorted({
            doc.card.id
            for doc in self.list_cards()
            if key in {_norm(doc.card.id), _norm(doc.card.name), *map(_norm, doc.card.aliases)}
        })
        if len(hits) > 1:
            raise AmbiguousProtocol(f"{name!r} matches cards {hits}.")
        return hits[0] if hits else None

    # ------------------------------------------------------------------ writing

    def save_card(self, card: Card, cause: str) -> str:
        """
        Write a card, retaining what it replaced.

        `card.card_version` must be the version that was read (0 for a new card). The
        written card carries the next version, today's date and a changelog record.
        """
        path = self._card_path(card.id)
        with self.locked(path):
            return self._write_locked(path, card, cause)

    def update(self, protocol_id: str, change: Callable[[Card], Card], cause: str) -> str:
        """Read, change and write one card under its lock, so no concurrent write is lost."""
        path = self._card_path(protocol_id)
        with self.locked(path):
            current = self._parse(path) if path.exists() else None
            if current is None:
                raise CardWriteRefused(f"No readable card {protocol_id!r} to update.")
            changed = change(current.card.model_copy(deep=True))
            if changed.id != protocol_id:
                raise CardWriteRefused("An update may not change a card's id.")
            return self._write_locked(path, changed, cause)

    def _write_locked(self, path: Path, card: Card, cause: str) -> str:
        existing = self._parse(path) if path.exists() else None
        if path.exists() and existing is None:
            raise CardWriteRefused(
                f"{path} exists but does not parse. Refusing to overwrite it; fix or "
                "remove it by hand."
            )
        on_disk = existing.card.card_version if existing else 0
        if card.card_version != on_disk:
            raise CardWriteRefused(
                f"Card {card.id!r} is at version {on_disk} on disk, but this write was "
                f"based on version {card.card_version}. It changed since it was read."
            )

        self._check_sources(card)

        new = card.model_copy(deep=True)
        new.card_version = on_disk + 1
        new.last_updated = utc_today()
        record = ChangeRecord(
            at=new.last_updated,
            card_version=new.card_version,
            cause=cause,
            changes=self._diff(existing.card if existing else None, new),
        )
        new.changelog = [*new.changelog, record]
        # Round-trip through validation, so a write can never produce a card that the
        # next read would refuse.
        new = Card(**new.model_dump(mode="json"))

        self.retain_revision(path)
        self.write_atomic(path, self.render_document(_frontmatter(new), render_body(new)))
        return str(path)

    @staticmethod
    def _check_sources(card: Card) -> None:
        domains = allowed_domains()
        bad = []
        for kind in ENTRY_KINDS:
            for entry in card.entries(kind):
                checked = check_sources(getattr(entry, "sources", []) or [], domains)
                bad += [f"{kind}:{entry.id} cites {d}" for d in checked["disallowed"]]
        if bad:
            raise CardWriteRefused(
                "Sources outside the documentation allowlist and known explorers: "
                + "; ".join(bad)
            )

    @staticmethod
    def _diff(old: Optional[Card], new: Card) -> List[str]:
        before = old.statuses() if old else {}
        after = new.statuses()
        changes = [f"+ {key} ({after[key]})" for key in after if key not in before]
        changes += [f"- {key}" for key in before if key not in after]
        changes += [
            f"~ {key} {before[key]} -> {after[key]}"
            for key in after
            if key in before and before[key] != after[key]
        ]
        return changes


# ---------------------------------------------------------------------- rendering

def _frontmatter(card: Card) -> dict:
    """
    The card as YAML-ready data, with each entry's identity before its provenance.

    Pydantic orders inherited fields first, which put `status` and `sources` above `id`
    and `address` and made every diff harder to read than the fact it recorded.
    """
    data = card.model_dump(mode="json", exclude_none=True)
    provenance = set(Provenance.model_fields)
    for kind in ENTRY_KINDS:
        data[kind] = [
            {**{k: v for k, v in e.items() if k not in provenance},
             **{k: v for k, v in e.items() if k in provenance}}
            for e in data.get(kind, [])
        ]
    return data


def _short(address: str) -> str:
    return f"`{address}`"


def render_body(card: Card) -> str:
    """
    The Markdown under the frontmatter, for people reading diffs.

    Regenerated on every write, so a hand edit here is overwritten. The agent is never
    shown this: it gets a rendering built from the frontmatter fields, so nothing typed
    into the body can reach a model.
    """
    lines = [
        f"# {card.name}",
        "",
        "Generated from the frontmatter on every write; edits below this line are "
        "overwritten. Statuses: `lead` unchecked, `checked` passed on-chain checks, "
        "`verified` used in an approved broadcast, `demoted` failed after that.",
        "",
    ]
    if not card.executable:
        lines += [f"**Not executable yet.** Blocked on: {', '.join(card.blocked_on) or 'unstated'}.", ""]

    if card.deployments:
        lines += ["## Deployments", "", "| Id | Address | Status |", "|---|---|---|"]
        lines += [f"| {d.id} | {_short(d.address)} | {d.status.value} |" for d in card.deployments]
        lines.append("")
    if card.destinations:
        lines += ["## Destinations", "", "| Chain id | Identifier | Status |", "|---|---|---|"]
        lines += [
            f"| {d.chain_id} | {d.ref.scheme} {d.ref.value} | {d.status.value} |"
            for d in card.destinations
        ]
        lines.append("")
    if card.tokens:
        lines += ["## Tokens", "", "| Id | Address | Decimals | Status |", "|---|---|---|---|"]
        lines += [
            f"| {t.id} | {_short(t.address)} | {t.decimals if t.decimals is not None else '?'} | {t.status.value} |"
            for t in card.tokens
        ]
        lines.append("")
    if card.functions:
        lines += ["## Functions", "", "| Id | Signature | Status |", "|---|---|---|"]
        lines += [f"| {f.id} | `{f.signature}` | {f.status.value} |" for f in card.functions]
        lines.append("")
    if card.parameters:
        lines += ["## Parameters", "", "| Id | Value | Status |", "|---|---|---|"]
        lines += [f"| {p.id} | `{p.value}` | {p.status.value} |" for p in card.parameters]
        lines.append("")
    if card.known_reverts:
        lines += ["## Known reverts", "", "| Revert | Seen | Resolution |", "|---|---|---|"]
        lines += [
            f"| `{r.revert}` | {r.count} | {r.resolution or ''}"
            + (f" ({r.resolution_status.value})" if r.resolution else "")
            + " |"
            for r in card.known_reverts
        ]
        lines.append("")
    if card.changelog:
        lines += ["## Changelog", ""]
        for rec in reversed(card.changelog):
            lines.append(f"- v{rec.card_version} ({rec.at}): {rec.cause}")
            lines += [f"  - {c}" for c in rec.changes]
        lines.append("")
    return "\n".join(lines)
