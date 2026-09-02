"""
Google Gemini implementation of the discovery contract.

Structured in two phases, which is a consequence of the API rather than a preference:
Gemini rejects a request that declares Google Search grounding *and* function
declarations together. So research happens first with grounding and no functions, then
a second call — grounding removed, function calling forced — turns what was found into
the structured answer.

Two differences from the Anthropic provider are worth knowing before trusting this:

  - **No search allowlist.** `GoogleSearch` accepts `exclude_domains` only; there is no
    per-request allowlist. The model can therefore read anything, and the allowlist
    degrades from a restriction on browsing to a filter on citations, enforced after
    the fact by `provenance.check_sources`. That is weaker: a page outside the
    allowlist can still influence the answer, it just cannot be cited as evidence for
    it. The corroboration requirement is what keeps this honest.

  - **No strict schemas.** `parameters_json_schema` is a hint, not an enforced contract.
    A missing field reaches `RouteProposal.from_tool_input`, which raises rather than
    defaulting, and a malformed one reaches `synthesis._check_shape`. Safe, but noisier
    than Anthropic's server-side enforcement.

Neither difference weakens the guardrails: provenance still reads the router's bytecode
for the claimed selector, and the simulator still has to get a real eth_call through.
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
)
from omnibridge.agent.provider import (
    SYSTEM_PROMPT,
    DiscoveryProvider,
    register_provider,
)

DEFAULT_MODEL = "gemini-2.5-pro"

RESEARCH_SUFFIX = """

First, research. Search the documentation and read the pages that look authoritative.
Prefer these hosts, and say so when you had to rely on anything else:

{domains}

Write down what you established: the protocol, the router contract on the source chain,
the exact function signature, every argument it takes, and the URLs you read it from.
Do not answer from memory — a router address you recall but did not just read is a guess."""

DECIDE_SUFFIX = """

Now report your conclusion by calling exactly one function: propose_route if you
established a route, or no_route_found if you did not. Use only what your research
actually supports. An honest no_route_found is a correct answer; a guessed router
address is somebody's money."""


def _strip_unsupported(schema: Any) -> Any:
    """
    Remove JSON Schema keywords Gemini's validator rejects.

    `additionalProperties: false` is the notable one: Anthropic enforces it server-side
    and Gemini errors on it. Dropping it means a model *could* return an extra field —
    harmless, because `RouteProposal.from_tool_input` reads only what it needs and
    raises on anything absent.
    """
    if isinstance(schema, dict):
        return {
            k: _strip_unsupported(v)
            for k, v in schema.items()
            if k not in ("additionalProperties", "$schema", "strict")
        }
    if isinstance(schema, list):
        return [_strip_unsupported(v) for v in schema]
    return schema


