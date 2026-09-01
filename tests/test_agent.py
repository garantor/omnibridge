import pytest
from omnibridge.core.models import UserIntent
from omnibridge.core.agent import OmnibridgeAgent


def test_agent_in_transit_flow():
    agent = OmnibridgeAgent()
    intent = UserIntent(
        token="USDC",
        amount=10.0,
        source_chain="base",
        dest_chain="arbitrum",
        recipient="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        max_slippage_bps=50,
    )

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
