"""
Provenance checks for an agent-proposed route.

Once a router address comes from fetched web content, a malicious or merely wrong page
can name the wrong contract — and the simulator will not catch it. A hostile contract
that accepts a USDC transfer simulates perfectly: eth_call succeeds, gas estimates fine,
no revert. Simulation proves a transaction is well-formed. It proves nothing about
whether the recipient is who the agent says.

So the defence is provenance plus generic on-chain checks, none of which needs prior
knowledge of the protocol. Per Rule B there is deliberately no pinned router table here:
that would only cover routes already known, which are exactly the routes not needing an
agent.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from web3 import Web3

# EIP-1967 implementation slot: bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1)
EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)

# Minimum independent sources that must name a router address before it is eligible.
MIN_CORROBORATING_SOURCES = 2


@dataclass
class ProvenanceReport:
    """What was checked, what passed, and why anything failed."""

    router_address: str
    is_contract: bool = False
    bytecode_size: int = 0
    implementation_address: Optional[str] = None
    selector: Optional[str] = None
    selector_present: bool = False
    corroborating_domains: List[str] = field(default_factory=list)
    disallowed_domains: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> Dict[str, Any]:
        return {
            "router_address": self.router_address,
            "is_contract": self.is_contract,
            "bytecode_size": self.bytecode_size,
            "implementation_address": self.implementation_address,
            "selector": self.selector,
            "selector_present": self.selector_present,
            "corroborating_domains": self.corroborating_domains,
            "disallowed_domains": self.disallowed_domains,
            "failures": self.failures,
            "passed": self.ok,
        }


def resolve_proxy_implementation(w3: Web3, address: str) -> Optional[str]:
    """
    Read the EIP-1967 implementation slot, if the contract is a proxy.

    Most real routers are proxied. The Across SpokePool on Base holds ~680 bytes of
    bytecode and does not contain the depositV3 selector at all — its implementation
    does. A selector check that reads only the address's own code therefore returns a
    false negative on exactly the contracts most likely to be correct.
    """
    try:
        slot = w3.eth.get_storage_at(Web3.to_checksum_address(address), EIP1967_IMPLEMENTATION_SLOT)
    except Exception:
        return None

    raw = slot.hex() if hasattr(slot, "hex") else str(slot)
    tail = raw[-40:]
    if not tail or int(tail, 16) == 0:
        return None
    return Web3.to_checksum_address("0x" + tail)


def selector_for(function_signature: str) -> str:
    """The 4-byte selector, computed from the signature rather than looked up."""
    return "0x" + Web3.keccak(text=function_signature)[:4].hex()


def registrable_domain(url: str) -> str:
    """Host of a URL, lowercased and without a leading www."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def check_sources(
    sources: List[str], allowed_domains: Optional[List[str]] = None
) -> Dict[str, List[str]]:
    """
    Corroboration and allowlisting.

    An address named by a single page is a claim; one named by two independent hosts is
    evidence. The allowlist is operator security configuration — it constrains where
    information may come from and encodes no per-bridge behaviour.
    """
    domains, disallowed = [], []
    for url in sources or []:
        domain = registrable_domain(url)
        if not domain:
            continue
        if allowed_domains and not any(
            domain == a or domain.endswith("." + a) for a in allowed_domains
        ):
            disallowed.append(domain)
            continue
        if domain not in domains:
            domains.append(domain)
    return {"corroborating": domains, "disallowed": disallowed}


def verify_proposal(
    w3: Web3,
    router_address: str,
    function_signature: str,
    sources: List[str],
    allowed_domains: Optional[List[str]] = None,
    min_sources: int = MIN_CORROBORATING_SOURCES,
) -> ProvenanceReport:
    """
    Run every check that needs no prior knowledge of the protocol.

    Returns a report rather than raising, so the caller can put the whole thing in front
    of a human and in the trace. `ok` is false if any check failed.
    """
    report = ProvenanceReport(router_address=router_address)

    try:
        checksummed = Web3.to_checksum_address(router_address)
    except Exception:
        report.failures.append(f"{router_address!r} is not a valid EVM address.")
        return report

    # 1. It holds bytecode. Note this is weaker than "is a contract" since EIP-7702:
    # a delegated EOA carries 23 bytes (0xef0100 || address). The selector check below
    # is what actually rejects those, which is why both run rather than just this one.
    try:
        code = w3.eth.get_code(checksummed)
    except Exception as exc:
        report.failures.append(f"Could not read bytecode for {checksummed}: {exc}")
        return report

    report.bytecode_size = len(code)
    report.is_contract = len(code) > 0
    if not report.is_contract:
        report.failures.append(
            f"{checksummed} holds no bytecode — it is an externally owned account, not a router."
        )
        return report

    # 2. It implements what was claimed — following the proxy if there is one.
    report.selector = selector_for(function_signature)
    needle = report.selector[2:]
    haystack = code.hex()

    if needle not in haystack:
        implementation = resolve_proxy_implementation(w3, checksummed)
        if implementation:
            report.implementation_address = implementation
            try:
                impl_code = w3.eth.get_code(Web3.to_checksum_address(implementation))
                haystack = impl_code.hex()
            except Exception as exc:
                report.failures.append(f"Could not read implementation bytecode: {exc}")
                return report

    report.selector_present = needle in haystack
    if not report.selector_present:
        where = (
            f"{checksummed} (nor its implementation {report.implementation_address})"
            if report.implementation_address
            else checksummed
        )
        report.failures.append(
            f"Selector {report.selector} for {function_signature!r} is absent from the "
            f"deployed bytecode of {where}. The contract does not implement what was claimed."
        )

    # 3. Corroboration and allowlisting.
    checked = check_sources(sources, allowed_domains)
    report.corroborating_domains = checked["corroborating"]
    report.disallowed_domains = checked["disallowed"]

    if checked["disallowed"]:
        report.failures.append(
            "Sources outside the allowlist: " + ", ".join(sorted(set(checked["disallowed"])))
        )
    if len(checked["corroborating"]) < min_sources:
        report.failures.append(
            f"Router named by {len(checked['corroborating'])} independent source(s); "
            f"{min_sources} required. A single page is a claim, not evidence."
        )

    return report
