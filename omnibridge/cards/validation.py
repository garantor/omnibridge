"""
The automated code check a card fact must pass before it is written.

Step 2 of the lifecycle in docs/AGENTIC_OMNIBRIDGE.md: bytecode exists at the address,
and the selectors of the card's interfaces are in it. A row that passes is saved as
`unverified`; only an approved broadcast makes it `verified` (cards.writeback).

Every check reuses one that already guards route proposals:
`provenance.verify_proposal` for bytecode, the EIP-1967 proxy and the selector, and
`provenance.check_sources` for the documentation source.

Fail closed. No reachable node that confirms the row's chain means the row is not
written — never "unverified for now", because an unreachable chain would otherwise be a
way to write unchecked addresses.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from web3 import Web3

from omnibridge.agent.provenance import check_sources, verify_proposal
from omnibridge.cards.models import Card, ChainRow, Interface

#: chain id → a Web3 whose node confirmed that chain id and serves eth_getCode, or None.
Web3ForChain = Callable[[int], Optional[Web3]]


@dataclass
class CheckResult:
    subject: str
    passed: bool
    checks: Dict[str, bool] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    observed: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "subject": self.subject,
            "passed": self.passed,
            "checks": self.checks,
            "reasons": self.reasons,
            "observed": self.observed,
        }


def has_documentation_source(sources: List[str], domains: List[str]) -> bool:
    return bool(check_sources(sources, domains)["primary"])


def check_row(
    card: Card,
    row: ChainRow,
    w3: Optional[Web3],
    domains: List[str],
    sources: Optional[List[str]] = None,
) -> CheckResult:
    """
    Every address in the row holds every selector the card's interfaces declare for its
    column, and (when `sources` is given) a documentation source names it.

    `sources` is None when re-checking a row already on the card, whose documentation
    was checked when it was added.
    """
    result = CheckResult(subject=f"{row.network} ({row.chain_id})", passed=False)

    if sources is not None:
        result.checks["documentation_source"] = has_documentation_source(sources, domains)
        if not result.checks["documentation_source"]:
            result.reasons.append("No source from the documentation allowlist names this row.")

    if not row.addresses:
        result.reasons.append("The row names no contract address, so there is nothing to check.")
        return result

    if w3 is None:
        result.checks["chain_identity"] = False
        result.reasons.append(f"No RPC endpoint confirmed chain {row.chain_id}; nothing was checked.")
        return result
    result.checks["chain_identity"] = True

    for role, address in row.addresses.items():
        interfaces = card.interfaces_for(role)
        if not interfaces:
            result.checks[f"{role}:interfaces"] = False
            result.reasons.append(
                f"No interface is recorded for {role}. Bytecode alone does not say what a "
                "contract is, so there is nothing to check it against."
            )
            continue
        for iface in interfaces:
            report = verify_proposal(w3, address, iface.signature, [], allowed_domains=domains)
            if report.implementation_address:
                result.observed[f"{role}:implementation"] = report.implementation_address
            result.checks[f"{role}:bytecode"] = report.is_contract
            if not report.is_contract:
                result.reasons += [f"{role}: {f}" for f in report.failures]
                break
            result.checks[f"{role}:{iface.name}"] = report.selector_present
            if not report.selector_present:
                result.reasons.append(
                    f"{role} {address}: selector {report.selector} for {iface.signature} "
                    "is absent from its bytecode."
                )

    result.passed = all(result.checks.values())
    return result


def check_interface(
    card: Card, iface: Interface, web3_for: Web3ForChain, domains: List[str]
) -> CheckResult:
    """
    A new interface must not contradict a contract already on the card: every row
    address in its column whose bytecode can be read must hold its selector. An
    unreadable node is skipped, never counted against it — found live, a public node
    that confirmed its chain id and then failed eth_getCode.
    """
    result = CheckResult(subject=iface.signature, passed=True)
    role = iface.target or (card.roles[0] if card.roles else None)
    if role is None:
        result.passed = False
        result.reasons.append("The card has no contract column to call this on.")
        return result
    for row in card.rows:
        address = row.addresses.get(role)
        if not address:
            continue
        w3 = web3_for(row.chain_id)
        if w3 is None:
            result.reasons.append(f"{row.network}: no usable node; skipped.")
            continue
        report = verify_proposal(w3, address, iface.signature, [], allowed_domains=domains)
        if not report.is_contract:
            result.reasons.append(f"{row.network}: bytecode not readable; skipped.")
            continue
        result.checks[f"{row.network}"] = report.selector_present
        if not report.selector_present:
            result.passed = False
            result.reasons.append(f"{row.network} {address} does not implement {iface.signature}.")
    return result


def default_web3_for() -> Web3ForChain:
    """
    Resolve a chain id through the public registry and connect to a node that confirms
    it *and* serves eth_getCode. Confirming the chain id is not the same as serving the
    reads these checks make.
    """
    from omnibridge.core.simulator import Simulator
    from omnibridge.discovery.rpc_manager import RPCManager

    rpc = RPCManager()
    cache: Dict[int, Optional[Web3]] = {}

    def web3_for(chain_id: int) -> Optional[Web3]:
        if chain_id in cache:
            return cache[chain_id]
        cache[chain_id] = None
        try:
            urls = rpc.discover_chain_by_id(chain_id).rpc_urls
        except Exception:
            return None
        for url in urls:
            w3, _url = Simulator([url], chain_id).connect_verified()
            if w3 is None:
                continue
            try:
                w3.eth.get_code("0x0000000000000000000000000000000000000000")
            except Exception:
                continue
            cache[chain_id] = w3
            break
        return cache[chain_id]

    return web3_for


def check_card(card: Card, web3_for: Optional[Web3ForChain] = None,
               domains: Optional[List[str]] = None) -> List[CheckResult]:
    """Re-run the code check over every row on a card. Reports; never changes a status."""
    from omnibridge.agent.contract import allowed_domains

    web3_for = web3_for or default_web3_for()
    domains = allowed_domains() if domains is None else domains
    return [check_row(card, row, web3_for(row.chain_id), domains) for row in card.rows]
