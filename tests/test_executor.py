from web3 import Web3
from omnibridge.core.executor import CalldataExecutor


def test_pad_bytes32_address():
    addr = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    b32 = CalldataExecutor.pad_bytes32_address(addr)
    assert len(b32) == 32
    assert b32.hex().endswith(addr.lower().replace("0x", ""))


def test_encode_erc20_approve():
    spender = "0x12ade2554a39A27dd2465376F4302cCc8004ce38"
    calldata = CalldataExecutor.encode_erc20_approve(spender, 1000000)
    assert calldata.startswith("0x095ea7b3")
    assert len(calldata) == 2 + 8 + 64 + 64


def test_encode_cctp_deposit_for_burn():
    recipient = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    burn_token = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    calldata = CalldataExecutor.encode_cctp_deposit_for_burn(
        amount_raw=100000000,
        destination_domain=3,
        recipient_address=recipient,
        burn_token_address=burn_token,
    )
    assert calldata.startswith("0x")
    assert len(calldata) > 200
