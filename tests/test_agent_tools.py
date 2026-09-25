"""
The agent toolkit, the provider loops that run it, the fork, and the graph changes.

No paid API. Providers are driven by fake clients; the fork is exercised live only in
the one test marked `network`, which also needs Anvil installed.
"""

import tempfile
import types as pytypes

import pytest

from omnibridge.agent.budget import RequestBudget
from omnibridge.agent.discovery_agent import DiscoveryAgent
from omnibridge.agent.roles import ROLE_TOOLS
from omnibridge.agent.tools import TOOL_NAMES, AgentToolkit, ToolContext, ToolLimits
from omnibridge.cards.manager import CardManager
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.fork import ForkRun, ForkStepResult, _is_revert
from omnibridge.core.models import SimulationResult, SimulationStatus, UserIntent
from tests.test_agent_discovery import (
    BASE as BASE_CHAIN,
    FakeClient,
    INTENT,
    _Block,
    _Response,
    _proposal_payload,
)
from tests.test_cards import ARB, DOCS, SIG, router_w3

SENDER = "0x" + "ab" * 20


class _FakeSimulator:
    def __init__(self, fork_run=None, why_not="anvil is not installed", call_result=None):
        self.fork_run, self.why_not = fork_run, why_not
        self.call_result = call_result or SimulationResult(status=SimulationStatus.VERIFIED, gas_used=21000)
        self.fork_calls, self.eth_calls = [], []

    def simulate_sequence_on_fork(self, sender, steps):
        self.fork_calls.append((sender, steps))
        return (self.fork_run, None) if self.fork_run else (None, self.why_not)

    def simulate_call(self, **kwargs):
        self.eth_calls.append(kwargs)
        return self.call_result


def _toolkit(simulator=None, nodes=None, allowed=None, limits=None, cards=None):
    nodes = nodes if nodes is not None else {42161: router_w3()}
    ctx = ToolContext(
        sender=SENDER, source_chain_id=42161, simulator=simulator or _FakeSimulator(),
        cards=cards or CardManager(repository_path=tempfile.mkdtemp()),
        web3_for=lambda cid: nodes.get(cid), domains=["developers.circle.com"],
    )
    return AgentToolkit(ctx, allowed=allowed, limits=limits or ToolLimits())


# ------------------------------------------------------------------ tools

def test_the_toolkit_offers_the_five_tools_from_the_plan():
    names = [s["name"] for s in _toolkit().schemas()]
    assert names == list(TOOL_NAMES)
    assert all(s["input_schema"]["additionalProperties"] is False for s in _toolkit().schemas())


def test_encode_jit_payload_matches_the_pipelines_encoder():
    params = ["5000000", "6", "0x" + "00" * 12 + "ab" * 20, ARB]
    result = _toolkit().handle("encode_jit_payload", {"abi_signature": SIG, "parameters": params})
    types = ["uint256", "uint32", "bytes32", "address"]
    expected = CalldataExecutor.encode_dynamic_calldata(
        SIG, types, CalldataExecutor.coerce_param_values(types, params))
    assert result["ok"] and result["calldata"] == expected


def test_encode_jit_payload_reports_a_bad_signature_as_data():
    result = _toolkit().handle("encode_jit_payload", {"abi_signature": "f(uint)", "parameters": ["1"]})
    assert result["ok"] is False and "not canonical" in result["error"]


def test_fetch_bytecode_and_verify_reads_the_selector():
    result = _toolkit().handle("fetch_bytecode_and_verify",
                               {"chain_id": 42161, "contract_address": ARB, "function_signature": SIG})
    assert result["ok"] and result["has_bytecode"] and result["selector_present"]
    assert result["problems"] == [], "the documentation check is not this tool's business"


def test_simulation_runs_approve_then_call_on_a_fork_when_available():
    run = ForkRun(chain_id=42161, fork_url="x", steps=[
        ForkStepResult("approve", True, 55000), ForkStepResult("call", True, 98500)])
    sim = _FakeSimulator(fork_run=run)
    result = _toolkit(simulator=sim).handle("simulate_transaction", {
        "chain_id": 42161, "to": ARB, "calldata": "0x6fd3504e",
        "approve": {"token": ARB, "spender": ARB, "amount": "5000000"}})
    assert result["mode"] == "anvil_fork" and result["success"] and result["gas_used"] == 98500
    sender, steps = sim.fork_calls[0]
    assert sender == SENDER and [s.label for s in steps] == ["approve", "call"]


