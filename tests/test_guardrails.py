"""
Phase 1 guardrail regressions.

Every test here asserts a *refusal*. The system's habit was to manufacture something
answer-shaped whenever it could not answer — the intent parser substituted
"Unknown / 1.0 NATIVE", web search fabricated a hit, the simulator returned
success=True with invented gas. These tests exist so that habit cannot come back.
"""

import pytest

from omnibridge.core.models import SimulationStatus
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

def test_the_fabricating_search_tools_are_gone():
    """
    Phase 1 stopped `web_search_tool` appending a fabricated hit citing docs.circle.com.
    Phase 4 deleted it along with `fetch_doc_content_tool`, which invented an ABI
    signature, a selector and a Strkey contract address whenever a page fetch failed —
    the normal case against JS-rendered docs.

    The module that held them is gone entirely: with those two removed, nothing in the
    running system called anything else in it. Search is now the model's own
    server-side tool, constrained to an allowlist and cited in the proposal.
    """
    with pytest.raises(ImportError):
        import tools  # noqa: F401


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

    Asserted through the generic path now that the per-protocol encoders are gone; the
    check itself is the same `require_evm_address` those encoders called.
    """
    from omnibridge.core.executor import CalldataExecutor

    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.coerce_param_values(
            ["uint256", "address"], ["1000000", "GABCDEF1234567890NOTANEVMADDRESS"]
        )
    assert "Refusing to substitute the zero address" in str(excinfo.value)


def test_a_missing_parameter_cannot_be_defaulted_away():
    """
    CCTP domain 0 is Ethereum, not "unset", and the named encoder guarded that. With
    one generic encoder the guarantee is structural instead: a value cannot go missing,
    because the arity of the signature and the values must agree before anything is
    packed. A dropped domain is a refusal, not a silent 0.
    """
    from omnibridge.core.executor import CalldataExecutor

    types = ["uint256", "uint32", "bytes32", "address"]
    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.coerce_param_values(types, ["1000000", "0x" + "11" * 32, SOME_ROUTER])
    assert "arity mismatch" in str(excinfo.value)


def test_non_evm_recipient_is_never_hashed_into_a_new_address():
    """
    pad_bytes32_address used to `return Web3.keccak(text=address)` for anything longer
    than 20 bytes — every non-EVM recipient, which is the case it exists to serve. The
    result was a well-formed bytes32 that the contract accepts, that simulates with no
    revert, and that nobody holds the key for: funds burned on the source chain and
    minted to an unrecoverable address.
    """
    from omnibridge.core.executor import CalldataExecutor
    from web3 import Web3

    solana_key = "0x" + "ab" * 32
    out = CalldataExecutor.pad_bytes32_address(solana_key)

    assert out == bytes.fromhex("ab" * 32), "32-byte destination key must pass through intact"
    assert out != Web3.keccak(text=solana_key), "recipient must never be hashed"


def test_evm_recipient_is_left_padded():
    from omnibridge.core.executor import CalldataExecutor

    out = CalldataExecutor.pad_bytes32_address(VALID_SENDER)
    assert out[:12] == b"\x00" * 12
    assert "0x" + out[12:].hex() == VALID_SENDER.lower()


def test_recipient_of_an_impossible_width_is_refused():
    from omnibridge.core.executor import CalldataExecutor

    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.pad_bytes32_address("0x" + "ab" * 27)
    assert "Refusing to hash or truncate" in str(excinfo.value)


# ------------------------------------------------------------------- CLI surface

@pytest.mark.parametrize("subcommand", ["bridge", "list-skills", "rpc-bench"])
def test_every_cli_subcommand_can_print_its_help(subcommand, capsys):
    """
    `bridge --help` crashed: a literal `%` in the slippage help text was read as an
    argparse interpolation. Cheap to reintroduce, and it makes the command look broken
    before it has done anything.
    """
    from omnibridge.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main([subcommand, "--help"])
    assert excinfo.value.code == 0
    assert "usage:" in capsys.readouterr().out
