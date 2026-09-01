"""
FastAPI Server Exposing LangGraph Bridge Execution Engine.
Manages /api/bridge/start and /api/bridge/resume using native LangGraph state checkpoints.
"""

import uuid
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from langgraph.types import Command
from graph import graph, skill_mgr

server_app = FastAPI(
    title="LangGraph Web3 Bridging Gateway",
    description="Headless Agentic Bridging Engine built with LangGraph State Machine",
    version="2.0.0",
)


class StartBridgeRequest(BaseModel):
    input: Optional[str] = Field(default=None, example="Bridge 10 USDC from Base to Solana")
    sourceChain: Optional[str] = Field(default="Base", example="Base")
    targetChain: Optional[str] = Field(default="Solana", example="Solana")
    amount: Optional[float] = Field(default=100.0, example=100.0)
    asset: Optional[str] = Field(default="USDC", example="USDC")
    userAddress: Optional[str] = Field(default="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    slippage: Optional[str] = Field(default="0.1%")


class ResumeBridgeRequest(BaseModel):
    thread_id: str
    signed_transaction_hex: Optional[str] = None


@server_app.get("/health")
def health():
    return {"status": "HEALTHY", "runtime": "LangGraph State Machine Engine"}


@server_app.post("/api/bridge/start")
def start_bridge_flow(req: StartBridgeRequest):
    """
    Executes LangGraph Node 1 -> Node 2 -> Node 3 -> Node 4 -> Node 5 (HITL Interrupt).
    Pauses execution at the approval gate and yields the AWAITING_USER_APPROVAL payload.
    """
    thread_id = f"thread_{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "raw_input": req.input or f"Bridge {req.amount} {req.asset} from {req.sourceChain} to {req.targetChain}",
        "source_chain": req.sourceChain,
        "target_chain": req.targetChain,
        "amount": req.amount,
        "asset": req.asset,
        "user_address": req.userAddress,
        "slippage": req.slippage,
    }

    # Run the graph until the interrupt in Node 5
    for event in graph.stream(initial_state, config=config):
        pass

    # Inspect current state checkpoint
    snapshot = graph.get_state(config)
    if snapshot.tasks and snapshot.tasks[0].interrupts:
        # Extract the interrupt payload yielded by hitl_gate_node
        interrupt_val = snapshot.tasks[0].interrupts[0].value
        response = dict(interrupt_val)
        response["thread_id"] = thread_id
        return response

    if snapshot.values.get("status") == "ERROR":
        return {
            "status": "ERROR",
            "thread_id": thread_id,
            "error": snapshot.values.get("error", "Simulation error"),
        }

    return {"status": snapshot.values.get("status"), "thread_id": thread_id}


@server_app.post("/api/bridge/resume")
def resume_bridge_flow(req: ResumeBridgeRequest):
    """
    Resumes LangGraph from Node 5 HITL Interrupt, executes Node 6 (Broadcast & Skill Save).
    """
    config = {"configurable": {"thread_id": req.thread_id}}

    # Resume the graph with the signed transaction hex
    resume_payload = {"signed_transaction_hex": req.signed_transaction_hex}
    
    try:
        final_state = None
        for event in graph.stream(Command(resume=resume_payload), config=config):
            if "broadcast_skill_node" in event:
                final_state = event["broadcast_skill_node"]

        if not final_state:
            snapshot = graph.get_state(config)
            final_state = snapshot.values

        return {
            "status": "CONFIRMED",
            "thread_id": req.thread_id,
            "tx_hash": final_state.get("tx_hash"),
            "skill_persisted": final_state.get("skill_path"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resume graph: {e}")


@server_app.get("/api/skills")
def list_skills():
    """Lists all saved SKILL.md modules."""
    return [
        {
            "name": s.metadata.name,
            "description": s.metadata.description,
            "source": s.metadata.source_chain,
            "destination": s.metadata.dest_chain,
            "token": s.metadata.token,
            "path": s.file_path,
        }
        for s in skill_mgr.list_skills()
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(server_app, host="0.0.0.0", port=8000)
