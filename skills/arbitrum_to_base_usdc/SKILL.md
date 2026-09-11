---
name: arbitrum_to_base_usdc
description: Verified bridge workflow for USDC from arbitrum to base via Circle CCTP
source_chain: Arbitrum One
source_chain_id: 42161
dest_chain: base
dest_chain_id: 8453
dest_chain_ref:
  scheme: cctp_domain
  value: '6'
token: USDC
bridge_protocol: Circle CCTP
router_address: '0x19330d10D9Cc8751218eaf51E8885D058642E08A'
network: mainnet
function_signature: depositForBurn(uint256,uint32,bytes32,address)
param_types:
- uint256
- uint32
- bytes32
- address
param_values:
- '{amount_raw}'
- '6'
- '{recipient_bytes32}'
- '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'
spend:
  token: '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'
  spender: '0x19330d10D9Cc8751218eaf51E8885D058642E08A'
  amount: '5000000'
aliases:
- arbitrum
- arbitrum one
- base
verified: true
verification_count: 1
last_verified: '2026-09-11'
sources:
- https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
- https://arbiscan.io/address/0x19330d10D9Cc8751218eaf51E8885D058642E08A
discovered_by: skill_replay
revisions: []
rpc_fallbacks:
- https://arb1.arbitrum.io/rpc
- https://arbitrum-one-rpc.publicnode.com
- https://rpcfree.com/arbitrum-rpc
- https://rpc.satelink.network/rpc/arbitrum
- https://xrpc.cl/arbitrum
tags:
- cross-chain
- usdc
- arbitrum
- base
---

# ARBITRUM_TO_BASE_USDC Execution Routine

## Route
- **Protocol:** Circle CCTP
- **Router:** `0x19330d10D9Cc8751218eaf51E8885D058642E08A`
- **Function:** `depositForBurn(uint256,uint32,bytes32,address)`
- **Destination:** cctp_domain 6

## Parameters
Encoded through `CalldataExecutor.encode_dynamic_calldata`. Placeholders are bound per
request by `CalldataExecutor.bind_param_values`; everything else is fixed for this route.

| # | Type | Value |
|---|------|-------|
| 0 | `uint256` | `{amount_raw}` |
| 1 | `uint32` | `6` |
| 2 | `bytes32` | `{recipient_bytes32}` |
| 3 | `address` | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` |

## Verification
- Simulation: VERIFIED on chain 42161 via https://arbitrum-one-rpc.publicnode.com
- Gas: 116156 units
- Approved by a human and broadcast as `0x14f1f5bf7306df19e600411550bfc30f14a3d80e84553549e986d1fc0ba9661e`
- Sources: https://developers.circle.com/cctp-v1/docs/evm-smart-contracts, https://arbiscan.io/address/0x19330d10D9Cc8751218eaf51E8885D058642E08A

A cache hit skips discovery, not the guardrails: a replay is re-simulated and
re-approved before it can send anything.
