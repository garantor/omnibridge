"""
Golden route regressions.

Every fixture is a real, confirmed mainnet transaction (see tests/golden/README.md).
The property under test: our generic encoder must reproduce, byte for byte, calldata
that a deployed contract actually accepted on chain.

This is what makes Phase 4's deletion of the per-protocol template chain provable. Both
the generic path and the template path are measured against the same real transactions,
so removing one cannot silently change behaviour.
"""

import json
from pathlib import Path

import pytest
from web3 import Web3

from omnibridge.core.executor import CalldataExecutor

GOLDEN_DIR = Path(__file__).parent / "golden"


def _load(name):
    path = GOLDEN_DIR / name
    return json.loads(path.read_text()) if path.exists() else []


CCTP = _load("cctp_base.json")
ACROSS = _load("across_base.json")
FIXTURES = CCTP + ACROSS

# Fixture ids that read as route names in pytest output.
IDS = [f"{f.get('protocol', 'CCTP')}:{f['source_chain']}->{f['dest_chain']}" for f in FIXTURES]
CCTP_IDS = IDS[: len(CCTP)]


def canonical_and_suffix(fx):
    """
    Split a fixture's on-chain calldata into the part the ABI defines and anything the
    submitting integrator appended after it.

    Real transactions carry trailing bytes: the Across fixture ends in a 9-byte
    attribution tag that is not part of the encoding and is not derivable from the
    parameters. An encoder is right to omit it, so the test accounts for it rather than
    calling correct behaviour a mismatch.
    """
    return fx["expected_calldata"], fx.get("calldata_suffix", "0x")[2:]


def test_fixtures_are_present():
    assert FIXTURES, "golden fixture set is empty"
    for f in FIXTURES:
        assert f["tx_hash"].startswith("0x") and len(f["tx_hash"]) == 66
        assert f["expected_calldata"].startswith("0x")


@pytest.mark.parametrize("fx", FIXTURES, ids=IDS)
def test_selector_is_derived_not_asserted(fx):
    """The recorded selector must be the keccak of the recorded signature."""
    computed = "0x" + Web3.keccak(text=fx["function_signature"])[:4].hex()
    assert computed == fx["selector"]
    assert fx["expected_calldata"].startswith(fx["selector"])


@pytest.mark.parametrize("fx", FIXTURES, ids=IDS)
def test_generic_encoder_reproduces_real_calldata(fx):
    """
    The Phase 3 path: signature + types + textual values -> the exact bytes a real
    transaction carried. Non-circular, because the reference is on-chain data.
    """
    values = CalldataExecutor.coerce_param_values(fx["param_types"], fx["param_values"])
    encoded = CalldataExecutor.encode_dynamic_calldata(
        fx["function_signature"], fx["param_types"], values
    )
    expected, suffix = canonical_and_suffix(fx)
    assert (encoded + suffix).lower() == expected.lower()


@pytest.mark.parametrize("fx", CCTP, ids=CCTP_IDS)
def test_template_encoder_agrees_with_the_generic_one(fx):
    """
    Phase 4 deletes encode_cctp_deposit_for_burn in favour of the generic encoder.
    Holding both against the same real bytes is what licenses that deletion.

    The non-EVM fixtures (Solana, Sui) are the load-bearing ones here: their mintRecipient
    is a genuine 32-byte key, and pad_bytes32_address used to keccak-hash any recipient
    longer than 20 bytes into a well-formed, unrecoverable address.
    """
    recipient_b32 = bytes.fromhex(fx["param_values"][2][2:])
    evm_shaped = recipient_b32[:12] == b"\x00" * 12
    recipient = "0x" + (recipient_b32[12:] if evm_shaped else recipient_b32).hex()

    encoded = CalldataExecutor.encode_cctp_deposit_for_burn(
        amount_raw=int(fx["param_values"][0]),
        destination_domain=fx["dest_domain"],
        recipient_address=recipient,
        burn_token_address=fx["param_values"][3],
    )
    assert encoded.lower() == fx["expected_calldata"].lower()


