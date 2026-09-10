"""
Funding checks that run before anything is searched for, built or signed.

Why this is its own gate
------------------------
Discovery costs money. A run that ends at the simulation gate with "you hold nothing"
has already paid a model to read documentation, and told the user nothing that reading
their balance first would not have. The checks here need no protocol knowledge, no
route and no calldata, so they can run before the first token is spent.

Two balances, both of which can stop a run
------------------------------------------
1. **The sending asset.** The thing the user is actually moving — the question they
   asked. For a native transfer that is the native balance; for an ERC-20 it is the
   token's, read from the contract that `token_registry` resolves the symbol to.
2. **The native balance.** Gas is payable only in the chain's own currency, so a
   wallet holding a million USDC and no ETH still cannot send anything.

Neither substitutes for the other, and both are checked.

The address the token balance is read at comes from a public token list and is
**read-only** — see `token_registry`. It never reaches calldata. The token that gets
approved comes from the model's proposal and is checked separately by
`planner.build_plan`. So a wrong list entry can only make this check less certain,
never make a transaction wrong, and every candidate is corroborated by asking the
contract for its own `symbol()` before its balance is believed.

That is why `FundingStatus` has three values rather than being a bool. A symbol no
list carries is not a token the user has none of, and a check that could not be made
is not a check that passed. Both are reported as themselves.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from web3 import Web3

from omnibridge.core import erc20
from omnibridge.discovery.token_registry import (
    TokenRef,
    TokenRegistry,
    TokenResolutionError,
)

# Native currency on an EVM chain is 18 decimals. That is an EIP-155 invariant rather
# than per-chain knowledge, so relying on it is not the curated table Rule B rules out.
NATIVE_DECIMALS = 18


class FundingStatus(str, Enum):
    """
    Outcome of a funding check.

    UNCHECKED exists for the same reason `SimulationStatus.UNAVAILABLE` does: a
    boolean cannot say "I could not look", and folding that into a pass is how an
    unverified run gets presented as a verified one. Only INSUFFICIENT refuses.
    """

    SUFFICIENT = "SUFFICIENT"      # read off chain, and there is enough
    INSUFFICIENT = "INSUFFICIENT"  # read off chain, and there is not — stop here
    UNCHECKED = "UNCHECKED"        # could not read; not a pass


@dataclass
class FundingReport:
    """What was read, what it means, and what is still unknown."""

    status: FundingStatus
    #: Why it refuses, when it does. None when it does not.
    reason: Optional[str] = None
    native_symbol: str = "ETH"
    #: None means the node did not answer, never zero.
    native_balance_wei: Optional[int] = None
    #: EIP-155 id the symbol is resolved against. 0 means "not supplied", which makes
    #: the ERC-20 leg UNCHECKED rather than resolving against some assumed chain.
    chain_id: int = 0
    #: True when the asset being moved *is* the chain's native currency, in which case
    #: the native read above covers the transfer amount as well as the gas.
    asset_is_native: bool = False

    # ---- the sending asset, when it is an ERC-20 -----------------------------
    #: Symbol as the user named it.
    asset_symbol: str = ""
    #: Contract the balance was read at. Evidence only — this address is never
    #: encoded into a transaction. See `token_registry`.
    asset_address: Optional[str] = None
    #: Read from the contract, not from the token list and not assumed.
    asset_decimals: Optional[int] = None
    #: None means not read, never zero.
    asset_balance_raw: Optional[int] = None
    #: Which token list produced the candidate address.
    asset_source: Optional[str] = None

    #: Statements about what was actually read.
    checked: List[str] = field(default_factory=list)
    #: Checks that could not be made, and why. Rendered so a caller cannot mistake
    #: this report for end-to-end assurance.
    deferred: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the pipeline may continue. UNCHECKED continues; INSUFFICIENT does not."""
        return self.status is not FundingStatus.INSUFFICIENT

    @property
    def native_balance(self) -> Optional[float]:
        if self.native_balance_wei is None:
            return None
        return self.native_balance_wei / (10 ** NATIVE_DECIMALS)

    @property
    def asset_balance(self) -> Optional[float]:
        """The sending asset's balance in human units, or None if it was not read."""
        if self.asset_is_native:
            return self.native_balance
        if self.asset_balance_raw is None or self.asset_decimals is None:
            return None
        return self.asset_balance_raw / (10 ** self.asset_decimals)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "native_symbol": self.native_symbol,
            "native_balance_wei": (
                str(self.native_balance_wei) if self.native_balance_wei is not None else None
            ),
            "native_balance": self.native_balance,
            "asset_is_native": self.asset_is_native,
            "asset_symbol": self.asset_symbol,
            # Labelled so no caller mistakes it for something to transact against.
            "asset_address_read_only": self.asset_address,
            "asset_decimals": self.asset_decimals,
            "asset_balance_raw": (
                str(self.asset_balance_raw) if self.asset_balance_raw is not None else None
            ),
            "asset_balance": self.asset_balance,
            "asset_source": self.asset_source,
            "checked": list(self.checked),
            "deferred": list(self.deferred),
        }


