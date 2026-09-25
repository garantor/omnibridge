"""
"Always use the latest version", and testnet runs that cannot touch mainnet.

Latest version is enforced in code at every place an older version could slip in:
card selection, route matching, proposals, and cached-skill replay. Network is carried
through the whole run and decided from chain-registry data, never from a name.
"""

import tempfile

import pytest

from omnibridge.cards import markdown
from omnibridge.cards.context import select_cards
from omnibridge.cards.manager import CardManager
from omnibridge.cards.models import Card
from omnibridge.cards.proposals import apply_patch
from omnibridge.cards.writeback import match_route
from tests.test_cards import ARB, DOCS, DOMAINS, TX, router_w3

V2 = "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d"
V2_TESTNET = "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"
V2_SIG = "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"


def _card(pid, version, network="mainnet", rows=(), sig="depositForBurn(uint256,uint32,bytes32,address)"):
    return Card(
        protocol_id=pid, protocol_name="Circle CCTP", network=network,
        protocol_family="cctp", protocol_version=version,
        docs=["https://developers.circle.com/llms.txt"], supported_tokens=["USDC"],
        interfaces=[dict(label="Deposit For Burn", signature=sig)],
        dest_id_label="CCTP Domain", address_labels=["Token Messenger"], rows=list(rows),
    )


V1_ROW = dict(network="Arbitrum One", chain_id=42161, dest_id="3", addresses={"token_messenger": ARB},
              status="verified", verified_date="2026-09-11", evidence=TX)


@pytest.fixture
def mgr():
    with tempfile.TemporaryDirectory() as tmp:
        m = CardManager(repository_path=tmp)
        m.save_card(_card("cctp", "v1", rows=[V1_ROW]))
        m.save_card(_card("cctp_v2", "v2", sig=V2_SIG))
        m.save_card(_card("cctp_v2_testnet", "v2", network="testnet", sig=V2_SIG))
        yield m


# ------------------------------------------------------------- latest version

def test_a_newer_version_supersedes_an_older_one_on_the_same_network(mgr):
    assert mgr.superseded_by(mgr.load("cctp").card).protocol_id == "cctp_v2"
    assert mgr.superseded_by(mgr.load("cctp_v2").card) is None
    # The testnet card does not supersede a mainnet one, or vice versa.
    assert mgr.superseded_by(mgr.load("cctp_v2_testnet").card) is None


def test_only_the_latest_card_is_offered_even_before_it_has_rows(mgr):
    offered = select_cards(mgr, "USDC", 42161, "arbitrum", "base", 8453, network="mainnet")
    assert [c.protocol_id for c in offered] == ["cctp_v2"]


def test_a_route_through_a_superseded_card_is_identified(mgr):
    match = match_route(mgr, 42161, ARB, 8453, {"scheme": "cctp_domain", "value": "6"})
    assert match.protocol_id == "cctp" and match.superseded_by == "cctp_v2"


def test_rows_cannot_be_added_to_a_superseded_card(mgr):
    patch = dict(kind="chain_row", network="Base", chain_id=8453, dest_id="6",
                 addresses={"Token Messenger": ARB})
    result = apply_patch(mgr, "cctp", patch, [DOCS], lambda _c: router_w3(), DOMAINS)
    assert not result.accepted and "superseded" in result.reasons[0]


def test_a_new_card_must_be_a_newer_version(mgr):
    patch = dict(kind="new_card", protocol_name="Circle CCTP", protocol_family="cctp",
                 protocol_version="v2", network="mainnet", docs=["https://developers.circle.com/llms.txt"],
                 supported_tokens=["USDC"], dest_id_label="CCTP Domain", address_labels=["Token Messenger"])
    same = apply_patch(mgr, "cctp_v2_again", patch, [], lambda _c: None, DOMAINS)
    assert not same.accepted and "newer version" in same.reasons[0]
    newer = apply_patch(mgr, "cctp_v3", {**patch, "protocol_version": "v3"}, [], lambda _c: None, DOMAINS)
    assert newer.accepted
    assert mgr.superseded_by(mgr.load("cctp_v2").card).protocol_id == "cctp_v3"


def test_version_and_network_round_trip_through_the_file(mgr):
    card = mgr.load("cctp_v2_testnet").card
    assert markdown.parse(markdown.render(card)) == card
    with pytest.raises(ValueError, match="network must be"):
        _card("x", "v1", network="devnet")
    with pytest.raises(ValueError, match="look like v1"):
        _card("x", "2.0")


def test_a_superseded_skill_is_not_replayed(monkeypatch, mgr):
    import types

    import graph

    monkeypatch.setattr(graph, "card_mgr", mgr)
    v1_skill = types.SimpleNamespace(metadata=types.SimpleNamespace(source_chain_id=42161, router_address=ARB))
    other = types.SimpleNamespace(metadata=types.SimpleNamespace(source_chain_id=42161, router_address=V2))
    assert graph._superseded_skill(v1_skill) == "cctp_v2"
    assert graph._superseded_skill(other) is None


# ------------------------------------------------------------------ networks

def test_a_testnet_run_is_only_offered_testnet_cards(mgr):
    offered = select_cards(mgr, "USDC", 421614, "arbitrum sepolia", "base sepolia", 84532, network="testnet")
    assert [c.protocol_id for c in offered] == ["cctp_v2_testnet"]


