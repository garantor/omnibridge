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

## 7. Latest version, always, and testnet

### The rule

The agent always uses a protocol's latest version. Circle's own `llms.txt` says the same
for CCTP: "V1 is legacy; always use V2." It's enforced in code:

| Where | How |
| --- | --- |
| Cards | `protocol_family`, `protocol_version` and `network` in frontmatter. A card is *superseded* when its family has a newer version on the same network |
| Selection | Superseded cards are never offered. The latest card is offered even with no rows, so the agent has its interfaces and docs to find them |
| Routing | A proposal whose router is on a superseded card is refused before simulation, naming the latest card |
| Skills | A cached skill on a superseded version is not replayed. With no model configured, the run ends `NO_ROUTE_FOUND` rather than replaying it |
| Proposals | Rows and interfaces can't be added to a superseded card. `new_card` must be for a newer version than the family's latest on that network |

Seeded now: `cctp` (V1, mainnet, superseded), `cctp_v2` (mainnet) and `cctp_v2_testnet`,
both with the V2 interface `depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)`
and **no chain rows**. The agent finds and proposes each deployment. The V2 selector was
confirmed on chain on Arbitrum, Base, Arbitrum Sepolia and Base Sepolia before seeding.

Consequence: the verified V1 Arbitrum → Base route is no longer used on mainnet. The
first V2 mainnet transfer goes through discovery.

### Testnet

`network` ("mainnet" | "testnet") now runs through the whole pipeline: the
`/api/bridge/start` and `/api/preflight` requests, graph state, every chain lookup and
RPC race, skill lookup and saving, card selection, and a Mainnet/Testnet toggle in the
UI. The chain resolver refuses to cross networks, and a chain row is only accepted on a
card of its network, decided from chain-registry data (`RPCManager.network_of`), never
from a name.

Write two-word testnet chains with a hyphen: `arbitrum-sepolia`, `base-sepolia`. The
parser turns hyphens into spaces, which is how the registry names them.

Verified live, with a scripted provider standing in for the model: on a testnet run only
`cctp_v2_testnet` was offered; Arbitrum Sepolia and Base Sepolia rows passed the live
code check and were saved `unverified`; a mainnet chain on the testnet card and a row on
the V1 card were both refused; the V2 route matched the Arbitrum Sepolia row.

## 8. Smart accounts: one signature, gas in USDC

A MetaMask user who turns on **Switch to smart account** (MetaMask: account details)
delegates their address, via EIP-7702, to MetaMask's `EIP7702StatelessDeleGator`. For such
an address, Omnibridge offers the whole plan as one ERC-4337 UserOperation, with gas paid
in USDC by an ERC-20 paymaster. Any bundler and paymaster that speak the standards work
(see *Providers* below). Nothing else changes: same address, same funds,
same recipient checks.

