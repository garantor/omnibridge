"""
Multi-step execution plans.

An ERC-20 bridge is two transactions. The pipeline built one, so a correct route died
at the simulation gate with `ERC20: transfer amount exceeds allowance` and the retry
edge blamed the route.

The property that matters most here is a negative one: the bridge call is *not*
reported as verified before its approval lands. It genuinely cannot be checked then,
and saying otherwise would be the same failure this project spent five phases removing.
"""

import pytest

from omnibridge.core import erc20
from omnibridge.core.models import RouteQuote, SimulationResult, SimulationStatus, StepStatus, UserIntent
from omnibridge.core.planner import PrerequisiteUnsatisfiable, build_plan, simulate_next

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ROUTER = "0x1682Ae6375C4E4A97e4B583BC394c861A46D8962"
WALLET = "0x060477185Be90701b825b80f827070ef0DA69c5E"
AMOUNT = 25_000_000

QUOTE = RouteQuote(
    bridge_name="CCTP", source_chain_id=8453, dest_chain_id=42161,
    source_token_address=USDC, dest_token_address="", router_address=ROUTER,
    deposit_amount_raw=AMOUNT, estimated_output_raw=AMOUNT, decimals=6, fee_amount_usd=0.0,
    calldata_template="agent_proposed",
    extra_params={"function_signature": "depositForBurn(uint256,uint32,bytes32,address)"},
)
INTENT = UserIntent(token="USDC", amount=25.0, source_chain="base",
                    dest_chain="arbitrum", recipient=WALLET)
SPEND = {"token": USDC, "spender": ROUTER, "amount": str(AMOUNT)}


class FakeChain:
    """Answers allowance and balanceOf; everything else is not called."""

    def __init__(self, allowance=0, balance=10**12):
        self.allowance, self.balance = allowance, balance

    def patch(self, monkeypatch):
        monkeypatch.setattr(erc20, "read_allowance", lambda *a, **k: self.allowance)
        monkeypatch.setattr(erc20, "read_balance", lambda *a, **k: self.balance)
        return object()  # stands in for a Web3


class FakeSimulator:
    def __init__(self, status=SimulationStatus.VERIFIED, gas=56252):
        self.status, self.gas, self.calls = status, gas, []

    def simulate_call(self, from_address, to_address, calldata, value_wei=0, **kw):
        self.calls.append(to_address)
        return SimulationResult(
            status=self.status,
            gas_used=self.gas if self.status is SimulationStatus.VERIFIED else None,
            simulated_on_chain_id=8453,
            revert_reason=None if self.status is SimulationStatus.VERIFIED else "reverted",
        )


# ------------------------------------------------------------------ planning

def test_an_erc20_route_becomes_approve_then_bridge(monkeypatch):
    w3 = FakeChain(allowance=0).patch(monkeypatch)
    plan = build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=w3, spend=SPEND)

    assert [s.kind for s in plan.steps] == ["approve", "bridge"]
    approve, bridge = plan.steps
    assert approve.to == USDC, "the approval goes to the token, not the router"
    assert approve.spender_address == ROUTER
    assert approve.data.startswith("0x095ea7b3")
    assert bridge.to == ROUTER
    assert bridge.index == 1


def test_an_existing_allowance_means_no_approval_is_proposed(monkeypatch):
    """Whether an approval is needed is read off chain, never taken from the model."""
    w3 = FakeChain(allowance=AMOUNT).patch(monkeypatch)
    notes = []
    plan = build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=w3, spend=SPEND, notes=notes)

    assert [s.kind for s in plan.steps] == ["bridge"]
    assert any("already covers" in n for n in notes)


def test_a_native_transfer_has_no_approval_step(monkeypatch):
    plan = build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=None, spend=None)
    assert [s.kind for s in plan.steps] == ["bridge"]


