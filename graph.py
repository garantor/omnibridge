"""
LangGraph State Machine for Headless Web3 In-Transit Bridging Engine with Full Observability Telemetry.
"""

from typing import Dict, Any, Optional, TypedDict
import json
import os
import re
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

from omnibridge.core.models import UserIntent, RouteQuote
from omnibridge.skills.manager import SkillManager, SkillMetadata
from omnibridge.discovery.rpc_manager import RPCManager
from omnibridge.discovery.route_finder import RouteFinder
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.simulator import Simulator, is_evm_address
from omnibridge.core.models import SimulationStatus
from omnibridge.core.signer import Signer
from tools import LOCAL_SKILLS_DIR


class GraphState(TypedDict):
    raw_input: str
    source_chain: str
    target_chain: str
    amount: float
    asset: str
    user_address: str
    slippage: str
    route: Optional[Dict[str, Any]]
    active_rpc: Optional[str]
    unsigned_transaction_hex: Optional[str]
    simulation_result: Optional[Dict[str, Any]]
    estimated_gas_usd: float
    status: str
    signed_transaction_hex: Optional[str]
    tx_hash: Optional[str]
    skill_path: Optional[str]
    error: Optional[str]
    traces: Dict[str, Any]
    # Retry edge (plan 3.4): a revert is feedback, not a terminus.
    retry_count: int
    failure_context: Optional[str]
    budget: Optional[Dict[str, Any]]
    agent_evidence: Optional[Dict[str, Any]]


# Bounded by plan 3.4. Three attempts, then the run ends.
MAX_DISCOVERY_RETRIES = int(os.environ.get("OMNIBRIDGE_MAX_RETRIES", "3"))

rpc_mgr = RPCManager()
skill_mgr = SkillManager(repository_path=str(LOCAL_SKILLS_DIR))
finder = RouteFinder(skill_manager=skill_mgr, rpc_manager=rpc_mgr)


# --- NODE 1: Intent & Orchestrator Parser ---
def intent_parser_node(state: GraphState) -> Dict[str, Any]:
    raw = state.get("raw_input", "")
    # Extract entities dynamically from natural language or structured payload
    src = state.get("source_chain") or ""
    dst = state.get("target_chain") or ""
    amt = float(state.get("amount") or 0.0)
    tok = state.get("asset") or ""
    # No zero-address default. It reads as a valid EVM address everywhere downstream,
    # so a missing wallet would simulate from 0x0 and encode 0x0 as the recipient —
    # a burn, dressed as a transfer.
    user_addr = (state.get("user_address") or "").strip()
    if not is_evm_address(user_addr):
        raise ValueError(
            f"Intent Error: no valid sender address supplied (got {user_addr!r}). "
            "Connect a wallet — simulating from any other account would not describe "
            "this transfer."
        )
    slip = state.get("slippage") or "0.5%"

    if raw:
        # The verb set is widened beyond the literal word "bridge". Where no destination
        # asset is named, swap/send/move/transfer all denote the same operation.
        match = re.search(
            r"(?:bridge|swap|send|move|transfer)\s+(\d+(?:\.\d+)?)\s*([a-zA-Z0-9_-]+)?"
            r"\s+from\s+([a-zA-Z0-9_-]+)\s+to\s+([a-zA-Z0-9_-]+)",
            raw,
            re.IGNORECASE,
        )
        if match:
            amt = float(match.group(1))
            if match.group(2):
                tok = match.group(2).upper()
            src = match.group(3)
            dst = match.group(4)

    # No defaults block. This previously substituted "Unknown"/"Unknown"/1.0 NATIVE for
    # anything it could not parse and then reported status "PARSED" / "COMPLETED", so
    # "swap 100 usdc from base to stellar" silently became "1.0 NATIVE Unknown -> Unknown".
    # There is no such thing as a partially understood instruction to move money.
    missing = [
        label
        for label, value in (
            ("source chain", src),
            ("destination chain", dst),
            ("asset", tok),
        )
        if not value
    ]
    if missing or amt <= 0:
        if amt <= 0:
            missing.append("amount")
        raise ValueError(
            "Intent Error: could not determine "
            + ", ".join(missing)
            + f" from input {raw!r}. Expected something like "
            "'bridge 100 USDC from base to arbitrum', or supply the fields directly."
        )

    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    _console.print(Panel(
        f"[bold white]Raw Input:[/bold white] \"{raw}\"\n"
        f"[bold white]Source Chain:[/bold white] {src}\n"
        f"[bold white]Target Chain:[/bold white] {dst}\n"
        f"[bold white]Amount & Asset:[/bold white] {amt} {tok}\n"
        f"[bold white]Recipient / User Wallet:[/bold white] {user_addr}\n"
        f"[bold white]Slippage Tolerance:[/bold white] {slip}",
        title="[bold cyan]🧠 [STEP 1: INTENT & PARAMETER EXTRACTION][/bold cyan]",
        border_style="cyan"
    ))

    traces = state.get("traces", {})
    traces["step_1_intent"] = {
        "title": "Intent & Parameter Extraction",
        "raw_input": raw,
        "parsed_parameters": {
            "source_chain": src,
            "target_chain": dst,
            "amount": amt,
            "asset": tok,
            "user_address": user_addr,
            "slippage": slip,
        },
        "status": "COMPLETED",
    }

    return {
        "source_chain": src,
        "target_chain": dst,
        "amount": amt,
        "asset": tok,
        "user_address": user_addr,
        "slippage": slip,
        "status": "PARSED",
        "traces": traces,
    }


