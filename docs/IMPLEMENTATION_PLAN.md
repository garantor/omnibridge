# Omnibridge — Implementation Plan

How to get from the current codebase to the system the README describes: an agent that reads
bridge documentation at runtime and synthesizes the transaction, with humans owning the
guardrails rather than the integrations.

Written 2026-09-02, against commit `5cc4274`.

---

## 1. Governing rules

Two rules generate almost every decision below. Where a later section seems arbitrary, it is
usually one of these applied to a specific file.

### Rule A — The model chooses. Deterministic code encodes. The simulator and the human verify.

The LLM decides *which protocol, which router, which parameters*. It never emits final calldata
bytes, never signs, never computes amounts. Those stay in reviewed Python, because they are the
steps where a plausible-looking mistake is indistinguishable from a correct one until the funds
are gone.

Handing the model the *decision* removes the per-bridge integration cost, which is the whole
point. Handing it the *encoder* would remove the one layer that can be tested against known-good
bytes.

| Layer | Owner | Runtime-generated? |
| --- | --- | --- |
| ABI encoding, signing, RPC racing, amount math, lookup mechanics | Reviewed Python | Never |
| Protocol selection, router discovery, parameter derivation, chain identity | Agent / live registry | Yes — this is the premise |
| Provenance checks, simulation gate, human approval | Reviewed Python | Never |

Membership in the first row is not a matter of taste. Two conditions have to hold together:

1. **No judgement is required** — exactly one correct answer, fully specified, no per-bridge
   variation. Generation cannot add value, only error.
2. **Errors survive the guardrails** — the mistake is caught neither by simulation nor by a human
   reading the manifest.

Amount math is the clearest case. `100 USDC` is `100000000` at six decimals; a model that emits
`1000000000` moves ten times the funds through a transaction that **simulates perfectly** — balance
covers it, no revert, clean gas. Nothing downstream objects. Signing is an asymmetry argument: no
upside, unrecoverable downside. ABI encoding earns its place less from danger than from being the
one layer testable against known-good bytes (Phase 2). RPC racing is the weakest member, there for
"no upside" rather than "catastrophic downside" — it acquired a real safety justification only once
the fallback list turned out to be partly search-derived (1.4).

Address codecs — a Stellar `G...` to bytes32, an EVM checksum — belong in this row too, and are the
one place Rule A and Rule B genuinely collide: mechanical, yet per-chain. The resolution is that an
address codec is a **format standard, not a route table**. `stellar-sdk` is infrastructure in the
same sense `eth_abi` is; neither enumerates bridges, and neither rots when a router is upgraded.

### Rule B — No curated per-chain or per-protocol tables in the production path

No chain lists, no router registries, no testnet keyword lists, no protocol enums. Every one of
those is the per-bridge integration cost in a different shape: it only covers what someone already
enumerated, it grows as chains × protocols, and it rots silently the moment a router is upgraded.

Chain and route knowledge is either **discovered at runtime** or **earned into a `SKILL.md`** by a
run that passed simulation and human approval. Earned knowledge is legitimate because it carries
provenance and a verification history; authored knowledge is not, because it carries only the
author's confidence on the day they typed it.

The single exception is test fixtures (Phase 2), whose entire value is being a fixed reference
the implementation cannot talk its way out of.

---

## 2. The graph today, and what is wrong with its shape

```
                    START
                      │
          ┌───────────▼────────────┐
          │  1  Intent Parsing     │   regex
          └───────────┬────────────┘
          ┌───────────▼────────────┐
          │  2  Skill/Search       │   SKILL.md cache → web search
          │     Discovery          │
          └───────────┬────────────┘
          ┌───────────▼────────────┐
          │  3  Code Generation    │   static if/elif templates
          └───────────┬────────────┘
          ┌───────────▼────────────┐
          │  4  Dry-Run Simulation │  ◄── the only fork in the graph
          └─────┬────────────┬─────┘
         (pass) │            │ (fail)
        ┌───────▼──────┐  ┌──▼──────────────┐
        │ 5  Sign-off  │  │ Diagnostic      │
        └───────┬──────┘  └──┬──────────────┘
        ┌───────▼──────┐     │
        │ 6  Broadcast │     │
        │    & Save    │     │
        └───────┬──────┘     │
                └─────┬──────┘
                     END
```

Three structural facts follow from this shape, and they drive the phase order.

**The simulator is the only branch in the entire graph.** `graph.py` contains exactly one
`add_conditional_edges` call; every other edge is unconditional. The whole predicate is:

```python
def route_after_simulation(state):
    if state["simulation_result"] and state["simulation_result"].get("success"):
        return "hitl_gate_node"
    return "error_node"
```

