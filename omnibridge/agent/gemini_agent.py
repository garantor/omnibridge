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
from omnibridge.steplog import StepLogger

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

    def _function_tools(self, toolkit: Any = None) -> List[Any]:
        from google.genai import types

        client = [
            types.FunctionDeclaration(
                name=spec["name"],
                description=spec["description"],
                parameters_json_schema=_strip_unsupported(spec["input_schema"]),
            )
            for spec in (toolkit.schemas() if toolkit is not None else [])
        ]
        return [
            types.Tool(
                function_declarations=client + [
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

    def _decide_config(self, toolkit: Any = None) -> Any:
        from google.genai import types

        client_names = [spec["name"] for spec in toolkit.schemas()] if toolkit is not None else []
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=self.max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=self.thinking_budget),
            tools=self._function_tools(toolkit),
            # ANY forces a call to one of the two terminal functions, so the model
            # cannot end the turn with prose where a decision belongs. Anthropic has no
            # equivalent; there the loop refuses an undecided turn instead.
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=client_names + ["propose_route", "no_route_found"],
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
                # `domain` first, deliberately. `uri` is a
                # vertexaisearch.cloud.google.com/grounding-api-redirect/... link, so
                # comparing it against a cited docs.circle.com URL never matches and
                # marked every citation "never fetched" — noise where the signal
                # should be, which is worse than no check at all.
                where = getattr(web, "domain", None) or getattr(web, "uri", None)
                if where:
                    urls.append(where)
        return urls

    def _generate(
        self, contents: Any, config: Any, budget: RequestBudget, log: Optional[StepLogger] = None
    ) -> Any:
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
                for marker in ("404", "not found", "not_found", "is not supported")
            ):
                # The API says only "Not Found" with an empty message, which is useless
                # on its own. Name the model that was asked for — it is almost always
                # the model id that is wrong.
                raise ProviderUnavailable(
                    f"Gemini rejected model {self.model!r} ({message.strip() or '404 Not Found'}). "
                    "Check OMNIBRIDGE_MODEL names a model your key can reach; "
                    "`python -m omnibridge.cli providers --list-models` will list them."
                ) from exc
            if any(
                marker in lowered
                for marker in ("api key", "permission", "quota", "unauthenticated", "403", "429")
            ):
                raise ProviderUnavailable(f"Gemini request refused: {message}") from exc
            raise
        usage = getattr(response, "usage_metadata", None)
        budget.record_usage(usage)
        if log is not None and usage is not None:
            log.value(
                "tokens",
                f"{getattr(usage, 'prompt_token_count', '?')} in / "
                f"{getattr(usage, 'candidates_token_count', '?')} out "
                f"(request {budget.model_calls}/{budget.max_model_calls})",
            )
        return response

    # ------------------------------------------------------------------ decide

    #: Decide turns that may be spent on client tools before a terminal answer.
    MAX_TOOL_TURNS = 6

    def _decide(self, prompt: str, budget: RequestBudget, log: StepLogger,
                turns: List[Dict[str, Any]], toolkit: Any = None) -> List[Any]:
        """
        Call the model with function calling forced until it gives a terminal answer.

        Without a toolkit this is one call, exactly as before. With one, a client-tool
        call is executed and its result sent back, and the model decides again — bounded,
        because mode=ANY forces *a* call every turn and a model that keeps checking
        things must still be made to conclude.
        """
        contents: Any = prompt
        if toolkit is not None:
            from google.genai import types

            contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

        for turn in range(self.MAX_TOOL_TURNS + 1):
            with log.timed("decide call" if turn == 0 else f"decide call {turn + 1}"):
                decision = self._generate(contents, self._decide_config(toolkit), budget, log)
            calls = self._function_calls(decision)
            turns.append({"phase": "decide", "function_calls": [c.name for c in calls]})
            log.value("function called", ", ".join(c.name for c in calls) or "none")

            client = [c for c in calls if toolkit is not None and toolkit.handles(c.name)]
            terminal = [c for c in calls if c.name in ("propose_route", "no_route_found")]
            if not client:
                return calls

            from google.genai import types

            parts = []
            for call in client:
                result = toolkit.handle(call.name, dict(call.args or {}))
                log.value(f"  {call.name}", "ok" if result.get("ok") else result.get("error")
                          or "; ".join(result.get("reasons", [])), limit=200)
                parts.append(types.Part.from_function_response(name=call.name, response=result))
            if terminal:
                return terminal
            if turn == self.MAX_TOOL_TURNS:
                break
            contents.append(decision.candidates[0].content)
            contents.append(types.Content(role="user", parts=parts))

        log.fail(f"no terminal answer after {self.MAX_TOOL_TURNS} tool turns")
        return []

    # ---------------------------------------------------------------- discover

    def discover(
        self,
        intent: Any,
        source_chain: Any,
        dest_chain_name: str,
        budget: RequestBudget,
        failure_context: Optional[str] = None,
        trace: Optional[Dict[str, Any]] = None,
        cards_context: Optional[str] = None,
        tools: Optional[Any] = None,
    ) -> RouteProposal:
        """Research with grounding, then force a structured terminal answer."""
        budget.start_attempt()
        log = StepLogger("gemini")
        question = self.build_question(
            intent, source_chain, dest_chain_name, failure_context, cards_context
        )
        turns: List[Dict[str, Any]] = []

        log.step(f"model [bold]{self.model}[/bold], attempt {budget.attempts}/{budget.max_attempts}")
        log.value("route asked for", f"{intent.amount} {intent.token} "
                                     f"{source_chain.name}({source_chain.chain_id}) → {dest_chain_name}")
        if failure_context:
            log.warn("retrying with the previous failure in context")
            log.value("previous failure", failure_context, limit=200)
        log.detail(f"question:\n{question}")

        # Phase 1 — research.
        log.step("phase 1/2 — research (google_search + url_context, no functions)")
        research_prompt = question + RESEARCH_SUFFIX.format(
            domains="\n".join(f"  - {d}" for d in self.domains)
        )
        with log.timed("research call"):
            research = self._generate(research_prompt, self._research_config(), budget, log)
        findings = self._text_of(research)
        retrieved = self._grounded_urls(research)
        turns.append({"phase": "research", "retrieved_urls": retrieved, "chars": len(findings)})

        log.bullets("URLs grounding actually fetched", retrieved)
        if not retrieved:
            # Worth saying out loud: with nothing retrieved, whatever the model reports
            # came from its weights, and any URL it cites is a claim it did not check.
            log.warn("grounding fetched nothing — findings are from model memory, not documentation")
        log.value("findings", f"{len(findings)} chars")
        log.detail(f"findings:\n{findings}")

        if not findings.strip():
            log.fail("research returned no text")
            raise RuntimeError(
                "Gemini research phase returned no text; nothing to conclude from."
            )

        # Phase 2 — decide. Grounding is dropped here because the API refuses it
        # alongside function declarations; the findings carry forward as text.
        log.step("phase 2/2 — decide (function calling forced, grounding dropped)")
        decide_prompt = (
            f"{question}\n\nYour research found:\n\n{findings}\n{DECIDE_SUFFIX}"
        )
        calls = self._decide(decide_prompt, budget, log, turns, tools)

        if not calls:
            log.fail("no terminal function call despite mode=ANY")
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
            log.ok(f"no_route_found: {payload.get('reason', '')[:140]}")
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
        proposal.retrieved = list(retrieved)
        log.ok(f"proposed {proposal.protocol}")
        log.value("router", proposal.router_address)
        log.value("function", proposal.function_signature)
        log.value("params", ", ".join(
            f"{t}={v}" for t, v in zip(proposal.param_types, proposal.param_values)
        ), limit=240)
        log.value("destination ref", proposal.dest_chain_ref)
        log.bullets("sources cited by the model", proposal.sources)
        uncited = [u for u in retrieved if u not in proposal.sources]
        if uncited:
            log.detail(f"fetched but not cited: {len(uncited)}")
        unread = [u for u in proposal.sources if u not in retrieved]
        if unread:
            # The check that matters when search has no allowlist: a cited page the
            # grounding never fetched is a claim, not corroboration.
            log.warn(f"{len(unread)} cited source(s) were never fetched by grounding")
            for u in unread[:4]:
                log.value("  unverified citation", u)

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
