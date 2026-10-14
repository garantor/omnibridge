"""
What may be submitted as a UserOperation for a run, and the batch it must carry.

The browser builds and signs the UserOperation with MetaMask's own kit, so the signing
format is MetaMask's rather than a reimplementation of it. That makes the browser the
party assembling what is sent, and the browser is not trusted: a compromised page could
swap a call. So every UserOperation passing through the server's proxy is checked
against the batch the graph simulated and the human approved, call for call, before
the bundler or paymaster ever sees it.

The batch is: an allowance for the paymaster's gas fee (capped by code), then the plan's
unsigned steps in order — normally approve and bridge. One signature, one atomic
execution.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from web3 import Web3

from omnibridge.aa.account import ENTRYPOINT_V07, BatchDecodeError, Call, decode_batch
from omnibridge.core import erc20


@dataclass
class ApprovedBatch:
    sender: str
    chain_id: int
    token: str
    paymaster: str
    fee_cap: int
    calls: List[Call] = field(default_factory=list)
    #: The ERC-7677 context the paymaster must be sent, exactly; provider-specific.
    paymaster_context: Any = None

    def __post_init__(self) -> None:
        if self.paymaster_context is None:
            self.paymaster_context = {"token": self.token}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sender": self.sender, "chain_id": self.chain_id, "token": self.token,
            "paymaster": self.paymaster, "fee_cap": self.fee_cap,
            "entry_point": ENTRYPOINT_V07,
            "calls": [{"to": c.to, "value": str(c.value), "data": c.data} for c in self.calls],
            "paymaster_context": self.paymaster_context,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ApprovedBatch":
        return cls(sender=d["sender"], chain_id=int(d["chain_id"]), token=d["token"],
                   paymaster=d["paymaster"], fee_cap=int(d["fee_cap"]),
                   calls=[Call(c["to"], int(c["value"]), c["data"]) for c in d["calls"]],
                   paymaster_context=d.get("paymaster_context"))


def build_batch(sender: str, chain_id: int, steps: List[Any], token: str, paymaster: str,
                fee_cap: int, paymaster_context: Any = None) -> ApprovedBatch:
    """The paymaster's fee allowance, then every unsigned plan step, in order."""
    calls = [Call(Web3.to_checksum_address(token), 0, erc20.build_approve(paymaster, fee_cap))]
    for step in steps:
        if getattr(step, "tx_hash", None):
            continue
        calls.append(Call(Web3.to_checksum_address(step.to), int(step.value_wei or 0), step.data))
    return ApprovedBatch(Web3.to_checksum_address(sender), int(chain_id), Web3.to_checksum_address(token),
                         Web3.to_checksum_address(paymaster), int(fee_cap), calls, paymaster_context)


def check_user_operation(op: Dict[str, Any], approved: ApprovedBatch,
                         entry_point: Optional[str] = None) -> List[str]:
    """Every reason this UserOperation is not the approved batch. Empty means it is."""
    problems: List[str] = []
    if entry_point is not None and entry_point.lower() != ENTRYPOINT_V07.lower():
        problems.append(f"EntryPoint {entry_point} is not v0.7 ({ENTRYPOINT_V07}).")
    sender = str(op.get("sender", ""))
    if sender.lower() != approved.sender.lower():
        problems.append(f"sender {sender} is not the run's wallet {approved.sender}.")
    # A 7702 UserOperation may carry an authorization that re-delegates the account. The
    # user switched to MetaMask's account code in MetaMask; nothing here may change it.
    if op.get("eip7702Auth") or op.get("authorization"):
        problems.append("the UserOperation carries an EIP-7702 authorization; re-delegating the account is never allowed.")
    if op.get("factory") or op.get("initCode") not in (None, "0x", ""):
        problems.append("the UserOperation deploys an account; a 7702 account needs no deployment.")
    paymaster = op.get("paymaster")
    if paymaster and str(paymaster).lower() != approved.paymaster.lower():
        problems.append(f"paymaster {paymaster} is not the approved ERC-20 paymaster {approved.paymaster}.")
    try:
        calls = decode_batch(str(op.get("callData", "")))
    except BatchDecodeError as exc:
        problems.append(f"callData: {exc}")
        return problems
    got = [c.normalised() for c in calls]
    want = [c.normalised() for c in approved.calls]
    if got != want:
        problems.append(
            f"the batch has {len(got)} call(s) that differ from the {len(want)} approved: "
            + "; ".join(f"#{i}: {g[0]} {g[2][:10]}" for i, g in enumerate(got) if i >= len(want) or g != want[i])
        )
    return problems


def check_paymaster_context(context: Any, approved: ApprovedBatch) -> List[str]:
    """The context must be the one approved: it is what tells the paymaster which token to charge."""
    if context != approved.paymaster_context:
        return [f"the paymaster context must be the approved one ({approved.paymaster_context}), "
                f"naming the approved gas token {approved.token}."]
    return []
