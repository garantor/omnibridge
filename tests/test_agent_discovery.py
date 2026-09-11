"""
Reasoning-core tests.

None of these call the Anthropic API. The loop is driven by a fake client that returns
canned responses, so the control flow — terminal tool calls, refusals, pause_turn,
budget ceilings — is verified without spending anything. A live smoke test lives at the
bottom and skips unless credentials are present.
"""

import types

import pytest

from tests.conftest import requires_live

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
@requires_live("ANTHROPIC_API_KEY")
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


# ----------------------------------------------------- tiered corroboration

def test_documentation_plus_an_explorer_is_enough():
    """
    The case that was failing. A route named by the protocol's own docs and
    cross-checked on a block explorer was rejected for having looked in one place too
    many — while a model citing two allowlisted URLs it never opened passed.
    """
    from omnibridge.agent.contract import DEFAULT_ALLOWED_DOMAINS
    from omnibridge.agent.provenance import check_sources

    result = check_sources(
        ["https://developers.circle.com/cctp", "https://basescan.org/address/0x1682"],
        DEFAULT_ALLOWED_DOMAINS,
    )
    assert result["primary"] == ["developers.circle.com"]
    assert result["secondary"] == ["basescan.org"]
    assert len(result["corroborating"]) == 2
    # Dropped, not fatal.
    assert result["disallowed"] == []


def test_an_explorer_can_never_be_the_only_kind_of_source():
    """
    Explorers corroborate; they do not establish authority. Anyone can deploy a
    byte-identical clone and verify it on one, and it looks exactly as legitimate.
    """
    from omnibridge.agent.contract import DEFAULT_ALLOWED_DOMAINS
    from omnibridge.agent.provenance import check_sources

    for sources in (
        ["https://basescan.org/address/0x1"],
        ["https://basescan.org/address/0x1", "https://arbiscan.io/address/0x2"],
    ):
        result = check_sources(sources, DEFAULT_ALLOWED_DOMAINS)
        assert result["primary"] == [], sources


def test_an_unrelated_host_is_still_dropped():
    from omnibridge.agent.contract import DEFAULT_ALLOWED_DOMAINS
    from omnibridge.agent.provenance import check_sources

    result = check_sources(["https://some-blog.example/bridges"], DEFAULT_ALLOWED_DOMAINS)
    assert result["disallowed"] == ["some-blog.example"]
    assert result["corroborating"] == []


def test_a_citation_the_search_never_fetched_is_recorded():
    """
    check_sources inspects hostnames; it never visits a URL. Without retrieval evidence
    "two independent sources" means "two plausible hostnames the model typed".
    """
    from omnibridge.agent.contract import DEFAULT_ALLOWED_DOMAINS
    from omnibridge.agent.provenance import check_sources

    result = check_sources(
        ["https://developers.circle.com/a", "https://docs.base.org/b"],
        DEFAULT_ALLOWED_DOMAINS,
        retrieved=["https://docs.across.to/elsewhere"],
    )
    assert len(result["unretrieved_citations"]) == 2


def test_one_documentation_source_alone_is_enough():
    """
    The CCTP case. Circle is the only publisher that documents CCTP, so no honest second
    host exists; requiring one refused correct routes for a gap in the world. Four pages
    on developers.circle.com still count as the one source they are — what changed is
    that one is now sufficient.
    """
    from omnibridge.agent.contract import DEFAULT_ALLOWED_DOMAINS
    from omnibridge.agent.provenance import verify_proposal

    class _W3:
        class eth:
            @staticmethod
            def get_code(_):
                return bytes.fromhex("6fd3504e" + "00" * 64)

    report = verify_proposal(
        _W3(), ROUTER, SIGNATURE,
        [
            "https://developers.circle.com/cctp/references/contract-addresses",
            "https://developers.circle.com/cctp/references/contract-interfaces",
            "https://developers.circle.com/cctp/concepts/supported-chains-and-domains",
        ],
        allowed_domains=DEFAULT_ALLOWED_DOMAINS,
    )
    assert report.ok, report.failures
    assert report.corroborating_domains == ["developers.circle.com"]


def test_across_is_still_on_the_allowlist():
    """
    Regression. Two malformed URL entries without commas were concatenated with
    "docs.across.to" by Python, silently removing Across from the allowlist — every
    Across route would have been refused for having no documentation source at all.
    """
    from omnibridge.agent.contract import DEFAULT_ALLOWED_DOMAINS

    assert "docs.across.to" in DEFAULT_ALLOWED_DOMAINS
    # A full URL here matches nothing, since only hosts are compared.
    assert not [d for d in DEFAULT_ALLOWED_DOMAINS if "/" in d]