So when the simulator returns `success=True` because the RPC was unreachable or the target was
not EVM, the only decision point in the graph always votes Pass — **the diagram collapses to a
straight line and the fork is decorative.** This is the entire argument for why guardrails precede
autonomy: wiring a reasoning core into a graph whose sole branch cannot say no would convert a
system that fails loudly into one that fails silently with funds behind it.

**Diagnostic Output is a dead end.** `add_edge("error_node", END)`, with no path back to Discovery.
A revert reason is the highest-quality signal the system ever produces — concrete, deterministic,
machine-readable evidence that the synthesized calldata was wrong — and it is currently discarded.
An agentic system that throws away its only ground-truth feedback cannot improve.

**Sign-off is not an edge.** The gate is `interrupt()` *inside* node 5, so rejection is expressed
by never resuming the thread. That is the right design and needs no new edge — but it means the
node's resume handling is load-bearing, and today it is not careful (Phase 1.8).

### The habit these share

Fail-open is not localised to the simulator. Three independent layers behave identically when they
cannot do their job:

| Layer | Behaviour on failure |
| --- | --- |
| Intent parser | substitutes `Unknown / 1.0 NATIVE`, reports `COMPLETED` |
| Web search | fabricates a hit citing real doc domains, reports a latency |
| Simulator | returns `success=True` with invented gas figures |

Each, asked a question it cannot answer, manufactures something shaped like an answer. That is the
habit Phase 1 exists to break, and it is why Phase 1 is much broader than "fix the simulator."

---

## 3. The target graph

```
                    START
                      │
          ┌───────────▼────────────┐
          │  1  Intent Parsing     │
          └───────────┬────────────┘
                      │
          ┌───────────▼────────────┐◄─────────────┐
          │  2  Discovery          │              │
          │     (bounded agent     │              │  retry ≤ N,
          │      tool loop)        │              │  revert reason
          └───────────┬────────────┘              │  in context
                      │                           │
          ┌───────────▼────────────┐              │
          │  3  Encode             │ deterministic│
          └───────────┬────────────┘              │
                      │                           │
          ┌───────────▼────────────┐              │
          │  4  Verify             │              │
          │     provenance +       │              │
          │     simulation         │              │
          └─────┬────────────┬─────┘              │
       VERIFIED │            │ REVERTED /         │
                │            │ UNAVAILABLE /      │
                │            │ UNSUPPORTED        │
                │      ┌─────▼──────────┐         │
                │      │  Diagnostic    ├─────────┘
                │      └─────┬──────────┘
                │            │ retry budget exhausted
        ┌───────▼──────┐     │
        │ 5  Sign-off  │     │
        └───────┬──────┘     │
        ┌───────▼──────┐     │
        │ 6  Broadcast │     │
        │    & Save    │     │
        └───────┬──────┘     │
                └─────┬──────┘
                     END
```

Two changes to the topology: node 4 gains a real veto (four states, not a boolean), and the
Diagnostic node gains a bounded edge back to Discovery so a revert becomes a learning signal rather
than a terminus.

A third outcome has no box because it is not a failure. **`NO_ROUTE_FOUND`** — the agent searched
honestly and no route exists between this pair — is a correct answer that terminates successfully.
It is distinct from a discovery error and must never be retried. Conflating "there is no bridge"
with "discovery broke" is the same fail-open confusion wearing a different hat, and node 2 needs to
be able to say the first one out loud.

---

## 4. Phase ordering

1. **Phase 0** — Unblock. Mechanical.
2. **Phase 1** — Make the guardrails fail closed. **Prerequisite to everything after it.**
3. **Phase 2** — Build the evaluation harness. **Prerequisite to Phase 3.**
4. **Phase 3** — Wire the reasoning core, add the retry edge.
5. **Phase 4** — Delete the static residue.
6. **Phase 5** — Close the learning loop.

Phase 1 first, for the reason in §2: the only fork in the graph currently cannot say no.

Phase 2 before Phase 3 because Phase 3 replaces the component that decides what to encode. Without
a golden set to diff against, there is no way to know whether the agent reached the same answer the
static templates did, and no way to change a prompt afterwards with any confidence.

---

## Phase 0 — Unblock

- **Make the package installable at all.** `pip install -e .` fails before it ever reaches
  dependency resolution: setuptools finds `static`, `skills` and `omnibridge` as candidate
  top-level packages in a flat layout and refuses to guess. The four `omnibridge/` subpackages
  also have no `__init__.py`. Add those, then pin discovery with
  `[tool.setuptools.packages.find] include = ["omnibridge*"]` and
  `py-modules = ["graph", "server", "tools"]`.
