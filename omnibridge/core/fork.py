"""
Forked simulation with Anvil: run a sequence of transactions against a copy of the chain.

Plain `eth_call` runs one call against current state. That cannot check a bridge call
whose approval has not been signed yet — the allowance is genuinely absent, so it
reverts, and the planner has to report the bridge step PENDING_PREREQUISITE and check it
again after the approval is mined. By then the user has paid for the approval.

A fork removes that limit. Anvil copies the chain lazily from an RPC node, the sender is
impersonated, and each step is executed for real on the copy: the approval lands, then
the bridge call runs against the allowance it created. A route that would revert even
with its approval in place is caught before anything is signed.

What this is not: a substitute for the live re-check before signing. The fork is a
snapshot; the chain moves on. The planner still re-simulates each step against live
state immediately before it is signed, and the fork's verdict on a later step is
recorded as evidence, not as that step's status.

Availability: needs the `anvil` binary (Foundry). When it is missing or the fork cannot
start, callers fall back to plain `eth_call` and say so.
"""

import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from web3 import Web3

#: Seconds to wait for Anvil to answer after it starts forking.
_STARTUP_TIMEOUT = 25.0


def anvil_path() -> Optional[str]:
    """The anvil binary, from PATH or Foundry's default install location."""
    found = shutil.which("anvil")
    if found:
        return found
    import os

    candidate = os.path.expanduser("~/.foundry/bin/anvil")
    return candidate if os.path.exists(candidate) else None


def anvil_available() -> bool:
    return anvil_path() is not None


class ForkUnavailable(Exception):
    """A fork could not be started or did not confirm the expected chain."""


@dataclass
class ForkStep:
    to: str
    data: str
    value_wei: int = 0
    label: str = ""


@dataclass
class ForkStepResult:
    label: str
    success: bool
    gas_used: Optional[int] = None
    revert_reason: Optional[str] = None
    #: True when the failure was the fork's own (an RPC fetch failed), not the call's.
    infrastructure_error: bool = False


@dataclass
class ForkRun:
    chain_id: int
    fork_url: str
    steps: List[ForkStepResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return bool(self.steps) and all(s.success for s in self.steps)

    @property
    def first_failure(self) -> Optional[ForkStepResult]:
        return next((s for s in self.steps if not s.success), None)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": "anvil_fork",
            "chain_id": self.chain_id,
            "fork_url": self.fork_url,
            "success": self.success,
            "steps": [s.__dict__ for s in self.steps],
        }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: Failures that belong to the transaction or the wallet, not to the fork. Anything else
#: (a timeout, an upstream fetch the fork could not make) says nothing about the route.
_TRANSACTION_FAILURES = ("revert", "invalid opcode", "out of gas", "insufficient funds", "nonce too")


def _is_revert(message: str) -> bool:
    """True when the failure is the transaction's own, including an unfunded wallet."""
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSACTION_FAILURES)


@contextmanager
def anvil_fork(fork_url: str, expected_chain_id: int) -> Iterator[Web3]:
    """
    A running Anvil fork of `fork_url`, torn down on exit.

    The fork's chain id is checked before it is used, for the same reason the simulator
    checks a node's: a fork of the wrong chain answers every question confidently.
    """
    binary = anvil_path()
    if binary is None:
        raise ForkUnavailable("anvil is not installed.")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--fork-url", fork_url, "--port", str(port), "--silent",
         "--no-rate-limit"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 30}))
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while True:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode(errors="replace")[-300:]
                raise ForkUnavailable(f"anvil exited while starting: {err.strip()}")
            try:
                reported = int(w3.eth.chain_id)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise ForkUnavailable(f"anvil did not answer within {_STARTUP_TIMEOUT:.0f}s.")
                time.sleep(0.25)
        if reported != int(expected_chain_id):
            raise ForkUnavailable(
                f"The fork reports chain {reported}, expected {expected_chain_id}. "
                "The fork URL is not the chain it was supposed to be."
            )
        yield w3
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_on_fork(w3: Web3, sender: str, steps: List[ForkStep]) -> List[ForkStepResult]:
    """
    Execute steps in order as `sender`, stopping at the first failure.

    Each step is first eth_call'ed, so a revert comes back with the node's reason and
    leaves the fork untouched, then sent, so the next step sees its effect.
    """
    sender = Web3.to_checksum_address(sender)
    w3.provider.make_request("anvil_impersonateAccount", [sender])
    results: List[ForkStepResult] = []
    for step in steps:
        tx = {
            "from": sender,
            "to": Web3.to_checksum_address(step.to),
            "data": step.data if step.data.startswith("0x") else f"0x{step.data}",
            "value": int(step.value_wei),
        }
        try:
            w3.eth.call(tx)
            gas = int(w3.eth.estimate_gas(tx))
            tx_hash = w3.eth.send_transaction({**tx, "gas": gas + gas // 5})
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        except Exception as exc:
            message = str(exc)
            results.append(ForkStepResult(
                label=step.label, success=False, revert_reason=message,
                infrastructure_error=not _is_revert(message),
            ))
            break
        if int(receipt["status"]) != 1:
            results.append(ForkStepResult(
                label=step.label, success=False, gas_used=int(receipt["gasUsed"]),
                revert_reason="mined with status 0 on the fork (reverted)",
            ))
            break
        results.append(ForkStepResult(label=step.label, success=True, gas_used=int(receipt["gasUsed"])))
    return results


def simulate_sequence(
    fork_url: str, expected_chain_id: int, sender: str, steps: List[ForkStep]
) -> ForkRun:
    """Start a fork, run the steps, and tear it down. Raises ForkUnavailable."""
    with anvil_fork(fork_url, expected_chain_id) as w3:
        run = ForkRun(chain_id=expected_chain_id, fork_url=fork_url)
        run.steps = run_on_fork(w3, sender, steps)
        return run