def test_a_route_with_no_documentation_source_is_refused_by_verify_proposal():
    """End to end through the report, not just the sorter."""
    from omnibridge.agent.contract import DEFAULT_ALLOWED_DOMAINS
    from omnibridge.agent.provenance import verify_proposal

    class _W3:
        class eth:
            @staticmethod
            def get_code(_):
                # Bytecode containing the depositForBurn selector, so only sourcing fails.
                return bytes.fromhex("6fd3504e" + "00" * 64)

    report = verify_proposal(
        _W3(), ROUTER, SIGNATURE,
        ["https://basescan.org/address/0x1682"],
        allowed_domains=DEFAULT_ALLOWED_DOMAINS,
    )
    assert not report.ok
    assert any("documentation allowlist" in f for f in report.failures)
    assert report.selector_present, "the on-chain check should have passed"


# ------------------------------------------------- prerequisite vs routing failure

def test_an_allowance_revert_is_not_retried():
    """
    Observed burning three Gemini attempts (~170s) on the same allowance revert. The
    route was correct each time; the retry edge told the model "do not repeat the same
    router and arguments", so the only way to comply was to invent a different answer.
    It went V1 -> V2 -> V1 with a freshly confabulated reason each time, because it
    cannot observe allowance state and we asked it to explain a failure it could not see.
    """
    import graph as g

    out = g.error_node({
        "simulation_result": {
            "status": "REVERTED",
            "revert_reason": (
                "Transaction reverted during eth_call: "
                "('execution reverted: ERC20: transfer amount exceeds allowance', '0x08c379a0...')"
            ),
        },
        "route": {"router_address": ROUTER},
        "retry_count": 0,
        "traces": {},
    })

    assert out["status"] == "MISSING_PREREQUISITE"
    assert out["failure_context"] is None
    assert out["retry_count"] == 0, "a missing step must not consume a retry"
    assert g.route_after_error({**out}) == "__end__"
    assert "approve(router, amount)" in out["error"]


def test_an_insufficient_balance_revert_is_not_retried():
    import graph as g

    out = g.error_node({
        "simulation_result": {
            "status": "REVERTED",
            "revert_reason": "execution reverted: ERC20: transfer amount exceeds balance",
        },
        "route": {"router_address": ROUTER},
        "retry_count": 0,
        "traces": {},
    })
    assert out["status"] == "MISSING_PREREQUISITE"
    assert "does not hold enough" in out["error"]


def test_an_ordinary_revert_is_still_retried():
    """The retry edge is valuable when the route really is what failed."""
    import graph as g

    out = g.error_node({
        "simulation_result": {"status": "REVERTED", "revert_reason": "execution reverted"},
        "route": {"router_address": ROUTER},
        "retry_count": 0,
        "traces": {},
    })
    assert out["status"] == "ERROR"
    assert out["failure_context"] and ROUTER in out["failure_context"]
    assert g.route_after_error({**out}) == "discovery_node"


def test_grounding_domains_are_matched_not_redirect_urls():
    """
    Gemini reports grounding as vertexaisearch.cloud.google.com redirect links. Comparing
    those against a cited docs URL never matches, so every citation was flagged "never
    fetched" — noise where the signal should be, which is worse than no check.
    """
    from omnibridge.agent.provenance import check_sources, registrable_domain

    assert registrable_domain("developers.circle.com") == "developers.circle.com"
    assert registrable_domain("https://developers.circle.com/x") == "developers.circle.com"

    result = check_sources(
        ["https://developers.circle.com/cctp/v1", "https://basescan.org/address/0x16"],
        ["developers.circle.com"],
        retrieved=["developers.circle.com", "basescan.org"],
    )
    assert result["unretrieved_citations"] == []


def test_the_budget_survives_a_retry():
    """
    Restoring only `limits` meant every attempt began at zero: max_attempts was never
    reached, and a 12-call ceiling became 12 per attempt. Three retries could spend 36
    calls under a budget claiming to cap them at 12.
    """
    from omnibridge.agent.budget import RequestBudget

    first = RequestBudget(max_model_calls=12)
    first.start_attempt()
    first.record_usage(type("U", (), {"input_tokens": 1000, "output_tokens": 200})())
    first.record_usage(type("U", (), {"input_tokens": 1000, "output_tokens": 200})())

    second = RequestBudget.restore(first.as_dict())

    assert second.attempts == 1
    assert second.model_calls == 2
    assert second.input_tokens == 2000
    assert second.output_tokens == 400
    assert second.max_model_calls == 12
    assert second.elapsed_seconds >= first.elapsed_seconds - 0.1

    second.start_attempt()
    assert second.attempts == 2


