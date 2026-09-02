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
        table.add_row("Transfer Asset", ManifestFormatter._amount_cell(manifest))
        table.add_row("Estimated Output", f"[bold]{manifest.amount_out_est} {manifest.token_symbol}[/bold]")
        table.add_row("Guaranteed Min Output", f"{manifest.min_amount_out} {manifest.token_symbol} (max slippage {manifest.slippage_percent}%)")
        table.add_row("Estimated Gas Fee", f"${manifest.gas_cost_usd:.4f} USD")
        table.add_row("Bridge Protocol Fee", f"${manifest.bridge_fee_usd:.4f} USD")
        table.add_row("Total Estimated Cost", f"[bold red]${manifest.total_cost_usd:.4f} USD[/bold red]")
        table.add_row("Raw Calldata Preview", f"`{manifest.raw_data[:42]}...`")

        console.print("\n")
        console.print(Panel(table, border_style="bright_blue", title="[bold white]Omnibridge In-Transit Guardrail[/bold white]"))
        ManifestFormatter._render_verification(manifest)

    @staticmethod
    def _amount_cell(manifest: ExecutionManifest) -> str:
        """
        Show the amount recovered from the calldata, not the one the intent asked for.

        If the display and the transaction read from the same source, a decimal-scaling
        error is invisible to the human too — the manifest just repeats the mistake.
        """
        decoded = manifest.amount_in_decoded
        symbol = manifest.token_symbol
        if decoded is None:
            return f"{manifest.amount_in} {symbol}  [dim](calldata is not ABI-decodable — unverified)[/dim]"
        if abs(decoded - manifest.amount_in) < 1e-9:
            return f"{decoded} {symbol}  [dim](decoded from calldata)[/dim]"
        return (
            f"[bold red]{decoded} {symbol} — this is what the transaction actually moves. "
            f"You asked for {manifest.amount_in} {symbol}. DO NOT APPROVE.[/bold red]"
        )

    @staticmethod
    def _render_verification(manifest: ExecutionManifest) -> None:
        """
        State what was verified, and on which chain.

        This line previously read "Simulation succeeded on-chain with 0 reverts"
        unconditionally, regardless of whether a simulation had happened at all.
        """
        status = (manifest.simulation_status or "UNKNOWN").upper()

        if status == "VERIFIED":
            where = (
                f" on chain {manifest.simulated_on_chain_id}"
                if manifest.simulated_on_chain_id is not None
                else ""
            )
            console.print(
                f"[bold green]⚡ SIMULATION VERIFIED:[/bold green] eth_call completed{where} "
                "with no revert."
            )
            console.print(
                "[dim]   Scope: the source leg only. No eth_call can prove that funds arrive "
                "on the destination chain.[/dim]"
            )
            return

        explanation = {
            "REVERTED": "the transaction reverted during eth_call",
            "UNAVAILABLE": "no RPC endpoint could be reached and identity-checked",
            "UNSUPPORTED": "there is no eth_call path for the chain being submitted to",
        }.get(status, "the simulation status is unknown")

        console.print(
            f"[bold red]⚡ NOT VERIFIED [{status}]:[/bold red] {explanation}. "
            "This transaction has not been checked against a live node."
        )

    @staticmethod
    def prompt_user_approval() -> bool:
        """Prompts explicitly for user signing approval."""
        console.print("\n[bold cyan]Do you authorize signing and broadcasting this transaction payload? (Yes/No):[/bold cyan] ", end="")
        try:
            resp = input().strip().lower()
            return resp in ("y", "yes")
        except EOFError:
            return False
