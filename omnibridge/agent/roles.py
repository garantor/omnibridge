"""
Sub-agent roles for Stage 3 (multi-bridge orchestration). Designed, not wired.

docs/AGENTIC_OMNIBRIDGE.md, "Stage 3", describes a research agent, a compiler agent
and a QA agent coordinating a swap -> bridge -> swap route. What exists today is one
agent with the whole toolkit. These definitions pin down the split so that, when the
orchestration is built, each role gets only the tools its job needs — least privilege
for agents, the same reason a signer never gets the model's tools.

`AgentToolkit(ctx, allowed=ROLE_TOOLS["qa"])` already honours a role's tool set; nothing
constructs a toolkit that way yet.
"""

from typing import Dict, Tuple

ROLE_TOOLS: Dict[str, Tuple[str, ...]] = {
    # Reads docs (provider web tools) and the chain; records what it established.
    "research": ("resolve_token_address", "fetch_bytecode_and_verify", "propose_card_update"),
    # Turns an established route into candidate payloads. Cannot touch the card.
    "compiler": ("encode_jit_payload", "fetch_bytecode_and_verify"),
    # Tests candidates. Cannot change the card or produce new payloads to test.
    "qa": ("simulate_transaction",),
}

ROLE_PURPOSE: Dict[str, str] = {
    "research": "Establish contracts, interfaces and identifiers for one leg when the card "
                "has no row for it or the row is unverified or failed.",
    "compiler": "Assemble the call for each leg from established facts, in leg order.",
    "qa": "Simulate the legs in sequence on a fork and report gas and the first revert.",
}