def test_the_attempt_ceiling_now_actually_binds():
    from omnibridge.agent.budget import BudgetExceeded, RequestBudget

    budget = RequestBudget(max_attempts=3)
    for _ in range(3):
        budget = RequestBudget.restore(budget.as_dict())
        budget.start_attempt()

    budget = RequestBudget.restore(budget.as_dict())
    with pytest.raises(BudgetExceeded):
        budget.start_attempt()


# ------------------------------------------------------- a nominated recipient


def _proposal_with_recipient(addr):
    from omnibridge.agent.discovery_agent import RouteProposal

    return RouteProposal(
        protocol="Circle CCTP",
        router_address=ROUTER,
        function_signature=SIGNATURE,
        param_types=["uint256", "uint32", "bytes32", "address"],
        param_values=[
            "5000000",
            "6",
            "0x" + addr.lower().replace("0x", "").rjust(64, "0"),
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        ],
        source_chain_id=42161,
        dest_chain_ref={"scheme": "cctp_domain", "value": "6"},
        sources=["https://developers.circle.com/cctp"],
        rationale="r",
    )


def test_a_recipient_in_a_bytes32_parameter_is_found():
    from omnibridge.agent.synthesis import _recipient_is_encoded

    addr = "0xa397ef3874c89b3d2647d51ec7e64ea1a3f36628"
    padded = "0x" + addr.replace("0x", "").rjust(64, "0")
    assert _recipient_is_encoded(addr, ["5000000", "6", padded])
    # Case must not matter; these arrive checksummed as often as not.
    assert _recipient_is_encoded(addr.upper().replace("0X", "0x"), [padded])


def test_an_absent_recipient_is_not_found():
    from omnibridge.agent.synthesis import _recipient_is_encoded

    assert not _recipient_is_encoded(
        "0xa397ef3874c89b3d2647d51ec7e64ea1a3f36628",
        ["5000000", "6", "0x" + "11" * 32],
    )


def test_a_third_party_recipient_missing_from_the_calldata_is_refused():
    """
    The check that makes the recipient field mean anything. Provenance establishes the
    router and simulation establishes the call is well-formed; a transfer to the wrong
    destination passes both and the money is gone.
    """
    from omnibridge.agent.synthesis import ProposalRejected, synthesize
    from omnibridge.core.models import UserIntent

    sender = "0xa397ef3874c89b3d2647d51ec7e64ea1a3f36628"
    asked_for = "0x1111111111111111111111111111111111111111"

    class _Chain:
        chain_id = 42161
        name = "Arbitrum One"

    intent = UserIntent(
        token="USDC", amount=5.0, source_chain="arbitrum", dest_chain="base",
        recipient=asked_for,
    )
    # The proposal encodes the sender, not the address the user named.
    with pytest.raises(ProposalRejected) as err:
        synthesize(_proposal_with_recipient(sender), intent, _Chain(), None, sender=sender)

    assert asked_for in str(err.value)
    assert "would go somewhere else" in str(err.value)


def test_a_third_party_recipient_present_in_the_calldata_is_accepted():
    from omnibridge.agent.synthesis import synthesize
    from omnibridge.core.models import UserIntent

    sender = "0xa397ef3874c89b3d2647d51ec7e64ea1a3f36628"
    asked_for = "0x1111111111111111111111111111111111111111"

    class _Chain:
        chain_id = 42161
        name = "Arbitrum One"

    intent = UserIntent(
        token="USDC", amount=5.0, source_chain="arbitrum", dest_chain="base",
        recipient=asked_for,
    )
    _calldata, _quote, evidence = synthesize(
        _proposal_with_recipient(asked_for), intent, _Chain(), None, sender=sender
    )
    assert evidence["recipient_encoded"] is True
    assert evidence["recipient_is_third_party"] is True


def test_sending_to_yourself_is_never_blocked_by_the_recipient_check():
    """
    Every route that worked before this check existed must still work. Some protocols
    carry the destination somewhere the parameter list does not show, and refusing those
    would break them — so a miss is only fatal when the user named someone else.
    """
    from omnibridge.agent.synthesis import synthesize
    from omnibridge.core.models import UserIntent

    sender = "0xa397ef3874c89b3d2647d51ec7e64ea1a3f36628"

    class _Chain:
        chain_id = 42161
        name = "Arbitrum One"

    intent = UserIntent(
        token="USDC", amount=5.0, source_chain="arbitrum", dest_chain="base",
        recipient=sender,
    )
    proposal = _proposal_with_recipient("0x2222222222222222222222222222222222222222")
    _calldata, _quote, evidence = synthesize(proposal, intent, _Chain(), None, sender=sender)

    assert evidence["recipient_is_third_party"] is False
    assert evidence["recipient_encoded"] is False, "recorded, not fatal"