# --- NODE 2: Skill Check or Web Search Discovery ---
def discovery_node(state: GraphState) -> Dict[str, Any]:
    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    intent = UserIntent(
        token=state["asset"],
        amount=state["amount"],
        source_chain=state["source_chain"],
        dest_chain=state["target_chain"],
        recipient=state["user_address"],
    )

    search_query = f"Bridge {state['asset']} from {state['source_chain']} to {state['target_chain']} official routers & RPCs"

    # Tier 0: a verified skill short-circuits discovery entirely.
    # Tier 1: the reasoning core, unless the kill switch forces the static path.
    agent_result = _run_discovery_agent(state, intent)
    if agent_result is not None:
        return agent_result

    quote = finder.discover_route(intent)
    fastest_rpc = rpc_mgr.get_fastest_rpc_sync(state["source_chain"])
    is_cached = "Skill" in quote.bridge_name or "Verified" in quote.bridge_name

    # Explicitly invoke LangChain web_search_tool (it prints the RAW search results first)
    from tools import web_search_tool, fetch_doc_content_tool
    search_log = web_search_tool.invoke(search_query)

    # Extract documentation link from search results and deep-inspect the docs
    doc_urls = re.findall(r'https?://[^\s()]+', search_log)
    target_doc = doc_urls[0] if doc_urls else "https://docs.circle.com/cctp"
    doc_specs = fetch_doc_content_tool.invoke(target_doc)

    # PRINT 2: Then print the PARSED extraction and intended parameters
    _console.print(Panel(
        f"[bold green]Parsed Protocol:[/bold green] {quote.bridge_name} ({'⚡ Skill Memory' if is_cached else '🔍 Web Search'})\n"
        f"[bold green]Router Contract to Use:[/bold green] [white]`{quote.router_address}`[/white]\n"
        f"[bold green]Source Token Address:[/bold green] [white]`{quote.source_token_address}`[/white]\n"
        f"[bold green]Destination Token Address:[/bold green] [white]`{quote.dest_token_address}`[/white]\n"
        f"[bold green]Active Selected RPC:[/bold green] [cyan]{fastest_rpc}[/cyan]\n"
        f"[bold green]Chain IDs:[/bold green] {quote.source_chain_id} ({state['source_chain']}) ➔ {quote.dest_chain_id} ({state['target_chain']})",
        title="[bold green]📋 [STEP 2: PARSED EXTRACTED PARAMETERS INTENDED TO USE][/bold green]",
        border_style="green"
    ))

    traces = state.get("traces", {})
    traces["step_2_discovery"] = {
        "title": "Route & Contract Discovery",
        "source_type": "SKILL_MEMORY_CACHE" if is_cached else "DYNAMIC_WEB_DOCS_SEARCH",
        "search_query": search_query,
        "web_search_tool_output": search_log,
        "deep_doc_url": target_doc,
        "deep_doc_specs": doc_specs,
        "discovered_route": quote.bridge_name,
        "router_contract": quote.router_address,
        "source_token": quote.source_token_address,
        "dest_token": quote.dest_token_address,
        "active_rpc": fastest_rpc,
        "status": "COMPLETED",
    }

    return {
        "route": {
            "source": state["source_chain"],
            "target": state["target_chain"],
            "bridge": quote.bridge_name,
            "router_address": quote.router_address,
            "quote_obj": quote.model_dump(),
        },
        "active_rpc": fastest_rpc,
        "status": "ROUTE_DISCOVERED",
        "traces": traces,
    }


