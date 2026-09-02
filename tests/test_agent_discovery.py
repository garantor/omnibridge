"""
Reasoning-core tests.

None of these call the Anthropic API. The loop is driven by a fake client that returns
canned responses, so the control flow — terminal tool calls, refusals, pause_turn,
budget ceilings — is verified without spending anything. A live smoke test lives at the
bottom and skips unless credentials are present.
"""

import os
import types

import pytest

from omnibridge.agent.budget import BudgetExceeded, RequestBudget
from omnibridge.agent.discovery_agent import (
    DiscoveryAgent,
    NoRouteFound,
    RouteProposal,
    agent_disabled,
)
from omnibridge.agent.provenance import selector_for
from omnibridge.agent.synthesis import ProposalRejected, synthesize
from omnibridge.core.models import UserIntent

ROUTER = "0x1682Ae6375C4E4A97e4B583BC394c861A46D8962"
SIGNATURE = "depositForBurn(uint256,uint32,bytes32,address)"


# --------------------------------------------------------------- fakes

class _Block:
    def __init__(self, type_, name=None, input_=None):
        self.type = type_
        self.name = name
        self.input = input_


class _Usage:
    input_tokens = 1200
    output_tokens = 300
    cache_read_input_tokens = 0


class _Response:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()
        self.stop_details = None


class FakeClient:
    """Returns queued responses; records how many times it was called."""

    def __init__(self, responses):
        self._queue = list(responses)
        self.calls = 0
        self.last_kwargs = None
        self.beta = types.SimpleNamespace(messages=self)

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if not self._queue:
            raise AssertionError("fake client ran out of responses")
        return self._queue.pop(0)


def _proposal_payload(**overrides):
    payload = {
        "protocol": "CCTP",
        "router_address": ROUTER,
        "function_signature": SIGNATURE,
        "param_types": ["uint256", "uint32", "bytes32", "address"],
        "param_values": ["1000000", "3", "0x" + "00" * 12 + "11" * 20, "0x" + "22" * 20],
        "source_chain_id": 8453,
        "dest_chain_ref": {"scheme": "cctp_domain", "value": "3"},
        "sources": ["https://developers.circle.com/a", "https://docs.across.to/b"],
        "rationale": "CCTP is Circle's native path for USDC.",
    }
    payload.update(overrides)
    return payload


BASE = types.SimpleNamespace(chain_id=8453, name="Base", rpc_urls=[])
INTENT = UserIntent(token="USDC", amount=1.0, source_chain="base", dest_chain="arbitrum")


# --------------------------------------------------------------- the loop

def test_agent_returns_a_proposal():
    client = FakeClient([_Response([_Block("tool_use", "propose_route", _proposal_payload())])])
    trace = {}
    proposal = DiscoveryAgent(client=client).discover(
        INTENT, BASE, "Arbitrum", RequestBudget(), trace=trace
    )
    assert isinstance(proposal, RouteProposal)
    assert proposal.router_address == ROUTER
    assert proposal.dest_chain_ref == {"scheme": "cctp_domain", "value": "3"}
    assert trace["outcome"] == "PROPOSED"


def test_no_route_found_is_an_answer_not_an_error():
    """The agent must be able to say 'there is no bridge' distinctly from 'I broke'."""
    client = FakeClient([
        _Response([_Block("tool_use", "no_route_found",
                          {"reason": "No USDC bridge between these chains.", "sources": ["https://x/y"]})])
    ])
    trace = {}
    with pytest.raises(NoRouteFound) as excinfo:
        DiscoveryAgent(client=client).discover(INTENT, BASE, "Nowhere", RequestBudget(), trace=trace)
    assert "No USDC bridge" in str(excinfo.value)
    assert trace["outcome"] == "NO_ROUTE_FOUND"


def test_pause_turn_is_resumed():
    client = FakeClient([
        _Response([_Block("text")], stop_reason="pause_turn"),
        _Response([_Block("tool_use", "propose_route", _proposal_payload())]),
    ])
    DiscoveryAgent(client=client).discover(INTENT, BASE, "Arbitrum", RequestBudget())
    assert client.calls == 2


def test_a_refusal_is_surfaced_not_swallowed():
    client = FakeClient([_Response([_Block("text")], stop_reason="refusal")])
    with pytest.raises(RuntimeError) as excinfo:
        DiscoveryAgent(client=client).discover(INTENT, BASE, "Arbitrum", RequestBudget())
    assert "declined" in str(excinfo.value)


def test_ending_without_deciding_is_an_error():
    client = FakeClient([_Response([_Block("text")], stop_reason="end_turn")])
    with pytest.raises(RuntimeError) as excinfo:
        DiscoveryAgent(client=client).discover(INTENT, BASE, "Arbitrum", RequestBudget())
    assert "without proposing a route" in str(excinfo.value)


