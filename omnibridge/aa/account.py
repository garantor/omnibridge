"""
The smart-account side of signing: EIP-7702 accounts delegated to MetaMask's DeleGator.

A MetaMask user who turns on "Switch to smart account" delegates their EOA to MetaMask's
`EIP7702StatelessDeleGator`. On chain that leaves the 23-byte EIP-7702 designator
(0xef0100 || delegate) as the address's code. Such an account can run several calls as
one atomic batch, submitted by a bundler as an ERC-4337 UserOperation, with gas paid by a
paymaster — which is what lets approve-then-bridge happen in one signature, and gas be
paid in USDC.

Websites cannot sign the 7702 authorization themselves (MetaMask's kit: "signAuthorization
does not support JSON-RPC accounts"), so this module never upgrades anyone. It only reads
whether the user already did.

The two addresses below are infrastructure, not route knowledge: the account code
MetaMask delegates to and the ERC-4337 EntryPoint it validates against, from MetaMask's
published `@metamask/delegation-deployments`. They are checked on chain before use: code
present, and the selectors this module relies on present in it.
"""

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from eth_abi import decode, encode
from web3 import Web3

from omnibridge.agent.provenance import selector_for, verify_proposal

#: MetaMask's EIP7702StatelessDeleGatorImpl (delegation-deployments 2.x, every chain).
METAMASK_7702_DELEGATOR = "0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B"
#: ERC-4337 EntryPoint v0.7, the one the DeleGator validates UserOperations against.
ENTRYPOINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

EXECUTE_SIGNATURE = "execute(bytes32,bytes)"
EXECUTE_SELECTOR = selector_for(EXECUTE_SIGNATURE)

#: ERC-7579 execution mode: call type 0x01 (batch), exec type 0x00 (revert on any
#: failure), no selector or payload. "Revert on failure" is what makes the batch atomic:
#: an approval can never land without the bridge call that needed it.
MODE_BATCH_REVERT = bytes([0x01]) + bytes(31)

_DESIGNATOR_PREFIX = "ef0100"


@dataclass(frozen=True)
class Call:
    to: str
    value: int
    data: str

    def normalised(self) -> Tuple[str, int, str]:
        return (Web3.to_checksum_address(self.to), int(self.value), self.data.lower())


@dataclass
class AccountStatus:
    address: str
    #: "metamask_7702" (usable here), "eoa", "other_7702", "contract" or "unknown".
    kind: str
    delegate: Optional[str] = None
    reason: Optional[str] = None

    @property
    def smart_account(self) -> bool:
        return self.kind == "metamask_7702"

    def as_dict(self) -> dict:
        return {"address": self.address, "kind": self.kind, "delegate": self.delegate,
                "smart_account": self.smart_account, "reason": self.reason}


def delegate_of(code: bytes) -> Optional[str]:
    """The address an EIP-7702 designator points to, or None if the code is not one."""
    raw = code.hex() if isinstance(code, (bytes, bytearray)) else str(code)
    raw = raw[2:] if raw.startswith("0x") else raw
    if len(raw) == 46 and raw.lower().startswith(_DESIGNATOR_PREFIX):
        return Web3.to_checksum_address("0x" + raw[6:])
    return None


def account_status(w3: Any, address: str) -> AccountStatus:
    """
    Whether this address can be driven as a MetaMask 7702 smart account on this chain.

    Delegation is per chain: an account switched on Arbitrum Sepolia is still a plain EOA
    on Base Sepolia until the user switches there too.
    """
    try:
        checksummed = Web3.to_checksum_address(address)
    except Exception:
        return AccountStatus(address, "unknown", reason="not a valid address")
    try:
        code = w3.eth.get_code(checksummed)
    except Exception as exc:
        return AccountStatus(checksummed, "unknown", reason=f"could not read the account's code: {exc}")
    if not code:
        return AccountStatus(checksummed, "eoa", reason="plain EOA: not switched to a smart account on this chain")
    delegate = delegate_of(code)
    if delegate is None:
        return AccountStatus(checksummed, "contract", reason="a contract account, not an EIP-7702 EOA")
    if delegate.lower() != METAMASK_7702_DELEGATOR.lower():
        return AccountStatus(checksummed, "other_7702", delegate,
                             reason="delegated to account code this app does not know how to drive")
    # The designator names the right contract; make sure that contract is what we think.
    report = verify_proposal(w3, METAMASK_7702_DELEGATOR, EXECUTE_SIGNATURE, [])
    if not (report.is_contract and report.selector_present):
        return AccountStatus(checksummed, "unknown", delegate,
                             reason="the delegator on this chain does not implement execute(bytes32,bytes)")
    return AccountStatus(checksummed, "metamask_7702", delegate)


def encode_batch(calls: List[Call]) -> str:
    """ERC-7579 `execute(mode, abi.encode(Execution[]))` for an atomic batch."""
    executions = [(Web3.to_checksum_address(c.to), int(c.value), bytes.fromhex(c.data[2:] if c.data.startswith("0x") else c.data))
                  for c in calls]
    exec_data = encode(["(address,uint256,bytes)[]"], [executions])
    return EXECUTE_SELECTOR + encode(["bytes32", "bytes"], [MODE_BATCH_REVERT, exec_data]).hex()


class BatchDecodeError(ValueError):
    """The calldata is not an atomic ERC-7579 batch this app produces."""


def decode_batch(calldata: str) -> List[Call]:
    """
    The calls inside an `execute` batch, or raise.

    Only the exact mode this app encodes is accepted. A single-call or try-mode execute
    would still move funds, just not in the shape that was simulated and approved.
    """
    data = calldata[2:] if calldata.startswith("0x") else calldata
    if not data.lower().startswith(EXECUTE_SELECTOR[2:]):
        raise BatchDecodeError(f"not an execute(bytes32,bytes) call (selector {data[:8]!r}).")
    try:
        mode, exec_data = decode(["bytes32", "bytes"], bytes.fromhex(data[8:]))
    except Exception as exc:
        raise BatchDecodeError(f"execute arguments do not decode: {exc}") from exc
    if mode != MODE_BATCH_REVERT:
        raise BatchDecodeError(f"execution mode 0x{mode.hex()} is not an atomic batch.")
    try:
        (executions,) = decode(["(address,uint256,bytes)[]"], exec_data)
    except Exception as exc:
        raise BatchDecodeError(f"batch executions do not decode: {exc}") from exc
    return [Call(to=Web3.to_checksum_address(t), value=int(v), data="0x" + d.hex()) for t, v, d in executions]
