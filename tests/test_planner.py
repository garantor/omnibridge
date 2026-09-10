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


def test_the_models_spend_declaration_reaches_the_planner():
    """
    The wiring, asserted. `spend` existed on the proposal and the planner read it from
    the quote, and nothing carried it between the two — so no approve step was ever
    built and every ERC-20 route reverted on allowance exactly as before.
    """
    from omnibridge.agent.contract import RouteProposal, TokenSpend
    from omnibridge.agent.synthesis import synthesize
    from omnibridge.core.models import ChainInfo

    proposal = RouteProposal(
        protocol="CCTP", router_address=ROUTER,
        function_signature="depositForBurn(uint256,uint32,bytes32,address)",
        param_types=["uint256", "uint32", "bytes32", "address"],
        param_values=[str(AMOUNT), "3", "0x" + "11" * 32, USDC],
        source_chain_id=8453, dest_chain_ref={"scheme": "cctp_domain", "value": "3"},
        sources=["https://docs.circle.com/a", "https://developers.circle.com/b"],
        rationale="because",
        spend=TokenSpend(token=USDC, spender=ROUTER, amount=str(AMOUNT)),
    )
    chain = ChainInfo(chain_id=8453, name="Base", block_explorer="", rpc_urls=[])

    _calldata, quote, _evidence = synthesize(proposal, INTENT, chain, None)

    assert quote.extra_params["spend"] == {
        "token": USDC, "spender": ROUTER, "amount": str(AMOUNT),
    }


def test_a_replayed_skill_keeps_its_approval_step(tmp_path):
    """A cache hit that forgets the approval reverts exactly as a cold route would."""
    from omnibridge.discovery.route_finder import RouteFinder
    from omnibridge.skills.manager import SkillManager, SkillMetadata

    repo = SkillManager(repository_path=str(tmp_path))
    repo.save_skill(SkillMetadata(
        name="base_to_arbitrum_usdc", description="d",
        source_chain="Base", source_chain_id=8453, dest_chain="arbitrum",
        token="USDC", bridge_protocol="CCTP", router_address=ROUTER,
        function_signature="depositForBurn(uint256,uint32,bytes32,address)",
        param_types=["uint256"], param_values=["{amount_raw}"],
        spend={"token": USDC, "spender": ROUTER, "amount": "{amount_raw}"},
        aliases=["base", "arbitrum"],
    ), "# body")

    skill = repo.find_skill("base", "arbitrum", "USDC")
    assert skill.metadata.spend["token"] == USDC


# ---------------------------------------------------------- the transfer amount
#
# `deposit_amount_raw` is what the balance check compares against and what the
# approval is sized to, so reading it out of the wrong word is not cosmetic.

def test_the_transfer_amount_comes_from_the_declared_spend_not_argument_word_zero():
    """
    Across's depositV3 puts `address depositor` first.

    Read as a uint256 that is ~1.3e48, which the planner then demanded as a balance
    and built as an allowance — so every Across route failed for every wallet, and
    with no RPC to check the balance it silently escalated to a near-unlimited
    approval instead. The model already declares the base units in `spend.amount`.
    """
    from omnibridge.agent.contract import RouteProposal, TokenSpend
    from omnibridge.agent.synthesis import _transfer_amount

    depositor = "0xec64285c59a4509d6f8f7e1f871a77ca8c6f4f1a"
    # depositV3 calldata: selector, then the depositor address as word 0.
    calldata = "0x7b939232" + depositor[2:].rjust(64, "0")

    proposal = RouteProposal(
        protocol="Across V3", router_address=ROUTER,
        function_signature="depositV3(address,address)", param_types=[], param_values=[],
        source_chain_id=8453, dest_chain_ref={"scheme": "eip155", "value": "1"},
        sources=[], rationale="", spend=TokenSpend(token=USDC, spender=ROUTER, amount="25000000"),
    )
    assert _transfer_amount(proposal, calldata) == 25_000_000

    # No spend means a native transfer, which declares none — word 0 is all there is.
    proposal.spend = None
    assert _transfer_amount(proposal, calldata) == int(depositor, 16)
