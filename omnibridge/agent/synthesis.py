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
from omnibridge.core import abi_signature
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
    try:
        # Tuple-aware, and refuses aliases like `uint` whose selector is not the
        # deployed one. Splitting on commas broke on every struct argument.
        declared = abi_signature.parse(signature).types
    except abi_signature.SignatureError as exc:
        problems.append(f"Function signature {signature!r} is not a canonical ABI signature: {exc}")
        return problems

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


def _transfer_amount(proposal: RouteProposal, calldata: str) -> Optional[int]:
    """
    The number of base units this route actually moves.

    `decode_leading_uint256` reads argument word 0. That is the amount for every
    encoder in `CalldataExecutor`, which is what its docstring is about — but an
    agent-proposed signature puts whatever the *protocol* puts first. Across's
    `depositV3(address depositor, ...)` puts an address there, which read as a uint256
    is ~1.3e48, and that figure then became the balance the planner demanded and the
    allowance it built. Every Across route failed the balance check, for every wallet.

    The model already declares the base units the router will pull, in `spend.amount`,
    and reviewed code already trusts that figure to build the approval. Prefer it.
    Word 0 remains the fallback for a native transfer, which declares no spend.
    """
    if proposal.spend is not None:
        try:
            return int(str(proposal.spend.amount), 0)
        except (TypeError, ValueError):
            # Malformed enough that the planner will reject it too; fall through
            # rather than inventing a figure here.
            pass
    return CalldataExecutor.decode_leading_uint256(calldata)


def _recipient_is_encoded(recipient: str, param_values: List[Any]) -> bool:
    """
    Does the address the funds are meant for actually appear in what will be signed?

    A bridge names its destination address in its parameters, as an `address` or as a
    left-padded `bytes32` for chains whose addresses are not 20 bytes. Both forms are
    checked, case-insensitively, with any padding stripped.

    This is the one thing about a proposal that no other gate can catch. Provenance
    establishes the *router*; simulation establishes the call is well-formed. A transfer
    to the wrong destination passes both — eth_call succeeds, gas estimates fine — and
    the money is simply gone.
    """
    if not recipient:
        return False
    needle = recipient.lower().removeprefix("0x").lstrip("0")
    for value in param_values:
        text = str(value).lower().removeprefix("0x")
        if text.lstrip("0") == needle:
            return True
    return False


def synthesize(
    proposal: RouteProposal,
    intent: UserIntent,
    source_chain: Any,
    w3: Optional[Web3],
    allowed_domains: Optional[List[str]] = None,
    decimals: int = 6,
    retrieved: Optional[List[str]] = None,
    sender: Optional[str] = None,
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

    # A nominated recipient must be demonstrably in the transaction. When the recipient
    # is the sender — every route before this was expressible — a miss is recorded and
    # not fatal, because some protocols carry the destination somewhere this cannot see
    # and refusing would break routes that work today. When the user named someone else,
    # a miss is fatal: silently sending their funds back to themselves, or worse to an
    # address the model chose, is the failure this whole check exists to prevent.
    recipient = (intent.recipient or "").strip()
    nominated = bool(recipient and sender and recipient.lower() != sender.lower())
    recipient_encoded = _recipient_is_encoded(recipient, proposal.param_values)
    if nominated and not recipient_encoded:
        raise ProposalRejected(
            f"The transfer names {recipient} as the recipient, but that address does not "
            f"appear in the parameters the router would be called with "
            f"({proposal.param_values}). The funds would go somewhere else. Refusing."
        )

    raw_amount = _transfer_amount(proposal, calldata)
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
            # The approval this route needs, carried through to the planner. Without
            # it no approve step is built and every ERC-20 route reverts on allowance.
            "spend": proposal.spend.as_dict() if proposal.spend else None,
            "selector": selector,
            "sources": proposal.sources,
            "rationale": proposal.rationale,
        },
    )

    evidence = {
        "proposal": proposal.as_dict(),
        "selector_recomputed": selector,
        # Stated either way, so a route where the destination could not be located in
        # the calldata is visible rather than assumed correct.
        "recipient_checked": recipient or None,
        "recipient_encoded": recipient_encoded,
        "recipient_is_third_party": nominated,
        "provenance": report.as_dict() if report else {"skipped": "no RPC supplied"},
        "calldata_bytes": (len(calldata) - 2) // 2,
    }
    return calldata, quote, evidence