def test_an_empty_balance_stops_before_any_signature(monkeypatch):
    """
    No approval fixes an empty wallet. Catching it here avoids charging the user gas
    for an approval that cannot unblock anything.
    """
    w3 = FakeChain(allowance=0, balance=0).patch(monkeypatch)
    with pytest.raises(PrerequisiteUnsatisfiable) as excinfo:
        build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=w3, spend=SPEND)
    assert "No approval fixes this" in str(excinfo.value)


def test_an_approval_below_the_transfer_is_raised_not_accepted(monkeypatch):
    """Approving less than the transfer guarantees a revert after the user has paid."""
    w3 = FakeChain(allowance=0).patch(monkeypatch)
    notes = []
    plan = build_plan(
        QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=w3,
        spend={**SPEND, "amount": "1"}, notes=notes,
    )
    assert plan.steps[0].amount_raw == AMOUNT
    assert any("below the" in n for n in notes)


def test_an_unreadable_token_is_flagged_rather_than_read_as_zero(monkeypatch):
    monkeypatch.setattr(erc20, "read_allowance", lambda *a, **k: None)
    monkeypatch.setattr(erc20, "read_balance", lambda *a, **k: None)
    notes = []
    build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=object(), spend=SPEND, notes=notes)
    assert any("did not answer a standard ERC-20" in n for n in notes)


# ------------------------------------------------------------- simulation

def test_the_bridge_step_is_not_claimed_verified_before_the_approval(monkeypatch):
    """
    The load-bearing assertion. Its allowance genuinely is not there yet, so any
    "verified" on the bridge call before the approval lands would be a false pass.
    """
    w3 = FakeChain(allowance=0).patch(monkeypatch)
    plan = build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=w3, spend=SPEND)
    sim = FakeSimulator()

    plan, result, step = simulate_next(plan, sim, WALLET)

    assert step.kind == "approve"
    assert plan.steps[0].status is StepStatus.VERIFIED
    assert plan.steps[1].status is StepStatus.PENDING_PREREQUISITE
    assert plan.steps[1].signable is False
    # Only the approval was simulated; the bridge call was not touched.
    assert sim.calls == [USDC]


def test_only_a_verified_step_is_signable(monkeypatch):
    w3 = FakeChain(allowance=0).patch(monkeypatch)
    plan = build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=w3, spend=SPEND)
    plan, _, _ = simulate_next(plan, FakeSimulator(SimulationStatus.REVERTED), WALLET)

    assert plan.steps[0].status is StepStatus.REVERTED
    assert plan.steps[0].signable is False
    assert plan.ready is False


def test_the_next_step_is_simulated_once_the_previous_is_signed(monkeypatch):
    w3 = FakeChain(allowance=0).patch(monkeypatch)
    plan = build_plan(QUOTE, INTENT, WALLET, "0x6fd3504e", 8453, w3=w3, spend=SPEND)
    sim = FakeSimulator()

    plan, _, _ = simulate_next(plan, sim, WALLET)
    plan.steps[0].tx_hash = "0x" + "ab" * 32          # approval landed

    plan, _, step = simulate_next(plan, sim, WALLET)

    assert step.kind == "bridge"
    assert plan.steps[1].status is StepStatus.VERIFIED
    assert plan.ready is True
    assert sim.calls == [USDC, ROUTER]


# --------------------------------------------------------------- the approval

def test_an_unlimited_approval_is_refused():
    """
    An infinite allowance outlives the transfer and lets the router move the whole
    balance forever. Convenient, common, and not something to grant on the user's
    behalf for a single transfer.
    """
    with pytest.raises(ValueError) as excinfo:
        erc20.build_approve(ROUTER, erc20.UINT256_MAX)
    assert "unlimited approval" in str(excinfo.value)


def test_the_approval_encodes_the_standard_selector():
    from web3 import Web3

    data = erc20.build_approve(ROUTER, AMOUNT)
    assert data.startswith("0x" + Web3.keccak(text="approve(address,uint256)")[:4].hex())
    assert data.startswith("0x095ea7b3")
    assert ROUTER[2:].lower() in data.lower()