def test_budget_stops_a_runaway_loop():
    client = FakeClient([_Response([_Block("text")], stop_reason="pause_turn")] * 20)
    budget = RequestBudget(max_model_calls=3)
    with pytest.raises(BudgetExceeded):
        DiscoveryAgent(client=client).discover(INTENT, BASE, "Arbitrum", budget)
    assert client.calls == 3


def test_retry_budget_is_per_request_not_per_call():
    budget = RequestBudget(max_attempts=2)
    for _ in range(2):
        client = FakeClient([_Response([_Block("tool_use", "propose_route", _proposal_payload())])])
        DiscoveryAgent(client=client).discover(INTENT, BASE, "Arbitrum", budget)
    client = FakeClient([_Response([_Block("tool_use", "propose_route", _proposal_payload())])])
    with pytest.raises(BudgetExceeded):
        DiscoveryAgent(client=client).discover(INTENT, BASE, "Arbitrum", budget)


def test_failure_context_reaches_the_model():
    client = FakeClient([_Response([_Block("tool_use", "propose_route", _proposal_payload())])])
    DiscoveryAgent(client=client).discover(
        INTENT, BASE, "Arbitrum", RequestBudget(), failure_context="reverted: bad domain"
    )
    sent = client.last_kwargs["messages"][0]["content"]
    assert "reverted: bad domain" in sent


def test_request_shape_matches_the_documented_api():
    client = FakeClient([_Response([_Block("tool_use", "propose_route", _proposal_payload())])])
    DiscoveryAgent(client=client, model="claude-opus-5").discover(
        INTENT, BASE, "Arbitrum", RequestBudget()
    )
    kw = client.last_kwargs
    assert kw["model"] == "claude-opus-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "high"
    assert "budget_tokens" not in str(kw["thinking"])
    names = [t.get("name") for t in kw["tools"]]
    assert names == ["web_search", "web_fetch", "propose_route", "no_route_found"]
    for tool in kw["tools"]:
        if tool.get("name") in ("propose_route", "no_route_found"):
            assert tool["strict"] is True
            assert tool["input_schema"]["additionalProperties"] is False


def test_search_is_domain_constrained():
    client = FakeClient([_Response([_Block("tool_use", "propose_route", _proposal_payload())])])
    DiscoveryAgent(client=client, domains=["developers.circle.com"]).discover(
        INTENT, BASE, "Arbitrum", RequestBudget()
    )
    for tool in client.last_kwargs["tools"]:
        if tool.get("type", "").startswith(("web_search", "web_fetch")):
            assert tool["allowed_domains"] == ["developers.circle.com"]


# --------------------------------------------------------------- synthesis

def _proposal(**overrides):
    return RouteProposal(**{k: v for k, v in _proposal_payload(**overrides).items()})


def test_synthesis_encodes_a_sound_proposal():
    calldata, quote, evidence = synthesize(_proposal(), INTENT, BASE, w3=None)
    assert calldata.startswith(selector_for(SIGNATURE))
    assert quote.router_address.lower() == ROUTER.lower()
    assert quote.calldata_template == "agent_proposed"
    assert quote.extra_params["dest_chain_ref"] == {"scheme": "cctp_domain", "value": "3"}
    assert evidence["provenance"] == {"skipped": "no RPC supplied"}


def test_synthesis_refuses_a_signature_that_contradicts_the_types():
    with pytest.raises(ProposalRejected) as excinfo:
        synthesize(_proposal(param_types=["uint256", "uint256", "bytes32", "address"]),
                   INTENT, BASE, w3=None)
    assert "do not match" in str(excinfo.value)


def test_synthesis_refuses_a_router_on_the_wrong_chain():
    with pytest.raises(ProposalRejected) as excinfo:
        synthesize(_proposal(source_chain_id=42161), INTENT, BASE, w3=None)
    assert "wrong network" in str(excinfo.value)


def test_synthesis_refuses_a_bad_router_address():
    with pytest.raises(ProposalRejected):
        synthesize(_proposal(router_address="not-an-address"), INTENT, BASE, w3=None)


# --------------------------------------------------------------- kill switch

def test_kill_switch(monkeypatch):
    monkeypatch.delenv("OMNIBRIDGE_DISABLE_AGENT", raising=False)
    assert agent_disabled() is False
    monkeypatch.setenv("OMNIBRIDGE_DISABLE_AGENT", "1")
    assert agent_disabled() is True


# --------------------------------------------------------------- live smoke

