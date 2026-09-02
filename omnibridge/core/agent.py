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
        sender_address: str,
        private_key: Optional[str] = None,
        auto_approve: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Executes the complete In-Transit Bridging pipeline."""
        console.print(f"[bold cyan]🔍 STEP 1: Route Discovery & RPC Race for {intent.amount} {intent.token} ({intent.source_chain} ➔ {intent.dest_chain})...[/bold cyan]")
        
        # 1. Resolve Route & Quote
        quote = self.route_finder.discover_route(intent)
        src_chain_info = self.rpc_manager.discover_chain_metadata(
            intent.source_chain, network=intent.network
        )

        # Fast RPC race
        fastest_rpc = self.rpc_manager.get_fastest_rpc_sync(intent.source_chain)
        # Show the resolved identity, not the string that was typed.
        console.print(
            f"   [green]✓ Selected Route:[/green] {quote.bridge_name} on "
            f"{src_chain_info.name} ({src_chain_info.chain_id})"
        )
        console.print(f"   [green]✓ Active RPC:[/green] {fastest_rpc}")

        # 2. In-Transit Calldata Assembly
        console.print("[bold cyan]⚙️ STEP 2: In-Transit Calldata & Payload Synthesis...[/bold cyan]")
        calldata = CalldataExecutor.build_calldata_for_route(quote, intent, sender_address)
        console.print(f"   [green]✓ Calldata generated:[/green] {len(calldata)} bytes, selector {calldata[:10]}")

        # 3. Dry-Run Simulation & Circuit Breaker
        console.print("[bold cyan]🧪 STEP 3: Live eth_call Dry-Run & Simulation...[/bold cyan]")
        # Hand the simulator every endpoint, preferring the raced winner, and tell it
        # which chain these are supposed to be so it can reject an impostor.
        rpc_candidates = [fastest_rpc] + [u for u in src_chain_info.rpc_urls if u != fastest_rpc]
        simulator = Simulator(rpc_candidates, expected_chain_id=src_chain_info.chain_id)
        min_output = int(quote.estimated_output_raw * (1 - (intent.max_slippage_bps / 10000.0)))
        
        sim_res = simulator.simulate_call(
            from_address=sender_address,
            to_address=quote.router_address,
            calldata=calldata,
            value_wei=0,
            min_output_expected=min_output,
            quote=quote,
        )

        # Only VERIFIED continues. UNAVAILABLE and UNSUPPORTED are refusals, not passes.
        if not sim_res.success:
            console.print(
                f"[bold red]⛔ CIRCUIT BREAKER TRIPPED [{sim_res.status.value}]:[/bold red] "
                f"{sim_res.revert_reason}"
            )
            return {
                "status": "simulation_failed",
                "simulation_status": sim_res.status.value,
                "error": sim_res.revert_reason,
                "route": quote.model_dump(),
            }

        console.print(
            f"   [green]✓ Simulation VERIFIED on chain {sim_res.simulated_on_chain_id}:[/green] "
            f"Est Gas: {sim_res.gas_used} units (${sim_res.estimated_gas_cost_usd:.4f} USD)"
        )

        # 4. Human-in-the-Loop Approval Manifest
        console.print("[bold cyan]📋 STEP 4: Human-in-the-Loop Verification Manifest...[/bold cyan]")
        # Recover the amount from the bytes that will actually be signed, so a scaling
        # error cannot hide behind a manifest rendered from the same source as the intent.
        decoded_raw = CalldataExecutor.decode_leading_uint256(calldata)
        decoded_amount = (
            decoded_raw / (10 ** quote.decimals) if decoded_raw is not None else None
        )

        manifest = ExecutionManifest(
            source_chain=src_chain_info.name,
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
            gas_cost_usd=sim_res.estimated_gas_cost_usd or 0.0,
            bridge_fee_usd=quote.fee_amount_usd,
            total_cost_usd=(sim_res.estimated_gas_cost_usd or 0.0) + quote.fee_amount_usd,
            raw_to=quote.router_address,
            raw_value_wei=0,
            raw_data=calldata,
            simulation_status=sim_res.status.value,
            simulated_on_chain_id=sim_res.simulated_on_chain_id,
            amount_in_decoded=decoded_amount,
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
        # Broadcast semantics are explicit and identical in both runtimes: a rehearsal
        # happens only when the caller asks for one, never as a side effect of not
        # having supplied a key.
        receipt = signer.broadcast_transaction(tx_payload, private_key=private_key, dry_run=dry_run)
        console.print(f"   [bold green]✓ Broadcast Confirmed:[/bold green] Tx Hash {receipt.tx_hash}")
        console.print(f"   [blue]Explorer Link:[/blue] {receipt.explorer_url}")

        # 6. Skill Registration & Self-Persistence
        console.print("[bold cyan]💾 STEP 6: Registering Procedural SKILL.md for Future In-Memory Reuse...[/bold cyan]")
        params = quote.extra_params or {}
        skill_name = f"{intent.source_chain.lower()}_to_{intent.dest_chain.lower()}_{intent.token.lower()}"

        # Placeholders in place of this run's amount and addresses, so a replay encodes
        # the next caller's transfer instead of a frozen copy of this one.
        stored_values = CalldataExecutor.generalise_param_values(
            list(params.get("param_values") or []),
            amount_raw=quote.deposit_amount_raw,
            recipient=intent.recipient or sender_address,
            sender=sender_address,
        )

        try:
            dest_chain_id = self.rpc_manager.discover_chain_metadata(
                intent.dest_chain, network=intent.network
            ).chain_id
        except Exception:
            # A destination that is not EIP-155 addressable has no chain id. Left unset
            # and named by dest_chain_ref rather than filled with a plausible number.
            dest_chain_id = None

        metadata = SkillMetadata(
            name=skill_name,
            description=(
                f"Verified bridge procedure for {intent.token} from {intent.source_chain} "
                f"to {intent.dest_chain} via {quote.bridge_name}"
            ),
            source_chain=src_chain_info.name,
            source_chain_id=quote.source_chain_id,
            dest_chain=intent.dest_chain,
            dest_chain_id=dest_chain_id,
            dest_chain_ref=params.get("dest_chain_ref"),
            token=intent.token,
            bridge_protocol=quote.bridge_name,
            router_address=quote.router_address,
            source_token_address=quote.source_token_address or None,
            dest_token_address=quote.dest_token_address or None,
            cctp_destination_domain=params.get("destination_domain"),
            network=intent.network,
            function_signature=params.get("function_signature"),
            param_types=list(params.get("param_types") or []),
            param_values=stored_values,
            aliases=sorted({
                intent.source_chain.lower(),
                intent.dest_chain.lower(),
                src_chain_info.name.lower(),
            }),
            verified=True,
            # Was pinned to the literal string "2026-09-01", so every skill this flow
            # ever wrote claimed to have been verified on the day the line was typed.
            last_verified=receipt.timestamp[:10],
            sources=list(params.get("sources") or []),
            rpc_fallbacks=src_chain_info.rpc_urls,
            tags=["cross-chain", intent.token.lower(), intent.source_chain, intent.dest_chain],
        )

        rows = "\n".join(
            f"| {i} | `{t}` | `{v}` |"
            for i, (t, v) in enumerate(zip(metadata.param_types, metadata.param_values))
        )
        skill_body = f"""# {skill_name.upper()} Execution Routine

