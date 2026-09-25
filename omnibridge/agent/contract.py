"""
The discovery contract: what any model provider must produce, and what it may not.

This module is deliberately vendor-neutral. It holds the shape of the answer, the
schema the model fills in, the instructions it works under, and the domains it may
read — none of which are properties of any particular API.

Why the split exists
--------------------
Under Rule A the model *chooses* and reviewed code *encodes*. Everything a provider is
allowed to decide is `RouteProposal`, and every field on it is checked downstream:
`synthesis._check_shape` re-derives the parameter types from the signature and rejects
a proposal that contradicts itself, `provenance.verify_proposal` reads the router's
bytecode for the claimed selector, and the simulator has to get a real `eth_call`
through before a human is ever shown anything.

That is what makes the provider swappable. Nothing downstream of this file knows which
model produced the proposal, and a weaker model does not get a weaker guardrail — it
just gets rejected more often.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Documentation hosts the model may read. Not a route table: no address, chain id or
# ABI appears here, only where it is permissible to go looking. An answer sourced from
# somewhere else is not accepted, which is what stops a forum post or an AI-generated
# tutorial being cited as protocol documentation.
DEFAULT_ALLOWED_DOMAINS = [
    "developers.circle.com",
    # Bare hostnames only. `check_sources` compares a cited URL's *host* against this
    # list, so a full URL here matches nothing and is dead weight. Two such entries sat
    # here — developers.circle.com/ai/mcp, already covered by the host above, and
    # github.com/circlefin/skills.
    #
    # github.com is deliberately absent rather than added: a host entry cannot express
    # "this one repository", and now that a single documentation source is sufficient,
    # listing it would make every repository on GitHub an authority on router addresses.
    "docs.across.to",
    "docs.layerzero.network",
    "docs.wormhole.com",
    "docs.hyperlane.xyz",
    "docs.axelar.dev",
    "docs.stargate.finance",
    "docs.synapseprotocol.com",
    "docs.connext.network",
    "docs.chain.link",
    "docs.stellar.org",
    "developers.stellar.org",
    "docs.solana.com",
    "solana.com",
    "docs.sui.io",
    "aptos.dev",
    "docs.base.org",
]


def format_chain_ref(ref: Any) -> str:
    """
    A destination reference as a human reads it: "cctp_domain 6".

    The reference is a dict because the scheme is load-bearing — CCTP domain 6 and
    EIP-155 chain 6 are different chains — but printing the dict itself puts Python
    syntax in a manifest and in the console.
    """
    if not ref:
        return ""
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        scheme = str(ref.get("scheme") or "").strip()
        value = str(ref.get("value") or "").strip()
        if scheme and value:
            return f"{scheme} {value}"
        return value or scheme
    return str(ref)


def allowed_domains() -> List[str]:
    """Documentation hosts the model may read, overridable for testing."""
    override = os.environ.get("OMNIBRIDGE_ALLOWED_DOMAINS")
    if override:
        return [d.strip() for d in override.split(",") if d.strip()]
    return list(DEFAULT_ALLOWED_DOMAINS)


def agent_disabled() -> bool:
    """Kill switch. When set, discovery never reaches a model."""
    return os.environ.get("OMNIBRIDGE_DISABLE_AGENT", "").strip().lower() in ("1", "true", "yes")


class NoRouteFound(Exception):
    """
    No route exists for this pair.

    A successful outcome, not a failure: it terminates the run and is never retried,
    because another attempt spends money to reach the same answer.
    """

    def __init__(self, reason: str, sources: Optional[List[str]] = None):
        super().__init__(reason)
        self.reason = reason
        self.sources = sources or []


class ProviderUnavailable(Exception):
    """
    The provider could not be reached or refused the request for a reason that is about
    credentials, quota or configuration — never about the route.

    Distinct from NoRouteFound because the two mean opposite things to a user, and
    collapsing them is how "I could not ask" gets reported as "the answer is no".
    """


@dataclass
class TokenSpend:
    """
    The ERC-20 the router must be allowed to move, if any.

    Which token a bridge spends is protocol knowledge, so the model declares it; the
    approval itself is built by reviewed code from the standard ABI. `spender` is
    usually the router, but not always — some protocols take allowance on a separate
    contract, and guessing wrong produces a transaction that reverts after the user has
    already paid for an approval.

    A native-asset transfer has no spend at all, which is why this is optional.
    """

    token: str
    spender: str
    amount: str

    def as_dict(self) -> Dict[str, Any]:
        return {"token": self.token, "spender": self.spender, "amount": self.amount}


@dataclass
class RouteProposal:
    """What the model is allowed to decide. Everything else is computed from it."""

    protocol: str
    router_address: str
    function_signature: str
    param_types: List[str]
    param_values: List[str]
    source_chain_id: int
    dest_chain_ref: Dict[str, Any]
    sources: List[str]
    rationale: str
    #: The ERC-20 approval this route needs before it can run, if any.
    spend: Optional[TokenSpend] = None
    #: URLs the provider's search really fetched, when the provider can report them.
    #: Distinct from `sources`, which is what the model says it used — the two differ,
    #: and only the first is evidence that anything was read.
    retrieved: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "router_address": self.router_address,
            "function_signature": self.function_signature,
            "param_types": list(self.param_types),
            "param_values": list(self.param_values),
            "source_chain_id": self.source_chain_id,
            "dest_chain_ref": self.dest_chain_ref,
            "sources": list(self.sources),
            "rationale": self.rationale,
            "spend": self.spend.as_dict() if self.spend else None,
        }

    @classmethod
    def from_tool_input(cls, payload: Dict[str, Any]) -> "RouteProposal":
        """
        Build a proposal from a provider's tool-call arguments.

        Missing keys raise rather than defaulting. A provider whose function calling is
        not strictly schema-enforced can omit a field, and a defaulted parameter is a
        value nobody chose reaching real calldata.
        """
        missing = [k for k in PROPOSAL_REQUIRED_FIELDS if k not in payload]
        if missing:
            raise ValueError(
                f"Route proposal is missing required field(s): {', '.join(missing)}. "
                "Refusing to default them — an unchosen value would be encoded as though "
                "the model had picked it."
            )
        spend_raw = payload.get("spend") or None
        spend = None
        if spend_raw:
            missing_spend = [k for k in ("token", "spender", "amount") if not spend_raw.get(k)]
            if missing_spend:
                raise ValueError(
                    "Route proposal declares a token spend but omits "
                    f"{', '.join(missing_spend)}. An approval built from a guessed "
                    "spender or amount is a transaction the user pays for and that "
                    "does not unblock the transfer."
                )
            spend = TokenSpend(
                token=str(spend_raw["token"]),
                spender=str(spend_raw["spender"]),
                amount=str(spend_raw["amount"]),
            )

        return cls(
            spend=spend,
            protocol=str(payload["protocol"]),
            router_address=str(payload["router_address"]),
            function_signature=str(payload["function_signature"]),
            param_types=[str(t) for t in payload["param_types"]],
            param_values=[str(v) for v in payload["param_values"]],
            source_chain_id=int(payload["source_chain_id"]),
            dest_chain_ref=dict(payload["dest_chain_ref"]),
            sources=[str(u) for u in payload["sources"]],
            rationale=str(payload["rationale"]),
            raw=dict(payload),
        )


def unsupported_no_route(payload: Dict[str, Any]) -> Optional[str]:
    """
    Why a `no_route_found` answer is not yet a conclusion, or None if it is one.

    "No route" is terminal and never retried, so it has to be an answer about the world.
    Observed: a model with tools called it as a probe, with the reason "testing tool
    availability" and no sources, and the run ended there. A no-route that cites nothing
    is sent back while the model still has turns; on its last turn it is accepted, so
    an honest "no" is always possible.
    """
    sources = [s for s in (payload.get("sources") or []) if str(s).startswith("http")]
    if not sources:
        return ("no_route_found must cite the documentation you read that shows there is "
                "no route. If you have not researched it yet, use your tools and the docs "
                "first; if you have established a route, call propose_route.")
    return None


PROPOSAL_REQUIRED_FIELDS = (
    "protocol",
    "router_address",
    "function_signature",
    "param_types",
    "param_values",
    "source_chain_id",
    "dest_chain_ref",
    "sources",
    "rationale",
)


# The JSON Schema for the two terminal answers, in provider-neutral form. Anthropic
# takes these as `input_schema`, most others as `parameters`; the schema itself is the
# same document either way.
PROPOSE_ROUTE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "protocol": {"type": "string", "description": "Bridge protocol, e.g. 'CCTP', 'Across V3'."},
        "router_address": {
            "type": "string",
            "description": "Contract to call, on the SOURCE chain. 0x-prefixed, 20 bytes.",
        },
        "function_signature": {
            "type": "string",
            "description": "Canonical signature, no spaces, e.g. 'depositForBurn(uint256,uint32,bytes32,address)'.",
        },
        "param_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ABI types in order, matching the signature exactly.",
        },
        "param_values": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Argument values in order, as strings. Integers in base 10, addresses and "
                "byte strings 0x-prefixed. They are ABI-encoded by reviewed code, not by you."
            ),
        },
        "source_chain_id": {
            "type": "integer",
            "description": "EIP-155 chain id of the chain the transaction is submitted to.",
        },
        "dest_chain_ref": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scheme": {
                    "type": "string",
                    "description": "How the destination is identified: 'cctp_domain', 'eip155', 'layerzero_eid', 'wormhole_chain_id', ...",
                },
                "value": {"type": "string", "description": "The identifier under that scheme."},
            },
            "required": ["scheme", "value"],
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "URLs the router address and ABI came from. At least two independent hosts; "
                "a single page is a claim, not evidence."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "Why this route, in a few sentences. Shown to the human who approves it.",
        },
        "spend": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "The ERC-20 approval this route needs before the call can succeed. "
                "Almost every ERC-20 bridge needs one: the router calls transferFrom, "
                "which fails unless the sender has approved it first. Omit this ONLY "
                "for a native-asset transfer where no token is pulled."
            ),
            "properties": {
                "token": {
                    "type": "string",
                    "description": "ERC-20 contract whose balance the router will move, on the source chain.",
                },
                "spender": {
                    "type": "string",
                    "description": (
                        "Contract that must be approved. Usually the router, but some "
                        "protocols take allowance on a separate contract — report the "
                        "one the documentation names, not the one you assume."
                    ),
                },
                "amount": {
                    "type": "string",
                    "description": "Base units to approve, normally equal to the amount being transferred.",
                },
            },
            "required": ["token", "spender", "amount"],
        },
    },
    "required": list(PROPOSAL_REQUIRED_FIELDS),
}

NO_ROUTE_FOUND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reason": {"type": "string", "description": "What you established, and why no route follows."},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs consulted."},
    },
    "required": ["reason", "sources"],
}

PROPOSE_ROUTE_DESCRIPTION = (
    "Report the bridge route you have established from documentation. Call this once, "
    "when you can name the router contract on the SOURCE chain, the exact function to "
    "call, and every argument it takes. Do not call it on a guess."
)

NO_ROUTE_FOUND_DESCRIPTION = (
    "Report that no route exists between this pair for this asset. This is a correct, "
    "successful answer — use it rather than proposing a route you are unsure of. "
    "Guessing a router address costs the user their funds; saying no costs them nothing."
)