@register_provider
class GeminiDiscoveryAgent(DiscoveryProvider):
    """Two-phase grounded discovery over Gemini."""

    name = "gemini"
    credential_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(
        self,
        client: Any = None,
        model: Optional[str] = None,
        domains: Optional[List[str]] = None,
        max_tokens: int = 16000,
        thinking_budget: int = -1,
    ):
        super().__init__(model=model or self.default_model(), domains=domains)
        self._client = client
        self.max_tokens = max_tokens
        # -1 asks the model to decide how much to think, which is the analogue of
        # adaptive thinking on the Anthropic path.
        self.thinking_budget = thinking_budget

    @classmethod
    def default_model(cls) -> str:
        return os.environ.get("OMNIBRIDGE_MODEL") or DEFAULT_MODEL

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ProviderUnavailable(
                    "The gemini provider needs the google-genai package: "
                    "pip install 'omnibridge[gemini]'"
                ) from exc

            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                raise ProviderUnavailable(
                    "Set GEMINI_API_KEY (or GOOGLE_API_KEY) to use the gemini provider."
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    # ------------------------------------------------------------------ tools

    def _function_tools(self) -> List[Any]:
        from google.genai import types

        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="propose_route",
                        description=PROPOSE_ROUTE_DESCRIPTION,
                        parameters_json_schema=_strip_unsupported(PROPOSE_ROUTE_SCHEMA),
                    ),
                    types.FunctionDeclaration(
                        name="no_route_found",
                        description=NO_ROUTE_FOUND_DESCRIPTION,
                        parameters_json_schema=_strip_unsupported(NO_ROUTE_FOUND_SCHEMA),
                    ),
                ]
            )
        ]

    def _research_config(self) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=self.max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=self.thinking_budget),
            tools=[
                types.Tool(google_search=types.GoogleSearch()),
                types.Tool(url_context=types.UrlContext()),
            ],
        )

    def _decide_config(self) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=self.max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=self.thinking_budget),
            tools=self._function_tools(),
            # ANY forces a call to one of the two terminal functions, so the model
            # cannot end the turn with prose where a decision belongs. Anthropic has no
            # equivalent; there the loop refuses an undecided turn instead.
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=["propose_route", "no_route_found"],
                )
            ),
        )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _text_of(response: Any) -> str:
        parts = []
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                text = getattr(part, "text", None)
                if text:
                    parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _function_calls(response: Any) -> List[Any]:
        calls = []
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                call = getattr(part, "function_call", None)
                if call is not None:
                    calls.append(call)
        return calls

    @staticmethod
    def _grounded_urls(response: Any) -> List[str]:
        """
        URLs the grounding metadata says were actually consulted.

        Recorded next to the model's own citation list so the two can be compared: a
        source cited but never retrieved is the failure mode that defeats the
        corroboration check while appearing to pass it.
        """
        urls: List[str] = []
        for candidate in getattr(response, "candidates", None) or []:
            meta = getattr(candidate, "grounding_metadata", None)
            for chunk in getattr(meta, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None) or getattr(web, "domain", None)
                if uri:
                    urls.append(uri)
        return urls

    def _generate(self, contents: Any, config: Any, budget: RequestBudget) -> Any:
        budget.check()
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if any(
                marker in lowered
                for marker in ("api key", "permission", "quota", "unauthenticated", "403", "429")
            ):
                raise ProviderUnavailable(f"Gemini request refused: {message}") from exc
            raise
        budget.record_usage(getattr(response, "usage_metadata", None))
        return response

    # ---------------------------------------------------------------- discover

    def discover(
        self,
        intent: Any,
        source_chain: Any,
        dest_chain_name: str,
        budget: RequestBudget,
        failure_context: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> RouteProposal:
        """Research with grounding, then force a structured terminal answer."""
        budget.start_attempt()
        question = self.build_question(intent, source_chain, dest_chain_name, failure_context)
        turns: List[Dict[str, Any]] = []

        # Phase 1 — research.
        research_prompt = question + RESEARCH_SUFFIX.format(
            domains="\n".join(f"  - {d}" for d in self.domains)
        )
        research = self._generate(research_prompt, self._research_config(), budget)
        findings = self._text_of(research)
        retrieved = self._grounded_urls(research)
        turns.append({"phase": "research", "retrieved_urls": retrieved, "chars": len(findings)})

        if not findings.strip():
            raise RuntimeError(
                "Gemini research phase returned no text; nothing to conclude from."
            )

        # Phase 2 — decide. Grounding is dropped here because the API refuses it
        # alongside function declarations; the findings carry forward as text.
        decide_prompt = (
            f"{question}\n\nYour research found:\n\n{findings}\n{DECIDE_SUFFIX}"
        )
        decision = self._generate(decide_prompt, self._decide_config(), budget)
        calls = self._function_calls(decision)
        turns.append({"phase": "decide", "function_calls": [c.name for c in calls]})

        if not calls:
            if trace is not None:
                trace.update({"turns": turns, "outcome": "UNDECIDED"})
            raise RuntimeError(
                "Gemini ended without calling propose_route or no_route_found, despite "
                "function calling being forced. Nothing was decided, so nothing is returned."
            )

        call = calls[0]
        payload = dict(call.args or {})
        if isinstance(payload, str):
            payload = json.loads(payload)

        if call.name == "no_route_found":
            if trace is not None:
                trace.update({"turns": turns, "outcome": "NO_ROUTE_FOUND"})
            raise NoRouteFound(
                payload.get("reason", "No route reported."),
                list(payload.get("sources", []) or []),
            )

        if call.name != "propose_route":
            raise RuntimeError(f"Gemini called an unexpected function {call.name!r}.")

        # Raises on a missing field rather than defaulting. Gemini does not enforce the
        # schema server-side, so this is where an incomplete answer is caught.
        proposal = RouteProposal.from_tool_input(payload)

        if trace is not None:
            trace.update(
                {
                    "turns": turns,
                    "outcome": "PROPOSED",
                    "proposal": proposal.as_dict(),
                    # Kept so a proposal citing pages the grounding never fetched is
                    # visible in the manifest rather than only in the model's own claim.
                    "retrieved_urls": retrieved,
                }
            )
        return proposal
