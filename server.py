"""
FastAPI Server Exposing LangGraph Bridge Execution Engine.
Manages /api/bridge/start and /api/bridge/resume using native LangGraph state checkpoints.
"""

import uuid
from typing import Optional
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
    """
    A bridging request. Nothing about the transfer is defaulted.

    Every field below used to carry one: sourceChain "Base", targetChain "Solana",
    amount 100.0, asset "USDC". So an intent the parser could not read — "do something
    vague" — did not fail; it silently became a request to move 100 USDC from Base to
    Solana. The graph's own intent parser refuses that case, but it never saw it,
    because these defaults had already filled the gap before the graph was called.

    `userAddress` was worse: it defaulted to a real, hardcoded mainnet address. A
    request with no wallet connected simulated against a stranger's balances and
    allowances, which says nothing about the caller's transfer and encodes that
    stranger as the recipient.
    """

    input: Optional[str] = Field(default=None, example="Bridge 10 USDC from Base to Solana")
    sourceChain: Optional[str] = Field(default=None, example="Base")
    targetChain: Optional[str] = Field(default=None, example="Arbitrum")
    amount: Optional[float] = Field(default=None, example=100.0)
    asset: Optional[str] = Field(default=None, example="USDC")
    userAddress: str = Field(
        ...,
        description="Connected wallet address. Required: a simulation from any other "
                    "account does not describe this transfer.",
        example="0x0000000000000000000000000000000000000000",
    )
    slippage: Optional[str] = Field(default="0.5%")


class ResumeBridgeRequest(BaseModel):
    thread_id: str
    signed_transaction_hex: Optional[str] = None


from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    server_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@server_app.get("/")
def root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {
        "service": "Omnibridge Headless LangGraph Bridging Engine",
        "docs": "/docs",
        "health": "/health",
        "skills": "/api/skills",
    }


@server_app.get("/api")
def api_info():
    return {
        "service": "Omnibridge Headless LangGraph Bridging Engine",
        "docs": "/docs",
        "health": "/health",
        "skills": "/api/skills",
        "endpoints": {
            "start": "POST /api/bridge/start",
            "resume": "POST /api/bridge/resume",
        },
    }


@server_app.get("/health")
def health():
    return {"status": "HEALTHY", "runtime": "LangGraph State Machine Engine"}


def _state_response(thread_id: str, values: dict) -> dict:
    """
    Package a graph state for the browser, the same way on every path.

    The error branch used to return `{status, thread_id, error}` and drop the traces,
    so a run that failed showed the user nothing about *where* it failed. Everything
    the page needs to render an honest verdict is here, including the fields that say
    what was not checked.
    """
    sim = values.get("simulation_result") or {}
    traces = values.get("traces") or {}
    diagnostic = traces.get("step_error") or {}

    return {
        "status": values.get("status", "UNKNOWN"),
        "thread_id": thread_id,
        "error": values.get("error"),
        "route": values.get("route"),
        "unsigned_transaction_hex": values.get("unsigned_transaction_hex"),
        "verification": {
            "simulation_status": sim.get("status"),
            "simulated_on_chain_id": sim.get("simulated_on_chain_id"),
            "simulated_via_rpc": sim.get("simulated_via_rpc"),
            "gas_used": sim.get("gas_used"),
            "revert_reason": sim.get("revert_reason"),
        },
        "agent_evidence": values.get("agent_evidence"),
        "budget": values.get("budget"),
        # Surfaced so the page can say a cached route was retired, rather than the
        # skill quietly vanishing from the library between one run and the next.
        "replayed_skill": values.get("replayed_skill"),
        "demoted_skill": diagnostic.get("demoted_skill"),
        "attempts": diagnostic.get("attempts", []),
        "retry_count": values.get("retry_count", 0),
        "traces": traces,
    }


