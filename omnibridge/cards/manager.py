"""
Storage for protocol cards: `protocols/<protocol_id>/CARD.md`.

Built on the same VersionedStore as skills, so a card gets the same guarantees: every
write retains what it replaced, writes are atomic, and read-modify-write is locked.

Stricter than skills in two ways, because a card is shared by every route of its
protocol and read by every later run:

  - A write is refused if the card changed on disk since it was read (its
    `card_version` no longer matches), so two runs learning different things about the
    same protocol cannot silently drop one of them. `update()` does read-modify-write
    under the lock and is what the agent tools and write-back use.
  - Every URL on the card (docs, evidence) must be on the documentation allowlist or a
    known block explorer. A card is not a place to launder a citation discovery would
    reject, and the agent is told to read `docs`.
"""

from pathlib import Path
from typing import Callable, List, Optional

from omnibridge.agent.contract import allowed_domains
from omnibridge.agent.provenance import check_sources
from omnibridge.cards import markdown
from omnibridge.cards.models import Card, CardDocument, RowStatus
from omnibridge.memory.store import VersionedStore, utc_today

#: Where the running system reads and writes cards, beside skills/ at the project root.
LOCAL_CARDS_DIR = Path(__file__).resolve().parents[2] / "protocols"


class CardWriteRefused(Exception):
    """A card write was refused. The message says why; nothing was written."""


class AmbiguousProtocol(Exception):
    """A name matches more than one card. Refusing to pick one."""


def _norm(value: Optional[str]) -> str:
    return " ".join((value or "").lower().split())


class CardManager(VersionedStore):
    document_name = "CARD.md"

    def __init__(self, repository_path: Optional[str] = None):
        self.repo_dir = Path(repository_path) if repository_path else LOCAL_CARDS_DIR
        self.repo_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ reading

    def card_path(self, protocol_id: str) -> Path:
        return self.repo_dir / protocol_id / self.document_name

    def _parse(self, file_path: Path) -> Optional[CardDocument]:
        try:
            card = markdown.parse(Path(file_path).read_text(encoding="utf-8"))
            return CardDocument(card=card, file_path=str(file_path))
        except Exception as exc:
            # A card that fails to parse is not served. Said, not swallowed.
            print(f"[CardManager] Warning: failed to parse {file_path}: {exc}")
            return None

    def list_cards(self) -> List[CardDocument]:
        return [doc for doc in map(self._parse, self.iter_documents([self.repo_dir])) if doc]

    def load(self, protocol_id: str) -> Optional[CardDocument]:
        path = self.card_path(protocol_id)
        return self._parse(path) if path.exists() else None

    def superseded_by(self, card: Card, cards: Optional[List[CardDocument]] = None) -> Optional[Card]:
        """
        The newest card of the same family on the same network, if it is newer than this.

        "Always use the latest version": a superseded card is never offered to the
        agent, never routed through, and never extended.
        """
        pool = cards if cards is not None else self.list_cards()
        newer = [
            d.card for d in pool
            if d.card.family == card.family and d.card.network == card.network
            and d.card.version_number > card.version_number
        ]
        return max(newer, key=lambda c: c.version_number) if newer else None

    def resolve_protocol(self, name: str) -> Optional[str]:
        """
        A card id for this name, matched exactly on id or protocol name. No substrings.

        Raises AmbiguousProtocol when two cards claim the name.
        """
        key = _norm(name)
        if not key:
            return None
        hits = sorted({
            doc.card.protocol_id
            for doc in self.list_cards()
            if key in {_norm(doc.card.protocol_id), _norm(doc.card.protocol_name)}
        })
        if len(hits) > 1:
            raise AmbiguousProtocol(f"{name!r} matches cards {hits}.")
        return hits[0] if hits else None

    # ------------------------------------------------------------------ writing

    def save_card(self, card: Card, cause: str = "") -> str:
        """
        Write a card, retaining what it replaced.

        `card.card_version` must be the version that was read (0 for a new card). The
        written card carries the next version and today's date.
        """
        path = self.card_path(card.protocol_id)
        with self.locked(path):
            return self._write_locked(path, card)

    def update(self, protocol_id: str, change: Callable[[Card], Card], cause: str = "") -> str:
        """Read, change and write one card under its lock, so no concurrent write is lost."""
        path = self.card_path(protocol_id)
        with self.locked(path):
            current = self._parse(path) if path.exists() else None
            if current is None:
                raise CardWriteRefused(f"No readable card {protocol_id!r} to update.")
            changed = change(current.card.model_copy(deep=True))
            if changed.protocol_id != protocol_id:
                raise CardWriteRefused("An update may not change a card's protocol_id.")
            return self._write_locked(path, changed)

    def _write_locked(self, path: Path, card: Card) -> str:
        existing = self._parse(path) if path.exists() else None
        if path.exists() and existing is None:
            raise CardWriteRefused(
                f"{path} exists but does not parse. Refusing to overwrite it; fix or "
                "remove it by hand."
            )
        on_disk = existing.card.card_version if existing else 0
        if card.card_version != on_disk:
            raise CardWriteRefused(
                f"Card {card.protocol_id!r} is at version {on_disk} on disk, but this write "
                f"was based on version {card.card_version}. It changed since it was read."
            )
        self._check_urls(card)

        new = card.model_copy(deep=True)
        new.card_version = on_disk + 1
        new.last_updated = utc_today()
        text = markdown.render(new)
        # Round-trip through the parser, so a write can never produce a card that the
        # next read would refuse.
        try:
            markdown.parse(text)
        except Exception as exc:
            raise CardWriteRefused(f"The written card would not parse back: {exc}") from exc

        self.retain_revision(path)
        self.write_atomic(path, text)
        return str(path)

    @staticmethod
    def _check_urls(card: Card) -> None:
        domains = allowed_domains()
        urls = list(card.docs) + [
            r.evidence for r in card.rows if r.evidence and r.evidence.startswith("https://")
        ]
        disallowed = check_sources(urls, domains)["disallowed"]
        if disallowed:
            raise CardWriteRefused(
                "URLs outside the documentation allowlist and known explorers: "
                + ", ".join(sorted(set(disallowed)))
            )


def status_counts(card: Card) -> dict:
    counts = {s.value: 0 for s in RowStatus}
    for row in card.rows:
        counts[row.status.value] += 1
    return counts
