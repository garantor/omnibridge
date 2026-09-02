# Model providers

Which model answers the discovery question is a configuration choice, not an
architectural one.

## Why the seam is where it is

Under Rule A the model *chooses* and reviewed code *encodes*. The only thing a provider
is allowed to decide is a `RouteProposal`:

```python
protocol, router_address, function_signature,
param_types, param_values, source_chain_id,
dest_chain_ref, sources, rationale
```

Every field is re-checked by code that has no idea which model produced it:

| Check | Where | What it catches |
| --- | --- | --- |
| Parameter types re-derived from the signature | `synthesis._check_shape` | A proposal contradicting itself |
| Router is on the resolved source chain | `synthesis._check_shape` | A destination-side contract |
| Selector recomputed from the signature | `synthesis.synthesize` | A mis-copied selector |
| Router holds bytecode implementing that selector | `provenance.verify_proposal` | An address that is not the contract claimed (EIP-1967 proxies resolved) |
| Two independent allowlisted sources | `provenance.check_sources` | A single page treated as evidence |
| ABI encoding from types and values | `executor.encode_dynamic_calldata` | The model touching bytes at all |
| A real `eth_call` on an identity-checked node | `simulator` | Everything else |
| A human approving a decoded amount | `hitl_gate_node` | The rest |

A conformance test asserts those modules contain no vendor references at all. That is
what makes providers swappable: a weaker model does not get a weaker guardrail, it just
gets rejected more often.

## Selecting one

| Variable | Effect |
| --- | --- |
| `OMNIBRIDGE_PROVIDER` | Which provider answers. Required when more than one is configured |
| `OMNIBRIDGE_MODEL` | Override the provider's default model |
| `OMNIBRIDGE_DISABLE_AGENT=1` | Kill switch — discovery never reaches a model |
| `OMNIBRIDGE_ALLOWED_DOMAINS` | Comma-separated documentation hosts the model may read |

With none configured the system is cache-only and says so. With exactly one configured
it is selected automatically. With several configured and none named, `get_provider()`
**refuses** — which model priced and proposed a financial transaction is not a detail to
settle by dict ordering.

### Anthropic

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OMNIBRIDGE_WORKSPACE_ID=wrkspc_...   # identity-linked keys only
```

## Adding a provider

1. Subclass `DiscoveryProvider` in a new module under `omnibridge/agent/`.
2. Set `name`, `credential_env`, and `default_model()`.
3. Implement `discover(...) -> RouteProposal`.
4. Decorate with `@register_provider` and import it from `provider._load_builtins()`.
5. Add the class to `PROVIDERS` in `tests/test_provider_contract.py` and make it pass.

A provider **must**:

- return a `RouteProposal`, raise `NoRouteFound`, or raise `BudgetExceeded`;
- raise `ProviderUnavailable` for credentials, quota and configuration problems, and
  never fold them into `NoRouteFound` — *"I could not ask"* and *"the answer is no"* are
  opposite statements to the person reading them;
- build proposals with `RouteProposal.from_tool_input`, which raises on a missing field
  rather than defaulting one;
- charge the `RequestBudget` for every model call.

It **does not** need to reproduce Anthropic's server-side search. Client-side search is
fine, provided what it cites is what it actually read — provenance checks the cited hosts
and the router's bytecode regardless of where the text came from.

### What differs in practice

`SYSTEM_PROMPT` and `build_question` are shared, so providers are asked the same thing.
What varies is enforcement:

- **Strict tool schemas.** Anthropic enforces `additionalProperties: false` server-side.
  Where a provider does not, malformed proposals reach `from_tool_input` and
  `_check_shape` instead — still safe, just noisier.
- **Search.** Anthropic's `web_search_20260209` / `web_fetch_20260209` run server-side
  with a per-request domain allowlist. A provider using a grounding API with weaker
  source attribution will find `MIN_CORROBORATING_SOURCES = 2` harder to satisfy
  honestly. **Verify that before trusting it, not after** — a provider that cites
  plausible URLs it did not read defeats the corroboration check while appearing to pass.
- **Refusals and pauses.** `pause_turn` and refusal blocks are Anthropic-shaped. Other
  APIs signal these differently, or not at all.

### Cost of a swap

Roughly 200 lines plus search, contained to one new file. Nothing in `graph.py`,
`synthesis.py`, `provenance.py`, `executor.py`, `simulator.py` or the skill layer changes.
