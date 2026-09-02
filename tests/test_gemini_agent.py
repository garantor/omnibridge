"""
Gemini provider control flow.

No live API call. The two-phase loop is driven by a fake client so the parts that can
go wrong — a forced call that does not arrive, a proposal missing a field, an auth
failure reported as a routing answer — are exercised without spending anything.
"""

import types as pytypes

import pytest

from omnibridge.agent.budget import RequestBudget
from omnibridge.agent.contract import NoRouteFound, ProviderUnavailable, RouteProposal
from omnibridge.agent.gemini_agent import GeminiDiscoveryAgent, _strip_unsupported
from omnibridge.core.models import ChainInfo, UserIntent

BASE = ChainInfo(chain_id=8453, name="Base", block_explorer="", rpc_urls=[])
INTENT = UserIntent(token="USDC", amount=100.0, source_chain="base",
                    dest_chain="arbitrum", recipient="0x" + "11" * 20)

VALID_ARGS = {
    "protocol": "CCTP",
    "router_address": "0x1682Ae6375C4E4A97e4B583BC394c861A46D8962",
    "function_signature": "depositForBurn(uint256,uint32,bytes32,address)",
    "param_types": ["uint256", "uint32", "bytes32", "address"],
    "param_values": ["100000000", "3", "0x" + "11" * 32, "0x" + "22" * 20],
    "source_chain_id": 8453,
    "dest_chain_ref": {"scheme": "cctp_domain", "value": "3"},
    "sources": ["https://docs.circle.com/a", "https://developers.circle.com/b"],
    "rationale": "CCTP burns and mints native USDC.",
}


def _part(text=None, call=None):
    return pytypes.SimpleNamespace(text=text, function_call=call)


def _call(name, args):
    return pytypes.SimpleNamespace(name=name, args=args)


def _response(parts, urls=()):
    chunks = [
        pytypes.SimpleNamespace(web=pytypes.SimpleNamespace(uri=u, domain=u)) for u in urls
    ]
    return pytypes.SimpleNamespace(
        candidates=[
            pytypes.SimpleNamespace(
                content=pytypes.SimpleNamespace(parts=parts),
                grounding_metadata=pytypes.SimpleNamespace(grounding_chunks=chunks),
            )
        ],
        usage_metadata=pytypes.SimpleNamespace(
            prompt_token_count=1200, candidates_token_count=300, cached_content_token_count=0
        ),
    )


class FakeClient:
    """Returns queued responses; records the config each phase was called with."""

    def __init__(self, responses):
        self._queue = list(responses)
        self.configs = []
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        self.configs.append(config)
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _agent(responses):
    return GeminiDiscoveryAgent(client=FakeClient(responses), model="gemini-test")


RESEARCH = _response([_part(text="CCTP TokenMessenger on Base is 0x1682...")],
                     urls=["https://docs.circle.com/a"])


# --------------------------------------------------------------- happy path

def test_a_proposal_comes_back_from_the_decide_phase():
    agent = _agent([RESEARCH, _response([_part(call=_call("propose_route", VALID_ARGS))])])
    budget = RequestBudget()
    trace = {}

    proposal = agent.discover(INTENT, BASE, "Arbitrum", budget, trace=trace)

    assert isinstance(proposal, RouteProposal)
    assert proposal.router_address == VALID_ARGS["router_address"]
    assert trace["outcome"] == "PROPOSED"
    # Two phases, two model calls, both charged.
    assert budget.model_calls == 2
    assert budget.input_tokens == 2400 and budget.output_tokens == 600


def test_no_route_found_is_raised_as_an_answer():
    decision = _response([_part(call=_call(
        "no_route_found", {"reason": "no bridge for this pair", "sources": ["https://x"]}
    ))])
    with pytest.raises(NoRouteFound) as excinfo:
        _agent([RESEARCH, decision]).discover(INTENT, BASE, "Arbitrum", RequestBudget())
    assert "no bridge" in excinfo.value.reason


# ----------------------------------------------------- the two-phase shape

