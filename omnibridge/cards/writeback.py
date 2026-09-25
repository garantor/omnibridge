"""
Outcomes write back to cards. Step 4 of the lifecycle in docs/AGENTIC_OMNIBRIDGE.md.

`match_route` works out, in code, which card rows a route used: the row on the source
chain whose contract the route calls, and the row on the destination chain whose
identifier it passes. The model's say-so is not consulted.

On an approved broadcast (`record_success`), the source row becomes `verified` with the
transaction as evidence, and the destination row's identifier is marked `confirmed`.
Each claim covers only what that transaction proved.

On a revert (`record_failure`), the revert is recorded as a failure mode, from what the
node said, and an `unverified` source row becomes `failed`. A `verified` row is not
failed by one revert: a broadcast has already shown its contract is right, and a revert
is as often caused by the parameters as by the address.

Every write here is bookkeeping. The callers wrap it so that a failed write can never
change what the chain did or what the user is told about it.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from web3 import Web3

from omnibridge.cards.manager import CardManager
from omnibridge.cards.models import Card, FailureMode, RowStatus, clean_revert
from omnibridge.memory.store import utc_today


@dataclass
class RouteMatch:
    protocol_id: str
    source_chain_id: int
    role: str
    source_status: str
    dest_chain_id: Optional[int] = None
    dest_id: Optional[str] = None
    #: What the card says the destination identifier is, if it has a row for it.
    card_dest_id: Optional[str] = None
    #: Whether a broadcast confirmed the card's identifier.
    card_dest_confirmed: bool = False
    #: The newer card of the same family, when the matched card is superseded.
    superseded_by: Optional[str] = None
    protocol_version: Optional[str] = None

    @property
    def dest_agrees(self) -> Optional[bool]:
        if self.card_dest_id is None or self.dest_id is None:
            return None
        return self.card_dest_id == self.dest_id

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "dest_agrees": self.dest_agrees}


def match_route(
    manager: CardManager,
    source_chain_id: int,
    router_address: str,
    dest_chain_id: Optional[int],
    dest_ref: Optional[Dict[str, Any]],
) -> Optional[RouteMatch]:
    """The card row whose contract this route calls, or None. Matched on address."""
    try:
        router = Web3.to_checksum_address(router_address)
    except Exception:
        return None
    docs = manager.list_cards()
    for doc in docs:
        card = doc.card
        row = card.row(source_chain_id)
        if row is None:
            continue
        role = next((r for r, a in row.addresses.items() if a == router), None)
        if role is None:
            continue
        dest_value = None
        if dest_ref and card.dest_scheme and str(dest_ref.get("scheme")) == card.dest_scheme:
            dest_value = str(dest_ref.get("value"))
        dest_row = card.row(dest_chain_id) if dest_chain_id is not None else None
        return RouteMatch(
            protocol_id=card.protocol_id,
            source_chain_id=source_chain_id,
            role=role,
            source_status=row.status.value,
            dest_chain_id=dest_chain_id,
            dest_id=dest_value,
            card_dest_id=dest_row.dest_id if dest_row else None,
            card_dest_confirmed=bool(dest_row and dest_row.dest_confirmed),
            superseded_by=getattr(manager.superseded_by(card, docs), "protocol_id", None),
            protocol_version=card.protocol_version,
        )
    return None


def record_success(manager: CardManager, match: Dict[str, Any], tx_hash: str) -> str:
    m = RouteMatch(**{k: v for k, v in match.items() if k in RouteMatch.__dataclass_fields__})

    def change(card: Card) -> Card:
        row = card.row(m.source_chain_id)
        if row is not None:
            row.status = RowStatus.VERIFIED
            row.verified_date = utc_today()
            row.evidence = tx_hash
        dest = card.row(m.dest_chain_id) if m.dest_chain_id is not None else None
        if dest is not None and m.dest_id is not None and dest.dest_id == m.dest_id:
            dest.dest_confirmed = True
        return Card(**card.model_dump())

    return manager.update(m.protocol_id, change)


def record_failure(manager: CardManager, match: Optional[Dict[str, Any]], raw_revert: str,
                   protocol_id: Optional[str] = None) -> Optional[str]:
    """Record a revert on the card the route used; fail its source row if unverified."""
    pid = (match or {}).get("protocol_id") or protocol_id
    if not pid:
        return None
    revert = clean_revert(raw_revert)
    source_chain_id = (match or {}).get("source_chain_id")

    def change(card: Card) -> Card:
        mode = card.failure_mode(revert)
        if mode is None:
            card.failure_modes.append(FailureMode(revert=revert, seen=1))
            if len(card.failure_modes) > 32:
                # Keep the ones seen most; an evicted revert is back on first sighting.
                card.failure_modes.sort(key=lambda f: -f.seen)
                card.failure_modes = card.failure_modes[:32]
        else:
            mode.seen += 1
        row = card.row(source_chain_id) if source_chain_id is not None else None
        if row is not None and row.status is RowStatus.UNVERIFIED:
            row.status = RowStatus.FAILED
        return Card(**card.model_dump())

    return manager.update(pid, change)


def known_fix(manager: CardManager, protocol_id: Optional[str], raw_revert: str) -> Optional[str]:
    if not protocol_id:
        return None
    doc = manager.load(protocol_id)
    if doc is None:
        return None
    mode = doc.card.failure_mode(clean_revert(raw_revert))
    return mode.fix if mode else None
