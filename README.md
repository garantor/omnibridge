# Omnibridge 🌉
### Autonomous In-Transit Cross-Chain Execution System

Omnibridge is a cross-chain bridging engine built on one premise: **nobody should hand-write an integration per bridge.**

Router addresses, ABI signatures, chain IDs and CCTP domain mappings all live in public documentation. A reasoning agent can read that documentation at runtime, synthesize the calldata it implies, verify it against a live node, and throw the code away afterward — keeping only the verified procedure. The engineer's job in this model is not to write the Nth bridge adapter. It is to make the agent better, and to own the guardrails the agent is not allowed to cross.

This follows the Agentic Engineering framing in Cao, [*The End of Software Engineering*](https://arxiv.org/abs/2606.05608) (2026) — code as an ephemeral instrument of reasoning rather than the system itself — and takes its procedural-memory model (`SKILL.md`) from Nous Research's Hermes Agent.

---

## ⚠️ Implementation Status

The pipeline, the guardrails, the memory layer and the reasoning core are built. **The reasoning core has never been run against the live API** — every model call in the test suite goes through a fake client — so the request shape is unverified. Read this table before trusting anything below it.

| Layer | State |
| --- | --- |
| Pipeline orchestration — 6 stages, checkpointed, HITL interrupt | Working |
| Web UI, wallet connect, in-browser signing | Working |
| `SKILL.md` procedural memory — exact-match lookup, demote-on-failure, retained revisions | Working (repository is currently empty) |
| Calldata encoding | Working — one generic path, driven by a signature the route carries |
| Simulation & circuit breaker | Working on EVM against an identity-checked RPC; refuses (never passes) otherwise |
| Chain-name resolution | Working — refuses ambiguous and low-similarity matches rather than guessing |
| Reasoning core (Opus 5 + server-side search, provenance-checked proposals) | **Built, not verified against the live API** |
| Dynamic route discovery | Works only with `ANTHROPIC_API_KEY` set; without it the system is cache-only |
| Non-EVM preflight (Soroban, Solana) | **Not implemented** — those routes halt at the simulation gate |

Concretely: with no credentials configured, **a route this system has not already verified is a route it does not have**, and it says so. That is the intended behaviour, not a gap — the previous fallbacks completed the same requests by inventing router addresses, ABI signatures and chain IDs.

To exercise the discovery path:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m pytest tests/test_agent_discovery.py -k live   # live smoke test, skips without a key
```

`OMNIBRIDGE_DISABLE_AGENT=1` forces the cache-only path.

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
| [`discovery/rpc_manager.py`](omnibridge/discovery/rpc_manager.py) | Chain metadata from the ChainList registry; refuses ambiguous or low-similarity matches; races public RPCs concurrently on `eth_blockNumber` |
| [`discovery/route_finder.py`](omnibridge/discovery/route_finder.py) | Tier 0 `SKILL.md` cache lookup. Nothing else — discovery is the agent's job |
| [`agent/`](omnibridge/agent/) | The reasoning core: the discovery loop, request budgets, on-chain provenance checks, and the deterministic proposal→calldata boundary |
| [`core/executor.py`](omnibridge/core/executor.py) | ABI encoding via `encode_dynamic_calldata`, plus per-request parameter binding |
| [`core/simulator.py`](omnibridge/core/simulator.py) | `eth_call` + `estimate_gas`; the revert circuit breaker |
| [`core/signer.py`](omnibridge/core/signer.py) | Broadcast — raw RLP, or an already-submitted hash returned by a browser wallet |
| [`skills/manager.py`](omnibridge/skills/manager.py) | `SKILL.md` with YAML frontmatter — exact-match lookup, demote-on-failure, retained revisions |
| [`tools.py`](tools.py) | LangChain tool surface with verbose telemetry |

---

## Installation

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
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
omnibridge list-skills
omnibridge bridge --token USDC --amount 100 --from base --to arbitrum   # needs ANTHROPIC_API_KEY
omnibridge rpc-bench base                                               # broken, see Known Gaps
```

### Tests

```bash
pytest tests/ -m 'not network'   # 102 pass, 2 xfail (Phase 3 tripwires)
pytest tests/ -m network         # golden-route checks against live Base
```

---

## The Guardrail Model

Two controls are human-owned and must fail closed. Their importance scales with how much of the transaction the agent generated: when a router address has been extracted from a search result rather than reviewed by a person, **the simulator is the only thing standing between a hallucinated address and a signed transaction.**

1. **Simulation gate** — a real `eth_call` against a live node must succeed before the user is shown anything. Any revert trips the circuit breaker and the run ends at the error node.
2. **Approval gate** — execution halts for an explicit human signature. Private keys never enter generated code; the web path signs in the user's own wallet and hands back only the resulting hash.

Both now fail closed. `UNAVAILABLE` ("could not check") and `UNSUPPORTED` ("no `eth_call` path") are refusals, not passes, and the manifest names which of them occurred and on which chain. A cache hit is a shortcut past discovery, not past either gate.

---

## Known Gaps

Ordered by how much they matter.

1. **The reasoning core has never been run against the live API.** Every model call in the test suite goes through a fake client. The control flow is genuinely covered — terminal tool calls, `pause_turn`, refusals, budget ceilings, retry bounds — but no request has ever been sent, so the request shape is unverified. Set `ANTHROPIC_API_KEY` and run the live smoke test in [`tests/test_agent_discovery.py`](tests/test_agent_discovery.py) before trusting it.

2. **Non-EVM destinations halt at the simulation gate.** A Base → Stellar transfer is an ordinary Base transaction and simulates normally; a transaction *submitted to* a non-EVM chain has no `eth_call` path, so it returns `UNSUPPORTED` and the run ends. Real Soroban and Solana preflight is unbuilt, and it is the largest remaining item.

3. **`eth_call` proves the source leg only.** No simulation can establish that funds arrive on the destination chain. The manifest says so explicitly rather than implying end-to-end verification.

4. **Discovery is non-deterministic.** The same intent, run twice, may yield two different valid routers. The skill cache bounds this in practice — the first verified answer becomes the cached one — but whether that is sufficient for a financial path is a product decision.

5. **Cold-path cost is unmeasured.** An Opus 5 discovery run with search and fetches costs materially more than a cache hit. Skills amortise it, but the cold number is not yet known.

6. **`rpc-bench` cannot succeed.** [`cli.py:67`](omnibridge/cli.py#L67) calls `get_chain_info()`, which only reads the in-memory cache that `discover_chain_metadata()` is what populates. It always reports `Unknown chain`.

### Closed since the first revision of this file

Recorded because each was silently wrong rather than loudly broken — the class of failure that reaches a signed transaction.

- `pad_bytes32_address` keccak-hashed every recipient longer than 20 bytes, so a Solana, Sui, Aptos or Stellar key was encoded as the *hash* of itself: well-formed, accepted by the contract, and unrecoverable.
- The simulator returned `success=True` with invented gas when the RPC was unreachable or the target non-EVM; the manifest printed "Simulation succeeded on-chain with 0 reverts" unconditionally.
- `Solana` resolved to Neon EVM Devnet (`245022926`) via the registry's `chain` family field, and that devnet's IDs and RPCs flowed into calldata.
- `RPCManager` fabricated `chain_id=1` for any chain it discovered by web search.
- The CCTP destination domain was computed as `chain_id % 10` (Arbitrum is domain 3; `42161 % 10` is 1), and defaulted to 0 — Ethereum — when absent.
- The encoder's fall-through invented `bridge(uint256,address,uint256)` for any unrecognised route, and its Soroban branch returned the UTF-8 bytes of a Python-looking string as a transaction payload.
- `fetch_doc_content_tool` invented ABI signatures, selectors and a Strkey contract address whenever a page fetch failed — which, against JS-rendered docs, was the normal path.
- The skill cache matched chain names by substring in both directions, so a mainnet skill answered a testnet request.
- Persisted skills recorded `dest_chain_id=200` for every route, and a `last_verified` date pinned to a literal string.
- The intent parser substituted `Unknown / 1.0 NATIVE` for anything it could not parse and reported `COMPLETED`.
- A malformed resume payload read as `USER_APPROVED`, carrying unsigned calldata forward as though a human had signed it.

---

## Roadmap

**Next — verify against the live API.** Everything downstream of the reasoning core is gated on this. The golden set proves the encoder reproduces real mainnet bytes; it does not prove the agent arrives at those bytes from the intent alone. A strict `xfail` tripwire in [`tests/test_golden_routes.py`](tests/test_golden_routes.py) fires the moment it does.

**Then — non-EVM preflight.** Soroban and Solana simulation paths, so those routes can clear the gate rather than halt at it.

**Then — cost and determinism.** Measure the cold discovery path, and decide whether cache-bounded non-determinism is acceptable in a financial system or needs a stronger constraint.
