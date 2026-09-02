# Omnibridge 🌉
### Autonomous In-Transit Cross-Chain Execution System

Omnibridge is a cross-chain bridging engine built on one premise: **nobody should hand-write an integration per bridge.**

Router addresses, ABI signatures, chain IDs and CCTP domain mappings all live in public documentation. A reasoning agent can read that documentation at runtime, synthesize the calldata it implies, verify it against a live node, and throw the code away afterward — keeping only the verified procedure. The engineer's job in this model is not to write the Nth bridge adapter. It is to make the agent better, and to own the guardrails the agent is not allowed to cross.

This follows the Agentic Engineering framing in Cao, [*The End of Software Engineering*](https://arxiv.org/abs/2606.05608) (2026) — code as an ephemeral instrument of reasoning rather than the system itself — and takes its procedural-memory model (`SKILL.md`) from Nous Research's Hermes Agent.

---

## ⚠️ Implementation Status

The pipeline, the tool surface, the guardrails and the memory layer are built. **The reasoning core is not yet wired in**, so the stages that are meant to be agent-driven currently run on static fallbacks. Read this table before trusting anything below it.

| Layer | State |
| --- | --- |
| Pipeline orchestration — 6 stages, checkpointed, HITL interrupt | Working |
| Web UI, wallet connect, in-browser signing | Working |
| `SKILL.md` procedural memory — parse / find / save | Working (repository is currently empty) |
| Calldata encoding (CCTP, Across V3, generic, Soroban) | Working — but selected by static template, not by fetched docs |
| Simulation & circuit breaker | Working on EVM against a live RPC; **fails open** otherwise |
| Chain-name resolution | **Unsafe** — matches testnets and unrelated networks silently |
| Tool surface — search, doc fetch, REPL, `eth_call`, broadcast, skill save | Defined; 2 of 6 ever invoked |
| **Reasoning core (LLM)** | **Not implemented** |
| Dynamic route discovery | **Broken** — every uncached route raises |

Concretely: **no bridge request currently completes.** Route discovery raises before reaching the encoder. See [Known Gaps](#known-gaps).

---

## Design Premise

Three commitments shape the architecture:

**Code is ephemeral, procedures are durable.** The calldata for a given transfer is assembled in memory, used once, and discarded. What persists is a `SKILL.md` — a verified, reusable procedure with the router address, chain IDs, domain mapping and RPC fallbacks that were proven to work. The second identical request should skip discovery entirely.

**Tools over SDKs.** The agent is allowed to search the web, fetch documentation, run Python, and call an RPC. It is not given a vendored bridge SDK per protocol, because that is the per-bridge integration cost the design exists to avoid.

**The human owns the guardrails, not the implementation.** Two controls are deliberately not delegated to the agent: the simulation gate, which must pass a real `eth_call` before anything reaches a user, and the approval gate, which halts execution for an explicit human signature. Everything between them is fair game for runtime synthesis.

---

## Architecture

Two runtimes sit over one shared core. They implement the same six stages and are **not** behaviourally identical — see the note below.

```
[Intent] ─► [Discovery] ─► [JIT Calldata] ─► [eth_call Sim] ─► [HITL Gate] ─► [Broadcast + SKILL.md]
   1             2                3                4               5                  6
```

**LangGraph runtime** — [`graph.py`](graph.py), served by [`server.py`](server.py)
A `StateGraph` of six nodes with a `MemorySaver` checkpointer. Stage 5 uses a native LangGraph `interrupt()`, so the run pauses mid-graph: `/api/bridge/start` returns the pending payload plus a `thread_id`, the browser wallet signs, and `/api/bridge/resume` continues the same checkpoint. Every node writes into a `traces` dict that the UI renders as per-stage telemetry cards. This is the runtime under active development.

**CLI runtime** — [`omnibridge/core/agent.py`](omnibridge/core/agent.py), driven by [`omnibridge/cli.py`](omnibridge/cli.py)
The same six stages as a straight synchronous call, with approval as a terminal prompt.

> **Note:** the two runtimes differ on broadcast. The CLI passes `dry_run=(private_key is None)`, so it never broadcasts without a key. The LangGraph path passes `dry_run=False` and broadcasts for real. Do not assume behaviour observed in one carries to the other.

### Shared core

| Module | Responsibility |
| --- | --- |
| [`core/models.py`](omnibridge/core/models.py) | Pydantic contracts: `UserIntent`, `ChainInfo`, `RouteQuote`, `SimulationResult`, `ExecutionManifest`, `BroadcastReceipt` |
| [`discovery/rpc_manager.py`](omnibridge/discovery/rpc_manager.py) | Chain metadata from the ChainList registry, web-search fallback for non-EVM; races public RPCs concurrently on `eth_blockNumber` |
| [`discovery/route_finder.py`](omnibridge/discovery/route_finder.py) | Tier 0 `SKILL.md` cache, then live discovery |
| [`core/executor.py`](omnibridge/core/executor.py) | ABI encoding — CCTP `depositForBurn`, Across `depositV3`, generic deposit, Soroban invoke, plus `encode_dynamic_calldata` for arbitrary signatures |
| [`core/simulator.py`](omnibridge/core/simulator.py) | `eth_call` + `estimate_gas`; the revert circuit breaker |
| [`core/signer.py`](omnibridge/core/signer.py) | Broadcast — raw RLP, or an already-submitted hash returned by a browser wallet |
| [`skills/manager.py`](omnibridge/skills/manager.py) | `SKILL.md` with YAML frontmatter — parse, find, persist |
| [`tools.py`](tools.py) | LangChain tool surface with verbose telemetry |

---

## Installation

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Runtime dependencies not yet declared in pyproject.toml — see Known Gaps
pip install langgraph langchain-core fastapi uvicorn
```

---

## Running

### Web UI + API

```bash
python server.py          # http://localhost:8000
```

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Web UI |
| `GET /health` | Liveness |
| `GET /docs` | OpenAPI |
| `POST /api/bridge/start` | Runs stages 1–5, returns the approval payload and a `thread_id` |
| `POST /api/bridge/resume` | Resumes from the interrupt with a signed transaction |
| `GET /api/skills` | Lists persisted `SKILL.md` modules |

The UI offers Base → Solana, Base → Hyperliquid, Base → Arbitrum and Stellar → Arbitrum as starting prompts.

### CLI

```bash
omnibridge list-skills                                              # works
omnibridge bridge --token USDC --amount 100 --from base --to arbitrum   # fails at discovery
omnibridge rpc-bench base                                           # broken, see Known Gaps
```

### Tests

```bash
pytest tests/              # 4 pass, 1 fails on route discovery
python test_bridge_flow.py # end-to-end LangGraph flow — currently raises at stage 2
```

---

## The Guardrail Model

Two controls are human-owned and must fail closed. Their importance scales with how much of the transaction the agent generated: when a router address has been extracted from a search result rather than reviewed by a person, **the simulator is the only thing standing between a hallucinated address and a signed transaction.**

1. **Simulation gate** — a real `eth_call` against a live node must succeed before the user is shown anything. Any revert trips the circuit breaker and the run ends at the error node.
2. **Approval gate** — execution halts for an explicit human signature. Private keys never enter generated code; the web path signs in the user's own wallet and hands back only the resulting hash.

Both are currently weaker than this description. See gaps 3 and 4.

---

## Known Gaps

Ordered by how much they matter.

1. **No reasoning core.** There is no LLM in the codebase. `python_repl_runner`, `eth_call_simulator`, `rpc_broadcaster` and `save_skill_tool` in [`tools.py`](tools.py) are defined, decorated and never invoked — the graph calls the underlying classes directly. The planner is the static edge list at [`graph.py:413`](graph.py#L413); intent parsing is a regex at [`graph.py:60`](graph.py#L60).

2. **JIT synthesis is not yet closing the loop.** `discovery_node` fetches real ABI specifications via `fetch_doc_content_tool`, then writes them only into `traces` for display ([`graph.py:145`](graph.py#L145), [`graph.py:166`](graph.py#L166)). They never reach `code_builder_node`, which encodes from the static template chain in [`executor.py:124`](omnibridge/core/executor.py#L124) instead. The generic primitive this needs — `encode_dynamic_calldata` — already exists and is unused.

3. **The simulator fails open.** [`simulator.py:52-61`](omnibridge/core/simulator.py#L52-L61) returns `success=True` with invented gas figures when the RPC is unreachable; [`simulator.py:33-42`](omnibridge/core/simulator.py#L33-L42) does the same for any non-EVM target without simulating anything. [`formatter.py:37`](omnibridge/manifests/formatter.py#L37) then prints "Simulation succeeded on-chain with 0 reverts" unconditionally.

4. **Chain names resolve to the wrong network, silently.** Pass 1 of `discover_chain_metadata` ([`rpc_manager.py:49-55`](omnibridge/discovery/rpc_manager.py#L49-L55)) matches the registry's `chain` field and takes the first hit, with no mainnet preference and no testnet exclusion. Four Neon EVM networks declare `chain: "Solana"`, so **`Solana` resolves to Neon EVM Devnet** (chain ID `245022926`) — and that devnet's chain ID, RPCs and explorer then flow into the calldata and the persisted skill. Base → Solana is one of the UI's default suggestions. Any chain family whose testnet sorts first is exposed to the same failure.

5. **Route discovery raises on every uncached route.** `_query_online_route_manifest` in [`route_finder.py:130-136`](omnibridge/discovery/route_finder.py#L130-L136) references four names that no longer exist, and `import re` was dropped, so the address extraction at lines 103–104 raises `NameError` into a bare `except Exception: pass`. Combined with an empty skill repository, no route resolves. This one at least fails loudly.

6. **Persisted skills record a wrong chain ID.** [`graph.py:353`](graph.py#L353) hardcodes `dest_chain_id=200` in the metadata written at stage 6, which then feeds back through `RouteQuote` on a later cache hit.

7. **`rpc-bench` cannot succeed.** [`cli.py:67`](omnibridge/cli.py#L67) calls `get_chain_info()`, which only reads the in-memory cache that `discover_chain_metadata()` is what populates. It always reports `Unknown chain`.

8. **Undeclared dependencies.** `langgraph`, `langchain-core`, `fastapi`, `uvicorn` and `eth-abi` are imported but absent from `pyproject.toml`; a clean `pip install -e .` yields an environment where `server.py` will not import.

---

## Roadmap

**Stage 1 — Close the guardrails.** Make the simulator fail closed, make chain resolution refuse an ambiguous or testnet match instead of guessing, and make the manifest report what actually happened. These are the failures that are silently wrong rather than loudly broken, and they are prerequisite to trusting anything the agent generates.

**Stage 2 — Wire the reasoning core.** Bind the existing tool surface to a model in `discovery_node` and `code_builder_node`, so fetched ABI specifications drive `encode_dynamic_calldata` directly. The static template chain in `executor.py` then becomes removable, and gap 5 disappears along with the function that causes it.

**Stage 3 — Close the learning loop.** Skills are currently written but never re-verified. The target is Hermes' create → use → detect weakness → self-patch cycle: when a cached skill fails simulation, the agent repairs the `SKILL.md` rather than falling back to a cold search.
