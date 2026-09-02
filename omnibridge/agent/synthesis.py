"""
Turning an agent proposal into a transaction.

This is the Rule A boundary made concrete. Everything upstream is the model's judgement;
everything here is deterministic and reviewed. The proposal names values — this module
checks them against the chain and packs them into bytes, and it will refuse rather than
paper over anything that does not line up.
"""

from typing import Any, Dict, List, Optional, Tuple

from web3 import Web3

from omnibridge.agent.discovery_agent import RouteProposal
from omnibridge.agent.provenance import ProvenanceReport, selector_for, verify_proposal
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.models import RouteQuote, UserIntent


class ProposalRejected(Exception):
    """A proposal failed a deterministic check. Carries the report for the trace."""

    def __init__(self, message: str, report: Optional[ProvenanceReport] = None):
        super().__init__(message)
        self.report = report


def _check_shape(proposal: RouteProposal, source_chain_id: int) -> List[str]:
    """Consistency checks that need no network."""
    problems: List[str] = []

    signature = proposal.function_signature.strip()
    if "(" not in signature or not signature.endswith(")"):
        problems.append(f"Function signature {signature!r} is not a canonical ABI signature.")
        return problems

    declared = [t.strip() for t in signature[signature.index("(") + 1 : -1].split(",") if t.strip()]
    if declared != [t.strip() for t in proposal.param_types]:
        problems.append(
            f"param_types {proposal.param_types} do not match the types in the signature "
            f"{declared}. The agent contradicted itself; refusing to pick one."
        )

    if len(proposal.param_types) != len(proposal.param_values):
        problems.append(
            f"{len(proposal.param_types)} parameter types but "
            f"{len(proposal.param_values)} values."
        )

    if proposal.source_chain_id != source_chain_id:
        problems.append(
            f"Proposal targets chain {proposal.source_chain_id} but the resolved source "
            f"chain is {source_chain_id}. The router would be on the wrong network."
        )

    try:
        Web3.to_checksum_address(proposal.router_address)
    except Exception:
        problems.append(f"Router {proposal.router_address!r} is not a valid EVM address.")

    return problems


def synthesize(
    proposal: RouteProposal,
    intent: UserIntent,
    source_chain: Any,
    w3: Optional[Web3],
    allowed_domains: Optional[List[str]] = None,
    decimals: int = 6,
    retrieved: Optional[List[str]] = None,
) -> Tuple[str, RouteQuote, Dict[str, Any]]:
    """
    Check a proposal and encode it, or refuse.

    Returns (calldata, quote, evidence). Raises ProposalRejected on any failure — the
    caller feeds that back to the agent through the retry edge rather than proceeding.
    """
    problems = _check_shape(proposal, source_chain.chain_id)
    if problems:
        raise ProposalRejected("; ".join(problems))

    # The selector is recomputed here rather than trusted, so a signature the agent
    # mis-copied cannot silently address a different function.
    selector = selector_for(proposal.function_signature)

    report: Optional[ProvenanceReport] = None
    if w3 is not None:
        report = verify_proposal(
            w3,
            proposal.router_address,
            proposal.function_signature,
            proposal.sources,
            allowed_domains=allowed_domains,
            # What the provider's search actually fetched, when it can say. Without it
            # corroboration is a hostname check on URLs the model typed.
            retrieved=retrieved,
        )
        if not report.ok:
            raise ProposalRejected(
                "Provenance checks failed: " + "; ".join(report.failures), report
            )

    # Deterministic from here. The model chose the values; reviewed code packs them.
    values = CalldataExecutor.coerce_param_values(proposal.param_types, proposal.param_values)
    calldata = CalldataExecutor.encode_dynamic_calldata(
        proposal.function_signature, proposal.param_types, values
    )
    if not calldata.startswith(selector):
        raise ProposalRejected(
            f"Encoded calldata does not begin with the selector {selector} computed from "
            f"{proposal.function_signature!r}."
        )

    raw_amount = CalldataExecutor.decode_leading_uint256(calldata)
    quote = RouteQuote(
        bridge_name=proposal.protocol,
        source_chain_id=source_chain.chain_id,
        # Left at the source chain id: a destination that is not EIP-155 addressable has
        # no chain id, and inventing one is what dest_chain_ref exists to avoid.
        dest_chain_id=source_chain.chain_id,
        source_token_address="",
        dest_token_address="",
        router_address=Web3.to_checksum_address(proposal.router_address),
        deposit_amount_raw=raw_amount if raw_amount is not None else 0,
        estimated_output_raw=raw_amount if raw_amount is not None else 0,
        decimals=decimals,
        fee_amount_usd=0.0,
        calldata_template="agent_proposed",
        extra_params={
            "dest_chain_ref": proposal.dest_chain_ref,
            "function_signature": proposal.function_signature,
            # Carried through so the skill write-back can store this route as a
            # procedure rather than a single frozen transaction.
            "param_types": list(proposal.param_types),
            "param_values": [str(v) for v in proposal.param_values],
            "selector": selector,
            "sources": proposal.sources,
            "rationale": proposal.rationale,
        },
    )

    evidence = {
        "proposal": proposal.as_dict(),
        "selector_recomputed": selector,
        "provenance": report.as_dict() if report else {"skipped": "no RPC supplied"},
        "calldata_bytes": (len(calldata) - 2) // 2,
    }
    return calldata, quote, evidence
