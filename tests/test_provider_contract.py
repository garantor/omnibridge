"""
The provider contract: what any model behind discovery must satisfy.

Run this against a new provider before trusting it. It asserts the *refusals*, not the
happy path, because a provider that returns a plausible route when it should have
refused is the failure that costs money — and it is the failure a smoke test passes.

To conform a new provider, add its class to PROVIDERS. Nothing here calls a live API:
every case is driven through the provider's own client seam or through the contract
objects directly.
"""

import pytest

from omnibridge.agent.budget import RequestBudget
from omnibridge.agent.contract import (
    NO_ROUTE_FOUND_SCHEMA,
    PROPOSAL_REQUIRED_FIELDS,
    PROPOSE_ROUTE_SCHEMA,
    NoRouteFound,
    ProviderUnavailable,
    RouteProposal,
)
from omnibridge.agent.discovery_agent import AnthropicDiscoveryAgent
from omnibridge.agent.provider import (
    DiscoveryProvider,
    available_providers,
    get_provider,
)

#: Every provider that claims to implement the contract.
PROVIDERS = [AnthropicDiscoveryAgent]

VALID_PAYLOAD = {
    "protocol": "CCTP",
    "router_address": "0x1682Ae6375C4E4A97e4B583BC394c861A46D8962",
    "function_signature": "depositForBurn(uint256,uint32,bytes32,address)",
    "param_types": ["uint256", "uint32", "bytes32", "address"],
    "param_values": ["1000000", "3", "0x" + "11" * 32, "0x" + "22" * 20],
    "source_chain_id": 8453,
    "dest_chain_ref": {"scheme": "cctp_domain", "value": "3"},
    "sources": ["https://docs.circle.com/a", "https://developers.circle.com/b"],
    "rationale": "because",
}


# ------------------------------------------------------- the answer shape

def test_a_proposal_never_defaults_a_missing_field():
    """
    Anthropic's strict tool schema makes an omitted field unreachable. A provider whose
    function calling is not schema-enforced can omit one, and a defaulted parameter is
    a value nobody chose reaching real calldata.
    """
    for field in PROPOSAL_REQUIRED_FIELDS:
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != field}
        with pytest.raises(ValueError) as excinfo:
            RouteProposal.from_tool_input(payload)
        assert field in str(excinfo.value)


def test_a_complete_proposal_round_trips():
    proposal = RouteProposal.from_tool_input(VALID_PAYLOAD)
    assert proposal.as_dict()["router_address"] == VALID_PAYLOAD["router_address"]
    # Values are normalised to strings; reviewed code coerces them, not the model.
    assert all(isinstance(v, str) for v in proposal.param_values)


def test_the_schemas_forbid_unspecified_fields():
    """A model must not be able to smuggle a field past the checks by inventing one."""
    for schema in (PROPOSE_ROUTE_SCHEMA, NO_ROUTE_FOUND_SCHEMA):
        assert schema["additionalProperties"] is False
    assert set(PROPOSE_ROUTE_SCHEMA["required"]) == set(PROPOSAL_REQUIRED_FIELDS)


def test_no_route_found_is_an_answer_not_an_error_shape():
    exc = NoRouteFound("nothing bridges this pair", ["https://docs.circle.com/x"])
    assert exc.reason
    assert exc.sources == ["https://docs.circle.com/x"]


# ------------------------------------------------------- provider interface

@pytest.mark.parametrize("cls", PROVIDERS, ids=[c.name for c in PROVIDERS])
def test_provider_declares_its_identity_and_credentials(cls):
    assert issubclass(cls, DiscoveryProvider)
    assert cls.name and cls.name != "base"
    assert cls.credential_env, "a provider must say which env vars configure it"
    assert cls.default_model()


@pytest.mark.parametrize("cls", PROVIDERS, ids=[c.name for c in PROVIDERS])
def test_provider_is_registered_under_its_name(cls):
    assert available_providers().get(cls.name) is cls


@pytest.mark.parametrize("cls", PROVIDERS, ids=[c.name for c in PROVIDERS])
def test_provider_reports_unconfigured_rather_than_guessing(cls, monkeypatch):
    for var in cls.credential_env:
        monkeypatch.delenv(var, raising=False)
    assert cls.configured() is False