def _run_discovery_agent(state: GraphState, intent: UserIntent) -> Optional[Dict[str, Any]]:
    """
    Tier 1 discovery: let the model establish the route, then check and encode it here.

    Returns None to fall through to the static path — when the kill switch is set, when
    no credentials are configured, or when a verified skill already covers this route.
    Raises when the agent runs but its proposal cannot be trusted, which the retry edge
    turns into another attempt.
    """
    from omnibridge.agent.budget import RequestBudget
    from omnibridge.agent.discovery_agent import DiscoveryAgent, NoRouteFound, agent_disabled
    from omnibridge.agent.synthesis import ProposalRejected, synthesize

    if agent_disabled():
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    if skill_mgr.find_skill(intent.source_chain, intent.dest_chain, intent.token):
        return None

    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    src_info = rpc_mgr.discover_chain_metadata(intent.source_chain, network=intent.network)
    fastest_rpc = rpc_mgr.get_fastest_rpc_sync(intent.source_chain)

    budget = RequestBudget(**(state.get("budget") or {}).get("limits", {}))
    agent = DiscoveryAgent()
    agent_trace: Dict[str, Any] = {}

    try:
        proposal = agent.discover(
            intent,
            src_info,
            state["target_chain"],
            budget,
            failure_context=state.get("failure_context"),
            trace=agent_trace,
        )
    except NoRouteFound as exc:
        # A correct answer, not a failure: terminates, and is never retried.
        traces = state.get("traces", {})
        traces["step_2_discovery"] = {
            "title": "Route & Contract Discovery (agent)",
            "source_type": "AGENT_NO_ROUTE",
            "outcome": "NO_ROUTE_FOUND",
            "reason": exc.reason,
            "sources": exc.sources,
            "budget": budget.as_dict(),
            "status": "COMPLETED",
        }
        raise NoRouteFound(exc.reason, exc.sources)

    w3 = None
    try:
        from web3 import Web3

        candidate = Web3(Web3.HTTPProvider(fastest_rpc, request_kwargs={"timeout": 15}))
        if candidate.is_connected() and int(candidate.eth.chain_id) == src_info.chain_id:
            w3 = candidate
    except Exception:
        w3 = None

    calldata, quote, evidence = synthesize(
        proposal, intent, src_info, w3, allowed_domains=agent.domains
    )

    _console.print(Panel(
        f"[bold green]Protocol:[/bold green] {proposal.protocol}  [dim](agent, {budget.model_calls} model call(s))[/dim]\n"
        f"[bold green]Router:[/bold green] [white]`{quote.router_address}`[/white]\n"
        f"[bold green]Function:[/bold green] {proposal.function_signature}\n"
        f"[bold green]Destination ref:[/bold green] {proposal.dest_chain_ref}\n"
        f"[bold green]Sources:[/bold green] {', '.join(proposal.sources)}\n"
        f"[bold green]Provenance:[/bold green] {'✓ verified on chain' if w3 is not None else '⚠ not checked (no usable RPC)'}\n"
        f"[bold green]Rationale:[/bold green] {proposal.rationale}",
        title="[bold green]📋 [STEP 2: AGENT-DISCOVERED ROUTE][/bold green]",
        border_style="green",
    ))

    traces = state.get("traces", {})
    traces["step_2_discovery"] = {
        "title": "Route & Contract Discovery (agent)",
        "source_type": "AGENT_RUNTIME_DISCOVERY",
        "model": agent.model,
        "agent_turns": agent_trace.get("turns", []),
        "evidence": evidence,
        "budget": budget.as_dict(),
        "active_rpc": fastest_rpc,
        "status": "COMPLETED",
    }

    return {
        "route": {
            "bridge": quote.bridge_name,
            "router_address": quote.router_address,
            "source_chain_id": quote.source_chain_id,
            "dest_chain_id": quote.dest_chain_id,
            "quote_obj": quote.model_dump(),
        },
        "active_rpc": fastest_rpc,
        "unsigned_transaction_hex": calldata,
        "status": "ROUTE_DISCOVERED",
        "budget": budget.as_dict(),
        "agent_evidence": evidence,
        "traces": traces,
    }