def test_search_and_function_declarations_are_never_sent_together():
    """
    Gemini rejects a request declaring Google Search grounding alongside function
    declarations. The split into research-then-decide exists for that reason, and a
    regression here would fail only against the live API.
    """
    agent = _agent([RESEARCH, _response([_part(call=_call("propose_route", VALID_ARGS))])])
    agent.discover(INTENT, BASE, "Arbitrum", RequestBudget())

    research_cfg, decide_cfg = agent.client.configs

    def has_search(cfg):
        return any(getattr(t, "google_search", None) is not None for t in cfg.tools)

    def has_functions(cfg):
        return any(getattr(t, "function_declarations", None) for t in cfg.tools)

    assert has_search(research_cfg) and not has_functions(research_cfg)
    assert has_functions(decide_cfg) and not has_search(decide_cfg)


def test_the_decide_phase_forces_a_terminal_call():
    """
    Gemini can be made to call a function; ANY mode is what stops it ending the turn
    with prose where a decision belongs.
    """
    agent = _agent([RESEARCH, _response([_part(call=_call("propose_route", VALID_ARGS))])])
    agent.discover(INTENT, BASE, "Arbitrum", RequestBudget())

    cfg = agent.client.configs[1].tool_config.function_calling_config
    assert str(cfg.mode).endswith("ANY")
    assert set(cfg.allowed_function_names) == {"propose_route", "no_route_found"}


def test_the_schema_drops_keywords_gemini_rejects():
    """`additionalProperties` errors on Gemini's validator; Anthropic enforces it."""
    cleaned = _strip_unsupported(
        {"type": "object", "additionalProperties": False,
         "properties": {"a": {"type": "object", "additionalProperties": False}}}
    )
    assert "additionalProperties" not in cleaned
    assert "additionalProperties" not in cleaned["properties"]["a"]


# ------------------------------------------------------------------ refusals

def test_an_incomplete_proposal_is_refused_not_defaulted():
    """
    Gemini does not enforce the schema server-side, so this is the only thing standing
    between a dropped field and a defaulted value reaching real calldata.
    """
    incomplete = {k: v for k, v in VALID_ARGS.items() if k != "param_values"}
    agent = _agent([RESEARCH, _response([_part(call=_call("propose_route", incomplete))])])

    with pytest.raises(ValueError) as excinfo:
        agent.discover(INTENT, BASE, "Arbitrum", RequestBudget())
    assert "param_values" in str(excinfo.value)


def test_a_turn_with_no_function_call_is_not_treated_as_a_route():
    agent = _agent([RESEARCH, _response([_part(text="I think maybe use some bridge")])])
    with pytest.raises(RuntimeError) as excinfo:
        agent.discover(INTENT, BASE, "Arbitrum", RequestBudget())
    assert "Nothing was decided" in str(excinfo.value)


def test_empty_research_does_not_proceed_to_a_conclusion():
    agent = _agent([_response([_part(text="   ")])])
    with pytest.raises(RuntimeError) as excinfo:
        agent.discover(INTENT, BASE, "Arbitrum", RequestBudget())
    assert "nothing to conclude" in str(excinfo.value)


def test_an_auth_failure_is_provider_unavailable_not_no_route():
    """
    "I could not ask" and "the answer is no" are opposite statements. Folding the first
    into the second reports a credentials problem as a fact about the world.
    """
    agent = _agent([RuntimeError("403 permission denied: API key not valid")])
    with pytest.raises(ProviderUnavailable):
        agent.discover(INTENT, BASE, "Arbitrum", RequestBudget())


def test_budget_ceilings_still_bind():
    agent = _agent([RESEARCH, _response([_part(call=_call("propose_route", VALID_ARGS))])])
    budget = RequestBudget(max_model_calls=1)
    from omnibridge.agent.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        agent.discover(INTENT, BASE, "Arbitrum", budget)


# ------------------------------------------------------------- grounding audit

def test_retrieved_urls_are_recorded_alongside_the_cited_ones():
    """
    Gemini's search has no allowlist, so the allowlist becomes a filter on citations.
    Recording what grounding actually fetched makes a proposal citing pages it never
    read visible, rather than only the model's own claim about its sources.
    """
    agent = _agent([RESEARCH, _response([_part(call=_call("propose_route", VALID_ARGS))])])
    trace = {}
    agent.discover(INTENT, BASE, "Arbitrum", RequestBudget(), trace=trace)
    assert trace["retrieved_urls"] == ["https://docs.circle.com/a"]
