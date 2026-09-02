"""
Phase 1 guardrail regressions.

Every test here asserts a *refusal*. The system's habit was to manufacture something
answer-shaped whenever it could not answer — the intent parser substituted
"Unknown / 1.0 NATIVE", web search fabricated a hit, the simulator returned
success=True with invented gas. These tests exist so that habit cannot come back.
"""

import pytest

from omnibridge.core.models import SimulationStatus, UserIntent
from omnibridge.core.simulator import Simulator, is_evm_address
from omnibridge.discovery.rpc_manager import (
    RPCManager,
    UnresolvedChainError,
    AmbiguousChainError,
    name_similarity,
)

VALID_SENDER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
SOME_ROUTER = "0x" + "11" * 20
SOME_CALLDATA = "0x" + "de" * 40


# ---------------------------------------------------------------- 1.1 intent

def test_unparseable_intent_is_rejected_not_defaulted():
    """'1.0 NATIVE from Unknown to Unknown' is not a reading of an unparseable string."""
    from graph import intent_parser_node

    with pytest.raises(ValueError) as excinfo:
        intent_parser_node(
            {"raw_input": "do something clever with my money", "user_address": VALID_SENDER}
        )
    assert "could not determine" in str(excinfo.value)


def test_verbs_other_than_bridge_are_understood():
    """'swap' used to miss the regex entirely and fall into the defaults block."""
    from graph import intent_parser_node

    out = intent_parser_node(
        {"raw_input": "swap 100 usdc from base to arbitrum", "user_address": VALID_SENDER}
    )
    assert out["source_chain"].lower() == "base"
    assert out["target_chain"].lower() == "arbitrum"
    assert out["amount"] == 100.0
    assert out["asset"] == "USDC"


def test_missing_wallet_is_rejected_not_zero_addressed():
    """0x0 reads as a valid address downstream, so a burn would look like a transfer."""
    from graph import intent_parser_node

    with pytest.raises(ValueError) as excinfo:
        intent_parser_node({"raw_input": "bridge 5 usdc from base to arbitrum", "user_address": ""})
    assert "sender address" in str(excinfo.value)


# ---------------------------------------------------------------- 1.2 search

def test_search_does_not_fabricate_a_result():
    """
    An empty search returns nothing. It used to append a hit citing docs.circle.com
    and claiming router contracts had been "resolved" for the query.
    """
    from tools import web_search_tool

    text = web_search_tool.invoke("zzzq nonexistent chain xyzzy no such protocol 12345")
    assert "Official router contracts and public RPC endpoints resolved" not in text


# ------------------------------------------------------- 1.3/1.4/1.5 simulator

def test_unreachable_rpc_does_not_report_success():
    res = Simulator([], expected_chain_id=8453).simulate_call(
        VALID_SENDER, SOME_ROUTER, SOME_CALLDATA
    )
    assert res.status is SimulationStatus.UNAVAILABLE
    assert res.success is False
    # Never a plausible-looking default.
    assert res.gas_used is None
    assert res.estimated_gas_cost_usd is None


def test_endpoint_claiming_the_wrong_chain_is_rejected():
    """A node pointing at a fork or a testnet must not be allowed to answer."""
    sim = Simulator(["https://mainnet.base.org"], expected_chain_id=999_999)
    res = sim.simulate_call(VALID_SENDER, SOME_ROUTER, SOME_CALLDATA)
    assert res.status is SimulationStatus.UNAVAILABLE
    assert res.success is False


def test_no_eth_call_path_does_not_report_success():
    """UNSUPPORTED keys on the chain being submitted to, and is never a pass."""
    res = Simulator(["https://mainnet.base.org"], 8453).simulate_call(
        VALID_SENDER, "GABC123NOTANEVMADDRESS", SOME_CALLDATA
    )
    assert res.status is SimulationStatus.UNSUPPORTED
    assert res.success is False


def test_invalid_sender_is_not_substituted():
    """Simulating from someone else's account describes their balances, not yours."""
    res = Simulator(["https://mainnet.base.org"], 8453).simulate_call(
        "not-an-address", SOME_ROUTER, SOME_CALLDATA
    )
    assert res.status is SimulationStatus.UNSUPPORTED
    assert res.success is False
    assert "not a valid EVM address" in (res.revert_reason or "")


def test_only_verified_is_success():
    from omnibridge.core.models import SimulationResult

    for status in SimulationStatus:
        result = SimulationResult(status=status)
        assert result.success is (status is SimulationStatus.VERIFIED)


# ------------------------------------------------------- 1.6 chain resolution

