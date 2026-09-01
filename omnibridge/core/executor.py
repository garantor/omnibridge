"""
In-Transit Calldata Generation and Ephemeral Script Synthesis.
Assembles raw unsigned transactions dynamically without pre-compiled bridge packages.
"""

from typing import Dict, Any, Optional
from eth_abi import encode
from web3 import Web3
from omnibridge.core.models import RouteQuote, UserIntent


class CalldataExecutor:
    """Encodes standard raw calldata payloads on-the-fly and generates ephemeral execution scripts."""

    @staticmethod
    def pad_bytes32_address(address: str) -> bytes:
        """Pads an EVM address (20 bytes) to bytes32 for cross-chain recipients."""
        clean_addr = address.lower().replace("0x", "")
        return bytes.fromhex(clean_addr.zfill(64))

    @staticmethod
    def encode_erc20_approve(spender_address: str, amount_raw: int) -> str:
        """Encodes ERC-20 approve(address,uint256) -> 0x095ea7b3."""
        selector = bytes.fromhex("095ea7b3")
        encoded_args = encode(["address", "uint256"], [Web3.to_checksum_address(spender_address), amount_raw])
        return "0x" + (selector + encoded_args).hex()

    @classmethod
    def encode_cctp_deposit_for_burn(
        cls,
        amount_raw: int,
        destination_domain: int,
        recipient_address: str,
        burn_token_address: str,
    ) -> str:
        """
        Encodes Circle CCTP TokenMessenger depositForBurn:
        depositForBurn(uint256 amount, uint32 destinationDomain, bytes32 mintRecipient, address burnToken)
        Method selector: 0xb578ea00 (or standard depositForBurn)
        """
        # Keccak-256("depositForBurn(uint256,uint32,bytes32,address)") = 0x5a18a9fc or 0x6e25c040
        # Standard TokenMessenger: depositForBurn(uint256,uint32,bytes32,address)
        # Function selector for depositForBurn(uint256,uint32,bytes32,address) is 0x6e25c040
        func_sig = "depositForBurn(uint256,uint32,bytes32,address)"
        selector = Web3.keccak(text=func_sig)[:4]

        recipient_b32 = cls.pad_bytes32_address(recipient_address)
        burn_token_checksum = Web3.to_checksum_address(burn_token_address)

        encoded_args = encode(
            ["uint256", "uint32", "bytes32", "address"],
            [amount_raw, destination_domain, recipient_b32, burn_token_checksum],
        )
        return "0x" + (selector + encoded_args).hex()

    @classmethod
    def encode_across_deposit_v3(
        cls,
        depositor: str,
        recipient: str,
        input_token: str,
        output_token: str,
        input_amount: int,
        output_amount: int,
        destination_chain_id: int,
        relayer: str = "0x0000000000000000000000000000000000000000",
        quote_timestamp: int = 0,
        fill_deadline: int = 0,
        exclusivity_deadline: int = 0,
        message: bytes = b"",
    ) -> str:
        """Encodes Across Protocol V3 depositV3 call."""
        func_sig = "depositV3(address,address,address,address,uint256,uint256,uint256,address,uint32,uint32,uint32,bytes)"
        selector = Web3.keccak(text=func_sig)[:4]

        encoded_args = encode(
            [
                "address",
                "address",
                "address",
                "address",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "uint32",
                "uint32",
                "uint32",
                "bytes",
            ],
            [
                Web3.to_checksum_address(depositor),
                Web3.to_checksum_address(recipient),
                Web3.to_checksum_address(input_token),
                Web3.to_checksum_address(output_token),
                input_amount,
                output_amount,
                destination_chain_id,
                Web3.to_checksum_address(relayer),
                quote_timestamp,
                fill_deadline,
                exclusivity_deadline,
                message,
            ],
        )
        return "0x" + (selector + encoded_args).hex()

    @classmethod
    def build_calldata_for_route(
        cls, quote: RouteQuote, intent: UserIntent, sender_address: str
    ) -> str:
        """Selects the correct encoder based on route specifications."""
        recipient = intent.recipient or sender_address

        if quote.calldata_template in ("cctp_deposit_for_burn", "skill_verified"):
            dst_domain = quote.extra_params.get("destination_domain") or quote.extra_params.get("cctp_domain", 3)
            return cls.encode_cctp_deposit_for_burn(
                amount_raw=quote.deposit_amount_raw,
                destination_domain=dst_domain,
                recipient_address=recipient,
                burn_token_address=quote.source_token_address,
            )

        if quote.calldata_template == "across_deposit_v3":
            return cls.encode_across_deposit_v3(
                depositor=sender_address,
                recipient=recipient,
                input_token=quote.source_token_address,
                output_token=quote.dest_token_address,
                input_amount=quote.deposit_amount_raw,
                output_amount=quote.estimated_output_raw,
                destination_chain_id=quote.dest_chain_id,
            )

        # Fallback simple transfer payload
        return "0x"

    @staticmethod
    def generate_ephemeral_python_script(
        rpc_url: str,
        router_address: str,
        calldata: str,
        value_wei: int = 0,
        chain_id: int = 8453,
    ) -> str:
        """Produces a standalone ephemeral Python script demonstrating the exact transaction construction."""
        return f'''# Auto-generated Ephemeral Bridge Execution Script
from web3 import Web3

RPC_URL = "{rpc_url}"
ROUTER_ADDRESS = "{router_address}"
CALLDATA = "{calldata}"
VALUE_WEI = {value_wei}
CHAIN_ID = {chain_id}

w3 = Web3(Web3.HTTPProvider(RPC_URL))
assert w3.is_connected(), "RPC connection failed"

def construct_payload(sender_address, nonce=None):
    if nonce is None:
        nonce = w3.eth.get_transaction_count(sender_address)
    
    tx = {{
        "chainId": CHAIN_ID,
        "from": sender_address,
        "to": Web3.to_checksum_address(ROUTER_ADDRESS),
        "value": VALUE_WEI,
        "data": CALLDATA,
        "gas": 250000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei("0.001", "gwei"),
        "nonce": nonce,
    }}
    return tx

if __name__ == "__main__":
    print(f"Connected to Chain ID: {{w3.eth.chain_id}}")
    print(f"Target Bridge Router: {{ROUTER_ADDRESS}}")
'''