def test_simulation_falls_back_to_eth_call_and_says_the_approval_was_not_applied():
    sim = _FakeSimulator()
    result = _toolkit(simulator=sim).handle("simulate_transaction", {
        "chain_id": 42161, "to": ARB, "calldata": "0x6fd3504e",
        "approve": {"token": ARB, "spender": ARB, "amount": "1"}})
    assert result["mode"] == "eth_call" and result["fork_unavailable"] == "anvil is not installed"
    assert "approval could not be applied" in result["note"]
    assert sim.eth_calls[0]["from_address"] == SENDER


def test_simulation_is_refused_off_the_source_chain():
    result = _toolkit().handle("simulate_transaction", {"chain_id": 8453, "to": ARB, "calldata": "0x"})
    assert result["ok"] is False and "source chain" in result["error"]


def test_budgets_are_enforced_and_reported_as_data():
    kit = _toolkit(limits=ToolLimits(max_calls=10, max_simulations=1))
    args = {"chain_id": 42161, "to": ARB, "calldata": "0x"}
    assert kit.handle("simulate_transaction", args)["ok"]
    second = kit.handle("simulate_transaction", args)
    assert second["ok"] is False and "budget exhausted" in second["error"]


def test_a_role_only_gets_its_tools():
    kit = _toolkit(allowed=ROLE_TOOLS["qa"])
    assert [s["name"] for s in kit.schemas()] == ["simulate_transaction"]
    refused = kit.handle("propose_card_update", {"protocol_id": "cctp", "patch_data": {}, "sources": [], "reason": ""})
    assert refused["ok"] is False and "not available" in refused["error"]


def test_propose_card_update_goes_through_the_checks():
    from tests.test_card_lifecycle import _seed

    mgr = CardManager(repository_path=tempfile.mkdtemp())
    _seed(mgr)
    kit = _toolkit(cards=mgr, nodes={130: router_w3()})
    result = kit.handle("propose_card_update", {
        "protocol_id": "cctp",
        "patch_data": {"kind": "chain_row", "network": "Unichain", "chain_id": 130, "dest_id": "10",
                       "addresses": {"Token Messenger": "0x2B4069517957735bE00ceE0fadAE88a26365528f"}},
        "sources": [DOCS], "reason": "llms.txt lists it"})
    assert result["ok"] and result["status"] == "unverified"


# ------------------------------------------------------------ provider loops

def test_the_anthropic_loop_runs_a_client_tool_then_accepts_the_proposal():
    tool_use = _Block("tool_use", "encode_jit_payload",
                      {"abi_signature": SIG, "parameters": ["1", "6", "0x" + "11" * 32, ARB]})
    tool_use.id = "tu_1"
    client = FakeClient([
        _Response([tool_use], stop_reason="tool_use"),
        _Response([_Block("tool_use", "propose_route", _proposal_payload())]),
    ])
    kit = _toolkit()
    proposal = DiscoveryAgent(client=client).discover(
        INTENT, BASE_CHAIN, "Arbitrum", RequestBudget(), cards_context="CARDS HERE", tools=kit)
    assert proposal.protocol == "CCTP"
    assert client.calls == 2
    sent = client.last_kwargs["messages"]
    assert sent[-1]["content"][0]["type"] == "tool_result"
    assert sent[-1]["content"][0]["tool_use_id"] == "tu_1"
    assert "CARDS HERE" in sent[0]["content"]
    assert {t["name"] for t in client.last_kwargs["tools"]} >= set(TOOL_NAMES)
    assert kit.summary()["calls"] == 1


