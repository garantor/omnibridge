"""
On-chain checks for card entries. See docs/PROTOCOL_CARDS.md, section 6.3.

These decide whether a `lead` may become `checked`, and nothing else. They never make
an entry `verified` (only an approved broadcast does that) and never demote one (only a
run outcome does that): a check that fails on an entry already checked or verified is
reported, and the status is left alone. A transient RPC problem must not be able to
erase what a broadcast established.

Every check is one that already guards route proposals, reused rather than rewritten:
`provenance.verify_proposal` for bytecode, the EIP-1967 proxy and the selector, and
`erc20` for a token's own symbol and decimals.

Fail closed. No reachable node of the right chain means the entry stays where it is,
never "checked for now".

Destinations and parameters have no generic on-chain check, so they are not handled
here. They stay leads until a broadcast uses them.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from web3 import Web3

from omnibridge.agent.contract import allowed_domains
from omnibridge.agent.provenance import check_sources, verify_proposal
from omnibridge.cards.models import Card, Deployment, EntryStatus, Function, Token
from omnibridge.core import erc20

#: chain id → a Web3 whose node confirmed that chain id, or None.
Web3ForChain = Callable[[int], Optional[Web3]]


@dataclass
class EntryCheck:
    kind: str
    entry_id: str
    current: EntryStatus
    #: What the status would be after this check. Only ever lead → checked.
    proposed: EntryStatus
    passed: bool
    checks: Dict[str, bool] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    #: Facts read from the chain, e.g. a token's decimals or a proxy's implementation.
    observed: Dict[str, object] = field(default_factory=dict)

    @property
    def changes_status(self) -> bool:
        return self.proposed != self.current


def _outcome(kind: str, entry, passed: bool, checks, reasons, observed=None) -> EntryCheck:
    current = entry.status
    proposed = EntryStatus.CHECKED if (passed and current == EntryStatus.LEAD) else current
    if not passed and current in (EntryStatus.CHECKED, EntryStatus.VERIFIED):
        reasons = [*reasons, f"Status left at {current.value}: only a run outcome demotes."]
    return EntryCheck(kind, entry.id, current, proposed, passed, checks, reasons, observed or {})


def _has_primary_source(sources: List[str], domains: List[str]) -> bool:
    return bool(check_sources(sources, domains)["primary"])


def check_deployment(card: Card, dep: Deployment, w3: Optional[Web3], domains: List[str]) -> EntryCheck:
    """Bytecode holds every selector recorded for this role, and a doc source names it."""
    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    observed: Dict[str, object] = {}

    functions = [
        f for f in card.functions if f.role == dep.role and f.status != EntryStatus.DEMOTED
    ]
    checks["documentation_source"] = _has_primary_source(dep.sources, domains)
    if not checks["documentation_source"]:
        reasons.append("No source from the documentation allowlist names this address.")

    if not functions:
        reasons.append(
            f"No function is recorded for role {dep.role!r}. Bytecode alone does not say "
            "what a contract is, so there is nothing to check it against."
        )
        return _outcome("deployments", dep, False, checks, reasons)

    if w3 is None:
        checks["chain_identity"] = False
        reasons.append(f"No RPC endpoint confirmed chain {dep.chain_id}; nothing was checked.")
        return _outcome("deployments", dep, False, checks, reasons)
    checks["chain_identity"] = True

    selectors_ok = True
    for fn in functions:
        # Sources are passed so the report is complete, but decided above; here the
        # report is read for its on-chain facts.
        report = verify_proposal(w3, dep.address, fn.signature, dep.sources, allowed_domains=domains)
        checks["bytecode"] = report.is_contract
        if report.implementation_address:
            observed["implementation"] = report.implementation_address
        if not report.is_contract:
            reasons += [f for f in report.failures if "bytecode" in f or "valid EVM" in f]
            return _outcome("deployments", dep, False, checks, reasons, observed)
        checks[f"selector:{fn.id}"] = report.selector_present
        if not report.selector_present:
            selectors_ok = False
            reasons += [f for f in report.failures if "Selector" in f]

    passed = checks["documentation_source"] and selectors_ok
    return _outcome("deployments", dep, passed, checks, reasons, observed)


def check_token(tok: Token, w3: Optional[Web3], domains: List[str]) -> EntryCheck:
    """
    Bytecode, the contract's own symbol, decimals read from the chain, and a doc source.

    The source requirement is stricter than the design doc's first draft: anyone can
    deploy a contract that answers `symbol()` with "USDC", and this address becomes the
    token a route approves.
    """
    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    observed: Dict[str, object] = {}

    checks["documentation_source"] = _has_primary_source(tok.sources, domains)
    if not checks["documentation_source"]:
        reasons.append("No source from the documentation allowlist names this token address.")

    if w3 is None:
        checks["chain_identity"] = False
        reasons.append(f"No RPC endpoint confirmed chain {tok.chain_id}; nothing was checked.")
        return _outcome("tokens", tok, False, checks, reasons)
    checks["chain_identity"] = True

    try:
        code = w3.eth.get_code(Web3.to_checksum_address(tok.address))
    except Exception as exc:
        reasons.append(f"Could not read bytecode: {exc}")
        return _outcome("tokens", tok, False, checks, reasons)
    checks["bytecode"] = len(code) > 0
    if not checks["bytecode"]:
        reasons.append(f"{tok.address} holds no bytecode.")
        return _outcome("tokens", tok, False, checks, reasons)

    symbol = erc20.read_symbol(w3, tok.address)
    observed["symbol"] = symbol
    checks["symbol"] = bool(symbol) and symbol.upper() == tok.symbol.upper()
    if not checks["symbol"]:
        reasons.append(f"The contract calls itself {symbol!r}, not {tok.symbol!r}.")

    decimals = erc20.read_decimals(w3, tok.address)
    observed["decimals"] = decimals
    checks["decimals"] = decimals is not None and (tok.decimals is None or tok.decimals == decimals)
    if decimals is None:
        reasons.append("The contract did not answer decimals().")
    elif tok.decimals is not None and tok.decimals != decimals:
        reasons.append(f"The card says {tok.decimals} decimals; the contract says {decimals}.")

    passed = all(checks.values())
    return _outcome("tokens", tok, passed, checks, reasons, observed)


def check_function(card: Card, fn: Function, web3_for: Web3ForChain, domains: List[str]) -> EntryCheck:
    """
    The selector is present at a deployment of this role, and absent from none.

    One confirming deployment is enough; a reachable deployment of the same role that
    lacks the selector is a contradiction and fails the check, since ids carry a version
    and two contracts in one role and version should implement the same functions.
    """
    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    deployments = [
        d for d in card.deployments if d.role == fn.role and d.status != EntryStatus.DEMOTED
    ]
    if not deployments:
        reasons.append(f"No deployment with role {fn.role!r} to check the selector against.")
        return _outcome("functions", fn, False, checks, reasons)

    confirmed, contradicted = 0, 0
    for dep in deployments:
        w3 = web3_for(dep.chain_id)
        if w3 is None:
            reasons.append(f"{dep.id}: no RPC confirmed chain {dep.chain_id}; skipped.")
            continue
        report = verify_proposal(w3, dep.address, fn.signature, [], allowed_domains=domains)
        if not report.is_contract:
            # Unreadable, or no code at all. Neither says anything about this function:
            # a flaky node must not count as evidence against it. The deployment's own
            # check reports what is wrong with the address.
            reasons.append(f"{dep.id}: bytecode not available ({'; '.join(report.failures)}); skipped.")
            continue
        checks[f"selector@{dep.id}"] = report.selector_present
        if report.selector_present:
            confirmed += 1
        else:
            contradicted += 1
            reasons.append(f"{dep.id}: selector absent from its bytecode.")

    passed = confirmed > 0 and contradicted == 0
    if confirmed == 0 and contradicted == 0:
        reasons.append("No deployment of this role could be reached.")
    return _outcome("functions", fn, passed, checks, reasons)


def default_web3_for() -> Web3ForChain:
    """Resolve a chain id through the public registry and connect to a node that confirms it."""
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
            # Confirming the chain id is not the same as serving the reads these checks
            # make: public nodes that answer eth_chainId and fail eth_getCode exist.
            try:
                w3.eth.get_code("0x0000000000000000000000000000000000000000")
            except Exception:
                continue
            cache[chain_id] = w3
            break
        return cache[chain_id]

    return web3_for


def check_card(
    card: Card,
    web3_for: Optional[Web3ForChain] = None,
    domains: Optional[List[str]] = None,
) -> List[EntryCheck]:
    """Run every applicable check on every entry that is not demoted."""
    web3_for = web3_for or default_web3_for()
    domains = allowed_domains() if domains is None else domains
    results: List[EntryCheck] = []
    for dep in card.deployments:
        if dep.status != EntryStatus.DEMOTED:
            results.append(check_deployment(card, dep, web3_for(dep.chain_id), domains))
    for tok in card.tokens:
        if tok.status != EntryStatus.DEMOTED:
            results.append(check_token(tok, web3_for(tok.chain_id), domains))
    for fn in card.functions:
        if fn.status != EntryStatus.DEMOTED:
            results.append(check_function(card, fn, web3_for, domains))
    return results


def apply_checks(card: Card, results: List[EntryCheck], checked_on: str) -> Card:
    """
    A copy of the card with passing leads raised to `checked`, and token decimals set
    from the chain. Nothing else changes.
    """
    updated = card.model_copy(deep=True)
    for result in results:
        entry = updated.entry(result.kind, result.entry_id)
        if entry is None:
            continue
        if result.kind == "tokens" and result.passed and result.observed.get("decimals") is not None:
            entry.decimals = int(result.observed["decimals"])
        if result.changes_status:
            entry.status = result.proposed
            entry.added = entry.added or checked_on
    return updated
