"""
Phase 5 regressions: the skill cache must not be a way to persist a mistake.

Four properties, each of which was false before this phase:

  - A cache hit is scoped exactly. Matching used to be substring-based in both
    directions, so a mainnet skill answered a testnet request.
  - A failure demotes, it does not overwrite. A transient RPC error must not be able
    to replace a known-good route.
  - Every write retains what it replaced.
  - A stored route is a procedure, not one frozen transaction.
"""

import json
import tempfile
from pathlib import Path

import pytest

from omnibridge.core.executor import CalldataExecutor
from omnibridge.skills.manager import SkillManager, SkillMetadata

GOLDEN = json.loads((Path(__file__).parent / "golden" / "cctp_base.json").read_text())


def _meta(**overrides):
    base = dict(
        name="base_to_arbitrum_usdc",
        description="test route",
        source_chain="Base",
        source_chain_id=8453,
        dest_chain="arbitrum",
        dest_chain_id=42161,
        token="USDC",
        bridge_protocol="Circle CCTP",
        router_address="0x1682Ae6375C4E4A97e4B583BC394c861A46D8962",
        aliases=["base", "arbitrum"],
    )
    base.update(overrides)
    return SkillMetadata(**base)


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as tmp:
        yield SkillManager(repository_path=tmp)


# ------------------------------------------------------------------ cache scoping

def test_exact_match_hits(repo):
    repo.save_skill(_meta(), "# body")
    assert repo.find_skill("base", "arbitrum", "USDC") is not None


def test_a_mainnet_skill_does_not_answer_a_testnet_request(repo):
    """
    The bug this replaces: `m_src in s_src or s_src in m_src` meant "base" matched
    "base sepolia", so a mainnet router was returned for a testnet transfer. It
    simulates cleanly and sends real funds to the wrong network.
    """
    repo.save_skill(_meta(network="mainnet"), "# body")
    assert repo.find_skill("base sepolia", "arbitrum sepolia", "USDC") is None
    assert repo.find_skill("base", "arbitrum", "USDC", network="testnet") is None


def test_a_substring_of_a_chain_name_is_not_a_match(repo):
    repo.save_skill(_meta(), "# body")
    assert repo.find_skill("bas", "arbitrum", "USDC") is None
    assert repo.find_skill("base", "arb", "USDC") is None


def test_a_different_token_is_not_a_match(repo):
    repo.save_skill(_meta(), "# body")
    assert repo.find_skill("base", "arbitrum", "USDT") is None


# ----------------------------------------------------------- demote, never overwrite

def test_a_demoted_skill_stops_being_served(repo):
    repo.save_skill(_meta(), "# body")
    skill = repo.find_skill("base", "arbitrum", "USDC")

    repo.mark_unverified(skill, "REVERTED: ERC20: transfer amount exceeds allowance")

    assert repo.find_skill("base", "arbitrum", "USDC") is None
    still_there = repo.find_skill("base", "arbitrum", "USDC", include_unverified=True)
    assert still_there is not None
    assert still_there.metadata.verified is False
    assert "exceeds allowance" in still_there.metadata.last_failure


def test_demotion_does_not_touch_the_route_itself(repo):
    """
    A failure records *that* the route stopped working. It does not get to rewrite what
    the route was — the replacement must clear simulation and the human gate first.
    """
    repo.save_skill(_meta(), "# original body")
    skill = repo.find_skill("base", "arbitrum", "USDC")
    original_router = skill.metadata.router_address

    repo.mark_unverified(skill, "UNAVAILABLE: no endpoint reachable")

    after = repo.find_skill("base", "arbitrum", "USDC", include_unverified=True)
    assert after.metadata.router_address == original_router
    assert "original body" in after.content


# ---------------------------------------------------------------------- revisions

def test_every_write_retains_what_it_replaced(repo):
    repo.save_skill(_meta(), "# first")
    repo.save_skill(_meta(router_address="0x1682Ae6375C4E4A97e4B583BC394c861A46D8963"), "# second")

    skill = repo.find_skill("base", "arbitrum", "USDC")
    assert len(skill.metadata.revisions) == 1

    archived = Path(skill.file_path).parent / skill.metadata.revisions[0]
    assert archived.exists()
    assert "# first" in archived.read_text()


