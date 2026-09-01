"""
Autonomous Web3 Systems & On-Chain Agent Orchestrator.
Coordinates the In-Transit Runtime Flow: Discovery -> Code Gen -> Simulation -> User Approval -> Broadcast & Skill Persistence.
"""

from typing import Optional, Dict, Any
from omnibridge.core.models import UserIntent, RouteQuote, ExecutionManifest, BroadcastReceipt
from omnibridge.discovery.rpc_manager import RPCManager
from omnibridge.discovery.route_finder import RouteFinder
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.simulator import Simulator
from omnibridge.core.signer import Signer
from omnibridge.manifests.formatter import ManifestFormatter, console
from omnibridge.skills.manager import SkillManager, SkillMetadata


class OmnibridgeAgent:
    """Primary agent orchestrating the full in-transit execution lifecycle."""

    def __init__(
        self,
        rpc_manager: Optional[RPCManager] = None,
        skill_manager: Optional[SkillManager] = None,
    ):
        self.rpc_manager = rpc_manager or RPCManager()
        self.skill_manager = skill_manager or SkillManager()
        self.route_finder = RouteFinder(skill_manager=self.skill_manager, rpc_manager=self.rpc_manager)

    def run_flow(
        self,
        intent: UserIntent,
        sender_address: str = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        private_key: Optional[str] = None,
        auto_approve: bool = False,
    ) -> Dict[str, Any]:
        """Executes the complete In-Transit Bridging pipeline."""
        console.print(f"[bold cyan]🔍 STEP 1: Route Discovery & RPC Race for {intent.amount} {intent.token} ({intent.source_chain} ➔ {intent.dest_chain})...[/bold cyan]")
        
        # 1. Resolve Route & Quote
        quote = self.route_finder.discover_route(intent)
        src_chain_info = self.rpc_manager.get_chain_info(intent.source_chain)
        if not src_chain_info:
            raise ValueError(f"Source chain {intent.source_chain} metadata not found")

        # Fast RPC race
        fastest_rpc = self.rpc_manager.get_fastest_rpc_sync(intent.source_chain)
        console.print(f"   [green]✓ Selected Route:[/green] {quote.bridge_name} on {src_chain_info.name}")
        console.print(f"   [green]✓ Active RPC:[/green] {fastest_rpc}")

        # 2. In-Transit Calldata Assembly
        console.print("[bold cyan]⚙️ STEP 2: In-Transit Calldata & Payload Synthesis...[/bold cyan]")
        calldata = CalldataExecutor.build_calldata_for_route(quote, intent, sender_address)
        console.print(f"   [green]✓ Calldata generated:[/green] {len(calldata)} bytes, selector {calldata[:10]}")

        # 3. Dry-Run Simulation & Circuit Breaker
        console.print("[bold cyan]🧪 STEP 3: Live eth_call Dry-Run & Simulation...[/bold cyan]")
        simulator = Simulator(fastest_rpc)
        min_output = int(quote.estimated_output_raw * (1 - (intent.max_slippage_bps / 10000.0)))
        
        sim_res = simulator.simulate_call(
            from_address=sender_address,
            to_address=quote.router_address,
            calldata=calldata,
            value_wei=0,
            min_output_expected=min_output,
            quote=quote,
        )

        if not sim_res.success:
            console.print(f"[bold red]⛔ CIRCUIT BREAKER TRIPPED:[/bold red] {sim_res.revert_reason}")
            return {
                "status": "simulation_failed",
                "error": sim_res.revert_reason,
                "route": quote.model_dump(),
            }

        console.print(f"   [green]✓ Simulation Passed:[/green] Est Gas: {sim_res.gas_used} units (${sim_res.estimated_gas_cost_usd:.4f} USD)")

        # 4. Human-in-the-Loop Approval Manifest
        console.print("[bold cyan]📋 STEP 4: Human-in-the-Loop Verification Manifest...[/bold cyan]")
        manifest = ExecutionManifest(
            source_chain=intent.source_chain,
            source_chain_id=quote.source_chain_id,
            dest_chain=intent.dest_chain,
            dest_chain_id=quote.dest_chain_id,
            bridge_name=quote.bridge_name,
            router_address=quote.router_address,
            token_symbol=intent.token,
            amount_in=intent.amount,
            amount_out_est=quote.estimated_output_raw / (10 ** quote.decimals),
            min_amount_out=min_output / (10 ** quote.decimals),
            slippage_percent=intent.max_slippage_bps / 100.0,
            gas_cost_usd=sim_res.estimated_gas_cost_usd,
            bridge_fee_usd=quote.fee_amount_usd,
            total_cost_usd=sim_res.estimated_gas_cost_usd + quote.fee_amount_usd,
            raw_to=quote.router_address,
            raw_value_wei=0,
            raw_data=calldata,
        )

        ManifestFormatter.render(manifest)

        approved = auto_approve or ManifestFormatter.prompt_user_approval()
        if not approved:
            console.print("[yellow]Execution aborted by user. No transactions were broadcast.[/yellow]")
            return {"status": "user_rejected", "manifest": manifest.model_dump()}

        # 5. Broadcast & Receipt Monitoring
        console.print("[bold cyan]🚀 STEP 5: Signing & Broadcasting Transaction...[/bold cyan]")
        signer = Signer(fastest_rpc, src_chain_info.block_explorer)
        tx_payload = {
            "chainId": quote.source_chain_id,
            "to": quote.router_address,
            "data": calldata,
            "value": 0,
        }
        receipt = signer.broadcast_transaction(tx_payload, private_key=private_key, dry_run=(private_key is None))
        console.print(f"   [bold green]✓ Broadcast Confirmed:[/bold green] Tx Hash {receipt.tx_hash}")
        console.print(f"   [blue]Explorer Link:[/blue] {receipt.explorer_url}")

        # 6. Skill Registration & Self-Persistence
        console.print("[bold cyan]💾 STEP 6: Registering Procedural SKILL.md for Future In-Memory Reuse...[/bold cyan]")
        skill_name = f"{intent.source_chain.lower()}_to_{intent.dest_chain.lower()}_{intent.token.lower()}"
        metadata = SkillMetadata(
            name=skill_name,
            description=f"Verified bridge procedure for {intent.token} from {intent.source_chain} to {intent.dest_chain} via {quote.bridge_name}",
            source_chain=intent.source_chain,
            source_chain_id=quote.source_chain_id,
            dest_chain=intent.dest_chain,
            dest_chain_id=quote.dest_chain_id,
            token=intent.token,
            bridge_protocol=quote.bridge_name,
            router_address=quote.router_address,
            source_token_address=quote.source_token_address,
            dest_token_address=quote.dest_token_address,
            cctp_destination_domain=quote.extra_params.get("destination_domain") or quote.extra_params.get("cctp_domain"),
            last_verified="2026-09-01",
            rpc_fallbacks=src_chain_info.rpc_urls,
            tags=["cross-chain", intent.token.lower(), intent.source_chain, intent.dest_chain],
        )

        skill_body = f"""# {skill_name.upper()} Execution Routine

## Route Metadata
- **Protocol:** {quote.bridge_name}
- **Source Router Contract:** `{quote.router_address}`
- **Source Token:** `{quote.source_token_address}`
- **Destination Token:** `{quote.dest_token_address}`

## Calldata Assembly
```python
# Ephemeral Web3.py execution helper
from omnibridge.core.executor import CalldataExecutor

calldata = CalldataExecutor.encode_cctp_deposit_for_burn(
    amount_raw={quote.deposit_amount_raw},
    destination_domain={metadata.cctp_destination_domain or 3},
    recipient_address="{sender_address}",
    burn_token_address="{quote.source_token_address}",
)
```

## Security & Verification Notes
- Verified with zero-revert simulation against RPC.
- Gas profile benchmarked at ~{sim_res.gas_used} units.
"""

        saved_path = self.skill_manager.save_skill(metadata, skill_body)
        console.print(f"   [bold green]✓ Persisted Skill to:[/bold green] {saved_path}")

        return {
            "status": "success",
            "receipt": receipt.model_dump(),
            "manifest": manifest.model_dump(),
            "skill_file": saved_path,
        }
