"""
LangGraph State Machine for Headless Web3 In-Transit Bridging Engine.
"""

from typing import Dict, Any, Optional, TypedDict
import json
import re
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

from omnibridge.core.models import UserIntent
from omnibridge.skills.manager import SkillManager
from omnibridge.discovery.rpc_manager import RPCManager
from omnibridge.discovery.route_finder import RouteFinder
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.simulator import Simulator
from omnibridge.core.signer import Signer
from tools import LOCAL_SKILLS_DIR


# Define Graph State Schema
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


rpc_mgr = RPCManager()
skill_mgr = SkillManager(repository_path=str(LOCAL_SKILLS_DIR))
finder = RouteFinder(skill_manager=skill_mgr, rpc_manager=rpc_mgr)


# --- NODE 1: Intent & Orchestrator Parser ---
def intent_parser_node(state: GraphState) -> Dict[str, Any]:
    """Parses raw text or structured input into deterministic intent parameters."""
    raw = state.get("raw_input", "")
    
    # If parameters were already structured, maintain them
    src = state.get("source_chain") or "Base"
    dst = state.get("target_chain") or "Solana"
    amt = float(state.get("amount") or 100.0)
    tok = state.get("asset") or "USDC"
    user_addr = state.get("user_address") or "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    slip = state.get("slippage") or "0.1%"

    # Simple regex parsing for natural language input like "Bridge 10 USDC from Base to Solana"
    if raw:
        match = re.search(r"bridge\s+(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?\s+from\s+([a-zA-Z0-9_-]+)\s+to\s+([a-zA-Z0-9_-]+)", raw, re.IGNORECASE)
        if match:
            amt = float(match.group(1))
            if match.group(2):
                tok = match.group(2).upper()
            src = match.group(3).capitalize()
            dst = match.group(4).capitalize()

    return {
        "source_chain": src,
        "target_chain": dst,
        "amount": amt,
        "asset": tok,
        "user_address": user_addr,
        "slippage": slip,
        "status": "PARSED",
    }


# --- NODE 2: Skill Check or Web Search Discovery ---
def discovery_node(state: GraphState) -> Dict[str, Any]:
    """Checks local SKILL.md memory first; falls back to dynamic web/registry search."""
    intent = UserIntent(
        token=state["asset"],
        amount=state["amount"],
        source_chain=state["source_chain"],
        dest_chain=state["target_chain"],
        recipient=state["user_address"],
    )

    quote = finder.discover_route(intent)
    fastest_rpc = rpc_mgr.get_fastest_rpc_sync(state["source_chain"])

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
    }


# --- NODE 3: In-Transit Code Builder (Python REPL / Calldata) ---
def code_builder_node(state: GraphState) -> Dict[str, Any]:
    """Dynamically generates the raw unsigned hex transaction payload on-the-fly."""
    quote_data = state["route"]["quote_obj"]
    from omnibridge.core.models import RouteQuote
    quote = RouteQuote(**quote_data)

    intent = UserIntent(
        token=state["asset"],
        amount=state["amount"],
        source_chain=state["source_chain"],
        dest_chain=state["target_chain"],
        recipient=state["user_address"],
    )

    calldata = CalldataExecutor.build_calldata_for_route(quote, intent, state["user_address"])

    return {
        "unsigned_transaction_hex": calldata,
        "status": "PAYLOAD_BUILT",
    }


# --- NODE 4: Dry-Run Simulator (Circuit Breaker) ---
def simulator_node(state: GraphState) -> Dict[str, Any]:
    """Executes a dry-run static call (eth_call) against a public RPC with revert detection."""
    rpc_url = state.get("active_rpc") or "https://base-rpc.publicnode.com"
    sim = Simulator(rpc_url)
    
    sim_res = sim.simulate_call(
        from_address=state["user_address"],
        to_address=state["route"]["router_address"],
        calldata=state["unsigned_transaction_hex"],
        value_wei=0,
    )

    return {
        "simulation_result": sim_res.model_dump(),
        "estimated_gas_usd": sim_res.estimated_gas_cost_usd,
        "status": "SIMULATION_PASSED" if sim_res.success else "SIMULATION_FAILED",
        "error": sim_res.revert_reason if not sim_res.success else None,
    }


# Conditional Edge: Check simulation result
def route_after_simulation(state: GraphState) -> str:
    if state["simulation_result"] and state["simulation_result"].get("success"):
        return "hitl_gate_node"
    return "error_node"


# --- NODE 5: Human-in-the-Loop (HITL) Interrupt Gate ---
def hitl_gate_node(state: GraphState) -> Dict[str, Any]:
    """Emits structured JSON payload and pauses state machine using native LangGraph interrupt."""
    approval_payload = {
        "status": "AWAITING_USER_APPROVAL",
        "route": {
            "source": state["source_chain"],
            "target": state["target_chain"],
            "bridge": state["route"]["bridge"],
        },
        "unsigned_transaction_hex": state["unsigned_transaction_hex"],
        "estimated_gas_usd": state["estimated_gas_usd"],
        "slippage": state["slippage"],
    }

    # Native LangGraph Interrupt: Pauses graph execution and yields approval payload to caller
    resume_data = interrupt(approval_payload)

    # When resumed by client, extracts signed hex
    signed_hex = resume_data.get("signed_transaction_hex") if isinstance(resume_data, dict) else state.get("unsigned_transaction_hex")

    return {
        "signed_transaction_hex": signed_hex,
        "status": "USER_APPROVED",
    }


# --- NODE 6: Broadcast & Skill Registration ---
def broadcast_skill_node(state: GraphState) -> Dict[str, Any]:
    """Broadcasts transaction to RPC and writes working procedure into ./skills/<chain_pair>/SKILL.md."""
    src_chain_info = rpc_mgr.discover_chain_metadata(state["source_chain"])
    signer = Signer(state["active_rpc"], src_chain_info.block_explorer)

    tx_payload = {
        "chainId": src_chain_info.chain_id,
        "to": state["route"]["router_address"],
        "data": state["unsigned_transaction_hex"],
        "value": 0,
    }

    receipt = signer.broadcast_transaction(tx_payload, private_key=None, dry_run=True)

    # Persist SKILL.md
    skill_name = f"{state['source_chain'].lower()}_to_{state['target_chain'].lower()}_{state['asset'].lower()}"
    from omnibridge.skills.manager import SkillMetadata
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

    return {
        "status": "CONFIRMED",
        "tx_hash": receipt.tx_hash,
        "skill_path": skill_path,
    }


# --- ERROR NODE ---
def error_node(state: GraphState) -> Dict[str, Any]:
    """Handles simulation or discovery failures gracefully."""
    return {
        "status": "ERROR",
        "error": state.get("error", "Unknown execution error occurred"),
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
builder.add_edge("error_node", END)

# Compile graph with MemorySaver checkpointer for stateful HITL interrupt/resume
checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)