def test_retained_revisions_are_not_served_as_skills(repo):
    repo.save_skill(_meta(), "# first")
    repo.save_skill(_meta(), "# second")
    assert len([s for s in repo.list_skills() if s.metadata.name == "base_to_arbitrum_usdc"]) == 1


def test_verification_count_accumulates(repo):
    repo.save_skill(_meta(), "# body")
    repo.save_skill(_meta(), "# body")
    assert repo.find_skill("base", "arbitrum", "USDC").metadata.verification_count == 2


# ------------------------------------------------------- a skill is a procedure

def test_a_stored_route_replays_at_a_different_amount():
    """
    The property that makes a skill procedural memory rather than a saved transaction.

    Generalising a real CCTP call and binding it back at a new amount must reproduce
    exactly the bytes that amount would have produced — checked against a confirmed
    mainnet transaction, so the reference is not our own encoder.
    """
    fx = next(f for f in GOLDEN if f["dest_chain"] == "Arbitrum")
    recipient = "0x" + fx["param_values"][2][-40:]

    stored = CalldataExecutor.generalise_param_values(
        fx["param_values"],
        amount_raw=int(fx["amount_raw"]),
        recipient=recipient,
        sender=recipient,
    )
    assert "{amount_raw}" in stored
    assert "{recipient_bytes32}" in stored
    # The burn token and the destination domain are properties of the route, not of the
    # request, so they stay concrete.
    assert stored[1] == "3"
    assert stored[3] == fx["param_values"][3]

    replayed = CalldataExecutor.bind_param_values(
        stored, amount_raw=int(fx["amount_raw"]), recipient=recipient, sender=recipient
    )
    encoded = CalldataExecutor.encode_dynamic_calldata(
        fx["function_signature"],
        fx["param_types"],
        CalldataExecutor.coerce_param_values(fx["param_types"], replayed),
    )
    assert encoded.lower() == fx["expected_calldata"].lower()

    # And at a different amount it must differ, in the amount slot only.
    other = CalldataExecutor.bind_param_values(
        stored, amount_raw=1, recipient=recipient, sender=recipient
    )
    other_encoded = CalldataExecutor.encode_dynamic_calldata(
        fx["function_signature"],
        fx["param_types"],
        CalldataExecutor.coerce_param_values(fx["param_types"], other),
    )
    assert other_encoded != encoded
    assert CalldataExecutor.decode_leading_uint256(other_encoded) == 1
    assert other_encoded[74:] == encoded[74:]


def test_an_unknown_placeholder_is_refused_not_encoded_literally():
    with pytest.raises(ValueError) as excinfo:
        CalldataExecutor.bind_param_values(
            ["{gas_price}"], amount_raw=1, recipient="0x" + "11" * 20, sender="0x" + "11" * 20
        )
    assert "does not know how to bind" in str(excinfo.value)


# ------------------------------------------------- the loop, through the graph

def test_a_cache_hit_still_has_to_clear_simulation():
    """
    A skill is a shortcut past *discovery*, never past the guardrails.

    Asserted on the compiled graph rather than on a run: there is no edge that reaches
    approval without passing through the simulator, so no cache hit can acquire one.
    """
    import graph as g

    # A route that was found goes to the builder, and the builder's only onward path
    # for a built plan is the simulator. That edge became conditional when the funding
    # gate landed — a plan `build_plan` refused to build must reach the diagnostic
    # rather than being simulated anyway — so the property is asserted on the branch
    # rather than on a static edge, which no longer exists.
    assert g.route_after_discovery({"status": "ROUTE_DISCOVERED"}) == "code_builder_node"
    assert g.route_after_code_builder({"status": "PAYLOAD_BUILT"}) == "simulator_node"
    assert g.route_after_code_builder({"status": "MISSING_PREREQUISITE"}) == "error_node"

    # The only way into the approval gate is the simulator's conditional edge — no
    # static edge reaches it — and it opens only for VERIFIED.
    assert not any(dst == "hitl_gate_node" for _, dst in g.builder.edges)
    assert g.route_after_simulation({"simulation_result": {"status": "VERIFIED"}}) == "hitl_gate_node"
    for status in ("REVERTED", "UNAVAILABLE", "UNSUPPORTED", None):
        assert g.route_after_simulation({"simulation_result": {"status": status}}) == "error_node"