def test_a_card_update_sent_with_the_proposal_is_still_recorded():
    update = _Block("tool_use", "propose_card_update",
                    {"protocol_id": "none", "patch_data": {"kind": "chain_row"}, "sources": [], "reason": "r"})
    update.id = "tu_2"
    client = FakeClient([_Response([update, _Block("tool_use", "propose_route", _proposal_payload())])])
    kit = _toolkit()
    DiscoveryAgent(client=client).discover(INTENT, BASE_CHAIN, "Arbitrum", RequestBudget(), tools=kit)
    assert kit.calls and kit.calls[0]["tool"] == "propose_card_update"


def test_without_tools_the_anthropic_loop_is_unchanged():
    client = FakeClient([_Response([_Block("tool_use", "propose_route", _proposal_payload())])])
    DiscoveryAgent(client=client).discover(INTENT, BASE_CHAIN, "Arbitrum", RequestBudget())
    assert {t["name"] for t in client.last_kwargs["tools"]}.isdisjoint(TOOL_NAMES)


def test_the_gemini_decide_phase_runs_client_tools_until_a_terminal_answer():
    from tests import test_gemini_agent as g

    decide_tool = g._response([g._part(call=g._call("encode_jit_payload", {
        "abi_signature": SIG, "parameters": ["1", "6", "0x" + "11" * 32, ARB]}))])
    decide_final = g._response([g._part(call=g._call("propose_route", g.VALID_ARGS))])
    agent = g._agent([g.RESEARCH, decide_tool, decide_final])
    kit = _toolkit()
    proposal = agent.discover(g.INTENT, g.BASE, "arbitrum", RequestBudget(), tools=kit)
    assert proposal.protocol == "CCTP"
    assert kit.summary()["by_tool"] == {"encode_jit_payload": 1}
    decide_config = agent.client.configs[-1]
    allowed = decide_config.tool_config.function_calling_config.allowed_function_names
    assert "simulate_transaction" in allowed and "propose_route" in allowed


# ------------------------------------------------------------------ the fork

def test_fork_failures_are_classified():
    assert _is_revert("('execution reverted: No TokenMessenger for domain', '0x..')")
    assert not _is_revert("HTTPConnectionPool: Max retries exceeded")
    # Found in the live run: an unfunded wallet is the wallet's failure, not the fork's.
    assert _is_revert("{'code': -32003, 'message': 'Insufficient funds for gas * price + value'}")


def test_no_anvil_means_no_fork_and_a_reason(monkeypatch):
    from omnibridge.core import fork
    from omnibridge.core.simulator import Simulator

    monkeypatch.setattr(fork, "anvil_available", lambda: False)
    run, why = Simulator(["http://127.0.0.1:1"], expected_chain_id=1).simulate_sequence_on_fork(SENDER, [])
    assert run is None and "not installed" in why


@pytest.mark.network
def test_live_fork_catches_what_eth_call_cannot():
    """
    On a fork of Arbitrum: the bridge call alone reverts on allowance, the same call
    after its approval succeeds, and a destination given by chain id reverts with
    CCTP's own message — the three facts the forked simulator exists to establish.
    """
    from omnibridge.cards.validation import default_web3_for
    from omnibridge.core import erc20
    from omnibridge.core.fork import ForkStep, anvil_available, anvil_fork, run_on_fork

    if not anvil_available():
        pytest.skip("anvil is not installed")
    w3live = default_web3_for()(42161)
    if w3live is None:
        pytest.skip("no Arbitrum node reachable")
    usdc, sender, amount = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "0x" + "00" * 17 + "0ab123", 5_000_000

    def burn(domain):
        return CalldataExecutor.encode_dynamic_calldata(
            SIG, ["uint256", "uint32", "bytes32", "address"],
            [amount, domain, CalldataExecutor.pad_bytes32_address(sender), usdc])

    with anvil_fork(w3live.provider.endpoint_uri, 42161) as w3:
        w3.provider.make_request("anvil_setBalance", [sender, hex(10**18)])
        w3.provider.make_request("anvil_dealERC20", [sender, usdc, hex(amount * 2)])
        alone = run_on_fork(w3, sender, [ForkStep(ARB, burn(6), label="bridge")])
        assert not alone[0].success and "allowance" in alone[0].revert_reason

        approve = ForkStep(usdc, erc20.build_approve(ARB, amount), label="approve")
        wrong = run_on_fork(w3, sender, [approve, ForkStep(ARB, burn(8453), label="bridge")])
        assert "No TokenMessenger for domain" in wrong[-1].revert_reason

        right = run_on_fork(w3, sender, [ForkStep(ARB, burn(6), label="bridge")])
        assert right[0].success and right[0].gas_used


