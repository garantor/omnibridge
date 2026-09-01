"""
Omnibridge Command Line Interface (CLI).
"""

import argparse
import sys
from rich.console import Console
from rich.table import Table
from omnibridge.core.models import UserIntent
from omnibridge.core.agent import OmnibridgeAgent
from omnibridge.skills.manager import SkillManager
from omnibridge.discovery.rpc_manager import RPCManager

console = Console()


def handle_bridge(args):
    """Executes a cross-chain bridging intent."""
    intent = UserIntent(
        token=args.token,
        amount=args.amount,
        source_chain=args.source_chain,
        dest_chain=args.dest_chain,
        recipient=args.recipient,
        max_slippage_bps=args.slippage_bps,
        priority=args.priority,
    )

    agent = OmnibridgeAgent()
    agent.run_flow(
        intent=intent,
        sender_address=args.sender or "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        auto_approve=args.yes,
    )


def handle_list_skills(args):
    """Lists all locally registered SKILL.md modules."""
    sm = SkillManager()
    skills = sm.list_skills()

    table = Table(title="[bold green]Omnibridge Skill Repository[/bold green]")
    table.add_column("Skill Name", style="cyan")
    table.add_column("Source", style="white")
    table.add_column("Destination", style="white")
    table.add_column("Token", style="yellow")
    table.add_column("Protocol", style="magenta")
    table.add_column("Path", style="dim")

    for s in skills:
        m = s.metadata
        table.add_row(
            m.name,
            f"{m.source_chain} ({m.source_chain_id})",
            f"{m.dest_chain} ({m.dest_chain_id})",
            m.token,
            m.bridge_protocol,
            s.file_path or "",
        )

    console.print(table)


def handle_rpc_bench(args):
    """Benchmarks and races public RPCs for a chain."""
    rm = RPCManager()
    chain = rm.get_chain_info(args.chain)
    if not chain:
        console.print(f"[red]Unknown chain {args.chain}[/red]")
        sys.exit(1)

    console.print(f"[bold cyan]Racing RPCs for {chain.name}...[/bold cyan]")
    fastest = rm.get_fastest_rpc_sync(args.chain)
    console.print(f"[bold green]✓ Fastest RPC endpoint:[/bold green] {fastest}")


def main():
    parser = argparse.ArgumentParser(description="Omnibridge: Autonomous In-Transit Cross-Chain Agent")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Bridge subcommand
    bridge_parser = subparsers.add_parser("bridge", help="Execute cross-chain bridging flow")
    bridge_parser.add_argument("--token", "-t", required=True, help="Token symbol (e.g. USDC, ETH)")
    bridge_parser.add_argument("--amount", "-a", type=float, required=True, help="Amount to bridge")
    bridge_parser.add_argument("--from", "--src", dest="source_chain", required=True, help="Source chain")
    bridge_parser.add_argument("--to", "--dst", dest="dest_chain", required=True, help="Destination chain")
    bridge_parser.add_argument("--recipient", "-r", default=None, help="Recipient address on destination")
    bridge_parser.add_argument("--sender", "-s", default="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", help="Sender address")
    bridge_parser.add_argument("--slippage-bps", type=int, default=50, help="Max slippage in basis points (50 = 0.5%)")
    bridge_parser.add_argument("--priority", default="low_fee", choices=["low_fee", "fastest", "native"])
    bridge_parser.add_argument("--yes", "-y", action="store_true", help="Auto-approve without interactive prompt (for tests/automation)")

    # List skills subcommand
    subparsers.add_parser("list-skills", help="List persistent SKILL.md modules")

    # RPC bench subcommand
    rpc_parser = subparsers.add_parser("rpc-bench", help="Benchmark and race public RPC endpoints")
    rpc_parser.add_argument("chain", help="Chain name to test (e.g. base, arbitrum, ethereum)")

    args = parser.parse_args()

    if args.command == "bridge":
        handle_bridge(args)
    elif args.command == "list-skills":
        handle_list_skills(args)
    elif args.command == "rpc-bench":
        handle_rpc_bench(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
