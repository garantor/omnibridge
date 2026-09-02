"""
HTTP surface regressions.

The API layer carried its own copy of the fail-open habit, and it survived every
guardrail added inside the graph — because it filled the gaps in a request *before*
the graph ever saw it. These tests assert the refusals.
"""

import pytest
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
