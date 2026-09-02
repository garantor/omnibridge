"""
Anthropic implementation of the discovery contract.

One provider among possibly several — the interface, the answer shape, the schema and
the prompt live in provider.py and contract.py, which know nothing about any vendor.
What is Anthropic-specific and stays here: the SDK client, the server-side
`web_search` / `web_fetch` tools with their domain allowlist, adaptive thinking,
effort, `pause_turn` resumption, refusal blocks and server-side fallbacks.

None of that is reproducible on another API, and none of it needs to be. A provider
that searches client-side is equally acceptable, because what makes a proposal
trustworthy is checked afterwards by code that cannot tell the difference.
"""

import json
import os
from typing import Any, Dict, List, Optional

from omnibridge.agent.budget import RequestBudget
from omnibridge.agent.contract import (
    NO_ROUTE_FOUND_DESCRIPTION,
    NO_ROUTE_FOUND_SCHEMA,
    PROPOSE_ROUTE_DESCRIPTION,
    PROPOSE_ROUTE_SCHEMA,
    NoRouteFound,
    ProviderUnavailable,
    RouteProposal,
    agent_disabled,
    allowed_domains,
)
from omnibridge.agent.provider import (
    SYSTEM_PROMPT,
    DiscoveryProvider,
    register_provider,
)

# Re-exported so existing imports from this module keep working.
__all__ = [
    "AnthropicDiscoveryAgent",
    "DiscoveryAgent",
    "NoRouteFound",
    "ProviderUnavailable",
    "RouteProposal",
    "SYSTEM_PROMPT",
    "agent_disabled",
    "allowed_domains",
    "workspace_from_env",
    "MODEL",
    "PROPOSE_ROUTE_TOOL",
    "NO_ROUTE_FOUND_TOOL",
]

MODEL = os.environ.get("OMNIBRIDGE_MODEL", "claude-opus-5")

# The neutral schemas, wrapped in Anthropic's tool shape. `strict` makes the API
# enforce the schema rather than leaving it to the prompt.
PROPOSE_ROUTE_TOOL: Dict[str, Any] = {
    "name": "propose_route",
    "description": PROPOSE_ROUTE_DESCRIPTION,
    "strict": True,
    "input_schema": PROPOSE_ROUTE_SCHEMA,
}

NO_ROUTE_FOUND_TOOL: Dict[str, Any] = {
    "name": "no_route_found",
    "description": NO_ROUTE_FOUND_DESCRIPTION,
    "strict": True,
    "input_schema": NO_ROUTE_FOUND_SCHEMA,
}


def workspace_from_env() -> Optional[str]:
    """
    The workspace an identity-linked key acts in, if one is configured.

    An identity-linked API key is not scoped to a single workspace, so the API cannot
    infer which one a request belongs to and rejects the call outright:
    "anthropic-workspace-id is required when authenticating with an identity-linked
    API key". A plain key needs none of this and is unaffected.
    """
    return (
        os.environ.get("OMNIBRIDGE_WORKSPACE_ID")
        or os.environ.get("ANTHROPIC_WORKSPACE_ID")
        or None
    )