- **Declare missing dependencies** in `pyproject.toml`: `eth-abi`, `langgraph`, `langchain-core`,
  `fastapi`, `uvicorn`. Verify against a clean venv.
- **Fix `rpc-bench`** — `cli.py:67` calls `get_chain_info()`, which reads a cache that
  `discover_chain_metadata()` is what populates. Call the discovery path.
- **Minimally repair route discovery** — the missing `import re` and the four undefined names in
  `_query_online_route_manifest`'s return dict, which would `NameError` on every success path.
  A repair, not an improvement: the function is deleted in Phase 4. Where a value cannot be known
  without the agent, it becomes an explicit unknown rather than an invented fact — and the
  `destination_domain: chain_id % 10` expression is deleted outright, since a CCTP domain bears no
  arithmetic relationship to a chain ID (Arbitrum is domain 3; `42161 % 10` is 1) and that value
  would have encoded a wrong destination into real calldata.

**Done when:** `pip install -e .` succeeds in a clean venv, `server` / `graph` / `tools` import from
it, and `pytest tests/` is green.

**What Phase 0 cannot deliver.** The original criterion — "5 passing and `test_bridge_flow.py`
completes end to end" — was wrong. `test_agent_in_transit_flow` asserts a *completed* bridge, and no
router address is obtainable from any source: `web_search_tool` returns a fabricated hit containing
no addresses (1.2), and the skill repository is empty. Supplying one by hand is precisely the curated
router table Rule B forbids. The test is therefore marked `xfail(strict=True)` with that reasoning
recorded in the marker — strict, so it becomes a tripwire that fires the moment Phase 3 makes it
genuinely passable. A companion test asserts the property that *is* true today and should stay true:
discovery refuses rather than inventing a router. `test_bridge_flow.py` is blocked on the same thing.

Deferred to Phase 4: `omnibridge/discovery/route_finder.py` does `from tools import ...`, importing a
top-level module from inside the package. That resolves under an editable install and breaks under a
real one — but `tools.py` is replaced wholesale in Phase 3.2, so it is not worth restructuring now.

---

## Phase 1 — Guardrails fail closed

Each item converts a silent wrong answer into a loud refusal.

### 1.1 Intent parsing: reject, never default

`intent_parser_node` requires the literal token `bridge`. Anything else — `swap`, `send`, `move` —
misses the regex, and the parser then falls into a defaults block substituting
`Unknown / Unknown / 1.0 NATIVE` and reporting `status: "PARSED"`, `"COMPLETED"`. Verified against
the running graph: `swap 100 usdc from base to stellar` silently becomes `1.0 NATIVE from Unknown
to Unknown`, with no error anywhere.

- Widen the verb set — `bridge`, `swap`, `send`, `move`, `transfer`. Where no destination asset is
  named, they all denote the same operation.
- **Delete the defaults block.** An intent that does not parse is rejected. There is no such thing
  as a partially understood instruction to move money.
- Add `network: mainnet|testnet` to `UserIntent`; 1.6 depends on it.
- `UserIntent` carries one `token` field and no destination asset, so a real swap — "USDC for XLM" —
  cannot be represented at all. Either add `dest_token` or reject asset-conversion phrasing
  explicitly. Silently reading it as a same-asset bridge is today's behaviour and is wrong.

### 1.2 Search: an empty result is empty

`web_search_tool` queries DuckDuckGo's Instant Answer API and DeFiLlama, and when both come back
empty it **fabricates a hit** — echoing the query back with `docs.circle.com / across.to` hardcoded
as the source and the snippet "Official router contracts and public RPC endpoints resolved for
query '...'" (`tools.py`). The UI renders it as a real search result, with a latency figure attached.

This is not a rare branch. The Instant Answer API returns entity abstracts rather than web results
and is empty for nearly every technical query, so the fabricated path is the normal one.

Return nothing when there is nothing. The synthetic hit is deleted, not improved. Phase 3.2 replaces
this tool wholesale, but the fix cannot wait for Phase 3 — it is currently feeding invented text
into route discovery.

### 1.3 Simulator: tri-state, never fabricate

`SimulationResult.success: bool` cannot express "I could not check." Replace it:

```python
class SimulationStatus(str, Enum):
    VERIFIED    = "VERIFIED"     # eth_call succeeded against a live node
    REVERTED    = "REVERTED"     # eth_call reverted — circuit breaker
    UNAVAILABLE = "UNAVAILABLE"  # no reachable RPC — could not verify
    UNSUPPORTED = "UNSUPPORTED"  # no eth_call path on the chain being submitted to
```