def test_no_route_found_terminates_and_is_never_retried():
    """
    A correct answer, not a failure. It ends the run with its traces intact rather than
    throwing out of the graph, and it does not go round the retry edge — another
    attempt spends money to reach the same answer.
    """
    import graph as g

    assert g.route_after_discovery({"status": "NO_ROUTE_FOUND"}) == "error_node"

    out = g.error_node({
        "status": "NO_ROUTE_FOUND",
        "error": "No verified skill covers USDC from base to stellar on mainnet.",
        "simulation_result": {},
        "traces": {},
        "retry_count": 0,
    })
    assert out["failure_context"] is None
    assert g.route_after_error({**out}) == "__end__"
    assert "stellar" in out["error"]


def test_a_failed_replay_demotes_the_skill_it_replayed(tmp_path, monkeypatch):
    """
    The write-back half of the learning loop: a cached route that stops working stops
    being served. It is not repaired here — the replacement has to clear simulation and
    the human gate itself, which is what keeps a transient RPC error from overwriting a
    known-good route with whatever a cold search turned up.
    """
    import graph as g

    repo = SkillManager(repository_path=str(tmp_path))
    repo.save_skill(_meta(), "# body")
    monkeypatch.setattr(g, "skill_mgr", repo)

    out = g.error_node({
        "simulation_result": {
            "status": "REVERTED",
            "revert_reason": "execution reverted: router upgraded",
        },
        "route": {"router_address": _meta().router_address},
        "replayed_skill": "base_to_arbitrum_usdc",
        "retry_count": 0,
        "traces": {},
    })

    assert out["traces"]["step_error"]["demoted_skill"]
    assert repo.find_skill("base", "arbitrum", "USDC") is None

    demoted = repo.find_skill("base", "arbitrum", "USDC", include_unverified=True)
    assert "router upgraded" in demoted.metadata.last_failure
    # Demoted, not rewritten.
    assert demoted.metadata.router_address == _meta().router_address
    # And the failure still drives a retry.
    assert out["failure_context"] and g.route_after_error({**out}) == "discovery_node"


def test_a_fresh_discovery_failure_demotes_nothing(tmp_path, monkeypatch):
    """No skill was replayed, so there is nothing to demote and nothing to corrupt."""
    import graph as g

    repo = SkillManager(repository_path=str(tmp_path))
    repo.save_skill(_meta(), "# body")
    monkeypatch.setattr(g, "skill_mgr", repo)

    out = g.error_node({
        "simulation_result": {"status": "REVERTED", "revert_reason": "execution reverted"},
        "route": {"router_address": _meta().router_address},
        "retry_count": 0,
        "traces": {},
    })

    assert out["traces"]["step_error"]["demoted_skill"] is None
    assert repo.find_skill("base", "arbitrum", "USDC") is not None


def test_the_demotion_record_survives_the_retry(tmp_path, monkeypatch):
    """
    error_node runs once per attempt. The demotion happens on the pass that saw the
    failure, so the pass after the retry must not erase the record of it.
    """
    import graph as g

    repo = SkillManager(repository_path=str(tmp_path))
    repo.save_skill(_meta(), "# body")
    monkeypatch.setattr(g, "skill_mgr", repo)

    first = g.error_node({
        "simulation_result": {"status": "REVERTED", "revert_reason": "router upgraded"},
        "route": {"router_address": _meta().router_address},
        "replayed_skill": "base_to_arbitrum_usdc",
        "retry_count": 0,
        "traces": {},
    })
    assert first["traces"]["step_error"]["demoted_skill"]

    second = g.error_node({
        "status": "NO_ROUTE_FOUND",
        "error": "no verified skill covers this route",
        "simulation_result": {},
        "replayed_skill": "base_to_arbitrum_usdc",
        "retry_count": 1,
        "traces": first["traces"],
    })
    record = second["traces"]["step_error"]
    assert record["demoted_skill"], "the demotion was erased by the retry"
    assert len(record["attempts"]) == 2


# ------------------------------------------------- a destination is a structured ref