@register_provider
class AnthropicDiscoveryAgent(DiscoveryProvider):
    """Bounded tool loop over Claude, returning one route proposal or an honest refusal."""

    name = "anthropic"
    credential_env = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

    def __init__(
        self,
        client: Any = None,
        model: str = MODEL,
        domains: Optional[List[str]] = None,
        max_tokens: int = 16000,
        effort: str = "high",
        use_fallbacks: bool = True,
        workspace_id: Optional[str] = None,
    ):
        super().__init__(model=model, domains=domains)
        self._client = client
        self.max_tokens = max_tokens
        self.effort = effort
        # Opus 5 may decline a request outright; a server-side fallback keeps a legitimate
        # bridging question from dead-ending on a classifier. Disable with use_fallbacks=False.
        self.use_fallbacks = use_fallbacks
        # Identity-linked API keys must say which workspace a request acts in, or the
        # API refuses it. Not a secret and not derivable from the key, so it is
        # configuration: OMNIBRIDGE_WORKSPACE_ID, or ANTHROPIC_WORKSPACE_ID.
        self.workspace_id = workspace_id or workspace_from_env()

    @classmethod
    def default_model(cls) -> str:
        return MODEL

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            headers = (
                {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
            )
            self._client = anthropic.Anthropic(default_headers=headers)
        return self._client

    def _tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 8,
                "allowed_domains": self.domains,
            },
            {
                "type": "web_fetch_20260209",
                "name": "web_fetch",
                "max_uses": 8,
                "allowed_domains": self.domains,
            },
            PROPOSE_ROUTE_TOOL,
            NO_ROUTE_FOUND_TOOL,
        ]

    def _request(self, messages: List[Dict[str, Any]]) -> Any:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": self._tools(),
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }
        if self.use_fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            return self.client.beta.messages.create(**kwargs)
        return self.client.messages.create(**kwargs)

    def discover(
        self,
        intent: Any,
        source_chain: Any,
        dest_chain_name: str,
        budget: RequestBudget,
        failure_context: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> RouteProposal:
        """
        Run the loop until the model reports a route or refuses.

        Raises NoRouteFound when it honestly finds none, BudgetExceeded when it runs out
        of room, and RuntimeError when it stops without deciding either way.
        """
        budget.start_attempt()
        question = self.build_question(intent, source_chain, dest_chain_name, failure_context)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": question}]
        turns: List[Dict[str, Any]] = []

        while True:
            budget.check()
            response = self._request(messages)
            budget.record_usage(getattr(response, "usage", None))

            turns.append(
                {
                    "stop_reason": getattr(response, "stop_reason", None),
                    "block_types": [getattr(b, "type", "?") for b in response.content],
                }
            )

            if getattr(response, "stop_reason", None) == "refusal":
                details = getattr(response, "stop_details", None)
                raise RuntimeError(
                    "Discovery declined by the model"
                    + (f" ({getattr(details, 'category', None)})" if details else "")
                )

            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                name = getattr(block, "name", "")
                payload = getattr(block, "input", {}) or {}
                if isinstance(payload, str):
                    payload = json.loads(payload)

                if name == "no_route_found":
                    if trace is not None:
                        trace.update({"turns": turns, "outcome": "NO_ROUTE_FOUND"})
                    raise NoRouteFound(
                        payload.get("reason", "No route reported."),
                        payload.get("sources", []),
                    )

                if name == "propose_route":
                    # Built through the contract, which raises on a missing field
                    # rather than defaulting one. Anthropic's strict schema should make
                    # that unreachable here; a provider without strict tools will not.
                    proposal = RouteProposal.from_tool_input(payload)
                    if trace is not None:
                        trace.update(
                            {
                                "turns": turns,
                                "outcome": "PROPOSED",
                                "proposal": proposal.as_dict(),
                            }
                        )
                    return proposal

            # Server-side tools may pause a long turn; continue where it left off.
            if getattr(response, "stop_reason", None) == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue

            if getattr(response, "stop_reason", None) == "tool_use":
                # A client tool we do not serve. Nothing to execute, so stop rather than
                # loop; the model has no other client-side tool it is allowed to use.
                raise RuntimeError("Model requested an unsupported tool; refusing to continue.")

            if trace is not None:
                trace.update({"turns": turns, "outcome": "UNDECIDED"})
            raise RuntimeError(
                "Discovery ended without proposing a route or reporting that none exists "
                f"(stop_reason={getattr(response, 'stop_reason', None)})."
            )


# The original name, kept so existing call sites and tests are unaffected by the
# provider split. `get_provider()` is the way to reach whichever one is configured.
DiscoveryAgent = AnthropicDiscoveryAgent
