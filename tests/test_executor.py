"""
Encoder unit tests.

These used to exercise a named encoder per protocol. Those were deleted once the
generic path was shown to reproduce real mainnet bytes (tests/test_golden_routes.py),
so what remains is the recipient encoding and the one generic entry point.
"""

import pytest
from web3 import Web3

from omnibridge.core.executor import CalldataExecutor


def test_pad_bytes32_address():
    addr = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    b32 = CalldataExecutor.pad_bytes32_address(addr)
    assert len(b32) == 32
    assert b32.hex().endswith(addr.lower().replace("0x", ""))


def test_generic_encoder_produces_the_erc20_approve_selector():
    """
    `approve(address,uint256)` needs no dedicated encoder — it is a signature like any
    other, and the selector falls out of keccak rather than being asserted from memory.
    """
    spender = "0x12ade2554a39A27dd2465376F4302cCc8004ce38"
    calldata = CalldataExecutor.encode_dynamic_calldata(
        "approve(address,uint256)",
        ["address", "uint256"],
        CalldataExecutor.coerce_param_values(["address", "uint256"], [spender, "1000000"]),
    )
    assert calldata.startswith("0x" + Web3.keccak(text="approve(address,uint256)")[:4].hex())
    assert calldata.startswith("0x095ea7b3")
    assert len(calldata) == 2 + 8 + 64 + 64


def test_the_encoder_refuses_an_arity_it_cannot_satisfy():
    """
    What replaced the per-protocol required-argument checks. A named CCTP encoder used
    to guard its own `destinationDomain`; the generic path cannot lose a parameter
    silently, because types and values must line up before anything is packed.
    """
    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.coerce_param_values(
            ["uint256", "uint32", "bytes32", "address"],
            ["1000000", "0x" + "11" * 32, "0x" + "22" * 20],
        )
    assert "arity mismatch" in str(excinfo.value)
