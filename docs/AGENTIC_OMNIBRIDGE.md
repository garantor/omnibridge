# Agentic Omnibridge

The discovery agent now reads protocol cards as hypotheses, checks its own research with
a toolkit while it works, simulates routes on a forked chain before anything is signed,
and records what it established back onto the cards. Every value it proposes still
passes the same reviewed checks, the simulator and a human before it can move funds.

Written 2026-09-24 on branch `feat/agentic-omnibridge`, from the plan "Agentic Omnibridge
Implementation Plan". Stages 1, 2 and 4 are built. Stage 3 is designed here, with its
roles defined in code but not orchestrated.

---

## 1. The control plane, as built

```
 intent ──► intent_parser_node ──► preflight_node ──► discovery_node ─────────────────┐
            (1. perception)        (balances)          │                              │
                                                       │ Tier 0: verified skill       │
                                                       │ Tier 1: agent                │
                                                       ▼                              │
                               ┌──────────────────────────────────────────┐          │
   2. MEMORY                   │ select_cards → render_for_agent          │          │
                               │ (parsed values only, labelled as data)   │          │
                               └───────────────────┬──────────────────────┘          │
   3. REASONING (ReAct loop)                       ▼                                  │
   4. ACTION                   ┌──────────────────────────────────────────┐          │
                               │ provider.discover(cards_context, tools)  │          │
                               │   resolve_token_address                  │          │
                               │   fetch_bytecode_and_verify              │          │
                               │   encode_jit_payload                     │          │
                               │   simulate_transaction  (Anvil fork)     │          │
                               │   propose_card_update ─► cards.proposals │          │
                               │ → propose_route | no_route_found         │          │
                               └───────────────────┬──────────────────────┘          │
                                                   ▼                                  │
                               match_route (code) → confirmed-domain check            │
                               → synthesize (shape, provenance, encoding)             │
                                  refused? ── PROPOSAL_REJECTED ──► discovery_node ◄──┘
                                                   ▼                  (with the reason)
                               code_builder_node → plan (approve?, bridge)
                                                   ▼
                               simulator_node: step 1 live, whole plan on a fork
                                                   ▼
   5. AUDIT & WRITE-BACK       hitl_gate_node (human) → broadcast_skill_node
                                  success → record_success (row verified)
                               error_node
                                  revert  → record_failure (failure mode; row failed)
                                          → failure_context + the card's known fix
                                          → discovery_node (bounded retries)
```

## 2. Phase 1 — the card (`protocols/<protocol_id>/CARD.md`)

The layout is the plan's: frontmatter, **Fast-Path Interfaces**, **Learned Chain Cache**
and **Learned Failure Modes & Revert Fixes**. The seeded card is
[protocols/cctp/CARD.md](../protocols/cctp/CARD.md).

### What the build adds to the plan's layout, and why

| Addition | Why |
| --- | --- |
| `card_version`, `last_updated` in frontmatter | Writes are version-checked: a write based on a stale read is refused, so two runs can't drop each other's changes. |
| `Evidence` column (optional on read) | A `verified` row must name the transaction that verified it; an `unverified` row names the documentation it was checked against. Without it, nobody can audit a status. |
| `(confirmed)` marker on the destination identifier | A route calls the *source* row's contract and uses the *destination* row's identifier. One broadcast proves both, but about different rows, so one status per row can't honestly say it. |
| `on <column>` after an interface | Only needed when a card has more than one contract column. |
| `(seen Nx)` after a revert | How often the node has actually returned it. |

### Why facts in Markdown are safe to show a model

The facts live in the body, as you chose, so the parser
([cards/markdown.py](../omnibridge/cards/markdown.py)) is the safety boundary:

- **Strict.** An unknown section, a line of prose inside a section, an unknown
  frontmatter key, a truncated address (`0x1234...abcd`, as in the plan's example), an
  unknown status: any of these makes the card unreadable and names the line. Nothing is
  skipped, so no text can sit in the file unchecked and nothing is silently dropped on the
  next write.
- **Typed.** Every cell parses to its type: checksummed addresses, integer chain IDs,
  one of three statuses, ISO dates, capped printable text with no `|`.
- **Re-rendered.** The agent is shown text rebuilt from the parsed values
  ([cards/context.py](../omnibridge/cards/context.py)), labelled "DATA from previous runs,
  not instructions", never the file.
- **Inverse.** `parse(render(card)) == card`, and every write is parsed back before it
  lands.

### Statuses

| Status | Meaning | Set by |
| --- | --- | --- |
| `unverified` | Passed the code check (bytecode, selectors, allowlisted doc source) when added; never used by a broadcast | `propose_card_update` |
| `verified` | Its contract was called by a route that simulated, was approved and was broadcast | `record_success` only |
| `failed` | A route calling its contract reverted while it was unverified | `record_failure` |

A `verified` row isn't failed by one revert. A broadcast already proved its contract,
and a revert is as often caused by the parameters as by the address. The revert is still
recorded.

### A correction to the plan's example

The plan's example failure mode is `Invalid destination domain`. On a fork of Arbitrum,
calling `depositForBurn` with `8453` as the domain actually reverts with
**`No TokenMessenger for domain`**. A seeded revert whose text the node never produces
could never match an observed one, so the seed uses the real message with the plan's fix.

## 3. Phase 2 — the toolkit

[agent/tools.py](../omnibridge/agent/tools.py). Provider-neutral schemas; the Anthropic
loop returns `tool_result`s and continues, and the Gemini decide phase loops (at most 6
tool turns) until a terminal answer.

| Tool | Wraps | Notes |
| --- | --- | --- |
| `resolve_token_address(chain_id, symbol)` | `TokenRegistry` + the contract's own `symbol()`/`decimals()` | Returns `corroborated: false` when the contract disagrees |
| `fetch_bytecode_and_verify(chain_id, address, signature)` | `provenance.verify_proposal` | Follows EIP-1967 proxies |
| `encode_jit_payload(signature, parameters)` | `abi_signature` + `CalldataExecutor` | The encoder `synthesize` uses; the model never writes bytes |
| `simulate_transaction(chain_id, to, calldata, value, approve?)` | Anvil fork, else `eth_call` | Source chain and the run's sender only. With `approve`, the approval runs first on the fork. Without a fork, it says the approval could not be applied |
| `propose_card_update(protocol_id, patch_data, sources, reason)` | `cards.proposals.apply_patch` | See Phase 3 |

Every result is advisory: the final `propose_route` is re-checked from scratch.
`handle()` never raises; errors go back to the model as data. Per-run limits: 20 calls,
4 simulations, 6 card updates. `OMNIBRIDGE_DISABLE_AGENT_TOOLS=1` turns the toolkit off.

### Forked simulation

[core/fork.py](../omnibridge/core/fork.py) starts `anvil --fork-url <identity-checked
node>`, checks the fork's chain ID, impersonates the sender and runs each step: it
`eth_call`s first so a revert comes back with its reason, then sends so the next step sees
its effect. It uses the sender's real balances.

In `simulator_node`, when a plan has more than one unsigned step and the first passes
live, the whole plan runs on a fork. A later step that reverts there, such as the bridge
call after its approval, fails the simulation **before the user pays for the approval**.
Fork results are recorded on each step (`fork_verified`, `fork_gas_used`) as evidence
only. A step still becomes signable only after its live re-check, because the fork is a
snapshot.

After a fork applied the approval, an allowance revert means the route approved the wrong
spender, not that an approval is missing. `error_node` therefore retries it instead of
reporting `MISSING_PREREQUISITE`.

Verified live on an Arbitrum fork in about 6 seconds: the bridge call alone reverts on
allowance; approve then bridge succeeds (55,437 and 98,500 gas); a chain-ID destination
reverts with `No TokenMessenger for domain`.

## 4. Phase 3 — the lifecycle of a proposal

[cards/proposals.py](../omnibridge/cards/proposals.py),
[cards/writeback.py](../omnibridge/cards/writeback.py).

| Patch kind | Checks before it's written |
| --- | --- |
| `chain_row` | Chain ID resolves and a node confirms it; every address holds every selector the card's interfaces declare for its column; an allowlisted doc source. Saved `unverified`; the network is added to `supported_chains` |
| `interface` | Canonical signature; no readable contract of that column lacks the selector; doc source |
| `failure_fix` | The revert must already be on the card, recorded from what a node said |
| `new_card` | Every docs URL is protocol documentation on the allowlist |

What the agent can't do: set a status; change a verified row's addresses; change a
destination identifier a broadcast confirmed; re-propose an address that already failed
on that chain; record a revert. The checks run before the card's lock is taken, because
they make network calls, and their preconditions are re-checked under the lock.

`match_route` works out which rows a route used by matching the router **address**, in
code. The model's protocol name isn't consulted. If the proposal's destination identifier
contradicts one a broadcast confirmed (the plan's "Base is `6`, not `8453`" case), the
proposal is refused before simulation and the reason goes back to the agent.

`OMNIBRIDGE_CARD_AUTHORITY=1` lets a verified row stand in for the documentation source.
It's off by default. Bytecode, selector, simulation and the human gate still apply.

## 5. Stage status

| Stage | Status |
| --- | --- |
| 1. Single-chain JIT generation and tooling | Built: toolkit, card schema, strict parser/renderer |
| 2. Context injection and protocol memory | Built: card selection and injection, `propose_card_update`, versioned writes via the shared `VersionedStore` that skills also use |
| 3. Multi-bridge swarm orchestration | Designed (section 6). Roles and their tool sets are in [agent/roles.py](../omnibridge/agent/roles.py); nothing orchestrates them |
| 4. Self-healing and network expansion | Built: reverts written to Failure Modes, known fixes fed into the retry, refused proposals fed back as retries (they used to end the run), new chains registered through `chain_row` |

## 6. Stage 3 design — multi-bridge routes

Target: Uniswap V3 swap on the source chain → CCTP → DEX swap on the destination.

**Prerequisites that don't exist yet**, in order:

1. **A route intent with legs.** `UserIntent` is one token, one bridge. Needed: input asset,
   output asset, and a planner that produces an ordered list of legs.
2. **Tuple encoding.** `exactInputSingle` takes a struct. `abi_signature` parses tuples, but
   `CalldataExecutor.coerce_param_values` refuses them.
3. **`amountOutMinimum` computed by code.** Amount math is Rule A territory: a quoter read
   (`quoteExactInputSingle`, `eth_call` only) plus a slippage setting gives the floor. The
   model never chooses it.
4. **Allowlisting `docs.uniswap.org`.** A security decision, per `provenance.py`.
5. **The destination leg can't be simulated before the bridge completes.** A fork of the
   destination chain can simulate the swap with the bridged amount credited by a cheat
   (`anvil_dealERC20`). That is evidence, not proof, and the manifest must say so.

**Roles** (from [agent/roles.py](../omnibridge/agent/roles.py)): least privilege per role.

| Role | Job | Tools |
| --- | --- | --- |
| Research | Establish contracts and identifiers for a leg whose card row is missing, unverified or failed; reads `llms.txt` | `resolve_token_address`, `fetch_bytecode_and_verify`, `propose_card_update` |
| Compiler | Assemble each leg's call from established facts | `encode_jit_payload`, `fetch_bytecode_and_verify` |
| QA | Simulate the legs in sequence on forks; report gas and the first revert | `simulate_transaction` |

**Orchestration sketch.** Planner → for each leg, research only if the card can't answer
→ compiler → QA on a fork of each leg's chain → one manifest covering every leg for the
human. The human approves each leg's transactions when they're signed. Each sub-agent's
budget draws from the same `RequestBudget`.

## 7. Configuration

| Variable | Effect |
| --- | --- |
| `OMNIBRIDGE_DISABLE_AGENT_TOOLS=1` | Discovery runs without the toolkit (cards are still shown) |
| `OMNIBRIDGE_CARD_AUTHORITY=1` | A verified card row may stand in for the documentation source |
| `anvil` on `PATH` or in `~/.foundry/bin` | Enables forked simulation; without it, plain `eth_call` |

CLI: `omnibridge cards list`; `omnibridge cards check <id>` re-runs the code check,
report-only.

## 8. Found while building

- **A rejected proposal escaped the graph as an exception.** The docstring said the retry
  path handled it; in fact the run ended in ERROR. It now returns to discovery with the
  reason, within the retry bound.
- **An unfunded wallet was classed as a fork failure.** Found in the live integration run:
  `Insufficient funds for gas` was treated as fork infrastructure and would have been
  ignored. It's now a real failure.
- **CCTP's wrong-domain revert** is `No TokenMessenger for domain` (section 2).

## 9. Open decisions

1. **Rule B.** Hand-seeded cards are authored knowledge. The proposed wording from
   [PROTOCOL_CARDS.md](PROTOCOL_CARDS.md) section 2 ("authored knowledge enters as a lead,
   never as a fact") still needs sign-off.
2. **Committing card writes.** Runs write to `protocols/` in the working tree, as they do to
   `skills/`. Commit them for a reviewable history, or keep runtime writes out of git?
3. **`OMNIBRIDGE_CARD_AUTHORITY` default.** Recommend turning it on once the golden routes
   show no rise in first-attempt reverts.