**Why the user switches in MetaMask, not in Omnibridge.** A website cannot sign an EIP-7702
authorization through a browser wallet (MetaMask's kit: "`signAuthorization` does not
support JSON-RPC accounts"). Delegation is also per chain: switched on Arbitrum Sepolia
means nothing on Base Sepolia.

**How it runs**

| Where | What |
| --- | --- |
| `simulator_node` → `_prepare_smart_account` | Reads the account's code (designator `0xef0100‖DeleGator`) and checks the DeleGator implements `execute(bytes32,bytes)`. Asks the configured bundler which chain it serves and that it supports EntryPoint v0.7. Gets the **fee cap** from code: sized from the paymaster's token quote when it offers one, otherwise the configured `AA_MAX_GAS_FEE` ceiling. Checks the balance covers the transfer plus the cap. Builds the batch: fee allowance for the paymaster, then the plan's steps. **Fork-simulates the exact batch** through the account's real delegation. Any miss sets `plan.aa_unavailable` with the reason, and the plan is signed step by step as before |
| Approval screen | "One signature, gas paid in USDC, at most X", or why it isn't offered (e.g. "turn on Switch to smart account") |
| Browser | MetaMask's own kit (`toMetaMaskSmartAccount`, `Stateless7702`) builds and signs the UserOperation, so the signature format is MetaMask's. viem's bundler and paymaster clients talk only to `/api/aa/rpc` |
| `/api/aa/rpc` | Holds the provider URLs and keys. Sends bundler methods to the bundler and `pm_*` to the paymaster, and answers `omnibridge_getUserOperationGasPrice` itself. Refuses any UserOperation that isn't exactly the approved batch: other sender, other EntryPoint, other paymaster, other paymaster context, an account deployment, an EIP-7702 re-delegation, or any call added, removed, reordered or changed. Allows only bundler and paymaster methods |
| Finish | The bundle's transaction hash goes through the same resume, broadcast, skill and card write-back as a directly signed one |

**Pinned infrastructure** (from MetaMask's `@metamask/delegation-deployments`, checked on
chain before use): DeleGator `0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B`, EntryPoint v0.7
`0x0000000071727De22E5E9d8BAf0edAc6f37da032`. The kit encodes multi-call batches in ERC-7579
`BatchDefault` mode (`0x0100…`, revert on any failure), the only mode the guard accepts.

**Verified on a fork of Arbitrum Sepolia:** with the delegation in place, approve plus V2
`depositForBurn` ran as one call (123,805 gas), burned exactly 1 USDC, left no allowance, and
the same call from another address was refused by the DeleGator.

**Providers.** What is standard is used as is: ERC-4337 bundler methods and ERC-7677
paymaster methods. Two things are not standard, so they are probed, never assumed:

| Need | When the provider offers it | Otherwise |
| --- | --- | --- |
| Gas price for the UserOperation | `pimlico_getUserOperationGasPrice` on the bundler | Base fee × 2 + `eth_maxPriorityFeePerGas`, read through the bundler |
| Token quote, to size the fee cap | `pimlico_getTokenQuotes` on the paymaster (also names the paymaster) | `AA_PAYMASTER_ADDRESS` and `AA_MAX_GAS_FEE` must be set; the cap is that ceiling |

The ERC-7677 context is provider-specific, so it is configured (`AA_PAYMASTER_CONTEXT`,
default `{"token": "{token}"}`), recorded in the approved batch, and the proxy refuses any
other. Paymasters that charge through an EIP-2612 permit in `paymasterData` instead of an
allowance granted in the batch are not supported.

**Not yet verified live:** any real bundler and paymaster, and MetaMask signing the
UserOperation. Both need a bundler configured and a switched MetaMask account.

## 9. Configuration

| Variable | Effect |
| --- | --- |
| `OMNIBRIDGE_DISABLE_AGENT_TOOLS=1` | Discovery runs without the toolkit (cards are still shown) |
| `OMNIBRIDGE_CARD_AUTHORITY=1` | A verified card row may stand in for the documentation source |
| `anvil` on `PATH` or in `~/.foundry/bin` | Enables forked simulation; without it, plain `eth_call` |
| `AA_BUNDLER_URL` (or `AA_BUNDLER_URL_<chainId>`) | Enables one-signature smart-account signing with gas in USDC (section 8); without it, step-by-step signing with ETH. `{chain_id}` is replaced |
| `AA_PAYMASTER_URL`, `AA_BUNDLER_HEADERS`, `AA_PAYMASTER_HEADERS`, `AA_PAYMASTER_CONTEXT` | Paymaster endpoint (default: the bundler's), extra HTTP headers, and the ERC-7677 context |
| `AA_PAYMASTER_ADDRESS`, `AA_MAX_GAS_FEE` | Required when the paymaster offers no token quote; `AA_MAX_GAS_FEE` also caps a quoted fee |
| `PIMLICO_API_KEY` | Shorthand for Pimlico's bundler and paymaster when `AA_BUNDLER_URL` is unset |

CLI: `omnibridge cards list`; `omnibridge cards check <id>` re-runs the code check,
report-only.

## 10. Found while building

- **A rejected proposal escaped the graph as an exception.** The docstring said the retry
  path handled it; in fact the run ended in ERROR. It now returns to discovery with the
  reason, within the retry bound.
- **An unfunded wallet was classed as a fork failure.** Found in the live integration run:
  `Insufficient funds for gas` was treated as fork infrastructure and would have been
  ignored. It's now a real failure.
- **CCTP's wrong-domain revert** is `No TokenMessenger for domain` (section 2).
- **"sepolia" alone resolves to the wrong chain.** The resolver's name-prefix match sends it
  to *Sepolia PGN* (58008), not Ethereum Sepolia, and the similarity guard lets it through.
  This predates this work and isn't fixed here. Use `ethereum-sepolia`.
- **Two-word chain names never reached the resolver.** The intent pattern captures one
  word per chain, so "Arbitrum Sepolia" became "Arbitrum", which is ambiguous on testnet.
  Hyphenated names now work.

## 11. Open decisions

1. **Rule B.** Hand-seeded cards are authored knowledge. The proposed wording from
   [PROTOCOL_CARDS.md](PROTOCOL_CARDS.md) section 2 ("authored knowledge enters as a lead,
   never as a fact") still needs sign-off.
2. **Committing card writes.** Runs write to `protocols/` in the working tree, as they do to
   `skills/`. Commit them for a reviewable history, or keep runtime writes out of git?
3. **CCTP V2's `maxFee` is chosen by the model.** V2 adds `maxFee`, the most the
   destination may deduct from the minted amount for a fast transfer. It is money, but the
   approval screen shows the burned amount, not the fee cap, and nothing bounds it. Under
   Rule A this should be computed or bounded by code (e.g. 0 for standard transfers, or a
   cap from Circle's fee endpoint) and shown to the human. Not done yet.
4. **Chains that still require V1.** Circle notes Noble, Sui and Aptos still need V1. The
   rule as built refuses V1 everywhere; a route to one of those would need an exception.
5. **`OMNIBRIDGE_CARD_AUTHORITY` default.** Recommend turning it on once the golden routes
   show no rise in first-attempt reverts.