@pytest.mark.parametrize("cls", PROVIDERS, ids=[c.name for c in PROVIDERS])
def test_every_provider_is_asked_the_same_question(cls, monkeypatch):
    """
    The prompt is shared. If two providers are asked different questions, a difference
    in their answers says nothing about the models.
    """
    from omnibridge.core.models import ChainInfo, UserIntent

    chain = ChainInfo(chain_id=8453, name="Base", block_explorer="", rpc_urls=[])
    intent = UserIntent(token="USDC", amount=100.0, source_chain="base",
                        dest_chain="arbitrum", recipient="0x" + "11" * 20)

    monkeypatch.setenv(cls.credential_env[0], "not-a-real-key")
    question = cls().build_question(intent, chain, "Arbitrum")

    assert "100.0 USDC" in question
    assert "Base" in question and "8453" in question
    assert "Arbitrum" in question
    assert "0x" + "11" * 20 in question

    with_context = cls().build_question(intent, chain, "Arbitrum", failure_context="it reverted")
    assert "it reverted" in with_context
    assert "Do not repeat it" in with_context


# ------------------------------------------------------------- selection

def test_no_configured_provider_is_reported_not_guessed(monkeypatch):
    for cls in available_providers().values():
        for var in cls.credential_env:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("OMNIBRIDGE_PROVIDER", raising=False)

    with pytest.raises(ProviderUnavailable) as excinfo:
        get_provider()
    assert "No model provider is configured" in str(excinfo.value)


def test_an_unknown_provider_name_is_refused(monkeypatch):
    monkeypatch.setenv("OMNIBRIDGE_PROVIDER", "definitely-not-a-provider")
    with pytest.raises(ProviderUnavailable) as excinfo:
        get_provider()
    assert "Unknown provider" in str(excinfo.value)


def test_a_named_but_unconfigured_provider_is_refused(monkeypatch):
    monkeypatch.setenv("OMNIBRIDGE_PROVIDER", "anthropic")
    for var in AnthropicDiscoveryAgent.credential_env:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderUnavailable) as excinfo:
        get_provider()
    assert "not configured" in str(excinfo.value)


def test_a_single_configured_provider_is_selected(monkeypatch):
    monkeypatch.delenv("OMNIBRIDGE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    assert get_provider().name == "anthropic"


def test_ambiguous_configuration_is_refused_not_resolved_by_ordering(monkeypatch):
    """
    Which model priced and proposed a financial transaction is not a detail to settle
    by dict ordering. With several configured and none named, refuse.
    """
    from omnibridge.agent import provider as provider_mod

    class _Second(DiscoveryProvider):
        name = "second"
        credential_env = ("SECOND_PROVIDER_KEY",)

        @classmethod
        def default_model(cls):
            return "second-model"

    provider_mod._load_builtins()
    monkeypatch.setitem(provider_mod._REGISTRY, "second", _Second)
    monkeypatch.delenv("OMNIBRIDGE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("SECOND_PROVIDER_KEY", "x")

    with pytest.raises(ProviderUnavailable) as excinfo:
        get_provider()
    assert "Set OMNIBRIDGE_PROVIDER" in str(excinfo.value)

    # Naming one resolves it.
    monkeypatch.setenv("OMNIBRIDGE_PROVIDER", "second")
    assert get_provider().name == "second"


# ------------------------------------------------- the guardrails are shared

def test_the_deterministic_layer_has_no_vendor_dependency():
    """
    The point of the split. Everything that touches money must be indifferent to which
    model produced the proposal — otherwise swapping providers silently swaps the
    guarantees with it.
    """
    from pathlib import Path

    from tests.conftest import code_only

    import omnibridge.agent.provenance as provenance
    import omnibridge.agent.synthesis as synthesis
    import omnibridge.core.executor as executor
    import omnibridge.core.simulator as simulator

    for module in (synthesis, provenance, executor, simulator):
        source = code_only(module.__file__).lower()
        for vendor in ("anthropic", "claude", "openai", "gemini", "genai"):
            assert vendor not in source, f"{Path(module.__file__).name} references {vendor}"