def read_native_balance(w3: Any, address: str) -> Optional[int]:
    """
    Native balance in wei, or None when it could not be read.

    None is not zero. "The node did not answer" and "the wallet is empty" lead to
    opposite actions — the second stops the run, the first must not.
    """
    try:
        return int(w3.eth.get_balance(Web3.to_checksum_address(address)))
    except Exception:
        return None


def preflight(
    w3: Any,
    sender: str,
    native_symbol: str,
    chain_name: str,
    token_symbol: str,
    amount: float,
    chain_id: int = 0,
    registry: Optional[TokenRegistry] = None,
) -> FundingReport:
    """
    Check the sending asset, and the gas needed to move it, before a route exists.

    `w3` is expected to be an identity-checked connection — a balance read from a node
    that is not the chain it claims to be describes some other chain's wallet.

    `chain_id` is what the token symbol is resolved against; without it the ERC-20 leg
    reports UNCHECKED rather than resolving the symbol on some assumed chain.
    """
    symbol = (token_symbol or "").strip().upper()
    native = (native_symbol or "ETH").strip().upper()
    is_native = bool(symbol) and symbol == native

    report = FundingReport(
        native_symbol=native,
        asset_symbol=symbol,
        asset_is_native=is_native,
        chain_id=chain_id,
        status=FundingStatus.UNCHECKED,
    )

    if w3 is None:
        report.reason = None
        report.deferred.append(
            f"No RPC for {chain_name} confirmed its own chain id, so no balance was read. "
            "This is not a pass — the run continues unverified on funding."
        )
        return report

    balance = read_native_balance(w3, sender)
    report.native_balance_wei = balance

    if balance is None:
        report.deferred.append(
            f"The {chain_name} node did not answer eth_getBalance for {sender}. "
            "Treating funding as unknown rather than sufficient."
        )
        return report

    human = balance / (10 ** NATIVE_DECIMALS)
    report.checked.append(f"Native balance on {chain_name}: {human} {native}.")

    # Gas is payable only in the native currency, so an empty wallet cannot send any
    # transaction on this chain — whatever the route turns out to be.
    if balance == 0:
        report.status = FundingStatus.INSUFFICIENT
        report.reason = (
            f"{sender} holds no {native} on {chain_name}. Gas is payable only in "
            f"{native}, so no transaction can be sent from this wallet on this chain — "
            "no route would change that."
        )
        return report

    if is_native:
        needed = int(amount * (10 ** NATIVE_DECIMALS))
        # `<=`, not `<`: a balance exactly equal to the transfer leaves nothing for gas.
        # No gas figure is invented here — only the fact that it is above zero is used.
        if balance <= needed:
            report.status = FundingStatus.INSUFFICIENT
            report.reason = (
                f"{sender} holds {human} {native} on {chain_name} but the transfer is "
                f"{amount} {native}, and gas is payable from the same balance. "
                "No route makes this transfer possible."
            )
            return report
        report.status = FundingStatus.SUFFICIENT
        report.checked.append(
            f"Covers the {amount} {native} transfer with {human - amount} {native} left for gas."
        )
        return report

    # An ERC-20. The native balance above only established that gas is payable; the
    # question the user asked is about the token, so read that too.
    _check_token(report, w3, sender, symbol, amount, chain_name, registry)
    return report


