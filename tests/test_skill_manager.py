import tempfile
from pathlib import Path
from omnibridge.skills.manager import SkillManager, SkillMetadata


def test_skill_manager_load_and_save():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = SkillManager(repository_path=tmpdir)

        metadata = SkillMetadata(
            name="test_eth_to_base_usdc",
            description="Test description",
            source_chain="ethereum",
            source_chain_id=1,
            dest_chain="base",
            dest_chain_id=8453,
            token="USDC",
            bridge_protocol="Circle CCTP",
            router_address="0xbd3fa81b58ba92d82136038b25adec7066af3155",
            cctp_destination_domain=6,
        )

        body = "# Execution steps\nRun calldata"
        saved_path = sm.save_skill(metadata, body)
        assert Path(saved_path).exists()

        # Retrieve skill
        found = sm.find_skill("ethereum", "base", "USDC")
        assert found is not None
        assert found.metadata.name == "test_eth_to_base_usdc"
        assert found.metadata.source_chain_id == 1
        assert found.metadata.dest_chain_id == 8453
        assert "Execution steps" in found.content
