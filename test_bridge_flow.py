"""
End-to-End Integration Test for LangGraph Web3 Bridging State Machine.
Validates:
1. State pipeline execution up to Node 5 (HITL Interrupt Gate).
2. Verification of the AWAITING_USER_APPROVAL payload.
3. Resuming the graph with a signed transaction payload.
4. Final broadcast confirmation and persistent SKILL.md creation.
"""

import uuid
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from langgraph.types import Command
from graph import graph, LOCAL_SKILLS_DIR

console = Console()


def test_langgraph_bridge_flow():
    console.print("\n[bold magenta]====================================================================[/bold magenta]")
    console.print("[bold cyan]🧪 LANGGRAPH BRIDGING STATE MACHINE - INTEGRATION TEST[/bold cyan]")
    console.print("[bold magenta]====================================================================[/bold magenta]\n")

    thread_id = f"test_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "raw_input": "Bridge 10 USDC from Base to Solana",
        "user_address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        "slippage": "0.1%",
    }

    console.print(f"[bold yellow]▶ STEP 1-4: Starting LangGraph pipeline (Thread ID: {thread_id})...[/bold yellow]")
    
    # 1. Stream graph execution until Node 5 (HITL interrupt)
    for event in graph.stream(initial_state, config=config):
        node_name = list(event.keys())[0] if event else "start"
        console.print(f"  [green]✓ Executed Node:[/green] [bold]{node_name}[/bold]")

    # 2. Check snapshot for the interrupt
    snapshot = graph.get_state(config)
    assert snapshot.tasks and snapshot.tasks[0].interrupts, "Expected graph to pause at HITL interrupt"

    interrupt_payload = snapshot.tasks[0].interrupts[0].value
    console.print(Panel(
        f"[bold white]Status:[/bold white] {interrupt_payload['status']}\n"
        f"[bold white]Route:[/bold white] {interrupt_payload['route']['source']} ➔ {interrupt_payload['route']['target']} via {interrupt_payload['route']['bridge']}\n"
        f"[bold white]Unsigned Tx Hex:[/bold white] {interrupt_payload['unsigned_transaction_hex'][:42]}...\n"
        f"[bold white]Estimated Gas USD:[/bold white] ${interrupt_payload['estimated_gas_usd']:.4f}\n"
        f"[bold white]Slippage:[/bold white] {interrupt_payload['slippage']}",
        title="[bold cyan]Node 5: HITL Gate Approval Payload[/bold cyan]",
        border_style="cyan"
    ))

    assert interrupt_payload["status"] == "AWAITING_USER_APPROVAL"
    assert interrupt_payload["unsigned_transaction_hex"].startswith("0x")

    # 3. Simulate User Wallet Signing & Graph Resume
    console.print("\n[bold yellow]▶ STEP 5-6: Resuming Graph with Signed Transaction...[/bold yellow]")
    mock_signed_hex = interrupt_payload["unsigned_transaction_hex"]
    
    final_output = None
    for event in graph.stream(Command(resume={"signed_transaction_hex": mock_signed_hex}), config=config):
        node_name = list(event.keys())[0]
        console.print(f"  [green]✓ Resumed & Executed Node:[/green] [bold]{node_name}[/bold]")
        if node_name == "broadcast_skill_node":
            final_output = event[node_name]

    console.print(Panel(
        f"[bold white]Status:[/bold white] {final_output['status']}\n"
        f"[bold white]Tx Hash:[/bold white] {final_output['tx_hash']}\n"
        f"[bold white]Persisted SKILL.md:[/bold white] {final_output['skill_path']}",
        title="[bold green]Node 6: Broadcast & Skill Registration Complete[/bold green]",
        border_style="green"
    ))

    assert final_output["status"] == "CONFIRMED"
    assert Path(final_output["skill_path"]).exists()

    console.print("[bold green]✨ TEST PASSED: Full LangGraph In-Transit Bridging pipeline successfully verified![/bold green]\n")


if __name__ == "__main__":
    test_langgraph_bridge_flow()