- `success` becomes a read-only property, `status is VERIFIED`. Only `VERIFIED` reaches the gate.
- Gas fields become `Optional[int] = None`. When nothing was simulated they are `None` — not
  `180000`, not `100000`. Fabricated defaults are precisely how an unverified transaction currently
  passes for a verified one.
- `UNAVAILABLE` retries the full RPC fallback list before giving up (see 1.4). The racer already
  produces that list; the simulator currently receives one URL.
- `UNSUPPORTED` halts, and is defined against **the chain being submitted to, not the destination**.
  A Base→Stellar transfer is an ordinary Base transaction and simulates normally; only a transfer
  whose *source* chain has no `eth_call` path is `UNSUPPORTED`. Building those paths is a feature to
  write, not a `return True`.

Simulation covers the **source leg only**. No `eth_call` can prove funds arrive on the far side of a
bridge — the attestation and mint happen later and elsewhere. The manifest has to say so (1.7)
rather than let silence imply end-to-end assurance.

Touches `omnibridge/core/models.py`, `omnibridge/core/simulator.py:33-42`, `:52-61`, and
`route_after_simulation` in `graph.py`.

### 1.4 Verify the endpoint before trusting the simulation

Retrying down the fallback list is only safe if each fallback is the chain we think it is. Before
any `eth_call`, assert that `eth_chainId` on the selected endpoint equals the expected ID, and
re-assert after every failover. An endpoint silently pointing at a fork, a testnet, or a stale
archive node yields something worse than no simulation: a confident answer about the wrong chain.

This matters more than it first appears, because **the fallback list is not trusted infrastructure**.
`rpc_manager` builds part of it by regex-extracting URLs out of *web search results*
(`rpc_manager.py:100-104`). An attacker-controlled endpoint that returns a successful `eth_call` for
a transaction that would really revert defeats the circuit breaker entirely — the guardrail is only
as honest as the node answering it. So: chain-ID check on every endpoint, and prefer
registry-supplied RPCs over search-derived ones whenever both exist.

### 1.5 Simulator: never substitute the sender

`simulator.py:45` substitutes `0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045` when the sender is not
valid EVM. That simulates against a stranger's balances and allowances, so the result says nothing
about *this user's* transaction. An invalid sender is an error, not a substitution.

### 1.6 Chain resolution: resolve dynamically, verify, display honestly

`discover_chain_metadata` matches the registry's `chain` field in its exact-match pass and takes
`[0]`. Four Neon EVM networks declare `chain: "Solana"`, so `Solana` resolves to a devnet.

Per Rule B the fix is not a curated chain table. Four generic rules, none of which names a chain:

- **A chain ID beats a name, always.** When the intent carries a numeric chain ID, match on it and
  skip name resolution entirely — `137` is unambiguous in a way `Polygon` never is. Name matching is
  the fallback for the common case where a human typed a word, not the primary path.
- **Rank by field, not list order.** `name` and `shortName` outrank `chain` — `chain` is a family
  label, not an identity. More than one survivor raises `AmbiguousChainError` listing candidates.
  Never `[0]`.
- **Refuse a weak name match.** Compare the requested string against the *resolved canonical name*
  and refuse below a similarity threshold. `Solana` against `Neon EVM Devnet` fails on its face,
  without the code knowing what Solana is. This is the real fix, and it generalises to every
  mis-resolution of the same shape.
- **Mainnet vs testnet is intent, not keywords.** An explicit field on `UserIntent` (default
  mainnet), filtered using the registry's own structured data. A `sepolia`/`goerli`/`holesky` list
  takes on new maintenance every time a network launches.
- **A registry miss means unresolved, not non-EVM.** chainid.network is an EVM registry; a miss
  says only "this source does not know," which routes to discovery like any other unknown.

**Then verify** with the endpoint check from 1.4 — the selected RPC's `eth_chainId` must equal the
resolved ID. That catches a stale or wrong endpoint. It does **not** catch a name-binding error,
because nothing deterministic can: Neon's RPC will happily confirm that it is Neon.

**So display the resolved identity, never the requested string.** The manifest reads
`Neon EVM Devnet (245022926)`, not `Solana`. Someone who asked for Solana sees the mismatch
immediately. This hardcodes nothing and is the last line of defence for every resolution error the
automated checks miss.

Touches `omnibridge/discovery/rpc_manager.py:49-55`, `omnibridge/manifests/formatter.py`.

### 1.7 Manifest tells the truth

`manifests/formatter.py:37` prints "Simulation succeeded on-chain with 0 reverts" unconditionally.
Render from `SimulationStatus`, and surface `UNAVAILABLE` / `UNSUPPORTED` as prominently as a
revert. A manifest that overstates its own verification is worse than no manifest.

