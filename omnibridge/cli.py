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
    )

    agent = OmnibridgeAgent()
    agent.run_flow(
        intent=intent,
        sender_address=args.sender,
        auto_approve=args.yes,
        dry_run=args.dry_run,
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
    # get_chain_info() only reads the cache that discover_chain_metadata() populates,
    # so on a fresh RPCManager it always misses. Resolve, don't look up.
    try:
        chain = rm.discover_chain_metadata(args.chain)
    except Exception as exc:
        console.print(f"[red]Could not resolve chain '{args.chain}': {exc}[/red]")
        sys.exit(1)

    # Show the resolved identity, not the string the user typed.
    console.print(f"[bold cyan]Racing RPCs for {chain.name} ({chain.chain_id})...[/bold cyan]")
    fastest = rm.get_fastest_rpc_sync(args.chain)
    console.print(f"[bold green]✓ Fastest RPC endpoint:[/bold green] {fastest}")


def handle_cards_list(args):
    """Lists protocol cards and how far each entry has been established."""
    from omnibridge.cards.manager import CardManager

    table = Table(title="[bold green]Omnibridge Protocol Cards[/bold green]")
    table.add_column("Card", style="cyan")
    table.add_column("Kind", style="white")
    table.add_column("Version", style="white")
    table.add_column("Verified", style="green")
    table.add_column("Checked", style="yellow")
    table.add_column("Lead", style="white")
    table.add_column("Demoted", style="red")
    table.add_column("Path", style="dim")

    for doc in CardManager().list_cards():
        counts = {"verified": 0, "checked": 0, "lead": 0, "demoted": 0}
        for status in doc.card.statuses().values():
            counts[status] += 1
        table.add_row(
            doc.card.id,
            doc.card.kind + ("" if doc.card.executable else " (not executable)"),
            str(doc.card.card_version),
            *(str(counts[k]) for k in ("verified", "checked", "lead", "demoted")),
            doc.file_path or "",
        )
    console.print(table)


def handle_cards_check(args):
    """
    Run the on-chain checks over a card and show what they would change.

    Read-only unless --write is given, and even then it only raises passing leads to
    `checked` and records token decimals read from the chain. It never verifies or
    demotes anything: only a run outcome does that.
    """
    from omnibridge.cards.manager import CardManager
    from omnibridge.cards.validation import apply_checks, check_card
    from omnibridge.memory.store import utc_today

    mgr = CardManager()
    doc = mgr.load(args.protocol_id)
    if doc is None:
        console.print(f"[red]No readable card '{args.protocol_id}'.[/red]")
        sys.exit(1)

    results = check_card(doc.card)
    table = Table(title=f"[bold cyan]Checks for {doc.card.id} (v{doc.card.card_version})[/bold cyan]")
    table.add_column("Entry", style="cyan")
    table.add_column("Status", style="white")
    table.add_column("Result", style="white")
    table.add_column("Reasons", style="dim")
    for r in results:
        change = f"{r.current.value} -> {r.proposed.value}" if r.changes_status else r.current.value
        verdict = "[green]pass[/green]" if r.passed else "[red]fail[/red]"
        table.add_row(f"{r.kind}:{r.entry_id}", change, verdict, "; ".join(r.reasons))
    console.print(table)
    console.print(
        "[dim]Destinations and parameters have no on-chain check and stay leads until a "
        "broadcast uses them.[/dim]"
    )

    promotions = [r for r in results if r.changes_status]
    if not args.write:
        if promotions:
            console.print(f"{len(promotions)} lead(s) would become checked. Re-run with --write to record it.")
        return
    if not promotions and not any(r.kind == "tokens" and r.passed for r in results):
        console.print("Nothing to write.")
        return
    updated = apply_checks(doc.card, results, utc_today())
    path = mgr.save_card(updated, cause=f"cards check: {len(promotions)} lead(s) passed on-chain checks")
    console.print(f"[green]Wrote {path}[/green]")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Omnibridge: Autonomous In-Transit Cross-Chain Agent")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Bridge subcommand
    bridge_parser = subparsers.add_parser("bridge", help="Execute cross-chain bridging flow")
    bridge_parser.add_argument("--token", "-t", required=True, help="Token symbol (e.g. USDC, ETH)")
    bridge_parser.add_argument("--amount", "-a", type=float, required=True, help="Amount to bridge")
    bridge_parser.add_argument("--from", "--src", dest="source_chain", required=True, help="Source chain")
    bridge_parser.add_argument("--to", "--dst", dest="dest_chain", required=True, help="Destination chain")
    bridge_parser.add_argument("--recipient", "-r", default=None, help="Recipient address on destination")
    # Required: simulating from anyone else's account reports their balances and
    # allowances, which says nothing about this transfer.
    bridge_parser.add_argument("--sender", "-s", required=True, help="Your sender address")
    bridge_parser.add_argument("--slippage-bps", type=int, default=50, help="Max slippage in basis points (50 = 0.5%%)")
    bridge_parser.add_argument("--yes", "-y", action="store_true", help="Auto-approve without interactive prompt (for tests/automation)")
    bridge_parser.add_argument("--dry-run", action="store_true", help="Rehearse without broadcasting; never inferred from a missing key")

    # List skills subcommand
    subparsers.add_parser("list-skills", help="List persistent SKILL.md modules")

    # RPC bench subcommand
    rpc_parser = subparsers.add_parser("rpc-bench", help="Benchmark and race public RPC endpoints")
    rpc_parser.add_argument("chain", help="Chain name to test (e.g. base, arbitrum, ethereum)")

    # Protocol cards
    cards_parser = subparsers.add_parser("cards", help="Inspect and check protocol cards")
    cards_sub = cards_parser.add_subparsers(dest="cards_command")
    cards_sub.add_parser("list", help="List protocol cards")
    check_parser = cards_sub.add_parser("check", help="Run on-chain checks over a card's entries")
    check_parser.add_argument("protocol_id", help="Card id, e.g. cctp")
    check_parser.add_argument(
        "--write", action="store_true",
        help="Record passing leads as checked (never verifies or demotes)",
    )

    args = parser.parse_args(argv)

    if args.command == "bridge":
        handle_bridge(args)
    elif args.command == "list-skills":
        handle_list_skills(args)
    elif args.command == "rpc-bench":
        handle_rpc_bench(args)
    elif args.command == "cards" and args.cards_command == "list":
        handle_cards_list(args)
    elif args.command == "cards" and args.cards_command == "check":
        handle_cards_check(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
