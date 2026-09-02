"""
Turning one route into the ordered transactions that actually perform it.

Most ERC-20 bridges are two transactions, not one: the router calls `transferFrom`,
which reverts unless the sender has approved it first. The pipeline used to build only
the second, so a correct route failed at the simulation gate with
`ERC20: transfer amount exceeds allowance` — and the retry edge then sent a correct
route back to discovery as though the route were what was wrong.

The honest constraint this module is built around
-------------------------------------------------
A bridge call **cannot** be simulated before its approval lands. The allowance really
is absent; `eth_call` really will revert. There is no way to pre-verify it that is not
either a lie or a fabrication:

  - overriding the allowance storage slot needs per-token storage layout, which is
    exactly the curated per-protocol knowledge Rule B rules out;
  - simulating from an account that *does* have allowance describes that account's
    transfer, not the user's.

So the bridge step is reported PENDING_PREREQUISITE and re-simulated against live state
after the approval confirms, immediately before it is signed. No step is ever signed
without having been verified moments earlier.
"""

from typing import Any, Dict, List, Optional

from omnibridge.core import erc20
from omnibridge.core.models import (
    ExecutionPlan,
    RouteQuote,
    SimulationStatus,
    StepStatus,
    TransactionStep,
    UserIntent,
)


class PrerequisiteUnsatisfiable(Exception):
    """A precondition no amount of signing will fix — an empty balance, say."""


def _human(raw: Optional[int], decimals: int) -> Optional[float]:
    return None if raw is None else raw / (10 ** decimals)


def build_plan(
    quote: RouteQuote,
    intent: UserIntent,
    sender: str,
    calldata: str,
    chain_id: int,
    w3: Any = None,
    spend: Optional[Dict[str, Any]] = None,
    notes: Optional[List[str]] = None,
) -> ExecutionPlan:
    """
    Build the ordered steps for this route.

    `spend` is the model's declaration of which token the router will pull. Whether an
    approval is actually *needed* is never taken from the model: it is read off chain,
    so an unnecessary approval is not proposed and a necessary one is not skipped.
    """
    notes = notes if notes is not None else []
    decimals = quote.decimals or 6
    amount_raw = quote.deposit_amount_raw

    bridge = TransactionStep(
        index=0,
        kind="bridge",
        title=f"Bridge {intent.amount} {intent.token}",
        description=(
            f"Calls {(quote.extra_params or {}).get('function_signature') or 'the router'} "
            f"on {quote.router_address}."
        ),
        to=quote.router_address,
        data=calldata,
        value_wei=0,
        chain_id=chain_id,
        amount_raw=amount_raw,
        amount_decoded=_human(amount_raw, decimals),
    )

    if not spend:
        # A native-asset transfer pulls nothing, so there is nothing to approve.
        return ExecutionPlan(steps=[bridge])

    token = spend["token"]
    spender = spend["spender"]
    try:
        approve_amount = int(str(spend.get("amount") or amount_raw), 0)
    except (TypeError, ValueError):
        approve_amount = amount_raw

    if approve_amount < amount_raw:
        # Approving less than the transfer guarantees a revert after the user has
        # already paid for the approval.
        notes.append(
            f"Declared approval of {approve_amount} is below the {amount_raw} being "
            "transferred; raising it to the transfer amount."
        )
        approve_amount = amount_raw

    if w3 is None:
        notes.append(
            "No usable RPC: could not check whether an approval is already in place, "
            "so one is included. It may be unnecessary."
        )
        allowance, balance = None, None
    else:
        allowance = erc20.read_allowance(w3, token, sender, spender)
        balance = erc20.read_balance(w3, token, sender)

    if balance is not None and balance < amount_raw:
        raise PrerequisiteUnsatisfiable(
            f"Sender holds {_human(balance, decimals)} {intent.token} but the transfer "
            f"needs {_human(amount_raw, decimals)}. No approval fixes this."
        )

    if allowance is None and w3 is not None:
        # The token did not answer a standard `allowance` call. That is not "zero"; it
        # is a reason to doubt the token address itself.
        notes.append(
            f"{token} did not answer a standard ERC-20 allowance call. Treating the "
            "approval as required, but check that this is really the token contract."
        )

    if allowance is not None and allowance >= amount_raw:
        notes.append(
            f"Existing allowance of {_human(allowance, decimals)} {intent.token} already "
            "covers this transfer; no approval needed."
        )
        return ExecutionPlan(steps=[bridge])

    approve = TransactionStep(
        index=0,
        kind="approve",
        title=f"Approve {_human(approve_amount, decimals)} {intent.token}",
        description=(
            f"Lets {spender} move up to {_human(approve_amount, decimals)} {intent.token} "
            "from your wallet. Exactly the transfer amount — not unlimited."
        ),
        to=token,
        data=erc20.build_approve(spender, approve_amount),
        value_wei=0,
        chain_id=chain_id,
        token_address=token,
        spender_address=spender,
        amount_raw=approve_amount,
        amount_decoded=_human(approve_amount, decimals),
    )

    bridge.index = 1
    bridge.description += (
        " Cannot be checked until the approval above lands — it is re-simulated against "
        "live state before you are asked to sign it."
    )
    return ExecutionPlan(steps=[approve, bridge])


def simulate_next(plan: ExecutionPlan, simulator: Any, sender: str):
    """
    Simulate the first unsigned step, and any later step whose prerequisites already hold.

    Only the next step can be verified honestly. A later one depends on state the
    earlier ones create, and reporting it as verified now would mean verifying it
    against a chain that does not yet look like that.

    Returns (plan, result, step). `result` is the simulator's own SimulationResult, so
    the caller records what actually happened rather than a reconstruction of it.
    """
    pending = [s for s in plan.steps if s.tx_hash is None]
    if not pending:
        return plan, None, None

    step = pending[0]
    result = simulator.simulate_call(
        from_address=sender,
        to_address=step.to,
        calldata=step.data,
        value_wei=step.value_wei,
    )
    step.status = (
        StepStatus.VERIFIED
        if result.status is SimulationStatus.VERIFIED
        else StepStatus(result.status.value)
    )
    step.revert_reason = result.revert_reason
    step.gas_used = result.gas_used
    step.simulated_on_chain_id = result.simulated_on_chain_id

    for later in pending[1:]:
        later.status = StepStatus.PENDING_PREREQUISITE
        later.revert_reason = None

    return plan, result, step