@server_app.post("/api/bridge/start")
def start_bridge_flow(req: StartBridgeRequest):
    """
    Runs the graph to the approval gate and returns whatever actually happened.

    Three outcomes, all reported with their traces: the interrupt payload when a route
    cleared simulation, a terminal state when it did not, and an exception surfaced as
    a state rather than swallowed.
    """
    thread_id = f"thread_{uuid.uuid4().hex[:12]}"
    config = {"configurable": {"thread_id": thread_id}}

    # Only synthesise a sentence when every field for one was actually supplied.
    # Interpolating None produced "Bridge None None from None to None", which the regex
    # then failed to match, falling through to whatever the defaults happened to be.
    structured = all(v is not None for v in (req.amount, req.asset, req.sourceChain, req.targetChain))
    raw_input = req.input or (
        f"Bridge {req.amount} {req.asset} from {req.sourceChain} to {req.targetChain}"
        if structured
        else ""
    )

    initial_state = {
        "raw_input": raw_input,
        "source_chain": req.sourceChain or "",
        "target_chain": req.targetChain or "",
        "amount": req.amount or 0.0,
        "asset": req.asset or "",
        "user_address": req.userAddress,
        "slippage": req.slippage,
        "traces": {},
        "retry_count": 0,
    }

    try:
        for _event in graph.stream(initial_state, config=config):
            pass
    except Exception as exc:
        # An intent that cannot be parsed raises before any node writes state. Report
        # the reason; never a generic failure the user cannot act on.
        partial = {}
        try:
            partial = graph.get_state(config).values or {}
        except Exception:
            pass
        response = _state_response(thread_id, partial)
        response["status"] = "ERROR"
        response["error"] = str(exc)
        return response

    snapshot = graph.get_state(config)
    if snapshot.tasks and snapshot.tasks[0].interrupts:
        payload = dict(snapshot.tasks[0].interrupts[0].value)
        payload["thread_id"] = thread_id
        return payload

    return _state_response(thread_id, snapshot.values or {})


@server_app.post("/api/bridge/resume")
def resume_bridge_flow(req: ResumeBridgeRequest):
    """
    Resumes from the approval gate and reports the outcome.

    This used to return a literal `"status": "CONFIRMED"` regardless of what the graph
    did, and raise a 500 on a rejected payload — so a resume that was refused for
    carrying no signature came back either as success or as a server fault. Neither is
    true: a refused approval is a refusal, and it belongs in the response body with its
    reason.
    """
    config = {"configurable": {"thread_id": req.thread_id}}

    try:
        snapshot = graph.get_state(config)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Unknown thread {req.thread_id}: {exc}")

    if not (snapshot.tasks and snapshot.tasks[0].interrupts):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Thread {req.thread_id} is not paused at the approval gate "
                f"(status {snapshot.values.get('status')!r}); there is nothing to resume."
            ),
        )

    resume_payload = {"signed_transaction_hex": req.signed_transaction_hex}

    try:
        for _event in graph.stream(Command(resume=resume_payload), config=config):
            pass
    except Exception as exc:
        # validate_resume_payload raises here when nothing was actually signed. That is
        # a rejection, reported as one.
        return {
            "status": "REJECTED",
            "thread_id": req.thread_id,
            "error": str(exc),
        }

    final = graph.get_state(config).values or {}
    response = _state_response(req.thread_id, final)
    response["tx_hash"] = final.get("tx_hash")
    response["skill_persisted"] = final.get("skill_path")
    response["explorer_url"] = (final.get("traces", {}).get("step_6_broadcast") or {}).get(
        "explorer_url"
    )
    return response


@server_app.get("/api/skills")
def list_skills():
    """
    Lists persisted SKILL.md modules, including demoted ones.

    Verification state is part of the listing. A skill whose last run failed is still
    on disk — retained deliberately, so the demotion is reversible — but it is no
    longer served, and the library should not present it as though it were.
    """
    return [
        {
            "name": s.metadata.name,
            "description": s.metadata.description,
            "source": s.metadata.source_chain,
            "destination": s.metadata.dest_chain_ref or s.metadata.dest_chain,
            "token": s.metadata.token,
            "protocol": s.metadata.bridge_protocol,
            "router": s.metadata.router_address,
            "function_signature": s.metadata.function_signature,
            "network": s.metadata.network,
            "verified": s.metadata.verified,
            "verification_count": s.metadata.verification_count,
            "last_verified": s.metadata.last_verified,
            "last_failure": s.metadata.last_failure,
            "sources": s.metadata.sources,
            "discovered_by": s.metadata.discovered_by,
            "revisions": len(s.metadata.revisions),
            "path": s.file_path,
        }
        for s in skill_mgr.list_skills()
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(server_app, host="0.0.0.0", port=8000)