@pytest.mark.network
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="no ANTHROPIC_API_KEY; the live path costs money and is opt-in",
)
def test_live_discovery_base_to_arbitrum():
    """Real model call. Skipped by default; run deliberately with a key set."""
    budget = RequestBudget()
    proposal = DiscoveryAgent().discover(INTENT, BASE, "Arbitrum", budget)
    assert proposal.source_chain_id == 8453
    assert proposal.router_address.startswith("0x")
    assert len(proposal.sources) >= 2
    print("\nbudget:", budget.as_dict())


# --------------------------------------------------------------- retry edge

def test_a_revert_becomes_feedback_not_a_terminus():
    from graph import error_node, route_after_error

    state = {
        "simulation_result": {"status": "REVERTED", "revert_reason": "execution reverted"},
        "route": {"router_address": ROUTER},
        "retry_count": 0,
        "traces": {},
    }
    out = error_node(state)
    assert out["failure_context"] and ROUTER in out["failure_context"]
    assert out["retry_count"] == 1
    assert route_after_error({**state, **out}) == "discovery_node"


def test_retries_are_bounded():
    from graph import MAX_DISCOVERY_RETRIES, error_node, route_after_error

    state = {
        "simulation_result": {"status": "REVERTED", "revert_reason": "execution reverted"},
        "route": {"router_address": ROUTER},
        "retry_count": MAX_DISCOVERY_RETRIES,
        "traces": {},
    }
    out = error_node(state)
    assert out["failure_context"] is None
    assert route_after_error({**state, **out}) == "__end__"


def test_an_unsupported_chain_is_not_retried():
    """Retrying a route with no simulation path just spends money to learn the same thing."""
    from graph import error_node, route_after_error

    state = {
        "simulation_result": {"status": "UNSUPPORTED", "revert_reason": "no eth_call path"},
        "route": {"router_address": ROUTER},
        "retry_count": 0,
        "traces": {},
    }
    out = error_node(state)
    assert out["failure_context"] is None
    assert route_after_error({**state, **out}) == "__end__"


def test_agent_path_is_skipped_without_credentials(monkeypatch):
    """No key means the static path, not a crash."""
    import graph

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert graph._run_discovery_agent({"target_chain": "arbitrum"}, INTENT) is None


def test_kill_switch_forces_the_static_path(monkeypatch):
    import graph

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("OMNIBRIDGE_DISABLE_AGENT", "1")
    assert graph._run_discovery_agent({"target_chain": "arbitrum"}, INTENT) is None


# ------------------------------------------------------------- workspace / config

def test_an_identity_linked_key_sends_its_workspace(monkeypatch):
    """
    An identity-linked API key is not scoped to one workspace, so the API cannot infer
    which one a request acts in and refuses the call outright. The id is configuration,
    not a secret, and not derivable from the key.
    """
    monkeypatch.setenv("OMNIBRIDGE_WORKSPACE_ID", "wrkspc_test123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")

    agent = DiscoveryAgent()
    assert agent.workspace_id == "wrkspc_test123"
    assert agent.client._custom_headers["anthropic-workspace-id"] == "wrkspc_test123"


def test_a_plain_key_sends_no_workspace_header(monkeypatch):
    """A key scoped to one workspace needs none of this and must not be given one."""
    monkeypatch.delenv("OMNIBRIDGE_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")

    agent = DiscoveryAgent()
    assert agent.workspace_id is None
    assert "anthropic-workspace-id" not in (agent.client._custom_headers or {})


def test_a_credentials_failure_is_not_reported_as_a_missing_route(monkeypatch):
    """
    A refused API call used to surface as a bare exception message, which read from the
    frontend like "this bridge does not exist". It is a configuration problem, it is
    terminal, and retrying it against the same credentials reaches the same answer.
    """
    import graph as g

    for marker in (
        "anthropic-workspace-id is required when authenticating with an identity-linked API key",
        "authentication_error: invalid x-api-key",
        "Your credit balance is too low",
    ):
        assert g._is_configuration_error(marker)

    assert not g._is_configuration_error("execution reverted: ERC20 transfer failed")
    assert not g._is_configuration_error("No verified skill covers USDC from base to stellar")

    # Terminal, and never routed back into discovery.
    assert g.route_after_discovery({"status": "CONFIG_ERROR"}) == "error_node"
    out = g.error_node({
        "status": "CONFIG_ERROR",
        "error": "anthropic-workspace-id is required",
        "simulation_result": {},
        "traces": {},
        "retry_count": 0,
    })
    assert out["status"] == "CONFIG_ERROR"
    assert out["failure_context"] is None
    assert g.route_after_error({**out}) == "__end__"
