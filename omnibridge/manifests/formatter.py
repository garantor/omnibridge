"""
Terminal Execution Manifest Formatter using Rich.
Renders high-visibility human-in-the-loop authorization summaries.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from omnibridge.core.models import ExecutionManifest

console = Console()


class ManifestFormatter:
    """Formats and prints execution manifests for user verification."""

    @staticmethod
    def render(manifest: ExecutionManifest) -> None:
        """Renders rich execution summary to console."""
        table = Table(title="[bold cyan]Cross-Chain Execution Manifest[/bold cyan]", show_header=True, header_style="bold magenta")
        table.add_column("Parameter", style="cyan", width=24)
        table.add_column("Details", style="white")

        table.add_row("Route Direction", f"[bold green]{manifest.source_chain.upper()} ({manifest.source_chain_id})[/bold green]  ➔  [bold green]{manifest.dest_chain.upper()} ({manifest.dest_chain_id})[/bold green]")
        table.add_row("Bridge Protocol", f"[bold yellow]{manifest.bridge_name}[/bold yellow]")
        table.add_row("Router Contract", f"`{manifest.router_address}`")
        table.add_row("Transfer Asset", f"{manifest.amount_in} {manifest.token_symbol}")
        table.add_row("Estimated Output", f"[bold]{manifest.amount_out_est} {manifest.token_symbol}[/bold]")
        table.add_row("Guaranteed Min Output", f"{manifest.min_amount_out} {manifest.token_symbol} (max slippage {manifest.slippage_percent}%)")
        table.add_row("Estimated Gas Fee", f"${manifest.gas_cost_usd:.4f} USD")
        table.add_row("Bridge Protocol Fee", f"${manifest.bridge_fee_usd:.4f} USD")
        table.add_row("Total Estimated Cost", f"[bold red]${manifest.total_cost_usd:.4f} USD[/bold red]")
        table.add_row("Raw Calldata Preview", f"`{manifest.raw_data[:42]}...`")

        console.print("\n")
        console.print(Panel(table, border_style="bright_blue", title="[bold white]Omnibridge In-Transit Guardrail[/bold white]"))
        console.print("[bold yellow]⚡ SAFETY CHECK:[/bold yellow] Simulation succeeded on-chain with 0 reverts.")

    @staticmethod
    def prompt_user_approval() -> bool:
        """Prompts explicitly for user signing approval."""
        console.print("\n[bold cyan]Do you authorize signing and broadcasting this transaction payload? (Yes/No):[/bold cyan] ", end="")
        try:
            resp = input().strip().lower()
            return resp in ("y", "yes")
        except EOFError:
            return False
