"""
The preflight funding gate.

Two properties matter more than the happy path:

  - an unread balance is UNCHECKED, never SUFFICIENT. "The node did not answer" and
    "the wallet has money" lead to opposite actions, and collapsing them is how an
    unverified run gets presented as a verified one;
  - the ERC-20 case reports what it did *not* check. A native-balance pass says
    nothing about the token the transfer actually moves.
"""

import pytest

from omnibridge.core.balance import (
    NATIVE_DECIMALS,
    FundingStatus,
    preflight,
    read_native_balance,
)

WALLET = "0x060477185Be90701b825b80f827070ef0DA69c5E"
ONE_ETH = 10 ** NATIVE_DECIMALS


class FakeEth:
    def __init__(self, balance):
        self._balance = balance

    def get_balance(self, address):
        if self._balance is None:
            raise ConnectionError("node did not answer")
        return self._balance


class FakeChain:
    def __init__(self, balance):
        self.eth = FakeEth(balance)


def check(balance, token="USDC", amount=25.0, native="ETH"):
    """A preflight against a node that answers `balance`; None means it does not."""
    return preflight(
        FakeChain(balance),
        WALLET,
        native_symbol=native,
        chain_name="Base",
        token_symbol=token,
        amount=amount,
    )


def test_no_rpc_is_unchecked_not_a_pass():
    report = preflight(
        None, WALLET, native_symbol="ETH", chain_name="Base", token_symbol="USDC", amount=25.0
    )
    assert report.status is FundingStatus.UNCHECKED
    assert report.native_balance_wei is None
    # It continues — an unreachable node is not evidence of an empty wallet — but it
    # must not claim the balance was fine.
    assert report.ok
    assert report.deferred and "not a pass" in " ".join(report.deferred)


def test_a_node_that_does_not_answer_is_unchecked():
    report = check(balance=None)
    assert report.status is FundingStatus.UNCHECKED
    assert report.native_balance_wei is None
    assert report.ok


def test_read_native_balance_returns_none_rather_than_zero_on_failure():
    assert read_native_balance(FakeChain(None), WALLET) is None
    assert read_native_balance(FakeChain(0), WALLET) == 0


def test_an_empty_wallet_is_refused_before_discovery():
    report = check(balance=0)
    assert report.status is FundingStatus.INSUFFICIENT
    assert not report.ok
    # Says why no route would help, rather than only that it stopped.
    assert "Gas is payable only in ETH" in report.reason


def test_an_erc20_without_a_chain_id_reports_the_token_leg_unchecked():
    """
    The native read alone is not an answer about the token.

    `check` supplies no chain id, so the symbol cannot be resolved — and the report
    says so rather than letting a passing gas check stand in for a balance it never
    read. The resolved path is covered in test_token_registry.py.
    """
    report = check(balance=ONE_ETH, token="USDC", amount=25.0)
    assert report.status is FundingStatus.UNCHECKED
    assert report.asset_is_native is False
    assert report.asset_balance is None
    assert any("no chain id was supplied" in d for d in report.deferred)


def test_a_native_transfer_larger_than_the_balance_is_refused():
    report = check(balance=ONE_ETH, token="ETH", amount=2.0)
    assert report.status is FundingStatus.INSUFFICIENT
    assert report.asset_is_native is True
    assert "No route makes this transfer possible" in report.reason


def test_a_native_transfer_that_leaves_nothing_for_gas_is_refused():
    # Exactly the balance. The transfer alone fits; the gas it costs does not, and
    # that is knowable without inventing a gas figure.
    report = check(balance=ONE_ETH, token="ETH", amount=1.0)
    assert report.status is FundingStatus.INSUFFICIENT
    assert "gas is payable from the same balance" in report.reason


def test_a_native_transfer_with_headroom_passes():
    report = check(balance=3 * ONE_ETH, token="ETH", amount=1.0)
    assert report.status is FundingStatus.SUFFICIENT
    assert report.asset_is_native is True
    assert not report.deferred


def test_the_native_symbol_is_taken_from_the_chain_not_assumed_to_be_eth():
    report = check(balance=3 * ONE_ETH, token="POL", amount=1.0, native="POL")
    assert report.asset_is_native is True
    assert report.native_symbol == "POL"


def test_as_dict_reports_the_status_by_name():
    body = check(balance=0).as_dict()
    assert body["status"] == "INSUFFICIENT"
    # Serialised as a string: a wei figure exceeds what JSON consumers handle safely.
    assert body["native_balance_wei"] == "0"