def test_solana_does_not_resolve_to_a_neon_chain():
    """
    The original defect: four Neon EVM networks declare chain == "Solana", and the
    resolver took the first, landing on a devnet.
    """
    with pytest.raises(UnresolvedChainError) as excinfo:
        RPCManager().discover_chain_metadata("solana")
    # It must refuse, and say why. Naming the chain it declined to return is fine —
    # what matters is that it is not handed back as the answer.
    assert "Refusing to resolve" in str(excinfo.value)


def test_known_chains_still_resolve_to_the_right_id():
    rm = RPCManager()
    assert rm.discover_chain_metadata("base").chain_id == 8453
    assert rm.discover_chain_metadata("arbitrum").chain_id == 42161
    assert rm.discover_chain_metadata("ethereum").chain_id == 1


def test_a_numeric_chain_id_beats_a_name():
    info = RPCManager().discover_chain_metadata("8453")
    assert info.chain_id == 8453


def test_mainnet_intent_never_resolves_onto_a_testnet():
    rm = RPCManager()
    assert rm.discover_chain_metadata("base", network="mainnet").chain_id == 8453
    with pytest.raises((AmbiguousChainError, UnresolvedChainError)):
        # Several Base test networks exist; picking one silently is the failure mode.
        rm.discover_chain_metadata("base", network="testnet")


def test_similarity_guard_separates_real_matches_from_coincidences():
    assert name_similarity("arbitrum", "Arbitrum One") == 1.0
    assert name_similarity("ethereum", "Ethereum Mainnet") == 1.0
    assert name_similarity("solana", "Neon EVM Mainnet") < 0.6


# ---------------------------------------------------------------- 1.8 sign-off

@pytest.mark.parametrize(
    "payload",
    [None, "0xsigned", 42, {}, {"signed_transaction_hex": ""}, {"other": "field"}],
)
def test_malformed_resume_is_not_approval(payload):
    from graph import validate_resume_payload

    with pytest.raises(ValueError):
        validate_resume_payload(payload, "0xunsigned")


def test_resuming_with_the_unsigned_calldata_is_not_approval():
    from graph import validate_resume_payload

    with pytest.raises(ValueError) as excinfo:
        validate_resume_payload({"signed_transaction_hex": "0xunsigned"}, "0xunsigned")
    assert "unsigned" in str(excinfo.value)


def test_a_real_signature_is_accepted():
    from graph import validate_resume_payload

    assert validate_resume_payload({"signed_transaction_hex": " 0xabc123 "}, "0xunsigned") == "0xabc123"


# ---------------------------------------------------------------- 1.9 broadcast

def test_broadcast_refuses_without_key_or_signature():
    """It used to fall back to raw_tx_payload['data'] — the unsigned calldata."""
    from omnibridge.core.signer import Signer

    with pytest.raises(ValueError) as excinfo:
        Signer("https://mainnet.base.org").broadcast_transaction(
            {"chainId": 8453, "to": SOME_ROUTER, "data": SOME_CALLDATA, "value": 0},
            private_key=None,
            dry_run=False,
        )
    assert "Refusing to broadcast" in str(excinfo.value)


def test_is_evm_address_rejects_near_misses():
    assert is_evm_address(VALID_SENDER)
    assert not is_evm_address("")
    assert not is_evm_address(None)
    assert not is_evm_address("0x123")
    assert not is_evm_address("0x" + "zz" * 20)
    assert not is_evm_address(VALID_SENDER[:-1])


# ------------------------------------------------ encoder: no silent substitution

def test_encoder_refuses_to_substitute_the_zero_address():
    """
    A recipient that cannot be encoded used to become 0x0 — a burn that simulates
    perfectly. A Stellar G-address reaching an EVM encoder is exactly this case.
    """
    from omnibridge.core.executor import CalldataExecutor

    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.encode_across_deposit_v3(
            depositor=VALID_SENDER,
            recipient="GABCDEF1234567890NOTANEVMADDRESS",
            input_token=SOME_ROUTER,
            output_token=SOME_ROUTER,
            input_amount=1_000_000,
            output_amount=999_000,
            destination_chain_id=42161,
        )
    assert "Refusing to substitute the zero address" in str(excinfo.value)


def test_cctp_requires_an_explicit_destination_domain():
    """Domain 0 is Ethereum, not 'unset'. Defaulting to it picks a wrong chain."""
    from omnibridge.core.executor import CalldataExecutor

    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.encode_cctp_deposit_for_burn(
            amount_raw=1_000_000,
            destination_domain=None,
            recipient_address=VALID_SENDER,
            burn_token_address=SOME_ROUTER,
        )
    assert "destinationDomain is required" in str(excinfo.value)
