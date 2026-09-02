"""
HTTP surface regressions.

The API layer carried its own copy of the fail-open habit, and it survived every
guardrail added inside the graph — because it filled the gaps in a request *before*
the graph ever saw it. These tests assert the refusals.
"""

import pytest
import time

from fastapi.testclient import TestClient

from server import server_app

client = TestClient(server_app)

WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


# --------------------------------------------------------------- request shape

def test_a_request_without_a_wallet_is_rejected():
    """
    `userAddress` defaulted to a real, hardcoded mainnet address, so an unconnected
    user got a simulation against a stranger's balances and allowances — which says
    nothing about their own transfer — and that stranger encoded as the recipient.
    """
    res = client.post("/api/bridge/start", json={"input": "bridge 5 usdc from base to arbitrum"})
    assert res.status_code == 422
    assert any("userAddress" in str(e.get("loc", "")) for e in res.json()["detail"])


def test_an_unparseable_intent_is_not_completed_from_defaults():
    """
    The request model defaulted sourceChain "Base", targetChain "Solana", amount 100.0
    and asset "USDC", so "do something vague" silently became a request to move 100
    USDC from Base to Solana. The graph refuses that input; it just never saw it.
    """
    res = client.post("/api/bridge/start", json={"input": "do something vague", "userAddress": WALLET})
    body = res.json()
    assert body["status"] == "ERROR"
    assert "could not determine" in body["error"]
    assert "solana" not in body["error"].lower()


def test_an_empty_request_does_not_invent_a_transfer():
    res = client.post("/api/bridge/start", json={"userAddress": WALLET})
    body = res.json()
    assert body["status"] == "ERROR"
    assert "could not determine" in body["error"]


# ------------------------------------------------------------ response shape

@pytest.mark.network
def test_a_failed_run_still_returns_its_traces():
    """
    The error branch returned `{status, thread_id, error}` and dropped the traces, so
    a user saw that something failed but nothing about where.
    """
    res = client.post(
        "/api/bridge/start",
        json={"input": "swap 100 usdc from base to stellar", "userAddress": WALLET},
    )
    body = res.json()
    assert body["status"] == "NO_ROUTE_FOUND"
    assert "step_1_intent" in body["traces"]
    assert "step_2_discovery" in body["traces"]
    assert body["verification"]["simulation_status"] is None


# ------------------------------------------------------------------- resume

def test_resuming_an_unknown_thread_is_refused():
    res = client.post("/api/bridge/resume", json={"thread_id": "nope", "signed_transaction_hex": "0xabc"})
    assert res.status_code in (404, 409)
    assert "nothing to resume" in res.json()["detail"] or "Unknown thread" in res.json()["detail"]


def test_the_response_status_always_comes_from_the_graph():
    """
    The resume handler returned a literal CONFIRMED regardless of what the graph did,
    including when the approval payload carried no signature. Status is now read from
    state, so a state that never reached broadcast cannot report success.
    """
    from server import _state_response

    assert _state_response("t", {})["status"] == "UNKNOWN"
    assert _state_response("t", {"status": "ERROR", "error": "boom"})["status"] == "ERROR"
    assert _state_response("t", {"status": "CONFIRMED"})["status"] == "CONFIRMED"


def test_a_resume_carrying_no_signature_is_a_rejection(monkeypatch):
    """
    An approval is a signature. A payload without one is refused — and refused as a
    rejection in the response body, not as a 500, which would read as a server fault
    rather than the guardrail doing its job.
    """
    import server
    from graph import validate_resume_payload

    with pytest.raises(ValueError) as excinfo:
        validate_resume_payload({"signed_transaction_hex": ""}, "0xdeadbeef")
    assert "Nothing was signed" in str(excinfo.value)

    class _Paused:
        values = {"status": "AWAITING_USER_APPROVAL"}
        tasks = [type("T", (), {"interrupts": [object()]})()]

    def _boom(*args, **kwargs):
        raise ValueError("Approval Error: resume payload carried no signature.")

    monkeypatch.setattr(server.graph, "get_state", lambda *a, **k: _Paused())
    monkeypatch.setattr(server.graph, "stream", _boom)

    res = client.post("/api/bridge/resume", json={"thread_id": "t", "signed_transaction_hex": None})
    assert res.status_code == 200
    assert res.json()["status"] == "REJECTED"
    assert "no signature" in res.json()["error"]


