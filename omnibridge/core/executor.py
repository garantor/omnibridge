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

    # Three named encoders were removed here: encode_erc20_approve,
    # encode_cctp_deposit_for_burn and encode_across_deposit_v3.
    #
    # They were the branch targets of the `calldata_template` switch that Phase 4
    # deleted, and once that switch was gone nothing in the running system called them
    # — only the golden tests, which existed to prove the generic encoder reproduced
    # the same bytes. It does, against real mainnet transactions, so keeping a
    # hand-written encoder per protocol is precisely the per-bridge integration cost
    # this project exists to avoid (Rule B).
    #
    # Anything they did is now expressible as a route: a canonical signature, its
    # parameter types, and its values. `approve(address,uint256)` included.

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

    # The only substitutions a stored route may ask for. Bounded and reviewed: the
    # agent decided at discovery time which slot carries the amount and which the
    # recipient; this code performs that binding and nothing else. An unrecognised
    # placeholder is refused rather than passed through as a literal string.
    PLACEHOLDERS = ("{amount_raw}", "{recipient}", "{recipient_bytes32}", "{sender}")

    @classmethod
    def bind_param_values(
        cls,
        param_values: List[Any],
        amount_raw: int,
        recipient: str,
        sender: str,
    ) -> List[Any]:
        """Substitute this request's amount and addresses into a stored parameter list."""
        bindings: Dict[str, Any] = {
            "{amount_raw}": amount_raw,
            "{recipient}": recipient,
            "{recipient_bytes32}": "0x" + cls.pad_bytes32_address(recipient).hex(),
            "{sender}": sender,
        }

        bound: List[Any] = []
        for value in param_values:
            if not isinstance(value, str) or "{" not in value:
                bound.append(value)
                continue
            if value in bindings:
                bound.append(bindings[value])
                continue
            raise ValueError(
                f"Stored route contains the placeholder {value!r}, which this encoder "
                f"does not know how to bind. Known placeholders: {', '.join(cls.PLACEHOLDERS)}. "
                "Refusing to encode it as a literal."
            )
        return bound

    @classmethod
    def generalise_param_values(
        cls,
        param_values: List[Any],
        amount_raw: int,
        recipient: str,
        sender: str,
    ) -> List[str]:
        """
        The inverse: replace this run's concrete values with placeholders before storing.

        Purely mechanical — a slot becomes a placeholder only when it is byte-identical
        to the value that was bound into it. A skill that stored the literal amount
        would replay one transfer forever, at whatever size happened to be verified first.
        """
        recipient_b32 = "0x" + cls.pad_bytes32_address(recipient).hex()
        reverse = [
            (str(amount_raw), "{amount_raw}"),
            (str(recipient).lower(), "{recipient}"),
            (recipient_b32.lower(), "{recipient_bytes32}"),
            (str(sender).lower(), "{sender}"),
        ]

        generalised: List[str] = []
        for value in param_values:
            text = str(value)
            match = next((ph for concrete, ph in reverse if text.lower() == concrete), None)
            generalised.append(match or text)
        return generalised

    @classmethod
    def build_calldata_for_route(
        cls, quote: RouteQuote, intent: UserIntent, sender_address: str
    ) -> str:
        """
        Encode a route from the signature and parameters it carries, or refuse.

        This replaced a switch on `quote.calldata_template`. Two of its branches were
        the worst kind of fabrication in the codebase: `"stellar_soroban_invoke"`
        returned the UTF-8 bytes of the string `stellar_invoke:deposit(...)` as though
        that were a transaction, and the fall-through invented the signature
        `bridge(uint256,address,uint256)` — a function that exists on no protocol — for
        any route it did not recognise, guaranteeing well-formed calldata for a call
        that cannot succeed.

        There is now one encoding path. Whether the route came from the agent or from a
        cached skill, it must say which function it is calling and with what.
        """
        recipient = intent.recipient or sender_address
        params = quote.extra_params or {}

        signature = params.get("function_signature")
        param_types = params.get("param_types") or []
        param_values = params.get("param_values")

        if not signature or param_values is None:
            raise ValueError(
                f"Cannot encode a call for {quote.bridge_name!r}: the route carries no "
                "function signature and parameter values. A route that does not say "
                "which function it calls is not a route."
            )

        bound = cls.bind_param_values(
            list(param_values),
            amount_raw=quote.deposit_amount_raw,
            recipient=recipient,
            sender=sender_address,
        )
        values = cls.coerce_param_values(list(param_types), bound)
        return cls.encode_dynamic_calldata(signature, list(param_types), values)

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