def route_after_error(state: GraphState) -> str:
    """
    A revert is the highest-quality signal this system produces. Feed it back.

    Bounded at three attempts, and only for failures another attempt could plausibly fix
    — a NO_ROUTE_FOUND or a budget exhaustion is not retried, because retrying it just
    spends money to reach the same answer.
    """
    if not state.get("failure_context"):
        return END
    if state.get("retry_count", 0) >= MAX_DISCOVERY_RETRIES:
        return END
    return "discovery_node"


# --- NODE 3: In-Transit Code Builder ---
def code_builder_node(state: GraphState) -> Dict[str, Any]:
    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    quote_data = state["route"]["quote_obj"]
    quote = RouteQuote(**quote_data)

    intent = UserIntent(
        token=state["asset"],
        amount=state["amount"],
        source_chain=state["source_chain"],
        dest_chain=state["target_chain"],
        recipient=state["user_address"],
    )

    calldata = CalldataExecutor.build_calldata_for_route(quote, intent, state["user_address"])
    ephemeral_script = CalldataExecutor.generate_ephemeral_python_script(
        rpc_url=state.get("active_rpc", ""),
        router_address=quote.router_address,
        calldata=calldata,
        value_wei=0,
        chain_id=quote.source_chain_id,
    )

    # PRINT 3: Then print In-Transit Codegen & Calldata
    _console.print(Panel(
        f"[bold cyan]Function Selector:[/bold cyan] {calldata[:10]}\n"
        f"[bold cyan]Calldata Byte Length:[/bold cyan] {len(calldata)} bytes\n"
        f"[bold cyan]Raw Calldata Hex Preview:[/bold cyan] {calldata[:66]}...\n\n"
        f"[bold yellow]Ephemeral In-Transit Python Script Generated in Memory:[/bold yellow]\n{ephemeral_script.strip()}",
        title="[bold cyan]⚙️ [STEP 3: JIT IN-TRANSIT CODEGEN & CALLDATA SYNTHESIS][/bold cyan]",
        border_style="cyan"
    ))

    traces = state.get("traces", {})
    traces["step_3_codegen"] = {
        "title": "In-Transit Calldata Synthesis",
        "method": "JIT Dynamic ABI Assembly (No Static Bridge SDKs)",
        "calldata_selector": calldata[:10],
        "calldata_length_bytes": len(calldata),
        "ephemeral_python_script": ephemeral_script,
        "status": "COMPLETED",
    }

    return {
        "unsigned_transaction_hex": calldata,
        "status": "PAYLOAD_BUILT",
        "traces": traces,
    }


# --- NODE 4: Dry-Run Simulator ---
def simulator_node(state: GraphState) -> Dict[str, Any]:
    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    src_name = state["source_chain"]
    src_info = rpc_mgr.discover_chain_metadata(src_name)
    rpc_url = state.get("active_rpc") or rpc_mgr.get_fastest_rpc_sync(src_name)

    # Every endpoint, preferred order, plus the chain these are supposed to be so an
    # impostor node cannot answer the only question that gates this graph.
    rpc_candidates = [rpc_url] + [u for u in src_info.rpc_urls if u != rpc_url]
    sim = Simulator(rpc_candidates, expected_chain_id=src_info.chain_id)

    sim_res = sim.simulate_call(
        from_address=state["user_address"],
        to_address=state["route"]["router_address"],
        calldata=state["unsigned_transaction_hex"],
        value_wei=0,
    )

    # PRINT 4: Then print Simulation Result
    status_color = "green" if sim_res.success else "red"
    gas_line = f"{sim_res.gas_used} gas" if sim_res.gas_used is not None else "not measured"
    usd_line = (
        f"${sim_res.estimated_gas_cost_usd:.4f}"
        if sim_res.estimated_gas_cost_usd is not None
        else "not measured"
    )
    _console.print(Panel(
        f"[bold {status_color}]Simulation Status:[/bold {status_color}] {sim_res.status.value}\n"
        f"[bold white]Target RPC Node:[/bold white] {sim_res.simulated_via_rpc or rpc_url}\n"
        f"[bold white]Verified Chain ID:[/bold white] {sim_res.simulated_on_chain_id}\n"
        f"[bold white]Gas Units Used:[/bold white] {gas_line}\n"
        f"[bold white]Estimated Gas Cost USD:[/bold white] {usd_line}\n"
        f"[bold white]Verdict:[/bold white] "
        f"{'✓ VERIFIED against a live node' if sim_res.success else '❌ NOT VERIFIED — refusing to proceed'}\n"
        + (f"[bold red]Reason:[/bold red] {sim_res.revert_reason}" if not sim_res.success else ""),
        title=f"[bold {status_color}]🧪 [STEP 4: LIVE DRY-RUN SIMULATION (eth_call)][/bold {status_color}]",
        border_style=status_color
    ))

    traces = state.get("traces", {})
    traces["step_4_simulation"] = {
        "title": "Dry-Run Simulation (eth_call)",
        "rpc_endpoint": rpc_url,
        "method": "eth_call / static gas estimation",
        "simulation_status": sim_res.status.value,
        "simulation_passed": sim_res.success,
        "verified_chain_id": sim_res.simulated_on_chain_id,
        "scope": "source leg only — the destination leg is not verifiable by eth_call",
        "gas_units": sim_res.gas_used,
        "estimated_gas_usd": sim_res.estimated_gas_cost_usd,
        "revert_reason": sim_res.revert_reason if not sim_res.success else None,
        "status": "COMPLETED" if sim_res.success else "FAILED",
    }

    return {
        "simulation_result": sim_res.model_dump(),
        "estimated_gas_usd": sim_res.estimated_gas_cost_usd or 0.0,
        "status": "SIMULATION_PASSED" if sim_res.success else "SIMULATION_FAILED",
        "error": sim_res.revert_reason if not sim_res.success else None,
        "traces": traces,
    }


