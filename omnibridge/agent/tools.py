"""
The agent's toolkit (T): five client-side tools, provider-neutral.

Phase 2 of docs/AGENTIC_OMNIBRIDGE.md. Each tool wraps reviewed code the pipeline
already trusts, and each result is advisory: whatever the agent learns here, its
`propose_route` is still re-checked from scratch by `synthesis`, simulated by the graph
and approved by a human. The tools make the agent's research checkable *while it is
researching*, so it can correct itself inside one turn instead of after a revert.

  resolve_token_address      token lists + the contract's own symbol()/decimals()
  fetch_bytecode_and_verify  bytecode, EIP-1967 proxy, selector — provenance.verify_proposal
  encode_jit_payload         abi_signature + CalldataExecutor, the encoder synthesis uses
  simulate_transaction       an Anvil fork when available (approve first, then the call),
                             plain eth_call otherwise
  propose_card_update        cards.proposals.apply_patch — checked, then saved unverified

Rule A still holds: the model chooses, reviewed code encodes. `encode_jit_payload`
shows the model the bytes reviewed code would produce; it does not let the model supply
bytes. Simulations run only on the run's source chain and only from the run's sender:
a simulation from any other account describes that account, not this transfer.

`handle` never raises. A bad argument, an unreachable node or an exhausted budget comes
back as {"ok": false, "error": ...} so the model can read it and adjust.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from web3 import Web3

from omnibridge.agent.provenance import verify_proposal
from omnibridge.cards import proposals
from omnibridge.cards.manager import CardManager
from omnibridge.core import abi_signature, erc20
from omnibridge.core.executor import CalldataExecutor

Web3ForChain = Callable[[int], Optional[Web3]]

TOOL_NAMES = (
    "resolve_token_address",
    "fetch_bytecode_and_verify",
    "encode_jit_payload",
    "simulate_transaction",
    "propose_card_update",
)

_CHAIN = {
    "type": "integer",
    "description": "EIP-155 chain id, e.g. 42161 for Arbitrum One, 8453 for Base.",
}

SCHEMAS: Dict[str, Dict[str, Any]] = {
    "resolve_token_address": {
        "description": (
            "Look up a token's contract address on a chain from public token lists, then ask "
            "the contract itself for its symbol and decimals. Use it to cross-check a token "
            "address from documentation. Read-only."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"chain_id": _CHAIN, "symbol": {"type": "string"}},
            "required": ["chain_id", "symbol"],
        },
    },
    "fetch_bytecode_and_verify": {
        "description": (
            "Check that a contract exists at an address and implements a function: reads its "
            "bytecode (following an EIP-1967 proxy) and looks for the function's selector. "
            "Call this on any router address before proposing it."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "chain_id": _CHAIN,
                "contract_address": {"type": "string"},
                "function_signature": {
                    "type": "string",
                    "description": "Canonical, no spaces, e.g. depositForBurn(uint256,uint32,bytes32,address).",
                },
            },
            "required": ["chain_id", "contract_address", "function_signature"],
        },
    },
    "encode_jit_payload": {
        "description": (
            "ABI-encode a call with the same reviewed encoder the pipeline uses, and return "
            "the calldata. Use it to check your parameters parse, and to get calldata for "
            "simulate_transaction. You never write calldata yourself."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "abi_signature": {"type": "string"},
                "parameters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Values in order, as strings: integers in base 10, addresses and bytes 0x-prefixed.",
                },
            },
            "required": ["abi_signature", "parameters"],
        },
    },
    "simulate_transaction": {
        "description": (
            "Dry-run a call on the SOURCE chain from the user's wallet. Returns success, gas "
            "used and the revert reason. If the call pulls an ERC-20, pass `approve` so the "
            "approval is executed first (on a forked chain); without it an ERC-20 bridge call "
            "always reverts on allowance. Use this to test a route before proposing it."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "chain_id": _CHAIN,
                "to": {"type": "string"},
                "calldata": {"type": "string"},
                "value": {"type": "string", "description": "Wei, base 10. Usually 0."},
                "approve": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "token": {"type": "string"},
                        "spender": {"type": "string"},
                        "amount": {"type": "string"},
                    },
                    "required": ["token", "spender", "amount"],
                },
            },
            "required": ["chain_id", "to", "calldata"],
        },
    },
    "propose_card_update": {
        "description": (
            "Record something you established about a protocol on its card, so later runs "
            "start from it. Code checks it first (bytecode, selectors, an allowlisted "
            "documentation source) and saves it as unverified; only an approved broadcast "
            "verifies it. Patch kinds: chain_row {network, chain_id, dest_id, addresses: "
            "{column label: address}}; interface {label, signature, target}; failure_fix "
            "{revert, fix} for a revert already on the card; new_card {protocol_name, docs, "
            "supported_tokens, dest_id_label, address_labels, interfaces}."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protocol_id": {"type": "string", "description": "e.g. cctp"},
                "patch_data": {"type": "object", "description": "One patch; must include `kind`."},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Documentation URLs you read that state this.",
                },
                "reason": {"type": "string", "description": "One sentence, for reviewers."},
            },
            "required": ["protocol_id", "patch_data", "sources", "reason"],
        },
    },
}


@dataclass
class ToolLimits:
    max_calls: int = 20
    max_simulations: int = 4
    max_card_updates: int = 6


@dataclass
class ToolContext:
    sender: str
    source_chain_id: int
    #: Simulator for the source chain (identity-checked endpoints).
    simulator: Any
    cards: CardManager
    web3_for: Web3ForChain
    domains: List[str]
    token_registry: Any = None


@dataclass
class AgentToolkit:
    ctx: ToolContext
    allowed: Optional[Iterable[str]] = None
    limits: ToolLimits = field(default_factory=ToolLimits)
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        names = list(self.allowed) if self.allowed is not None else list(TOOL_NAMES)
        unknown = set(names) - set(TOOL_NAMES)
        if unknown:
            raise ValueError(f"unknown tools {sorted(unknown)}")
        self.allowed = names
        self._counts: Dict[str, int] = {}

    # ------------------------------------------------------------ interface

    def schemas(self) -> List[Dict[str, Any]]:
        """Neutral {name, description, input_schema}; providers wrap them."""
        return [{"name": n, **SCHEMAS[n]} for n in self.allowed]

    def handles(self, name: str) -> bool:
        return name in (self.allowed or [])

    def handle(self, name: str, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        payload = dict(payload or {})
        result = self._dispatch(name, payload)
        self.calls.append({"tool": name, "input": payload, "result": result})
        return result

    def summary(self) -> Dict[str, Any]:
        return {"calls": len(self.calls), "by_tool": dict(self._counts), "log": self.calls}

    # ------------------------------------------------------------- dispatch

    def _spend(self, name: str) -> Optional[str]:
        self._counts[name] = self._counts.get(name, 0) + 1
        total = sum(self._counts.values())
        if total > self.limits.max_calls:
            return f"Tool budget exhausted ({self.limits.max_calls} calls). Decide with what you have."
        if name == "simulate_transaction" and self._counts[name] > self.limits.max_simulations:
            return f"Simulation budget exhausted ({self.limits.max_simulations})."
        if name == "propose_card_update" and self._counts[name] > self.limits.max_card_updates:
            return f"Card update budget exhausted ({self.limits.max_card_updates})."
        return None

    def _dispatch(self, name: str, p: Dict[str, Any]) -> Dict[str, Any]:
        if not self.handles(name):
            return {"ok": False, "error": f"{name} is not available to you."}
        exhausted = self._spend(name)
        if exhausted:
            return {"ok": False, "error": exhausted}
        try:
            return getattr(self, f"_{name}")(p)
        except Exception as exc:  # the model reads this and adjusts
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- tools

    def _w3(self, chain_id: Any) -> Web3:
        w3 = self.ctx.web3_for(int(chain_id))
        if w3 is None:
            raise RuntimeError(f"No RPC node confirmed chain {chain_id}; nothing could be read.")
        return w3

    def _resolve_token_address(self, p):
        from omnibridge.discovery.token_registry import TokenRegistry

        chain_id, symbol = int(p["chain_id"]), str(p["symbol"])
        registry = self.ctx.token_registry or TokenRegistry()
        ref = registry.resolve(chain_id, symbol)
        w3 = self._w3(chain_id)
        on_chain_symbol = erc20.read_symbol(w3, ref.address)
        decimals = erc20.read_decimals(w3, ref.address)
        corroborated = bool(on_chain_symbol) and on_chain_symbol.upper() == symbol.upper()
        return {
            "ok": True,
            "address": Web3.to_checksum_address(ref.address),
            "decimals": decimals,
            "symbol_on_chain": on_chain_symbol,
            "corroborated": corroborated,
            "list": ref.source,
            "note": None if corroborated else "The contract does not call itself this symbol; do not use it.",
        }

    def _fetch_bytecode_and_verify(self, p):
        signature = str(p["function_signature"])
        abi_signature.parse(signature)
        report = verify_proposal(self._w3(p["chain_id"]), str(p["contract_address"]), signature, [])
        return {
            "ok": True,
            "has_bytecode": report.is_contract,
            "bytecode_size": report.bytecode_size,
            "implementation": report.implementation_address,
            "selector": report.selector,
            "selector_present": report.selector_present,
            "problems": [f for f in report.failures if "documentation" not in f],
        }

    def _encode_jit_payload(self, p):
        signature = str(p["abi_signature"])
        types = abi_signature.parse(signature).types
        values = CalldataExecutor.coerce_param_values(types, list(p["parameters"]))
        calldata = CalldataExecutor.encode_dynamic_calldata(signature, types, values)
        return {"ok": True, "calldata": calldata, "selector": calldata[:10],
                "bytes": (len(calldata) - 2) // 2}

    def _simulate_transaction(self, p):
        from omnibridge.core.fork import ForkStep

        if int(p["chain_id"]) != self.ctx.source_chain_id:
            return {"ok": False, "error": (
                f"Simulations run on the source chain ({self.ctx.source_chain_id}) only: "
                "that is where the transaction is submitted.")}
        to, calldata = str(p["to"]), str(p["calldata"])
        value = int(str(p.get("value") or "0"), 0)
        steps = []
        approve = p.get("approve")
        if approve:
            steps.append(ForkStep(
                to=str(approve["token"]),
                data=erc20.build_approve(str(approve["spender"]), int(str(approve["amount"]), 0)),
                label="approve",
            ))
        steps.append(ForkStep(to=to, data=calldata, value_wei=value, label="call"))

        run, why_not = self.ctx.simulator.simulate_sequence_on_fork(self.ctx.sender, steps)
        if run is not None:
            last = run.steps[-1] if run.steps else None
            failure = run.first_failure
            return {
                "ok": True, "mode": "anvil_fork", "success": run.success,
                "gas_used": last.gas_used if last and last.success else None,
                "failed_step": failure.label if failure else None,
                "revert_reason": failure.revert_reason if failure else None,
                "fork_infrastructure_error": bool(failure and failure.infrastructure_error),
            }

        result = self.ctx.simulator.simulate_call(
            from_address=self.ctx.sender, to_address=to, calldata=calldata, value_wei=value
        )
        out = {
            "ok": True, "mode": "eth_call", "success": result.success,
            "gas_used": result.gas_used, "revert_reason": result.revert_reason,
            "status": result.status.value, "fork_unavailable": why_not,
        }
        if approve:
            out["note"] = ("No fork was available, so the approval could not be applied first; "
                           "an allowance revert here says nothing about the route.")
        return out

    def _propose_card_update(self, p):
        result = proposals.apply_patch(
            self.ctx.cards,
            str(p["protocol_id"]),
            dict(p.get("patch_data") or {}),
            [str(s) for s in (p.get("sources") or [])],
            self.ctx.web3_for,
            self.ctx.domains,
        )
        return {"ok": result.accepted, **result.as_dict()}