def _check_token(
    report: FundingReport,
    w3: Any,
    sender: str,
    symbol: str,
    amount: float,
    chain_name: str,
    registry: Optional[TokenRegistry],
) -> None:
    """
    Read the sending token's balance, or say precisely why it could not be read.

    Every failure short of "the contract answered and the balance is too low" is
    UNCHECKED. A symbol no public list carries is a gap in the lists; it is not
    evidence that the user holds none of it, and refusing on that would block real
    transfers of every token outside the top few thousand.
    """
    if not report.chain_id:
        report.status = FundingStatus.UNCHECKED
        report.deferred.append(
            f"{symbol} balance not checked — no chain id was supplied to resolve the "
            "symbol against, and resolving it on an assumed chain would read some "
            "other chain's contract."
        )
        return

    registry = registry if registry is not None else _default_registry()

    try:
        ref: TokenRef = registry.resolve(report.chain_id, symbol)
    except TokenResolutionError as exc:
        report.status = FundingStatus.UNCHECKED
        report.deferred.append(
            f"{symbol} balance not checked — {exc} The token balance is still read "
            "against the chain in build_plan, before any step is offered for signing."
        )
        return

    # Corroborate the list against the contract itself before believing anything read
    # from it. A list entry can be stale, or right for a different chain; the contract
    # calling itself something else is the tell, and it is one eth_call to find out.
    on_chain_symbol = erc20.read_symbol(w3, ref.address)
    if on_chain_symbol is None or on_chain_symbol.strip().upper() != symbol:
        report.status = FundingStatus.UNCHECKED
        report.deferred.append(
            f"{symbol} balance not checked — {ref.source} names {ref.address} for "
            f"{symbol} on {chain_name}, but the contract calls itself "
            f"{on_chain_symbol or 'nothing readable'}. Not trusting a balance read "
            "against an address the chain does not corroborate."
        )
        return

    # The contract's own decimals, not the list's and not a guess. This is the factor
    # between sending 25 USDC and sending 25 millionths of one.
    decimals = erc20.read_decimals(w3, ref.address)
    if decimals is None:
        report.status = FundingStatus.UNCHECKED
        report.deferred.append(
            f"{symbol} balance not checked — {ref.address} did not answer decimals(), "
            "so the amount cannot be scaled to base units without guessing."
        )
        return

    balance = erc20.read_balance(w3, ref.address, sender)
    if balance is None:
        report.status = FundingStatus.UNCHECKED
        report.deferred.append(
            f"{symbol} balance not checked — {ref.address} did not answer balanceOf()."
        )
        return

    report.asset_address = ref.address
    report.asset_decimals = decimals
    report.asset_balance_raw = balance
    report.asset_source = ref.source

    human = balance / (10 ** decimals)
    needed = int(amount * (10 ** decimals))
    report.checked.append(
        f"{symbol} balance on {chain_name}: {human} (read from {ref.address}, "
        f"corroborated by its own symbol(), {decimals} decimals)."
    )

    if balance < needed:
        report.status = FundingStatus.INSUFFICIENT
        report.reason = (
            f"{sender} holds {human} {symbol} on {chain_name} but the transfer is "
            f"{amount} {symbol}. No route makes this transfer possible."
        )
        return

    report.status = FundingStatus.SUFFICIENT


_REGISTRY_SINGLETON: Optional[TokenRegistry] = None


def _default_registry() -> TokenRegistry:
    """One registry per process, so the lists are fetched once rather than per request."""
    global _REGISTRY_SINGLETON
    if _REGISTRY_SINGLETON is None:
        _REGISTRY_SINGLETON = TokenRegistry()
    return _REGISTRY_SINGLETON