def test_a_mainnet_chain_cannot_be_written_to_a_testnet_card(mgr):
    families = {42161: "mainnet", 421614: "testnet"}
    patch = dict(kind="chain_row", network="Arbitrum One", chain_id=42161, dest_id="3",
                 addresses={"Token Messenger": V2})
    wrong = apply_patch(mgr, "cctp_v2_testnet", patch, [DOCS], lambda _c: router_w3(), DOMAINS,
                        network_for=families.get)
    assert not wrong.accepted and "mainnet chain; this is a testnet card" in wrong.reasons[0]


def test_a_chain_the_registry_does_not_know_is_refused(mgr):
    patch = dict(kind="chain_row", network="Mystery", chain_id=999999, addresses={"Token Messenger": V2})
    result = apply_patch(mgr, "cctp_v2", patch, [DOCS], lambda _c: router_w3(), DOMAINS,
                         network_for=lambda _c: None)
    assert not result.accepted and "not in the chain registry" in result.reasons[0]


def test_hyphenated_testnet_names_reach_the_resolver_as_two_words():
    import graph

    out = graph.intent_parser_node({
        "raw_input": "Bridge 1 USDC from arbitrum-sepolia to base-sepolia",
        "user_address": "0x" + "ab" * 20, "network": "testnet", "traces": {},
    })
    assert (out["source_chain"], out["target_chain"]) == ("arbitrum sepolia", "base sepolia")


def test_an_unknown_network_is_refused_not_defaulted():
    import graph

    with pytest.raises(ValueError, match="network must be"):
        graph.intent_parser_node({"raw_input": "Bridge 1 USDC from base to arbitrum",
                                  "user_address": "0x" + "ab" * 20, "network": "devnet", "traces": {}})


def test_the_api_carries_the_network_into_the_run(monkeypatch):
    from fastapi.testclient import TestClient

    import graph
    import server

    from omnibridge.discovery.rpc_manager import UnresolvedChainError

    seen = {}

    def resolve(name, network="mainnet", **_):
        seen["network"] = network
        raise UnresolvedChainError("stop here")

    monkeypatch.setattr(graph.rpc_mgr, "discover_chain_metadata", resolve)
    res = TestClient(server.server_app).post("/api/preflight", json={
        "userAddress": "0x" + "ab" * 20, "sourceChain": "arbitrum sepolia", "network": "testnet"})
    assert res.status_code == 404 and seen["network"] == "testnet"


# --------------------------------------------------------------------- seeds

def test_the_seeded_cards_put_cctp_v2_in_charge_on_both_networks():
    from pathlib import Path

    mgr = CardManager(repository_path=str(Path(__file__).resolve().parents[1] / "protocols"))
    v1, v2, v2t = (mgr.load(p).card for p in ("cctp", "cctp_v2", "cctp_v2_testnet"))
    assert mgr.superseded_by(v1).protocol_id == "cctp_v2"
    assert (v2.network, v2.protocol_version, v2t.network) == ("mainnet", "v2", "testnet")
    assert v2.interfaces[0].signature == V2_SIG == v2t.interfaces[0].signature
    # Seeded with no rows; runs add them. Whatever they add must have got there the
    # proper way: code-checked, cited, and verified only by a broadcast.
    for card in (v2, v2t):
        for row in card.rows:
            assert row.evidence, f"{card.protocol_id}:{row.chain_id} has no evidence"
            if row.status.value == "verified":
                assert row.evidence.startswith("0x"), "verified only by a broadcast"


# ------------------------------------------------------------- the resolver

_REGISTRY = [
    {"chainId": 42161, "name": "Arbitrum One", "shortName": "arb1", "rpc": ["https://a"]},
    {"chainId": 11155111, "name": "Ethereum Sepolia", "title": "Ethereum Testnet Sepolia",
     "shortName": "sep", "rpc": ["https://s"]},
]


@pytest.fixture
def resolver(monkeypatch):
    from omnibridge.discovery.rpc_manager import RPCManager

    r = RPCManager()
    monkeypatch.setattr(r, "_fetch_registry", lambda: _REGISTRY)
    return r


def test_a_hyphenated_name_resolves_as_the_spaced_name(resolver):
    """Found in the UI: the funding check sent 'ethereum-sepolia' raw, which matched nothing."""
    assert resolver.discover_chain_metadata("ethereum-sepolia", network="testnet").chain_id == 11155111


def test_a_name_on_the_other_network_is_still_refused_but_says_how_to_fix_it(resolver):
    from omnibridge.discovery.rpc_manager import UnresolvedChainError

    with pytest.raises(UnresolvedChainError, match="switch the network to Testnet"):
        resolver.discover_chain_metadata("ethereum-sepolia", network="mainnet")
    with pytest.raises(UnresolvedChainError, match="switch the network to Mainnet"):
        resolver.discover_chain_metadata("arbitrum one", network="testnet")


def test_an_unknown_name_gets_no_misleading_hint(resolver):
    from omnibridge.discovery.rpc_manager import UnresolvedChainError

    with pytest.raises(UnresolvedChainError) as exc:
        resolver.discover_chain_metadata("nowhere-chain", network="mainnet")
    assert "switch the network" not in str(exc.value)