Two further rules, both of which fell out of tracing a real request:

- **Render the amount decoded back out of the calldata**, never echoed from the parsed intent. If
  the display and the transaction read from the same source, a decimal-scaling error is invisible to
  the human as well — the manifest simply repeats the mistake. Decoding the actual bytes is what
  makes a 10x error visible at the last gate that could catch it.
- **State the scope of verification.** "Simulated on Base (8453); the destination leg is not
  verifiable from here." Silence reads as end-to-end assurance.

### 1.8 Sign-off: a malformed resume is not approval

`hitl_gate_node` falls back to `state["unsigned_transaction_hex"]` when `resume_data` is not a
dict, then returns `status: "USER_APPROVED"`. A malformed resume payload therefore reads as
approval and carries an unsigned transaction forward. Require a well-formed signed payload;
anything else is a rejection.

### 1.9 Broadcast parity

The CLI passes `dry_run=(private_key is None)`; the LangGraph path passes `dry_run=False`. Two
runtimes with different broadcast semantics is a trap. One rule, both paths.

### 1.10 Regression tests

One test per item, each asserting the *refusal*: an unparseable intent is rejected rather than
defaulted, a search with no results returns no results, an unreachable RPC does not return success,
an endpoint whose `eth_chainId` disagrees is rejected rather than used, a source chain with no
`eth_call` path does not return success, `Solana` does not resolve to a Neon chain ID, an invalid
sender raises, and a malformed resume does not approve. These are the first tests in the repo to encode a safety property, and
they are the regression net for every phase after this one.

**Done when:** no path reaches the approval gate without a `VERIFIED` simulation, no layer
manufactures a result it did not obtain, and every failure above halts with a specific reason.

### Found while implementing Phase 1

Six substitutions of the same shape that the plan had not catalogued. Each produced a
transaction that would simulate perfectly while doing the wrong thing, which is the
signature of a Rule A failure — and none would have been caught by the simulator, however
honest it became.

- **`RPCManager` fabricated a chain ID.** The old non-EVM fallback ended in
  `ChainInfo(chain_id=1, ...)` — Ethereum mainnet's id — for *whatever* chain had been
  asked for. Stellar, Solana, anything. That id then flows into the signed transaction.
  The web-search RPC path is deleted: URLs scraped from search text cannot establish a
  chain identity, and inventing one is worse than refusing.
- **The block explorer was invented** as `https://{name}scan.io` whenever the registry had
  none. Now empty.
- **`graph.py` defaulted a missing wallet to the zero address.** It reads as a valid EVM
  address everywhere downstream, so the transfer would simulate and encode as a burn.
- **`signer.broadcast_transaction` fell back to `raw_tx_payload["data"]`** — the *unsigned*
  calldata — when given no key and no signature, treating it as a signed transaction.
- **Eight call sites in `executor.py`** read
  `to_checksum_address(x) if x.startswith("0x") and len(x) == 42 else "0x000…0"`. An
  unusable recipient silently became the zero address. A Stellar `G…` recipient reaching an
  EVM encoder is precisely this case, and the result is a burn with a clean gas estimate.
  Replaced with `require_evm_address`, which raises; an *explicit* zero address still
  passes, since that is a real convention for a native asset.
- **CCTP's `destination_domain` defaulted to 0**, which is Ethereum — a wrong chain, not an
  absent one. Now required.

`intent_parser_node` also gained the widened verb set from 1.1, so `swap` / `send` / `move`
/ `transfer` parse; combined with the deleted defaults block, `swap 100 usdc from base to
stellar` now resolves Base correctly and fails honestly on Stellar instead of silently
becoming `1.0 NATIVE Unknown -> Unknown`.

Coverage lives in `tests/test_guardrails.py` — 31 tests, every one asserting a refusal.

---

## Phase 2 — Evaluation harness

Agent output quality is bounded by the quality of the evaluation signal. Under the premise, this
harness *is* the human engineer's product — it is what makes the agent improvable.

### 2.1 Golden route set

`tests/golden/` — real, independently verifiable transfers. For each: the intent, the correct
router, the function selector, and the exact expected calldata bytes, taken from a confirmed
transaction on a block explorer.

These are hardcoded on purpose and are the only place in the repo that should be. A fixture's whole
value is being a fixed reference the implementation cannot talk its way out of — the opposite of a
production lookup table, which exists to be consulted and therefore to rot.