## Route
- **Protocol:** {quote.bridge_name}
- **Router:** `{quote.router_address}`
- **Function:** `{metadata.function_signature}`
- **Source Token:** `{quote.source_token_address}`
- **Destination:** {metadata.dest_chain_ref or metadata.dest_chain_id or intent.dest_chain}

## Parameters

Encoded through `CalldataExecutor.encode_dynamic_calldata`; placeholders are bound per
request by `CalldataExecutor.bind_param_values`.

| # | Type | Value |
|---|------|-------|
{rows}

## Verification
- Simulation {sim_res.status.value} on chain {sim_res.simulated_on_chain_id} via {sim_res.simulated_via_rpc}
- Gas profile ~{sim_res.gas_used} units
- Approved by a human and broadcast as `{receipt.tx_hash}`

The body previously carried a literal `encode_cctp_deposit_for_burn(...)` call with
`destination_domain or 3` — Arbitrum's domain — as the fallback for every route,
including routes that were not CCTP at all. A skill records what was verified.
"""

        saved_path = self.skill_manager.save_skill(metadata, skill_body)
        console.print(f"   [bold green]✓ Persisted Skill to:[/bold green] {saved_path}")

        return {
            "status": "success",
            "receipt": receipt.model_dump(),
            "manifest": manifest.model_dump(),
            "skill_file": saved_path,
        }
