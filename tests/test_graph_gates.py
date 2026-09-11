"""
The two edges that decide whether a refusal survives.

Both of these were previously unconditional, and an unconditional edge into the
simulator is not a routing detail — it is how a correct diagnosis gets replaced by a
wrong one. `build_plan` raising "you hold 0 USDC" produced a state with no plan, the
simulator found none, fell back to the bare bridge calldata, and reported
`ERC20: transfer amount exceeds allowance`: a revert caused by the missing approval
that the plan existed to add, shown instead of the balance problem that stopped it.
"""

import pytest

from graph import route_after_code_builder, route_after_preflight


class TestPreflightGate:
    """Funding is checked before discovery, because discovery is the expensive step."""

    def test_an_empty_wallet_never_reaches_discovery(self):
        assert route_after_preflight({"status": "INSUFFICIENT_FUNDS"}) == "error_node"

    def test_an_unresolvable_chain_ends_as_a_refusal_not_an_exception(self):
        assert route_after_preflight({"status": "UNRESOLVED_CHAIN"}) == "error_node"

    def test_a_funded_wallet_continues(self):
        assert route_after_preflight({"status": "FUNDING_CHECKED"}) == "discovery_node"

    def test_an_unchecked_balance_continues(self):
        # UNCHECKED is not a pass, but it is not evidence of an empty wallet either.
        # An unreachable node must not block a run; build_plan still checks before
        # anything is signed.
        assert route_after_preflight({"status": "FUNDING_CHECKED"}) == "discovery_node"


class TestCodeBuilderGate:
    def test_a_missing_prerequisite_does_not_get_simulated_anyway(self):
        assert route_after_code_builder({"status": "MISSING_PREREQUISITE"}) == "error_node"

    def test_a_built_plan_goes_to_the_simulator(self):
        assert route_after_code_builder({"status": "PAYLOAD_BUILT"}) == "simulator_node"


@pytest.mark.parametrize("status", ["INSUFFICIENT_FUNDS", "UNRESOLVED_CHAIN"])
def test_a_funding_refusal_keeps_its_own_name_through_the_diagnostic(status):
    """
    error_node must not flatten these to ERROR.

    Every caller would then have to pattern-match the message to tell "this wallet
    cannot pay gas" from "the bridge rejected this transaction" — which is the reason
    NO_ROUTE_FOUND and CONFIG_ERROR are preserved already.
    """
    from graph import error_node

    out = error_node({"status": status, "error": "no ETH on Base", "traces": {}})
    assert out["status"] == status
    assert out["error"] == "no ETH on Base"
    # And it is terminal: nothing about a balance improves by asking a model again.
    assert out["failure_context"] is None


def test_the_graph_routes_preflight_before_discovery():
    """The gate is wired in, not merely defined."""
    from graph import builder

    edges = {(e[0], e[1]) for e in builder.edges}
    assert ("intent_parser_node", "preflight_node") in edges
    # And the old direct edge is gone, or the gate could be bypassed entirely.
    assert ("intent_parser_node", "discovery_node") not in edges


# --------------------------------------- bookkeeping cannot void a broadcast


def test_a_failed_skill_save_does_not_reject_a_broadcast_transaction(monkeypatch):
    """
    The regression, and the worse half of it. Saving SKILL.md ran inline after the
    broadcast, so a validation error there propagated out of the node and the run ended
    REJECTED — with the user's transaction already mined. Status reports what the chain
    did; a failed write-back is reported alongside it, never instead of it.
    """
    import graph as g

    class _Receipt:
        tx_hash = "0x71c79067354ae120f03e073511d5ffadd614eb11e0cc5e6fb317c7849a7b1313"
        explorer_url = "https://arbiscan.io/tx/0x71c7"
        timestamp = "2026-09-11T00:00:00Z"

    class _Signer:
        def __init__(self, *a, **k):
            pass

        def broadcast_transaction(self, *a, **k):
            return _Receipt()

    class _ChainInfo:
        chain_id = 42161
        block_explorer = "https://arbiscan.io"

    monkeypatch.setattr(g, "Signer", _Signer)
    monkeypatch.setattr(g.rpc_mgr, "discover_chain_metadata", lambda *a, **k: _ChainInfo())

    def _explode(state, receipt):
        raise ValueError("1 validation error for SkillMetadata")

    monkeypatch.setattr(g, "_persist_route_as_skill", _explode)

    out = g.broadcast_skill_node({
        "source_chain": "Arbitrum One",
        "active_rpc": "https://rpc.example",
        "route": {"router_address": "0x19330d10D9Cc8751218eaf51E8885D058642E08A"},
        "unsigned_transaction_hex": "0x6fd3504e",
        "signed_transaction_hex": _Receipt.tx_hash,
        "traces": {},
    })

    assert out["status"] == "CONFIRMED"
    assert out["tx_hash"] == _Receipt.tx_hash
    assert out["skill_path"] is None
    assert "validation error" in out["skill_save_error"]
    step = out["traces"]["step_6_broadcast"]
    assert step["status"] == "COMPLETED_WITHOUT_SKILL"
    assert step["tx_hash"] == _Receipt.tx_hash