# ------------------------------------------------------------------ the graph

def test_a_refused_proposal_goes_back_to_discovery_within_the_retry_bound():
    import graph as g

    assert g.route_after_discovery({"status": "PROPOSAL_REJECTED", "retry_count": 1}) == "discovery_node"
    assert g.route_after_discovery(
        {"status": "PROPOSAL_REJECTED", "retry_count": g.MAX_DISCOVERY_RETRIES}) == "error_node"


def test_after_a_fork_applied_the_approval_an_allowance_revert_is_a_routing_error():
    """
    Normally an allowance revert means the approval is missing, and it is not retried.
    On a fork the approval was applied first, so the same text means the route approved
    the wrong spender — and another attempt can fix that.
    """
    import graph as g

    state = {
        "status": "SIMULATION_FAILED", "retry_count": 0, "traces": {},
        "error": "On a fork ... the bridge step reverted: ERC20: transfer amount exceeds allowance",
        "simulation_result": {"status": "REVERTED",
                              "revert_reason": "ERC20: transfer amount exceeds allowance"},
        "route": {"router_address": ARB},
    }
    assert g.error_node({**state, "fork_failure": False})["status"] == "MISSING_PREREQUISITE"
    retried = g.error_node({**state, "fork_failure": True})
    assert retried["status"] == "ERROR" and retried["failure_context"]


def test_a_revert_on_a_card_route_is_written_to_the_card_and_fed_back(monkeypatch):
    import graph as g
    from omnibridge.cards.writeback import record_failure
    from tests.test_card_lifecycle import _seed

    mgr = CardManager(repository_path=tempfile.mkdtemp())
    _seed(mgr)
    record_failure(mgr, None, "execution reverted: No TokenMessenger for domain", protocol_id="cctp")
    mgr.update("cctp", lambda c: (setattr(c.failure_modes[0], "fix", "Use domain `6`."), c)[1])
    monkeypatch.setattr(g, "card_mgr", mgr)

    out = g.error_node({
        "status": "SIMULATION_FAILED", "retry_count": 0, "traces": {},
        "error": "execution reverted: No TokenMessenger for domain",
        "simulation_result": {"status": "REVERTED", "revert_reason": "execution reverted: No TokenMessenger for domain"},
        "route": {"router_address": ARB},
        "card_match": {"protocol_id": "cctp", "source_chain_id": 42161, "source_status": "verified"},
    })
    assert "Use domain `6`." in out["failure_context"]
    assert mgr.load("cctp").card.failure_mode("No TokenMessenger for domain").seen == 2


# ---------------------------------------------- found in the first live testnet run

def test_every_card_update_field_is_declared_so_gemini_can_fill_it():
    """
    `patch_data` was a bare object. Gemini fills only declared fields, so it sent `{}`
    seven times in a row and never concluded.
    """
    from omnibridge.agent.gemini_agent import _strip_unsupported
    from omnibridge.agent.tools import SCHEMAS

    patch = _strip_unsupported(SCHEMAS["propose_card_update"]["input_schema"])["properties"]["patch_data"]
    assert patch["properties"]["kind"]["enum"] == ["chain_row", "interface", "failure_fix", "new_card"]
    assert patch["required"] == ["kind"]
    item = patch["properties"]["addresses"]["items"]
    assert set(item["properties"]) == {"column", "address"}


def test_the_list_form_of_a_chain_row_is_accepted():
    from tests.test_card_lifecycle import _seed

    mgr = CardManager(repository_path=tempfile.mkdtemp())
    _seed(mgr)
    kit = _toolkit(cards=mgr, nodes={130: router_w3()})
    result = kit.handle("propose_card_update", {
        "protocol_id": "cctp", "sources": [DOCS], "reason": "r",
        "patch_data": {"kind": "chain_row", "chain_name": "Unichain", "chain_id": 130, "dest_id": "10",
                       "addresses": [{"column": "Token Messenger",
                                      "address": "0x2B4069517957735bE00ceE0fadAE88a26365528f"}]}})
    assert result["ok"], result
    assert mgr.load("cctp").card.row(130).network == "Unichain"


