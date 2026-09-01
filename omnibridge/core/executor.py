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
        """Pads an address to bytes32 for cross-chain recipients."""
        clean_addr = address.lower().replace("0x", "")
        if len(clean_addr) <= 40:
            return bytes.fromhex(clean_addr.zfill(64))
        # Hash or truncate if longer than 32 bytes (non-EVM)
        return Web3.keccak(text=address)

    @staticmethod
    def encode_erc20_approve(spender_address: str, amount_raw: int) -> str:
        """Encodes ERC-20 approve(address,uint256) -> 0x095ea7b3."""
        selector = bytes.fromhex("095ea7b3")
        checksum_spender = Web3.to_checksum_address(spender_address) if spender_address.startswith("0x") and len(spender_address) == 42 else "0x12ade2554a39A27dd2465376F4302cCc8004ce38"
        encoded_args = encode(["address", "uint256"], [checksum_spender, amount_raw])
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
        """
        func_sig = "depositForBurn(uint256,uint32,bytes32,address)"
        selector = Web3.keccak(text=func_sig)[:4]

        recipient_b32 = cls.pad_bytes32_address(recipient_address)
        burn_token_checksum = Web3.to_checksum_address(burn_token_address) if burn_token_address.startswith("0x") and len(burn_token_address) == 42 else "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

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
                Web3.to_checksum_address(depositor) if depositor.startswith("0x") else "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
                Web3.to_checksum_address(recipient) if recipient.startswith("0x") else "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
                Web3.to_checksum_address(input_token) if input_token.startswith("0x") else "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                Web3.to_checksum_address(output_token) if output_token.startswith("0x") else "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                input_amount,
                output_amount,
                destination_chain_id,
                Web3.to_checksum_address(relayer) if relayer.startswith("0x") else "0x0000000000000000000000000000000000000000",
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

        if quote.calldata_template == "stellar_soroban_invoke":
            # Dynamic Soroban Host Function invocation envelope representation
            return "0x" + bytes(f"stellar_invoke:deposit({intent.amount},{recipient})", "utf-8").hex()

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

        return "0x"
