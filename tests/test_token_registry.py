"""
Symbol to address, and the boundary that makes a third-party list safe to use.

The property that matters most is negative: an address resolved here is read-only. It
is used to call `balanceOf` and nothing else, and if it were ever allowed to reach
calldata a stale token list would become a way to make the system approve the wrong
contract. `test_a_resolved_address_never_reaches_the_transaction` asserts that the
report field carrying it is named for what it is and that nothing in the encoding path
reads it.
"""

import json

import pytest

from omnibridge.core.balance import FundingStatus, preflight
from omnibridge.discovery.token_registry import (
    AmbiguousToken,
    TokenRegistry,
    UnknownToken,
)

WALLET = "0x060477185Be90701b825b80f827070ef0DA69c5E"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class StubRegistry(TokenRegistry):
    """A registry over a literal token list, so no test touches the network."""

    def __init__(self, tokens):
        super().__init__(urls=["stub://list"])
        self._lists["stub://list"] = tokens


BASE_LIST = [
    {"chainId": 8453, "symbol": "USDC", "address": USDC_BASE, "decimals": 6},
    {"chainId": 1, "symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
]


def test_a_symbol_resolves_on_the_right_chain():
    ref = StubRegistry(BASE_LIST).resolve(8453, "usdc")
    assert ref.address == USDC_BASE
    assert ref.decimals == 6
    assert ref.chain_id == 8453


def test_the_same_symbol_on_another_chain_is_a_different_contract():
    reg = StubRegistry(BASE_LIST)
    assert reg.resolve(8453, "USDC").address != reg.resolve(1, "USDC").address


def test_an_unlisted_symbol_raises_rather_than_returning_a_guess():
    with pytest.raises(UnknownToken) as exc:
        StubRegistry(BASE_LIST).resolve(8453, "NOTATOKEN")
    # The message must not read as a statement about the balance.
    assert "gap in the lists, not evidence about the balance" in str(exc.value)


def test_a_symbol_on_an_unlisted_chain_is_unknown_not_wrong():
    with pytest.raises(UnknownToken):
        StubRegistry(BASE_LIST).resolve(999999, "USDC")


def test_two_contracts_claiming_one_symbol_are_refused_not_picked():
    """Picking one is how a balance gets read against the wrong contract."""
    ambiguous = BASE_LIST + [
        {"chainId": 8453, "symbol": "USDC", "address": "0x" + "ab" * 20, "decimals": 6}
    ]
    with pytest.raises(AmbiguousToken) as exc:
        StubRegistry(ambiguous).resolve(8453, "USDC")
    assert "Refusing to pick one" in str(exc.value)


def test_an_unreachable_list_is_reported_not_swallowed():
    reg = TokenRegistry(urls=["http://127.0.0.1:1/nope.json"])
    with pytest.raises(UnknownToken):
        reg.resolve(8453, "USDC")
    assert reg.failures, "the fetch failure should be recorded, not silently dropped"


# ----------------------------------------------------- the balance check itself


class FakeEth:
    def __init__(self, native, calls):
        self._native, self._calls = native, calls

    def get_balance(self, address):
        return self._native

    def call(self, tx):
        selector = tx["data"][:10]
        if selector not in self._calls:
            raise ValueError("no answer")
        return self._calls[selector]


class FakeChain:
    """Answers symbol(), decimals() and balanceOf() with whatever a test supplies."""

    def __init__(self, native=10 ** 18, symbol="USDC", decimals=6, balance=0):
        from eth_abi import encode

        calls = {}
        if symbol is not None:
            calls["0x95d89b41"] = encode(["string"], [symbol])
        if decimals is not None:
            calls["0x313ce567"] = encode(["uint8"], [decimals])
        if balance is not None:
            calls["0x70a08231"] = encode(["uint256"], [balance])
        self.eth = FakeEth(native, calls)


def check(**kw):
    chain = kw.pop("chain")
    return preflight(
        chain, WALLET, native_symbol="ETH", chain_name="Base",
        token_symbol=kw.pop("symbol", "USDC"), amount=kw.pop("amount", 25.0),
        chain_id=8453, registry=StubRegistry(BASE_LIST),
    )


def test_the_sending_token_balance_is_what_decides():
    """Plenty of ETH for gas, not enough USDC to send. The transfer is the question."""
    report = check(chain=FakeChain(native=10 ** 18, balance=10 * 10 ** 6), amount=25.0)
    assert report.status is FundingStatus.INSUFFICIENT
    assert "holds 10.0 USDC" in report.reason
    assert report.asset_balance == 10.0


def test_enough_of_the_token_and_enough_for_gas_passes():
    report = check(chain=FakeChain(native=10 ** 18, balance=100 * 10 ** 6), amount=25.0)
    assert report.status is FundingStatus.SUFFICIENT
    assert report.asset_balance == 100.0
    assert report.asset_decimals == 6
    assert not report.deferred


def test_decimals_come_from_the_contract_not_from_the_list():
    """
    A token that reports 18 decimals is scaled by 18 even where the list said 6.

    Scaling by the wrong factor is the difference between "you hold 25" and "you hold
    0.000000000000025", and it would refuse every transfer of such a token.
    """
    report = check(chain=FakeChain(decimals=18, balance=100 * 10 ** 18), amount=25.0)
    assert report.asset_decimals == 18
    assert report.asset_balance == 100.0
    assert report.status is FundingStatus.SUFFICIENT


def test_a_contract_that_calls_itself_something_else_is_not_believed():
    """
    The list's claim, corroborated against the chain.

    A stale or wrong entry points at a real contract with a real balance. Reading it
    would produce a confident, wrong answer, so the mismatch downgrades to UNCHECKED.
    """
    report = check(chain=FakeChain(symbol="WBTC", balance=10 ** 18))
    assert report.status is FundingStatus.UNCHECKED
    assert report.asset_balance is None
    assert any("calls itself WBTC" in d for d in report.deferred)


def test_a_token_no_list_carries_is_unchecked_not_refused():
    report = check(chain=FakeChain(balance=0), symbol="NOTATOKEN")
    assert report.status is FundingStatus.UNCHECKED
    # Never INSUFFICIENT: not knowing the address says nothing about the balance.
    assert report.ok


def test_a_zero_token_balance_is_refused_when_it_was_actually_read():
    report = check(chain=FakeChain(balance=0), amount=25.0)
    assert report.status is FundingStatus.INSUFFICIENT
    assert "holds 0.0 USDC" in report.reason


def test_no_gas_is_refused_even_holding_plenty_of_the_token():
    """Gas is payable only in the native currency; the token cannot buy it."""
    report = check(chain=FakeChain(native=0, balance=10 ** 12), amount=25.0)
    assert report.status is FundingStatus.INSUFFICIENT
    assert "Gas is payable only in ETH" in report.reason


def test_without_a_chain_id_the_symbol_is_not_resolved_against_an_assumed_chain():
    report = preflight(
        FakeChain(balance=10 ** 12), WALLET, native_symbol="ETH", chain_name="Base",
        token_symbol="USDC", amount=25.0, registry=StubRegistry(BASE_LIST),
    )
    assert report.status is FundingStatus.UNCHECKED
    assert any("no chain id was supplied" in d for d in report.deferred)


def test_a_resolved_address_never_reaches_the_transaction(code_only):
    """
    The boundary that makes a public token list an acceptable dependency.

    A resolved address is evidence for a balance read and nothing else. If the
    encoding path ever consumed it, a stale list entry would become a way to make the
    system approve the wrong contract.
    """
    report = check(chain=FakeChain(balance=10 ** 12))
    body = report.as_dict()
    # Named for what it is, so no caller picks it up by accident.
    assert body["asset_address_read_only"] == USDC_BASE
    assert "asset_address" not in body

    for module in (
        "omnibridge/agent/synthesis.py",
        "omnibridge/core/planner.py",
        "omnibridge/core/executor.py",
    ):
        source = code_only(module)
        assert "token_registry" not in source, f"{module} must not resolve tokens itself"
        assert "asset_address" not in source, f"{module} must not read the preflight address"