@pytest.mark.parametrize("fx", CCTP, ids=CCTP_IDS)
def test_manifest_decodes_the_amount_that_is_actually_encoded(fx):
    """Guards the 1.7 rule: the manifest reads the bytes, not the intent."""
    decoded = CalldataExecutor.decode_leading_uint256(fx["expected_calldata"])
    assert decoded == int(fx["amount_raw"])
    assert abs(decoded / (10 ** fx["decimals"]) - fx["amount_human"]) < 1e-9


@pytest.mark.parametrize("fx", CCTP, ids=CCTP_IDS)
def test_coercion_refuses_a_bad_address(fx):
    bad = list(fx["param_values"])
    bad[3] = "not-an-address"
    with pytest.raises(ValueError):
        CalldataExecutor.coerce_param_values(fx["param_types"], bad)


def test_coercion_refuses_arity_mismatch():
    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.coerce_param_values(["uint256", "address"], ["1"])
    assert "arity mismatch" in str(excinfo.value)


def test_coercion_refuses_an_unknown_abi_type():
    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.coerce_param_values(["uint256[]"], [[1, 2]])
    assert "Refusing to guess" in str(excinfo.value)


# ----------------------------------------------------------------- network checks

@pytest.mark.network
@pytest.mark.parametrize("fx", FIXTURES[:1], ids=IDS[:1])
def test_router_is_a_contract_implementing_the_claimed_selector(fx):
    """
    The Phase 3.5 provenance check, exercised against a known-good router: the address
    holds bytecode, and that bytecode contains the selector the fixture claims.
    Generic — it needs no prior knowledge of CCTP.
    """
    w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        pytest.skip("Base RPC unreachable")

    code = w3.eth.get_code(Web3.to_checksum_address(fx["router_address"]))
    assert len(code) > 0, "router address holds no bytecode"
    selector = Web3.keccak(text=fx["function_signature"])[:4]
    assert selector.hex() in code.hex(), "claimed selector absent from deployed bytecode"


@pytest.mark.network
@pytest.mark.skipif(not ACROSS, reason="no Across fixture captured")
def test_selector_check_must_follow_a_proxy():
    """
    The Across SpokePool is an EIP-1967 proxy holding ~680 bytes of bytecode, and the
    depositV3 selector is absent from it — the implementation has it. A provenance check
    that reads only the address's own code produces a false negative on every proxied
    router, which is most of them.

    Recorded here as evidence for Phase 3.5, which must resolve the implementation slot
    before concluding a contract does not implement what was claimed.
    """
    fx = ACROSS[0]
    w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        pytest.skip("Base RPC unreachable")

    selector = Web3.keccak(text=fx["function_signature"])[:4].hex()
    proxy_code = w3.eth.get_code(Web3.to_checksum_address(fx["router_address"]))
    assert selector not in proxy_code.hex(), "fixture assumes a proxy; this one is not"

    eip1967 = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
    slot = w3.eth.get_storage_at(Web3.to_checksum_address(fx["router_address"]), eip1967)
    impl = Web3.to_checksum_address("0x" + slot.hex()[-40:])
    assert impl.lower() == fx["implementation_address"].lower()
    assert selector in w3.eth.get_code(impl).hex(), "selector absent from implementation too"


# ----------------------------------------------------------------- Phase 3 tripwire

@pytest.mark.xfail(
    strict=True,
    reason=(
        "Agent-level scoring needs the reasoning core (plan Phase 3). Given only the "
        "intent, the agent must arrive at the router, selector and parameters these "
        "fixtures record. Strict, so it fires the moment Phase 3 makes it passable."
    ),
)
def test_agent_derives_the_golden_route_from_intent_alone():
    from omnibridge.discovery.route_finder import RouteFinder
    from omnibridge.core.models import UserIntent

    fx = next(f for f in FIXTURES if f["dest_chain"] == "Arbitrum")
    quote = RouteFinder().discover_route(
        UserIntent(token="USDC", amount=1.0, source_chain="base", dest_chain="arbitrum")
    )
    assert quote.router_address.lower() == fx["router_address"].lower()
