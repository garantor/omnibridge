"""
Custom LangChain Tool Definitions for In-Transit Web3 Execution with Verbose Telemetry Logging.
"""

from typing import Dict, Any, Optional
import json
import io
import sys
from pathlib import Path
from langchain_core.tools import tool
from rich.console import Console
from rich.panel import Panel
from web3 import Web3

from omnibridge.skills.manager import SkillManager, SkillMetadata
from omnibridge.discovery.rpc_manager import RPCManager
from omnibridge.discovery.route_finder import RouteFinder
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.simulator import Simulator
from omnibridge.core.signer import Signer

console = Console()
LOCAL_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
LOCAL_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
skill_manager = SkillManager(repository_path=str(LOCAL_SKILLS_DIR))
rpc_manager = RPCManager()
route_finder = RouteFinder(skill_manager=skill_manager, rpc_manager=rpc_manager)


# `web_search_tool` and `fetch_doc_content_tool` were deleted here (plan Phase 4).
#
# Search is now the model's own server-side `web_search_20260209` / `web_fetch_20260209`,
# constrained to a documentation allowlist and cited back in the proposal — see
# omnibridge/agent/discovery_agent.py.
#
# The two tools they replaced could not do the job they were called for, and failed
# open when they could not:
#
#   - `web_search_tool` queried DuckDuckGo's *Instant Answer* API, which returns entity
#     abstracts rather than search results and is empty for essentially every technical
#     query. Its DeFiLlama branch listed bridge names and 24h volumes — never a contract
#     address, which is the one thing route discovery needed from it.
#   - `fetch_doc_content_tool` invented documentation whenever a fetch failed: a CCTP
#     signature and domain table for any URL containing "circle", a *fabricated* Strkey
#     contract address for anything mentioning Stellar, and for everything else the
#     signature `deposit(uint256,address,uint32)` with the selector `0x2f820a5f` — a
#     made-up interface presented as a documented one. Because a bare `requests.get`
#     against a JS-rendered docs site usually fails, the fabricated branch was the
#     normal path.


@tool
def python_repl_runner(code: str) -> str:
    """Executes ephemeral single-use Python code in a sandboxed runtime to construct unsigned transaction payloads."""
    console.print(f"\n[bold cyan]⚙️ [LANGCHAIN TOOL CALL] -> python_repl_runner[/bold cyan]")
    console.print(f"   [bold yellow]↳ Executing Ephemeral Python Code ({len(code)} chars)[/bold yellow]")
    
    old_stdout = sys.stdout
    redirected_output = sys.stdout = io.StringIO()
    try:
        local_scope: Dict[str, Any] = {"Web3": Web3, "CalldataExecutor": CalldataExecutor}
        exec(code, {}, local_scope)
        sys.stdout = old_stdout
        output = redirected_output.getvalue() or "Executed successfully with return scope: " + str(list(local_scope.keys()))
        console.print(f"   [bold green]↳ Execution Output:[/bold green] [dim]{output}[/dim]\n")
        return output
    except Exception as e:
        sys.stdout = old_stdout
        console.print(f"   [bold red]↳ Execution Failed:[/bold red] {e}\n")
        return f"Python REPL Error: {e}"


@tool
def eth_call_simulator(rpc_url: str, from_address: str, to_address: str, data: str, value: int = 0, expected_chain_id: Optional[int] = None) -> str:
    """Executes a dry-run static call (eth_call) against a public RPC to verify zero-revert conditions and gas estimation."""
    console.print(f"\n[bold cyan]🧪 [LANGCHAIN TOOL CALL] -> eth_call_simulator[/bold cyan]")
    console.print(f"   [bold yellow]↳ Target RPC:[/bold yellow] [white]{rpc_url}[/white]")
    console.print(f"   [bold yellow]↳ From:[/bold yellow] [white]{from_address}[/white] ➔ [bold yellow]To:[/bold yellow] [white]{to_address}[/white]")

    sim = Simulator(rpc_url, expected_chain_id=expected_chain_id)
    res = sim.simulate_call(
        from_address=from_address,
        to_address=to_address,
        calldata=data,
        value_wei=value,
    )
    
    status_color = "green" if res.success else "red"
    console.print(f"   [bold {status_color}]↳ Simulation Result:[/bold {status_color}] Success={res.success}, GasUsed={res.gas_used}, Revert={res.revert_reason}\n")
    return json.dumps(res.model_dump())


@tool
def rpc_broadcaster(rpc_url: str, raw_payload_json: str, signed_tx_hex: Optional[str] = None) -> str:
    """Submits a signed raw transaction to the target blockchain RPC and returns the transaction hash."""
    console.print(f"\n[bold cyan]🚀 [LANGCHAIN TOOL CALL] -> rpc_broadcaster[/bold cyan]")
    console.print(f"   [bold yellow]↳ Submitting to RPC:[/bold yellow] [white]{rpc_url}[/white]")

    signer = Signer(rpc_url)
    try:
        payload = json.loads(raw_payload_json)
    except Exception:
        payload = {"chainId": 1, "to": "0x0000000000000000000000000000000000000000", "data": "0x"}
    
    if signed_tx_hex:
        payload["signed_hex"] = signed_tx_hex

    receipt = signer.broadcast_transaction(payload, private_key=None, dry_run=True)
    console.print(f"   [bold green]↳ Broadcast Complete:[/bold green] Tx Hash={receipt.tx_hash}\n")
    return json.dumps(receipt.model_dump())


@tool
def save_skill_tool(skill_name: str, metadata_json: str, procedural_markdown: str) -> str:
    """Persists a verified bridging procedure as an open standard SKILL.md for instant future reuse."""
    console.print(f"\n[bold cyan]💾 [LANGCHAIN TOOL CALL] -> save_skill_tool[/bold cyan]")
    console.print(f"   [bold yellow]↳ Registering Skill:[/bold yellow] [white]{skill_name}[/white]")

    try:
        meta_dict = json.loads(metadata_json)
        metadata = SkillMetadata(**meta_dict)
        path = skill_manager.save_skill(metadata, procedural_markdown, target_dir=str(LOCAL_SKILLS_DIR / skill_name))
        console.print(f"   [bold green]↳ Skill Saved at:[/bold green] [dim]{path}[/dim]\n")
        return json.dumps({"status": "SUCCESS", "skill_path": path})
    except Exception as e:
        console.print(f"   [bold red]↳ Skill Save Error:[/bold red] {e}\n")
        return json.dumps({"status": "ERROR", "error": str(e)})
