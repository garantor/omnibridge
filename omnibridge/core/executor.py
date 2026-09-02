"""
In-Transit Calldata Generation and Ephemeral Script Synthesis.
Assembles raw unsigned transactions dynamically without pre-compiled bridge packages or hardcoded addresses.
"""

from typing import Dict, Any, Optional, List
from eth_abi import encode
from web3 import Web3
from omnibridge.core.models import RouteQuote, UserIntent


class CalldataExecutor:
    """Encodes standard raw calldata payloads on-the-fly and generates ephemeral execution scripts."""

    @staticmethod
    def pad_bytes32_address(address: str) -> bytes:
        """
        Render a recipient as the bytes32 a cross-chain mint expects.

        This previously ended in `return Web3.keccak(text=address)` for anything longer
        than 20 bytes — that is, for every non-EVM recipient, the exact case the function
        exists to serve. A Solana, Sui, Aptos or Stellar key was encoded as the *hash* of
        itself: a well-formed bytes32 that the contract accepts, that simulates with no
        revert, and that nobody holds the private key for. Burned on the source chain,
        minted to an unrecoverable address on the destination.

        Now: left-pad a 20-byte address, pass a 32-byte key through untouched, and refuse
        anything else rather than transform it into something plausible.
        """
        raw = address.strip()
        clean = raw[2:] if raw.lower().startswith("0x") else raw
        try:
            int(clean, 16)
        except ValueError:
            raise ValueError(
                f"Recipient {address!r} is not hex. It must be a 20-byte EVM address or a "
                "32-byte destination key; encode non-hex address formats before this point."
            )

        if len(clean) <= 40:
            return bytes.fromhex(clean.zfill(64))
        if len(clean) == 64:
            return bytes.fromhex(clean)
        raise ValueError(
            f"Recipient {address!r} is {len(clean) // 2} bytes; expected 20 (EVM) or 32 "
            "(destination key). Refusing to hash or truncate it into something that would "
            "encode cleanly and be unrecoverable."
        )

    @staticmethod
    def encode_erc20_approve(spender_address: str, amount_raw: int) -> str:
        """Encodes ERC-20 approve(address,uint256) -> 0x095ea7b3."""
        selector = bytes.fromhex("095ea7b3")
        checksum_spender = (
            CalldataExecutor.require_evm_address(spender_address, "spender_address")
        )
        encoded_args = encode(["address", "uint256"], [checksum_spender, amount_raw])
        return "0x" + (selector + encoded_args).hex()

    @staticmethod
    def require_evm_address(value: str, field: str) -> str:
        """
        Checksum an address, or raise.

        Every call site used to read
        `to_checksum_address(x) if x.startswith("0x") and len(x) == 42 else "0x000...0"`,
        which silently turns an unusable address into the zero address. Encoded as a
        recipient that is a burn, and it simulates perfectly — no revert, correct gas.
        An explicit zero address still passes, since that is a real convention for a
        native asset; what is refused is a substitution nobody asked for.
        """
        if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
            raise ValueError(
                f"Invalid EVM address for {field}: {value!r}. Refusing to substitute the "
                "zero address — that would silently redirect or burn the transfer."
            )
        try:
            int(value, 16)
        except ValueError:
            raise ValueError(f"Invalid EVM address for {field}: {value!r} is not hex.")
        return Web3.to_checksum_address(value)

    @classmethod
    def encode_cctp_deposit_for_burn(
        cls,
        amount_raw: int,
        destination_domain: Optional[int],
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
        burn_token_checksum = (
            CalldataExecutor.require_evm_address(burn_token_address, "burn_token_address")
        )
        if destination_domain is None:
            raise ValueError(
                "CCTP destinationDomain is required. Defaulting it to 0 would encode "
                "Ethereum as the destination — a wrong chain, not an absent one."
            )
        valid_domain = int(destination_domain)

        encoded_args = encode(
            ["uint256", "uint32", "bytes32", "address"],
            [amount_raw, valid_domain, recipient_b32, burn_token_checksum],
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
        fill_deadline: int = 2147483647,
        exclusivity_deadline: int = 0,
        message: bytes = b"",
    ) -> str:
        """Encodes Across V3 SpokePool depositV3 calldata on-the-fly."""
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
                CalldataExecutor.require_evm_address(depositor, "depositor"),
                CalldataExecutor.require_evm_address(recipient, "recipient"),
                CalldataExecutor.require_evm_address(input_token, "input_token"),
                CalldataExecutor.require_evm_address(output_token, "output_token"),
                input_amount,
                output_amount,
                destination_chain_id,
                CalldataExecutor.require_evm_address(relayer, "relayer"),
                quote_timestamp,
                fill_deadline,
                exclusivity_deadline,
                message,
            ],
        )
        return "0x" + (selector + encoded_args).hex()

    @classmethod
    def encode_dynamic_calldata(cls, func_sig: str, param_types: List[str], param_values: List[Any]) -> str:
        """Dynamically calculates 4-byte selector and encodes any ABI parameter schema on the fly."""
        selector = Web3.keccak(text=func_sig)[:4]
        encoded_args = encode(param_types, param_values)
        return "0x" + (selector + encoded_args).hex()

    @staticmethod
    def coerce_param_values(param_types: List[str], param_values: List[Any]) -> List[Any]:
        """
        Turn textual ABI arguments into the Python types eth_abi expects.

        Values arrive as strings from two places: golden fixtures on disk, and the
        agent's `propose_route` call in Phase 3, whose schema carries
        `param_values: list[str]`. Coercion is mechanical and belongs in reviewed code
        (Rule A) — the model chooses the values, it does not get to decide how they
        are packed.

        Raises rather than guessing on anything it does not recognise.
        """
        if len(param_types) != len(param_values):
            raise ValueError(
                f"ABI arity mismatch: {len(param_types)} types but "
                f"{len(param_values)} values."
            )

        coerced: List[Any] = []
        for abi_type, value in zip(param_types, param_values):
            t = abi_type.strip()
            if t.startswith(("uint", "int")) and not t.endswith("]"):
                coerced.append(value if isinstance(value, int) else int(str(value), 0))
            elif t == "address":
                coerced.append(CalldataExecutor.require_evm_address(str(value), t))
            elif t == "bool":
                if isinstance(value, bool):
                    coerced.append(value)
                elif str(value).lower() in ("true", "false"):
                    coerced.append(str(value).lower() == "true")
                else:
                    raise ValueError(f"Cannot read {value!r} as a bool.")
            elif t.startswith("bytes") and not t.endswith("]"):
                if isinstance(value, (bytes, bytearray)):
                    coerced.append(bytes(value))
                else:
                    text = str(value)
                    coerced.append(bytes.fromhex(text[2:] if text.startswith("0x") else text))
            elif t == "string":
                coerced.append(str(value))
            else:
                raise ValueError(
                    f"Unsupported ABI type {t!r}. Refusing to guess an encoding for it."
                )
        return coerced

    @classmethod
    def decode_leading_uint256(cls, calldata: str) -> Optional[int]:
        """
        Read the first 32-byte argument word out of encoded calldata.

        Every encoder in this class places the transfer amount first, so this recovers
        the amount that is genuinely in the transaction rather than the one the intent
        asked for. Deliberately dumb: it reads bytes and consults nothing else, which is
        the entire point — a manifest rendered from the same source as the transaction
        cannot reveal a scaling error in it.

        Returns None when the payload is not ABI-encoded (e.g. a Soroban invoke blob).
        """
        try:
            raw = calldata[2:] if calldata.startswith("0x") else calldata
            if len(raw) < 8 + 64:
                return None
            return int(raw[8:8 + 64], 16)
        except (ValueError, TypeError):
            return None

    @classmethod
    def build_calldata_for_route(
        cls, quote: RouteQuote, intent: UserIntent, sender_address: str
    ) -> str:
        """Selects the correct encoder based on route specifications."""
        recipient = intent.recipient or sender_address

        if quote.calldata_template == "stellar_soroban_invoke":
            return "0x" + bytes(f"stellar_invoke:deposit({intent.amount},{recipient})", "utf-8").hex()

        if quote.calldata_template in ("cctp_deposit_for_burn", "skill_verified"):
            raw_domain = quote.extra_params.get("destination_domain") or quote.extra_params.get("cctp_domain")
            dst_domain = int(raw_domain) if raw_domain is not None else 0
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

        # Generic Bridge Deposit Call (dynamic fallback)
        func_sig = "bridge(uint256,address,uint256)"
        return cls.encode_dynamic_calldata(
            func_sig,
            ["uint256", "address", "uint256"],
            [
                quote.deposit_amount_raw,
                CalldataExecutor.require_evm_address(recipient, "recipient"),
                quote.dest_chain_id,
            ],
        )

    @staticmethod
    def generate_ephemeral_python_script(
        rpc_url: str,
        router_address: str,
        calldata: str,
        value_wei: int = 0,
        chain_id: int = 1,
    ) -> str:
        """Produces a standalone ephemeral Python script demonstrating the exact transaction construction."""
        return f'''# Ephemeral Bridge Execution Script
from web3 import Web3

RPC_URL = "{rpc_url}"
ROUTER_ADDRESS = "{router_address}"
CALLDATA = "{calldata}"
VALUE_WEI = {value_wei}
CHAIN_ID = {chain_id}

w3 = Web3(Web3.HTTPProvider(RPC_URL))

def construct_payload(sender_address):
    return {{
        "chainId": CHAIN_ID,
        "from": sender_address,
        "to": Web3.to_checksum_address(ROUTER_ADDRESS) if ROUTER_ADDRESS.startswith("0x") else ROUTER_ADDRESS,
        "value": VALUE_WEI,
        "data": CALLDATA,
        "gas": 250000,
    }}
'''