Start with four, one per protocol the static templates cover, so Phase 4's deletion is provably
behaviour-preserving: CCTP Base→Arbitrum, Across Base→Optimism, CCTP Ethereum→Base, and one
Soroban route.

### 2.2 Two levels

- **Encoder level** — given known-correct parameters, does `encode_dynamic_calldata` produce
  byte-identical calldata to the real transaction? This locks the deterministic layer and must pass
  unchanged before and after Phase 3.
- **Agent level** — given only the intent, does the agent arrive at the correct router and
  parameters? Scored on exact match of router address, selector, and parameter values. Expected to
  be noisy; the point is a number that moves when a prompt changes.

### 2.3 Cost and latency baseline

Record tokens and wall-clock per agent-level run from day one, so Phase 3's cost is a measured fact
rather than a surprise.

**Done when:** the golden set passes against the *current* static implementation. That is the
baseline Phase 3 must match.

### Found while implementing Phase 2

Fixtures were captured from chain data, never written by hand: router bytecode confirmed via
`eth_getCode`, selectors computed from their signatures and located in the deployed code,
calldata lifted verbatim from confirmed transactions found by reading router logs. Domain
numbers cross-checked against Circle's own table — which incidentally confirms **domain 27 is
Stellar**, so the Base to Stellar route traced earlier is a real one.

Three findings, one of them severe.

- **`pad_bytes32_address` hashed every non-EVM recipient.** It ended in
  `return Web3.keccak(text=address)` for any input longer than 20 bytes — that is, for every
  Solana, Sui, Aptos or Stellar recipient, the precise case the function exists to serve. The
  result is a well-formed bytes32 the contract accepts, that simulates with no revert, and
  that nobody holds a key for: burned on the source chain, minted to an unrecoverable address.
  Nothing in Phase 1 would have caught this; it took a real transaction with a real 32-byte
  recipient to expose it, which is the entire argument for the golden set.
- **The selector check needs to follow proxies.** Phase 3.5 proposes locating the claimed
  4-byte selector in a router's deployed bytecode. The Across SpokePool is an EIP-1967 proxy
  holding ~680 bytes, and `depositV3` is absent from it — the implementation has it. As
  specified, that check returns a false negative on most real routers. **Phase 3.5 must read
  the EIP-1967 implementation slot and check that bytecode before concluding a contract does
  not implement what was claimed.**
- **Real calldata carries trailing bytes.** The Across transaction ends in nine bytes of
  integrator attribution tag appended after the ABI encoding. Not part of the ABI, not
  derivable from the parameters. Fixtures record it separately so a correct encoder is not
  scored as wrong.

`coerce_param_values` was added to turn textual ABI arguments into encoder inputs — needed
here to drive fixtures, and needed again in Phase 3, whose `propose_route` schema carries
`param_values: list[str]`.

**Not delivered.** 2.3's cost and latency baseline measures agent runs, and there is no agent
yet; it belongs with the first real model call in Phase 3. The agent-level test in 2.2 exists
as an `xfail(strict=True)` tripwire that fires the moment Phase 3 can satisfy it. No Soroban
fixture: it is not EVM, so there is no comparable on-chain calldata to capture, and Phase 1
already halts those routes as `UNSUPPORTED`.

---

## Phase 3 — Wire the reasoning core

### 3.1 Model and loop

Official `anthropic` Python SDK, model `claude-opus-5`, adaptive thinking, high effort — long-horizon
agentic work with a financial cost of error is exactly where effort pays:

```python
thinking={"type": "adaptive"}
output_config={"effort": "high"}
```

Use the SDK's beta tool runner (`client.beta.messages.tool_runner` with `@beta_tool`) rather than
hand-writing the loop; its per-turn hooks are the natural home for approval gating, tool-error
interception and trace logging, all of which this design needs anyway.

Bound every run: a task budget (`output_config.task_budget`, minimum 20,000 tokens) plus a hard
iteration cap and wall-clock timeout. An unbounded loop against a paid API in a request path is its
own outage.

### 3.2 Replace the hand-rolled search

`tools.py` scrapes DuckDuckGo HTML and fetches docs with `requests`. Anthropic's server-side tools
do both without scrapers to maintain: `web_search_20260209` and `web_fetch_20260209`, both supported
on Opus 5. Constrain with `allowed_domains`. Net effect is a deletion.

### 3.3 The agent's only output is a strict schema

The agent returns neither prose nor calldata. It returns exactly one strict-schema tool call:

```python
propose_route(
    protocol:           str,
    router_address:     str,
    function_signature: str,        # e.g. "depositForBurn(uint256,uint32,bytes32,address)"
    param_types:        list[str],
    param_values:       list[str],
    source_chain_id:    int,        # the chain submitted to — selects the RPC, signs the tx
    dest_chain_ref:     dict,       # {scheme, value} — e.g. CCTP domain, LayerZero eid
    sources:            list[str],  # URLs the address and ABI came from
    rationale:          str,
)
```

