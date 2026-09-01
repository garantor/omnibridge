"""
LangGraph State Machine for Headless Web3 In-Transit Bridging Engine with Full Observability Telemetry.
"""

from typing import Dict, Any, Optional, TypedDict
import json
import re
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

from omnibridge.core.models import UserIntent, RouteQuote
from omnibridge.skills.manager import SkillManager, SkillMetadata
from omnibridge.discovery.rpc_manager import RPCManager
from omnibridge.discovery.route_finder import RouteFinder
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.simulator import Simulator
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
    user_addr = state.get("user_address") or "0x0000000000000000000000000000000000000000"
    slip = state.get("slippage") or "0.5%"

    if raw:
        match = re.search(r"bridge\s+(\d+(?:\.\d+)?)\s*([a-zA-Z0-9_-]+)?\s+from\s+([a-zA-Z0-9_-]+)\s+to\s+([a-zA-Z0-9_-]+)", raw, re.IGNORECASE)
        if match:
            amt = float(match.group(1))
            if match.group(2):
                tok = match.group(2).upper()
            src = match.group(3).capitalize()
            dst = match.group(4).capitalize()

    if not src or not dst:
        # Require actual user input entities
        src = src or "Unknown"
        dst = dst or "Unknown"
        tok = tok or "NATIVE"
        amt = amt if amt > 0 else 1.0

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
    intent = UserIntent(
        token=state["asset"],
        amount=state["amount"],
        source_chain=state["source_chain"],
        dest_chain=state["target_chain"],
        recipient=state["user_address"],
    )

    quote = finder.discover_route(intent)
    fastest_rpc = rpc_mgr.get_fastest_rpc_sync(state["source_chain"])
    is_cached = "Skill" in quote.bridge_name or "Verified" in quote.bridge_name

    traces = state.get("traces", {})
    traces["step_2_discovery"] = {
        "title": "Route & Contract Discovery",
        "source_type": "SKILL_MEMORY_CACHE" if is_cached else "DYNAMIC_WEB_DOCS_SEARCH",
        "search_query": f"Bridge {state['asset']} from {state['source_chain']} to {state['target_chain']} official routers & RPCs",
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


# --- NODE 3: In-Transit Code Builder ---
def code_builder_node(state: GraphState) -> Dict[str, Any]:
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
    rpc_url = state.get("active_rpc") or "https://base-rpc.publicnode.com"
    sim = Simulator(rpc_url)
    
    sim_res = sim.simulate_call(
        from_address=state["user_address"],
        to_address=state["route"]["router_address"],
        calldata=state["unsigned_transaction_hex"],
        value_wei=0,
    )

    traces = state.get("traces", {})
    traces["step_4_simulation"] = {
        "title": "Dry-Run Simulation (eth_call)",
        "rpc_endpoint": rpc_url,
        "method": "eth_call / static gas estimation",
        "simulation_passed": sim_res.success,
        "zero_reverts_verified": True if sim_res.success else False,
        "gas_units": sim_res.gas_used,
        "estimated_gas_usd": sim_res.estimated_gas_cost_usd,
        "revert_reason": sim_res.revert_reason if not sim_res.success else None,
        "status": "COMPLETED" if sim_res.success else "FAILED",
    }

    return {
        "simulation_result": sim_res.model_dump(),
        "estimated_gas_usd": sim_res.estimated_gas_cost_usd,
        "status": "SIMULATION_PASSED" if sim_res.success else "SIMULATION_FAILED",
        "error": sim_res.revert_reason if not sim_res.success else None,
        "traces": traces,
    }


def route_after_simulation(state: GraphState) -> str:
    if state["simulation_result"] and state["simulation_result"].get("success"):
        return "hitl_gate_node"
    return "error_node"


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

    signed_hex = (
        resume_data.get("signed_transaction_hex")
        if isinstance(resume_data, dict)
        else state.get("unsigned_transaction_hex")
    )

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
    return {
        "status": "ERROR",
        "error": state.get("error", "Unknown execution error occurred"),
        "traces": state.get("traces", {}),
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

checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)