def route_after_simulation(state: GraphState) -> str:
    """
    The only branch in the graph, so it has to be able to say no.

    VERIFIED means an eth_call completed against a node whose identity was checked.
    REVERTED, UNAVAILABLE and UNSUPPORTED are all refusals — in particular UNAVAILABLE
    ("could not check") must never read as a pass.
    """
    result = state.get("simulation_result") or {}
    if result.get("status") == SimulationStatus.VERIFIED.value:
        return "hitl_gate_node"
    return "error_node"


def validate_resume_payload(resume_data: Any, unsigned_hex: Optional[str]) -> str:
    """
    Turn a resume payload into a signed transaction, or refuse.

    A malformed resume is a rejection, not an approval. The gate previously fell back to
    state["unsigned_transaction_hex"] whenever resume_data was not a dict, and still
    returned USER_APPROVED — so a malformed payload carried an *unsigned* transaction
    forward as though a human had signed it.
    """
    if not isinstance(resume_data, dict):
        raise ValueError(
            f"Approval Error: resume payload was {type(resume_data).__name__}, expected "
            "an object containing 'signed_transaction_hex'. Treating this as a rejection."
        )

    signed_hex = resume_data.get("signed_transaction_hex")
    if not isinstance(signed_hex, str) or not signed_hex.strip():
        raise ValueError(
            "Approval Error: resume payload carried no 'signed_transaction_hex'. "
            "Nothing was signed, so nothing is approved."
        )

    signed_hex = signed_hex.strip()
    if signed_hex == (unsigned_hex or "").strip():
        raise ValueError(
            "Approval Error: the resumed payload is the unsigned calldata, not a signed "
            "transaction. Refusing to treat it as approved."
        )
    return signed_hex


# --- NODE 5: Human-in-the-Loop Interrupt Gate ---
def hitl_gate_node(state: GraphState) -> Dict[str, Any]:
    approval_payload = {
        "status": "AWAITING_USER_APPROVAL",
        "route": {
            "source": state["source_chain"],
            "target": state["target_chain"],
            "bridge": state["route"]["bridge"],
            "router_address": state["route"]["router_address"],
        },
        "unsigned_transaction_hex": state["unsigned_transaction_hex"],
        "estimated_gas_usd": state["estimated_gas_usd"],
        "slippage": state["slippage"],
        "traces": state.get("traces", {}),
    }

    # Native LangGraph Interrupt: Pauses graph and yields telemetry + approval payload
    resume_data = interrupt(approval_payload)

    signed_hex = validate_resume_payload(resume_data, state.get("unsigned_transaction_hex"))

    return {
        "signed_transaction_hex": signed_hex,
        "status": "USER_APPROVED",
    }


