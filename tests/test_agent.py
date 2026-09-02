import pytest

from omnibridge.core.models import UserIntent
from omnibridge.core.agent import OmnibridgeAgent
from omnibridge.discovery.route_finder import RouteFinder


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
    No router address is obtainable today: web search yields none for this query and
    the skill repository holds no verified route. Discovery must therefore fail loudly
    rather than return a route built from a guess.

    This asserts the guardrail, not the plumbing, so it should survive Phase 3 — it
    will simply stop being reached once the agent can genuinely find a route, and will
    still hold for pairs where no bridge exists.
    """
    finder = RouteFinder()
    with pytest.raises(Exception) as excinfo:
        finder.discover_route(UserIntent(**BASE_TO_ARBITRUM))

    message = str(excinfo.value).lower()
    assert "route discovery error" in message or "rpc discovery error" in message


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