def test_a_call_that_already_failed_is_not_run_again():
    kit = _toolkit()
    bad = {"protocol_id": "cctp", "patch_data": {}, "sources": [], "reason": "r"}
    first = kit.handle("propose_card_update", bad)
    second = kit.handle("propose_card_update", bad)
    assert not first["ok"] and "Unknown patch kind" in first["reasons"][0]
    assert not second["ok"] and "already made exactly this call" in second["error"]
    assert kit.summary()["by_tool"] == {"propose_card_update": 1}, "the repeat was not executed"


def test_gemini_is_made_to_conclude_on_its_last_turn():
    from omnibridge.agent.gemini_agent import GeminiDiscoveryAgent
    from tests import test_gemini_agent as g

    stuck = g._response([g._part(call=g._call("encode_jit_payload", {"abi_signature": SIG, "parameters": []}))])
    final = g._response([g._part(call=g._call("propose_route", g.VALID_ARGS))])
    turns = GeminiDiscoveryAgent.MAX_TOOL_TURNS
    agent = g._agent([g.RESEARCH] + [stuck] * turns + [final])
    proposal = agent.discover(g.INTENT, g.BASE, "arbitrum", RequestBudget(max_model_calls=20), tools=_toolkit())
    assert proposal.protocol == "CCTP"
    last = agent.client.configs[-1].tool_config.function_calling_config.allowed_function_names
    assert last == ["propose_route", "no_route_found"]


def test_gemini_sends_back_a_no_route_that_cites_nothing():
    """Observed live: no_route_found("testing tool availability") with no sources ended a run."""
    from tests import test_gemini_agent as g

    probe = g._response([g._part(call=g._call("no_route_found", {"reason": "testing tool availability", "sources": []}))])
    final = g._response([g._part(call=g._call("propose_route", g.VALID_ARGS))])
    agent = g._agent([g.RESEARCH, probe, final])
    proposal = agent.discover(g.INTENT, g.BASE, "arbitrum", RequestBudget(), tools=_toolkit())
    assert proposal.protocol == "CCTP"


def test_gemini_accepts_a_bare_no_route_on_its_last_turn():
    from omnibridge.agent.contract import NoRouteFound
    from omnibridge.agent.gemini_agent import GeminiDiscoveryAgent
    from tests import test_gemini_agent as g

    bare = g._response([g._part(call=g._call("no_route_found", {"reason": "none", "sources": []}))])
    agent = g._agent([g.RESEARCH] + [bare] * (GeminiDiscoveryAgent.MAX_TOOL_TURNS + 1))
    with pytest.raises(NoRouteFound):
        agent.discover(g.INTENT, g.BASE, "arbitrum", RequestBudget(max_model_calls=20), tools=_toolkit())


def test_a_sourced_no_route_is_an_answer_immediately():
    from omnibridge.agent.contract import NoRouteFound
    from tests import test_gemini_agent as g

    sourced = g._response([g._part(call=g._call("no_route_found", {
        "reason": "CCTP does not support this chain", "sources": ["https://developers.circle.com/cctp"]}))])
    with pytest.raises(NoRouteFound):
        g._agent([g.RESEARCH, sourced]).discover(g.INTENT, g.BASE, "arbitrum", RequestBudget(), tools=_toolkit())


def test_the_anthropic_loop_sends_back_a_no_route_that_cites_nothing():
    probe = _Block("tool_use", "no_route_found", {"reason": "testing", "sources": []})
    probe.id = "tu_9"
    client = FakeClient([
        _Response([probe]),
        _Response([_Block("tool_use", "propose_route", _proposal_payload())]),
    ])
    proposal = DiscoveryAgent(client=client).discover(INTENT, BASE_CHAIN, "Arbitrum", RequestBudget(), tools=_toolkit())
    assert proposal.protocol == "CCTP"
    sent_back = client.last_kwargs["messages"][-1]["content"][0]
    assert sent_back["tool_use_id"] == "tu_9" and sent_back["is_error"]