`dest_chain_ref` is deliberately not `dest_chain_id: int`. Stellar has no EVM chain ID; CCTP
addresses destinations by domain, LayerZero by endpoint ID, Wormhole by its own numbering. A bare
int would bake an EVM assumption into the agent's output contract — Rule B hiding inside a schema.
The value the transaction actually uses already sits in `param_values` where the ABI puts it;
`dest_chain_ref` exists for display and verification.

`strict: true`, `additionalProperties: false`, all fields required. Control then returns to
deterministic code: validate the checksum, confirm the selector matches the signature, encode via
the existing `encode_dynamic_calldata`, verify, gate.

`sources` and `rationale` are not decoration — they feed §3.5 and the manifest a human approves.

### 3.4 Node changes and the retry edge

Nodes 2 and 3 separate cleanly: node 2 becomes the bounded agent loop, node 3 becomes pure
deterministic encoding. `intent_parser_node` stays a regex, now that 1.1 has made its failure mode
actually loud.

Add the edge from the target graph in §3: `error_node → discovery_node`, carrying the revert reason
and the rejected proposal in context, bounded by a retry counter in `GraphState`. Cap it at **three
attempts**, then fall through to END as today. This is what turns a revert from a terminus into
feedback.

Budget at the *request* level, not just the agent level. The task budget in 3.1 bounds a single
agent invocation; the retry edge can invoke it three times, so a per-request ceiling has to sit
above both — total tokens, total wall-clock, total RPC calls. Note that the runaway cost here is
**tokens and RPC rate limit, not gas**: `eth_call` and `estimate_gas` are read-only and spend
nothing on chain. Instrument what actually accrues.

Every model turn, tool call, and the final rationale goes into the existing `traces` dict, which the
UI already renders. The observability layer was built first here, so this is nearly free.

### 3.5 The new risk: prompt injection, and why simulation does not cover it

The most important section in the plan.

Once router addresses come from fetched web content, a malicious page can name an attacker's
contract, and **the simulator will not catch it.** A hostile contract that accepts a USDC transfer
simulates perfectly — `eth_call` succeeds, gas estimates fine, no revert. Simulation proves a
transaction is well-formed and will not fail. It proves nothing about whether the recipient is who
the agent claims.

The defence is provenance plus generic on-chain checks. Per Rule B, deliberately **not** a pinned
router table.

Checks requiring no prior knowledge of the protocol:

- **It is a contract.** `eth_getCode` returns non-empty bytecode. An EOA masquerading as a router
  fails here.
- **It implements what was claimed.** Compute the 4-byte selector from the proposed
  `function_signature` and require it in the deployed runtime bytecode. Generic across every
  protocol; imperfect against proxies, but a real signal for one RPC call.
- **Corroboration.** The address must appear in at least two independent sources to be eligible.
- **Domain allowlist** on search and fetch — operator security configuration, encoding no
  per-bridge behaviour.
- **Sources in the manifest**, beside the resolved chain identity from 1.6.
- All fetched page content is untrusted data. It is never instruction.

Trust legitimately accumulates in the **skill repository** — earned, not authored. A `SKILL.md`
records a router that passed simulation and human approval on a real run: a pinned address with
provenance and a verification history, produced by the system rather than maintained by hand.

### 3.6 Kill switch

An env flag forcing the static path, so the agent can be disabled in one deploy without a rollback.

**Done when:** the golden set passes through the agent path at parity with the static baseline,
with cost and latency recorded.

### Built, and what differs from the plan

`omnibridge/agent/` holds four modules: `discovery_agent` (the bounded loop), `provenance`
(the on-chain checks), `synthesis` (proposal to transaction), `budget` (request-level
accounting). Wired into node 2, with the retry edge closed in the compiled graph.

**Not verified against the live API.** No credentials are configured in this environment, so
every model call is exercised through a fake client. The control flow is tested — terminal
tool calls, `pause_turn` resumption, refusals, budget ceilings, retry bounds — but no request
has ever been sent. `tests/test_agent_discovery.py` carries a live smoke test that skips
without `ANTHROPIC_API_KEY`. **Run it before trusting the request shape.**

Deviations, each for a stated reason:

- **A manual loop, not the SDK tool runner.** The runner exists to execute client tools and
  continue; here the only client tool *is* the answer and must terminate the loop. A manual
  loop is a better fit and has no beta dependency.
