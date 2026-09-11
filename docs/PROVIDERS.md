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
| A protocol-documentation source, plus a second independent one | `provenance.check_sources` | A single page treated as evidence; an address known only to a block explorer |
| Cited sources the search never fetched | `provenance.check_sources` | Corroboration satisfied by URLs the model typed but never opened |
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
ANTHROPIC_API_KEY=sk-ant-...
OMNIBRIDGE_WORKSPACE_ID=wrkspc_...   # identity-linked keys only
```

### Gemini

```bash
pip install -e ".[gemini]"
```

```dotenv
OMNIBRIDGE_PROVIDER=gemini
GEMINI_API_KEY=...
OMNIBRIDGE_MODEL=gemini-2.5-pro   # optional
```

Structured in two phases, because Gemini refuses a request that declares Google Search
grounding *and* function declarations together: research with grounding, then a second
call with grounding dropped and function calling forced (`mode=ANY`, restricted to the
two terminal functions). Two model calls per attempt, both charged to the budget.

**Read this before trusting it.** Two guarantees are weaker than on the Anthropic path:

- **No search allowlist.** `GoogleSearch` accepts `exclude_domains` only. The model can
  read anything, so the allowlist degrades from a restriction on *browsing* to a filter
  on *citations*, applied afterwards by `provenance.check_sources`. A page outside the
  allowlist can still shape the answer; it just cannot be cited as evidence for it. The
  provider records which URLs grounding actually fetched alongside the ones the model
  claims, so a proposal citing pages it never read is visible in the trace.
- **No strict schemas.** `parameters_json_schema` is a hint. `additionalProperties` is
  stripped because Gemini's validator rejects it. A missing field is caught by
  `RouteProposal.from_tool_input`, a malformed one by `synthesis._check_shape` — safe,
  but caught later and less often than server-side enforcement.

Run with `OMNIBRIDGE_LOG=normal` (the default) to watch both phases: which URLs
grounding actually fetched, the token spend per call, the function it chose, and a
warning for **any source the model cited that grounding never retrieved** — the failure
that would otherwise satisfy corroboration invisibly.

Everything downstream is unchanged: the selector is still recomputed, the router's
bytecode still has to contain it, a source on the documentation allowlist is still
required, and a real `eth_call` still has to pass before a human sees anything.

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
  source attribution cannot prove it read what it cites. Since
  `MIN_CORROBORATING_SOURCES = 1`, the documentation allowlist is the whole trust
  boundary. **Verify a provider's sourcing before trusting it, not after** — one that
  cites a plausible allowlisted URL it never opened now passes sourcing on its own word,
  and only the on-chain selector and bytecode checks are left to catch it.
- **Refusals and pauses.** `pause_turn` and refusal blocks are Anthropic-shaped. Other
  APIs signal these differently, or not at all.

### Cost of a swap

Roughly 200 lines plus search, contained to one new file. Nothing in `graph.py`,
`synthesis.py`, `provenance.py`, `executor.py`, `simulator.py` or the skill layer changes.
