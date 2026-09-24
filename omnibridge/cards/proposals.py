"""
`propose_card_update`: how the agent adds to a card, and what stops it doing harm.

The lifecycle from docs/AGENTIC_OMNIBRIDGE.md, Phase 3:

    agent proposes -> automated code check -> saved `unverified`
                   -> route simulation + human approval
                   -> verified (writeback.record_success) or failed (record_failure)

This module is the first three boxes. Patch kinds:

  chain_row    a chain the protocol is deployed on: its contract addresses and its
               destination identifier. How the agent registers a newly deployed chain.
  interface    a function signature and the contract column it is called on.
  failure_fix  a fix for a revert the system has already recorded.
  new_card     a card for a protocol that has none.

What the agent cannot do, by construction:

  - set a status. New rows are `unverified`; only outcomes move them.
  - change a verified row's addresses, or a destination identifier a broadcast
    confirmed. A different address is a different fact that must earn its own status.
  - re-propose an address that already failed on that chain.
  - record a revert. Revert text comes from the node, written by `writeback`. The agent
    may only attach a fix to one that exists.
  - write anything without an allowlisted documentation source (fixes excepted — a fix
    is advice about a revert the system observed, not a claim about the world).

Checks run before the card's lock is taken, because they make network calls; the
preconditions are re-checked under the lock, so a concurrent write cannot slip between.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from omnibridge.cards.manager import CardManager, CardWriteRefused
from omnibridge.cards.models import Card, ChainRow, FailureMode, Interface, RowStatus, slug
from omnibridge.cards.validation import (
    Web3ForChain,
    check_interface,
    check_row,
    has_documentation_source,
)
from omnibridge.agent.provenance import check_sources

PATCH_KINDS = ("chain_row", "interface", "failure_fix", "new_card")


@dataclass
class ProposalResult:
    accepted: bool
    kind: str
    protocol_id: str
    status: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, Any] = field(default_factory=dict)
    card_version: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        out = {"accepted": self.accepted, "kind": self.kind, "protocol_id": self.protocol_id,
               "reasons": self.reasons, "checks": self.checks}
        if self.status:
            out["status"] = self.status
        if self.card_version is not None:
            out["card_version"] = self.card_version
        return out


def _refuse(kind: str, protocol_id: str, *reasons: str, checks=None) -> ProposalResult:
    return ProposalResult(False, kind, protocol_id, reasons=list(reasons), checks=checks or {})


def _first_primary(sources: List[str], domains: List[str]) -> Optional[str]:
    primary_hosts = set(check_sources(sources, domains)["primary"])
    from omnibridge.agent.provenance import registrable_domain

    return next((u for u in sources if registrable_domain(u) in primary_hosts and u.startswith("https://")), None)


def _role_addresses(card: Card, raw: Dict[str, Any]) -> Dict[str, str]:
    """Accept either the column label ("Token Messenger") or its slug as the key."""
    out = {}
    for key, value in (raw or {}).items():
        role = slug(str(key).removesuffix(" Address").removesuffix(" address"))
        if role not in card.roles:
            raise ValueError(f"{key!r} is not an address column on this card; columns are {card.address_labels}.")
        out[role] = str(value)
    return out


def apply_patch(
    manager: CardManager,
    protocol_id: str,
    patch: Dict[str, Any],
    sources: List[str],
    web3_for: Web3ForChain,
    domains: List[str],
) -> ProposalResult:
    kind = str((patch or {}).get("kind", ""))
    if kind not in PATCH_KINDS:
        return _refuse(kind, protocol_id, f"Unknown patch kind {kind!r}; expected one of {PATCH_KINDS}.")
    try:
        if kind == "new_card":
            return _new_card(manager, protocol_id, patch, sources, domains)
        doc = manager.load(protocol_id)
        if doc is None:
            return _refuse(kind, protocol_id, f"No card {protocol_id!r}. Propose a new_card first.")
        if kind == "chain_row":
            return _chain_row(manager, doc.card, patch, sources, web3_for, domains)
        if kind == "interface":
            return _interface(manager, doc.card, patch, sources, web3_for, domains)
        return _failure_fix(manager, doc.card, patch)
    except CardWriteRefused as exc:
        return _refuse(kind, protocol_id, str(exc))
    except ValueError as exc:
        return _refuse(kind, protocol_id, str(exc))


def _chain_row(manager, card: Card, patch, sources, web3_for, domains) -> ProposalResult:
    pid = card.protocol_id
    row = ChainRow(
        network=patch.get("network", ""),
        chain_id=int(patch.get("chain_id")),
        dest_id=(str(patch["dest_id"]) if patch.get("dest_id") not in (None, "") else None),
        addresses=_role_addresses(card, patch.get("addresses") or {}),
        status=RowStatus.UNVERIFIED,
    )
    if row.dest_id and not card.dest_id_label:
        return _refuse("chain_row", pid, "This card has no destination identifier column.")

    existing = card.row(row.chain_id)
    if existing is not None:
        problem = _conflict(existing, row)
        if problem:
            return _refuse("chain_row", pid, problem)
        if existing.addresses == row.addresses and existing.dest_id == row.dest_id:
            return ProposalResult(True, "chain_row", pid, status=existing.status.value,
                                  reasons=["Already recorded; nothing changed."],
                                  card_version=card.card_version)

    result = check_row(card, row, web3_for(row.chain_id), domains, sources=sources)
    if not result.passed:
        return _refuse("chain_row", pid, *result.reasons, checks=result.checks)
    row.evidence = _first_primary(sources, domains)

    def change(current: Card) -> Card:
        prior = current.row(row.chain_id)
        if prior is not None:
            problem = _conflict(prior, row)
            if problem:
                raise CardWriteRefused(problem)
            # Keep what a broadcast already confirmed about the destination identifier.
            row.dest_confirmed = prior.dest_confirmed and prior.dest_id == row.dest_id
            current.rows = [row if r.chain_id == row.chain_id else r for r in current.rows]
        else:
            current.rows.append(row)
        name = row.network.lower()
        if name not in [c.lower() for c in current.supported_chains]:
            current.supported_chains.append(name)
        return Card(**current.model_dump())

    manager.update(pid, change)
    return ProposalResult(True, "chain_row", pid, status=RowStatus.UNVERIFIED.value,
                          checks=result.checks, card_version=manager.load(pid).card.card_version)


def _conflict(existing: ChainRow, proposed: ChainRow) -> Optional[str]:
    if existing.status is RowStatus.VERIFIED and existing.addresses != proposed.addresses:
        return (f"{existing.network} is verified by broadcast {existing.evidence}; its addresses "
                "cannot be changed by a proposal. If the protocol has redeployed, that needs a person.")
    if existing.dest_confirmed and proposed.dest_id != existing.dest_id:
        return (f"{existing.network}'s destination identifier {existing.dest_id} was confirmed by a "
                "broadcast and cannot be changed by a proposal.")
    if existing.status is RowStatus.FAILED and existing.addresses == proposed.addresses:
        return (f"{existing.network} already failed with these addresses. Propose different "
                "addresses, or look at the recorded failure modes.")
    return None


def _interface(manager, card: Card, patch, sources, web3_for, domains) -> ProposalResult:
    pid = card.protocol_id
    target_label = patch.get("target")
    iface = Interface(
        label=patch.get("label", ""),
        signature=patch.get("signature", ""),
        target=slug(str(target_label).removesuffix(" Address")) if target_label else None,
    )
    if not has_documentation_source(sources, domains):
        return _refuse("interface", pid, "No source from the documentation allowlist states this signature.")
    if any(i.signature == iface.signature and i.target == iface.target for i in card.interfaces):
        return ProposalResult(True, "interface", pid, reasons=["Already recorded; nothing changed."],
                              card_version=card.card_version)
    trial = Card(**{**card.model_dump(), "interfaces": [*card.model_dump()["interfaces"], iface.model_dump()]})
    result = check_interface(trial, iface, web3_for, domains)
    if not result.passed:
        return _refuse("interface", pid, *result.reasons, checks=result.checks)

    def change(current: Card) -> Card:
        if not any(i.signature == iface.signature and i.target == iface.target for i in current.interfaces):
            current.interfaces.append(iface)
        return Card(**current.model_dump())

    manager.update(pid, change)
    return ProposalResult(True, "interface", pid, checks=result.checks,
                          card_version=manager.load(pid).card.card_version)


def _failure_fix(manager, card: Card, patch) -> ProposalResult:
    pid = card.protocol_id
    revert, fix = str(patch.get("revert", "")), patch.get("fix")
    if not card.failure_mode(revert):
        return _refuse("failure_fix", pid,
                       f"No recorded revert {revert!r}. Reverts are recorded from what the node "
                       "said; a fix can only be attached to one that was observed.")
    FailureMode(revert=revert, fix=fix)  # validates the fix text before taking the lock

    def change(current: Card) -> Card:
        mode = current.failure_mode(revert)
        if mode is None:
            raise CardWriteRefused("The revert disappeared from the card while this was checked.")
        mode.fix = fix
        return Card(**current.model_dump())

    manager.update(pid, change)
    return ProposalResult(True, "failure_fix", pid, card_version=manager.load(pid).card.card_version)


def _new_card(manager: CardManager, protocol_id: str, patch, sources, domains) -> ProposalResult:
    if manager.load(protocol_id) is not None:
        return _refuse("new_card", protocol_id, f"A card {protocol_id!r} already exists.")
    docs = [str(u) for u in (patch.get("docs") or [])]
    if not docs or len(check_sources(docs, domains)["primary"]) == 0:
        return _refuse("new_card", protocol_id,
                       "A new card needs docs URLs on the documentation allowlist; the agent is "
                       "told to read them, so they must be places it may read.")
    if check_sources(docs, domains)["disallowed"] or check_sources(docs, domains)["secondary"]:
        return _refuse("new_card", protocol_id, "Every docs URL must be protocol documentation, not an explorer or other host.")
    card = Card(
        protocol_id=protocol_id,
        protocol_name=patch.get("protocol_name", protocol_id),
        docs=docs,
        supported_tokens=[str(t) for t in (patch.get("supported_tokens") or [])],
        dest_id_label=patch.get("dest_id_label") or None,
        address_labels=[str(l) for l in (patch.get("address_labels") or [])],
        interfaces=[Interface(**{**i, "target": slug(i["target"]) if i.get("target") else None})
                    for i in (patch.get("interfaces") or [])],
    )
    if not card.address_labels:
        return _refuse("new_card", protocol_id, "A card needs at least one contract address column.")
    manager.save_card(card)
    return ProposalResult(True, "new_card", protocol_id, card_version=1)
