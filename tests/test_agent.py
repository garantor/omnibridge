from pathlib import Path

import pytest

from omnibridge.core.models import UserIntent
from omnibridge.core.agent import OmnibridgeAgent
from omnibridge.discovery.route_finder import NoCachedRoute, RouteFinder


BASE_TO_ARBITRUM = dict(
    token="USDC",
    amount=10.0,
    source_chain="base",
    dest_chain="arbitrum",
    recipient="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    max_slippage_bps=50,
)


def test_discovery_refuses_rather_than_inventing_a_router():
    """
    The skill repository holds no verified route for this pair, so RouteFinder must say
    so rather than return one built from a guess.

    Before Phase 4 it fell through to scraping search-result text and returned the first
    `0x…` string it matched as the router contract. The refusal is the guardrail; the
    exception type changed when that path was deleted, so this asserts the behaviour
    rather than the wording.
    """
    finder = RouteFinder()
    with pytest.raises(NoCachedRoute) as excinfo:
        finder.discover_route(UserIntent(**BASE_TO_ARBITRUM))

    assert "has to be discovered" in str(excinfo.value)


def test_route_finder_cannot_scrape_the_web_at_all():
    """
    The deletion, asserted structurally. A refusal that can be reintroduced by one
    `except: pass` is not a guardrail, so the capability is gone, not disabled.
    """
    import omnibridge.discovery.route_finder as rf

    assert not hasattr(rf.RouteFinder, "_query_online_route_manifest")
    source = Path(rf.__file__).read_text()
    assert "web_search_tool" not in source
    assert "re.findall" not in source


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Requires a real router address, which no source can currently supply: "
        "web_search_tool returns a fabricated hit containing no addresses (plan 1.2) "
        "and the skill repository is empty. Hardcoding one here would be the curated "
        "router table Rule B forbids. Expected to pass at plan Phase 3, once the agent "
        "can read documentation. Remove this marker when it does."
    ),
)
def test_agent_in_transit_flow():
    agent = OmnibridgeAgent()
    intent = UserIntent(**BASE_TO_ARBITRUM)

    # Execute with auto-approval in dry-run mode
    result = agent.run_flow(
        intent=intent,
        sender_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        auto_approve=True,
    )

    assert result["status"] == "success"
    assert "tx_hash" in result["receipt"]
    assert result["manifest"]["token_symbol"] == "USDC"
    assert result["manifest"]["amount_in"] == 10.0
    assert result["skill_file"].endswith(".md")