def test_a_structured_destination_reference_survives_a_round_trip(tmp_path):
    """
    The regression. Every agent route carries dest_chain_ref as
    {"scheme": "cctp_domain", "value": "6"}, but SkillMetadata typed it as a string, so
    saving raised a validation error — after the transaction had been broadcast.
    """
    from omnibridge.skills.manager import SkillManager, SkillMetadata

    mgr = SkillManager(repository_path=str(tmp_path))
    ref = {"scheme": "cctp_domain", "value": "6"}
    meta = SkillMetadata(
        name="arbitrum_to_base_usdc",
        description="d",
        source_chain="Arbitrum One",
        source_chain_id=42161,
        dest_chain="Base",
        dest_chain_ref=ref,
        token="USDC",
        bridge_protocol="Circle CCTP",
        router_address="0x19330d10D9Cc8751218eaf51E8885D058642E08A",
    )
    assert meta.dest_chain_ref == ref

    path = mgr.save_skill(meta, "body", target_dir=str(tmp_path / "arbitrum_to_base_usdc"))
    reloaded = mgr._parse_skill_file(Path(path))
    assert reloaded is not None, "the saved skill must parse back"
    assert reloaded.metadata.dest_chain_ref == ref


def test_a_legacy_string_reference_still_loads():
    """A hand-written value must not make the whole skill unreadable."""
    from omnibridge.skills.manager import SkillMetadata

    meta = SkillMetadata(
        name="n", description="d", source_chain="Arbitrum One", source_chain_id=42161,
        dest_chain="Base", dest_chain_ref="6", token="USDC",
        bridge_protocol="p", router_address="0x0000000000000000000000000000000000000001",
    )
    # Recorded as unknown rather than guessed as a CCTP domain.
    assert meta.dest_chain_ref == {"scheme": "unknown", "value": "6"}


def test_a_reference_is_rendered_for_a_human():
    from omnibridge.agent.contract import format_chain_ref

    assert format_chain_ref({"scheme": "cctp_domain", "value": "6"}) == "cctp_domain 6"
    assert format_chain_ref({"value": "6"}) == "6"
    assert format_chain_ref(None) == ""


# ------------------------------------------------------- a route has a direction


def _route(**over):
    from omnibridge.skills.manager import SkillMetadata

    base = dict(
        name="arbitrum_to_base_usdc", description="d",
        source_chain="Arbitrum One", source_chain_id=42161, dest_chain="base",
        token="USDC", bridge_protocol="Circle CCTP",
        router_address="0x19330d10D9Cc8751218eaf51E8885D058642E08A",
        source_aliases=["arbitrum", "arbitrum one"], dest_aliases=["base"],
    )
    base.update(over)
    return SkillMetadata(**base)


def test_a_skill_matches_the_direction_it_was_verified_in():
    assert _route().matches("arbitrum", "base", "USDC", "mainnet")


def test_a_skill_never_matches_its_own_reverse():
    """
    The regression. Aliases were one flat pool consulted for both ends, so an
    Arbitrum -> Base route carried {arbitrum, arbitrum one, base} and a Base -> Arbitrum
    request found both names in it. The router of a reversed route is on the wrong
    chain; only a later chain-id check happened to catch it.
    """
    assert not _route().matches("base", "arbitrum", "USDC", "mainnet")


def test_a_legacy_pooled_alias_list_is_not_consulted_for_matching():
    """A shared pool cannot say which end a name belonged to, so it is not trusted."""
    legacy = _route(source_aliases=[], dest_aliases=[],
                    aliases=["arbitrum", "arbitrum one", "base"])
    assert not legacy.matches("base", "arbitrum", "USDC", "mainnet")
    # The recorded chain names still work, so a legacy skill keeps serving its own route.
    assert legacy.matches("arbitrum one", "base", "USDC", "mainnet")


def test_an_explicit_repository_is_not_mixed_with_the_working_tree(tmp_path):
    """
    A manager pointed at a directory searches that directory. It used to also search
    ./skills, so a real run that saved a route changed what unrelated callers saw.
    """
    from omnibridge.skills.manager import SkillManager

    mgr = SkillManager(repository_path=str(tmp_path))
    assert mgr.isolated is True
    assert mgr.list_skills() == []