- **No `task_budget`.** It requires streaming plus a beta header, and shipping an untestable
  streaming path as the default is worse than not having it. Its job — keeping a loop from
  running away — is done by hard ceilings in `RequestBudget`: attempts, model calls, input
  and output tokens, wall-clock. Revisit once a key exists to verify against.
- **`no_route_found` is a second terminal tool.** §3 said `NO_ROUTE_FOUND` needs to be
  expressible; giving the model an explicit way to say it is how. Honest refusal is cheap,
  a guessed router address is not.
- **Server-side fallbacks are on by default** (`fallbacks: "default"`), so a classifier
  decline does not dead-end a legitimate bridging question. Disable with
  `DiscoveryAgent(use_fallbacks=False)`.
- **Without credentials the agent path silently falls through to the static one.** Deliberate:
  the system keeps working, and the kill switch (`OMNIBRIDGE_DISABLE_AGENT=1`) forces the same
  fall-through on demand.
- **3.2's deletion of the scrapers is deferred to Phase 4**, since the static path still uses
  them. The agent path uses `web_search_20260209` / `web_fetch_20260209` with a domain
  allowlist and never touches them.

Two findings worth carrying forward:

- **The proxy fix from Phase 2 is implemented** and verified against both real routers: CCTP's
  TokenMessenger passes directly, Across's SpokePool passes via its EIP-1967 implementation.
- **"Holds bytecode" is no longer "is a contract".** Since EIP-7702 a delegated EOA carries 23
  bytes (`0xef0100 || address`), so the bytecode check alone would pass one. The selector check
  is what rejects it, which is why both run.

---

## Phase 4 — Delete the static residue

Only after Phase 3 reaches parity. Each deletion is justified by a golden-set test that still passes:

- The `if/elif` template chain, `executor.py:124-165`.
- `_query_online_route_manifest`, `route_finder.py` — README gap 5 disappears with the function that
  causes it.
- The hardcoded `dest_chain_id=200`, `graph.py:353`, replaced by the agent's resolved value.
- The DuckDuckGo scraper and doc fetcher in `tools.py`, superseded by §3.2.

This is where the codebase stops being traditional software with agentic vocabulary and starts
matching its own README.

---

## Phase 5 — Close the learning loop

Skills are currently written and never re-checked, which makes the cache a way to persist a mistake.
Hermes' create → use → detect weakness → self-patch cycle, applied:

- **Verify on cache hit.** A cached skill still passes through simulation. A skill is a shortcut past
  *discovery*, never past the guardrails.
- **Self-patch on failure.** When a cached skill fails simulation — router upgraded, domain ID
  changed — the retry edge from 3.4 already returns control to Discovery with the revert reason.
  Phase 5 adds the write-back: the agent repairs that `SKILL.md` rather than silently falling back
  to a cold search. This is the loop that makes the system improve with use.
- **Demote before patching. Never overwrite on the strength of a failure.** A failed verification
  marks the skill unverified; it does not rewrite it. A transient RPC error must not be able to
  replace a known-good route with whatever a cold search turned up. The patch lands only once a
  *replacement* route has itself passed simulation and human approval — the same gate the original
  cleared. This is the difference between a system that learns and one that launders its own errors.
- **Keep the previous revision.** Retain prior versions — the skills directory under git, or a
  `revisions:` list in the frontmatter — so a bad patch that clears both gates is still reversible.
  Human approval is a good filter, not an infallible one.
- **Provenance in metadata.** Which sources, which model, verified when, successful-use count, last
  failure. A skill with no recent successful verification is a hypothesis, not a fact, and the
  manifest should say so.

---

## Explicit non-goals

- The LLM never signs, never encodes final bytes, never computes amounts or slippage.
- No autonomous broadcast. The human approval gate stays, regardless of agent confidence.
- No agent-authored changes to guardrail code.
- Skills are procedural memory, not executable code loaded at runtime.
- No curated per-chain or per-protocol tables in the production path (Rule B). Test fixtures excepted.

---

## Open questions

1. **Cost per request.** An Opus 5 discovery run with search and fetches costs materially more than
   a dict lookup. Skills amortise it — the second identical route should never reach the model — but
   the cold-path number is needed before production. Phase 2.3 produces it.
2. **Non-EVM simulation.** Phase 1 makes those routes halt honestly. Making them *work* needs real
   Soroban and Solana preflight paths — separate work, and probably the largest item after Phase 5.
3. **Determinism in a financial path.** Same intent, two runs, possibly two different valid routers.
   The skill cache bounds this in practice, since the first verified answer becomes the cached one.
   Whether that is sufficient is a product decision, not a technical one.