# -------------------------------------------------------------------- skills

def test_the_skills_listing_states_verification_status():
    """A demoted skill is still listed, and must not read as a verified one."""
    res = client.get("/api/skills")
    assert res.status_code == 200
    for skill in res.json():
        assert "verified" in skill
        assert "last_failure" in skill
        assert "verification_count" in skill


# --------------------------------------------------------------------- caching

def test_the_ui_is_never_cached():
    """
    The page is the approval surface: a stale copy can show a verdict that does not
    match what the server computed. Asset URLs carry a version query, but that only
    helps if the document naming them is itself fresh — and index.html had no
    cache-busting of its own, so a browser kept serving an old page pointing at an old
    bundle, complete with the hardcoded verdicts that bundle contained.
    """
    for path in ("/", "/static/app.js", "/static/style.css"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert "no-store" in res.headers.get("cache-control", ""), path


# ------------------------------------------------------------- multi-step plans

def test_advancing_a_plan_on_an_unknown_thread_is_refused():
    res = client.post("/api/bridge/advance",
                      json={"thread_id": "nope", "step_index": 0, "tx_hash": "0x" + "ab" * 32})
    assert res.status_code == 409
    assert "no multi-step plan" in res.json()["detail"]


def test_the_hitl_payload_carries_the_plan():
    """
    The browser signs from the plan, not from a single hex blob, so a two-transaction
    transfer has one code path rather than a special case.
    """
    import inspect

    import graph

    source = inspect.getsource(graph.hitl_gate_node)
    assert '"plan": state.get("plan")' in source


# --------------------------------------------------------------- live log stream

def test_the_log_stream_is_server_sent_events():
    """
    Discovery takes a minute or more and /api/bridge/start does not return until it is
    over, so the browser subscribes with a client-generated run id *before* starting —
    a stream attached afterwards would have nothing left to show.

    A run is ended from another thread here: the stream stays open until the run
    finishes, which is the behaviour under test, and a test that never ends it hangs.
    """
    import threading

    from omnibridge.steplog import LOG_BUS

    run_id = "run_test_sse"

    def finish():
        for _ in range(100):
            if LOG_BUS.has_watchers(run_id):
                LOG_BUS.publish(run_id, {"t": 0.1, "marker": "OK", "level": "green",
                                         "scope": "discovery", "message": "router verified"})
                LOG_BUS.publish(run_id, {"_end": True})
                return
            time.sleep(0.02)

    threading.Thread(target=finish, daemon=True).start()

    with client.stream("GET", f"/api/logs/{run_id}") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        assert "no-store" in res.headers.get("cache-control", "")
        body = "".join(res.iter_text())

    assert "router verified" in body
    assert "event: end" in body


def test_records_reach_a_subscriber(monkeypatch):
    from omnibridge.steplog import LOG_BUS, StepLogger, run_context

    watcher = LOG_BUS.subscribe("run_x")
    with run_context("run_x"):
        StepLogger("discovery").ok("router holds bytecode: True")

    record = watcher.get_nowait()
    assert record["scope"] == "discovery"
    assert record["message"] == "router holds bytecode: True"
    assert record["level"] == "green"
    LOG_BUS.unsubscribe("run_x", watcher)


def test_records_outside_a_run_go_nowhere():
    """No run bound means terminal only; a stray record must not leak into another run."""
    from omnibridge.steplog import LOG_BUS, StepLogger

    watcher = LOG_BUS.subscribe("run_y")
    StepLogger("discovery").ok("not part of any run")
    import queue

    with pytest.raises(queue.Empty):
        watcher.get_nowait()
    LOG_BUS.unsubscribe("run_y", watcher)


def test_a_slow_watcher_cannot_stall_the_run():
    """Logging must never be able to block a transfer."""
    import queue

    from omnibridge.steplog import LogBus

    bus = LogBus()
    watcher = bus.subscribe("run_z")
    for _ in range(watcher.maxsize + 50):
        bus.publish("run_z", {"message": "x"})   # must not raise or block
    assert watcher.qsize() == watcher.maxsize


def test_markup_is_stripped_for_the_browser():
    from omnibridge.steplog import strip_markup

    assert strip_markup("[bold]gemini[/bold] ok [dim](0.4s)[/dim]") == "gemini ok (0.4s)"
