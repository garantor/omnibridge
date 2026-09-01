# Omnibridge 🌉
### Autonomous In-Transit Cross-Chain Execution System

Omnibridge is an autonomous, self-evolving Web3 cross-chain bridging engine. Built upon the principles of **Agentic Engineering** (where code is an ephemeral tool of reasoning rather than a static artifact), Omnibridge executes cross-chain transfers dynamically without hardcoded bridge SDKs or vendor locks.

---

## ⚡ Execution Pipeline

```
[User Intent] ──► [Web/Registry Discovery] ──► [JIT Calldata Synthesis] ──► [eth_call Simulation] ──► [Human Approval Manifest] ──► [Broadcast & SKILL.md Save]
```

1. **Route Discovery (Multi-Tier Acceleration):** Checks local `SKILL.md` cache (0 ms) or queries verified protocol registries and performs concurrent async RPC racing (<150 ms).
2. **In-Transit Calldata Assembly:** Ephemerally encodes raw ABI-compliant calldata (`web3.py` / `eth_abi`) for Circle CCTP, Across Protocol V3, and native vaults.
3. **Simulation & Circuit Breaker:** Executes dry-run static calls (`eth_call`) against live RPCs. Any unexpected revert or slippage immediately trips the circuit breaker before reaching user prompts.
4. **Human-in-the-Loop Manifest:** Displays a color-coded parameter and fee breakdown in the terminal, requesting explicit signature authorization.
5. **Broadcast & Skill Registration:** Submits raw transactions to the source chain and compiles the verified routine into an open-standard `SKILL.md` (compatible with `agentskills.io` and Hermes Agent) for instant subsequent reuse.

---

## 📦 Installation & Setup

```bash
# Clone and enter directory
cd /Users/afolabi/projects/omnibridge

# Setup environment
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## 🛠️ CLI Usage Examples

### 1. Bridge Assets
```bash
# Interactive bridging (Prompts for human authorization manifest)
omnibridge bridge --token USDC --amount 100 --from base --to hyperliquid

# Automated bridging (with --yes flag)
omnibridge bridge --token USDC --amount 50 --from base --to arbitrum --yes
```

### 2. Inspect Skill Repository
```bash
omnibridge list-skills
```

### 3. Public RPC Latency Race
```bash
omnibridge rpc-bench base
```

---

## 🛡️ Guardrails & Safety Guarantees
- **No Static Bridges:** All routes and calldata are dynamically synthesized.
- **Never Auto-Sign by Default:** Halts at Step 4 for human verification. Private keys are never placed in ephemeral scripts.
- **Fail Fast:** Zero tolerance for failed simulations or missing gas bounds.