# --- NODE 6: Broadcast & Skill Save ---
def broadcast_skill_node(state: GraphState) -> Dict[str, Any]:
    src_chain_info = rpc_mgr.discover_chain_metadata(state["source_chain"])
    signer = Signer(state["active_rpc"], src_chain_info.block_explorer)

    tx_payload = {
        "chainId": src_chain_info.chain_id,
        "to": state["route"]["router_address"],
        "signed_hex": state.get("signed_transaction_hex"),
        "data": state["unsigned_transaction_hex"],
        "value": 0,
    }

    receipt = signer.broadcast_transaction(tx_payload, private_key=None, dry_run=False)

    # Persist SKILL.md
    skill_name = f"{state['source_chain'].lower()}_to_{state['target_chain'].lower()}_{state['asset'].lower()}"
    metadata = SkillMetadata(
        name=skill_name,
        description=f"Verified bridge workflow for {state['asset']} from {state['source_chain']} to {state['target_chain']} via {state['route']['bridge']}",
        source_chain=state["source_chain"],
        source_chain_id=src_chain_info.chain_id,
        dest_chain=state["target_chain"],
        dest_chain_id=200,
        token=state["asset"],
        bridge_protocol=state["route"]["bridge"],
        router_address=state["route"]["router_address"],
        last_verified=receipt.timestamp[:10],
        rpc_fallbacks=src_chain_info.rpc_urls,
        tags=["cross-chain", state["asset"].lower(), state["source_chain"].lower(), state["target_chain"].lower()],
    )

    skill_body = f"""# {skill_name.upper()} Execution Routine

## Route Metadata
- **Bridge:** {state['route']['bridge']}
- **Router Contract:** `{metadata.router_address}`

## Procedural Execution Code
```python
# Auto-generated via LangGraph Node 6
unsigned_hex = "{state['unsigned_transaction_hex']}"
```
"""

    skill_path = skill_mgr.save_skill(metadata, skill_body, target_dir=str(LOCAL_SKILLS_DIR / skill_name))

    traces = state.get("traces", {})
    traces["step_6_broadcast"] = {
        "title": "On-Chain Broadcast & SKILL.md Registration",
        "tx_hash": receipt.tx_hash,
        "explorer_url": receipt.explorer_url,
        "persisted_skill_path": skill_path,
        "status": "COMPLETED",
    }

    return {
        "status": "CONFIRMED",
        "tx_hash": receipt.tx_hash,
        "skill_path": skill_path,
        "traces": traces,
    }


def error_node(state: GraphState) -> Dict[str, Any]:
    """
    Terminal for an unrecoverable failure, and the hinge of the retry edge.

    Where the failure is one the agent could learn from — a revert, a rejected proposal —
    it is packaged as context for another attempt rather than discarded.
    """
    sim = state.get("simulation_result") or {}
    reason = state.get("error") or sim.get("revert_reason")
    retryable = sim.get("status") in ("REVERTED", "UNAVAILABLE") and bool(reason)

    failure_context = None
    if retryable and state.get("retry_count", 0) < MAX_DISCOVERY_RETRIES:
        route = state.get("route") or {}
        failure_context = (
            f"Router {route.get('router_address')} with the parameters you gave was "
            f"rejected at simulation: {reason}. Establish what was wrong before proposing "
            "again, and do not repeat the same router and arguments."
        )

    traces = state.get("traces", {})
    traces["step_error"] = {
        "title": "Diagnostic",
        "error": reason,
        "simulation_status": sim.get("status"),
        "retry_count": state.get("retry_count", 0),
        "will_retry": failure_context is not None,
    }

    return {
        "status": "ERROR",
        "error": reason or "Unknown execution error occurred",
        "failure_context": failure_context,
        "retry_count": state.get("retry_count", 0) + (1 if failure_context else 0),
        "traces": traces,
    }


# Build LangGraph State Machine
builder = StateGraph(GraphState)

builder.add_node("intent_parser_node", intent_parser_node)
builder.add_node("discovery_node", discovery_node)
builder.add_node("code_builder_node", code_builder_node)
builder.add_node("simulator_node", simulator_node)
builder.add_node("hitl_gate_node", hitl_gate_node)
builder.add_node("broadcast_skill_node", broadcast_skill_node)
builder.add_node("error_node", error_node)

builder.add_edge(START, "intent_parser_node")
builder.add_edge("intent_parser_node", "discovery_node")
builder.add_edge("discovery_node", "code_builder_node")
builder.add_edge("code_builder_node", "simulator_node")
builder.add_conditional_edges("simulator_node", route_after_simulation, ["hitl_gate_node", "error_node"])
builder.add_edge("hitl_gate_node", "broadcast_skill_node")
builder.add_edge("broadcast_skill_node", END)
# The Diagnostic node is no longer a dead end: a revert returns to Discovery with the
# reason in context, bounded by MAX_DISCOVERY_RETRIES.
builder.add_conditional_edges("error_node", route_after_error, ["discovery_node", END])

checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)
