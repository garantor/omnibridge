"""
ERC-20 reads and the approval call.

Standard, protocol-agnostic, and deterministic — so it belongs here rather than in
anything the model decides. `approve(address,uint256)`, `allowance(address,address)`
and `balanceOf(address)` are the same on every token on every EVM chain; knowing them
is not per-bridge knowledge and does not violate Rule B.

What is *not* here is which token a given bridge spends. That varies by protocol and is
the model's judgement, declared as `RouteProposal.spend` and checked against the chain
by this module.
"""

from typing import Optional

from eth_abi import decode as abi_decode
from web3 import Web3

from omnibridge.core.executor import CalldataExecutor

APPROVE_SIGNATURE = "approve(address,uint256)"
ALLOWANCE_SIGNATURE = "allowance(address,address)"
BALANCE_OF_SIGNATURE = "balanceOf(address)"
SYMBOL_SIGNATURE = "symbol()"
DECIMALS_SIGNATURE = "decimals()"

# The maximum a uint256 can hold. Deliberately never used as a default approval amount:
# an unlimited allowance leaves the router able to move the whole balance forever, which
# is a standing risk the user did not ask for in exchange for one transfer.
UINT256_MAX = 2**256 - 1


def _static_call(w3: Web3, to: str, signature: str, types_, values) -> Optional[bytes]:
    """One eth_call against a view function, or None if it could not be made."""
    data = CalldataExecutor.encode_dynamic_calldata(signature, list(types_), list(values))
    try:
        return w3.eth.call({"to": Web3.to_checksum_address(to), "data": data})
    except Exception:
        return None


def read_allowance(w3: Web3, token: str, owner: str, spender: str) -> Optional[int]:
    """
    Current allowance, or None when it could not be read.

    None is not zero. "The token did not answer" and "the allowance is zero" lead to
    different actions — the first means we do not know whether an approval is needed.
    """
    raw = _static_call(
        w3,
        token,
        ALLOWANCE_SIGNATURE,
        ["address", "address"],
        [Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)],
    )
    if raw is None or len(raw) < 32:
        return None
    return int(abi_decode(["uint256"], raw)[0])


def read_balance(w3: Web3, token: str, owner: str) -> Optional[int]:
    """Token balance of `owner`, or None when it could not be read."""
    raw = _static_call(
        w3, token, BALANCE_OF_SIGNATURE, ["address"], [Web3.to_checksum_address(owner)]
    )
    if raw is None or len(raw) < 32:
        return None
    return int(abi_decode(["uint256"], raw)[0])


def read_decimals(w3: Web3, token: str) -> Optional[int]:
    """
    The token's own decimals, or None when it could not be read.

    Read rather than assumed. The scaling from a human amount to base units is the
    difference between sending 25 USDC and sending 25,000,000 of them, and a table of
    "USDC is 6, everything else is 18" is wrong for every token that is neither.
    """
    raw = _static_call(w3, token, DECIMALS_SIGNATURE, [], [])
    if raw is None or len(raw) < 32:
        return None
    value = int(abi_decode(["uint8"], raw)[0])
    # A token claiming more than 77 decimals cannot be scaled inside a uint256.
    return value if 0 <= value <= 77 else None


def read_symbol(w3: Web3, token: str) -> Optional[str]:
    """
    The token's own symbol, or None when it could not be read.

    Used to corroborate an address that came from a token list: if the contract does
    not call itself what the list says it is, the list entry is stale, wrong, or for a
    different chain, and nothing should be concluded from a balance read against it.

    Handles the pre-standard `bytes32` return as well as `string`. MKR is the famous
    one, and treating its answer as unreadable would silently skip the check.
    """
    raw = _static_call(w3, token, SYMBOL_SIGNATURE, [], [])
    if not raw:
        return None
    try:
        return str(abi_decode(["string"], raw)[0]).strip() or None
    except Exception:
        pass
    try:
        return raw[:32].rstrip(b"\x00").decode("utf-8").strip() or None
    except Exception:
        return None


def build_approve(spender: str, amount: int) -> str:
    """
    Calldata for `approve(spender, amount)`.

    Exact-amount only. Approving `UINT256_MAX` is common and convenient and is not done
    here: it grants a standing right to move the entire balance, which outlives the
    transfer the user actually asked for.
    """
    if amount <= 0:
        raise ValueError(f"Refusing to build an approval for {amount}; nothing to approve.")
    if amount == UINT256_MAX:
        raise ValueError(
            "Refusing to build an unlimited approval. Approve the amount being "
            "transferred; an infinite allowance is a standing risk nobody asked for."
        )
    return CalldataExecutor.encode_dynamic_calldata(
        APPROVE_SIGNATURE,
        ["address", "uint256"],
        [CalldataExecutor.require_evm_address(spender, "spender"), int(amount)],
    )
