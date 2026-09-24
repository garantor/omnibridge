---
id: cctp
name: Circle CCTP
kind: bridge
network: mainnet
card_version: 2
aliases:
- cctp
- circle cctp
- cross-chain transfer protocol
documentation:
- developers.circle.com
dest_scheme: cctp_domain
assets:
- USDC
executable: true
blocked_on: []
last_updated: '2026-09-24'
deployments:
- id: 42161/token_messenger/v1
  chain_id: 42161
  chain: Arbitrum One
  role: token_messenger
  version: v1
  address: '0x19330d10D9Cc8751218eaf51E8885D058642E08A'
  status: verified
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  - https://arbiscan.io/address/0x19330d10D9Cc8751218eaf51E8885D058642E08A
  added: '2026-09-11'
  added_by: skill:arbitrum_to_base_usdc
  verified_count: 1
  last_verified: '2026-09-11'
  evidence:
  - '0x14f1f5bf7306df19e600411550bfc30f14a3d80e84553549e986d1fc0ba9661e'
  consecutive_failures: 0
- id: 8453/token_messenger/v1
  chain_id: 8453
  chain: Base
  role: token_messenger
  version: v1
  address: '0x1682Ae6375C4E4A97e4B583BC394c861A46D8962'
  status: checked
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-24'
  added_by: seed
  verified_count: 0
  evidence: []
  consecutive_failures: 0
- id: 1/token_messenger/v1
  chain_id: 1
  chain: Ethereum
  role: token_messenger
  version: v1
  address: '0xBd3fa81B58Ba92a82136038B25aDec7066af3155'
  status: checked
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-24'
  added_by: seed
  verified_count: 0
  evidence: []
  consecutive_failures: 0
destinations:
- id: '8453'
  chain_id: 8453
  ref:
    scheme: cctp_domain
    value: '6'
  status: verified
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-11'
  added_by: skill:arbitrum_to_base_usdc
  verified_count: 1
  last_verified: '2026-09-11'
  evidence:
  - '0x14f1f5bf7306df19e600411550bfc30f14a3d80e84553549e986d1fc0ba9661e'
  consecutive_failures: 0
- id: '42161'
  chain_id: 42161
  ref:
    scheme: cctp_domain
    value: '3'
  status: lead
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-24'
  added_by: seed
  verified_count: 0
  evidence: []
  consecutive_failures: 0
- id: '1'
  chain_id: 1
  ref:
    scheme: cctp_domain
    value: '0'
  status: lead
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-24'
  added_by: seed
  verified_count: 0
  evidence: []
  consecutive_failures: 0
tokens:
- id: 42161/USDC
  chain_id: 42161
  symbol: USDC
  address: '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'
  decimals: 6
  status: verified
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-11'
  added_by: skill:arbitrum_to_base_usdc
  verified_count: 1
  last_verified: '2026-09-11'
  evidence:
  - '0x14f1f5bf7306df19e600411550bfc30f14a3d80e84553549e986d1fc0ba9661e'
  consecutive_failures: 0
- id: 8453/USDC
  chain_id: 8453
  symbol: USDC
  address: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
  decimals: 6
  status: checked
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-24'
  added_by: seed
  verified_count: 0
  evidence: []
  consecutive_failures: 0
- id: 1/USDC
  chain_id: 1
  symbol: USDC
  address: '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'
  decimals: 6
  status: checked
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-24'
  added_by: seed
  verified_count: 0
  evidence: []
  consecutive_failures: 0
functions:
- id: token_messenger/depositForBurn
  role: token_messenger
  signature: depositForBurn(uint256,uint32,bytes32,address)
  params:
  - name: amount
    type: uint256
    meaning: transfer amount in the token's base units
  - name: destinationDomain
    type: uint32
    meaning: the destinations[].ref value, not an EIP-155 chain id
  - name: mintRecipient
    type: bytes32
    meaning: recipient address left-padded to 32 bytes
  - name: burnToken
    type: address
    meaning: the source-chain USDC from tokens[]
  spend:
    token: burnToken
    spender: self
  read_only: false
  status: verified
  sources:
  - https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
  added: '2026-09-11'
  added_by: skill:arbitrum_to_base_usdc
  verified_count: 1
  last_verified: '2026-09-11'
  evidence:
  - '0x14f1f5bf7306df19e600411550bfc30f14a3d80e84553549e986d1fc0ba9661e'
  consecutive_failures: 0
parameters: []
known_reverts: []
changelog:
- at: '2026-09-24'
  card_version: 1
  cause: 'seed: CCTP v1; entries the arbitrum_to_base_usdc skill earned are verified,
    the rest are leads'
  changes:
  - + deployments:42161/token_messenger/v1 (verified)
  - + deployments:8453/token_messenger/v1 (lead)
  - + deployments:1/token_messenger/v1 (lead)
  - + destinations:8453 (verified)
  - + destinations:42161 (lead)
  - + destinations:1 (lead)
  - + tokens:42161/USDC (verified)
  - + tokens:8453/USDC (lead)
  - + tokens:1/USDC (lead)
  - + functions:token_messenger/depositForBurn (verified)
- at: '2026-09-24'
  card_version: 2
  cause: 'cards check: 4 lead(s) passed on-chain checks'
  changes:
  - ~ deployments:8453/token_messenger/v1 lead -> checked
  - ~ deployments:1/token_messenger/v1 lead -> checked
  - ~ tokens:8453/USDC lead -> checked
  - ~ tokens:1/USDC lead -> checked
---

# Circle CCTP

Generated from the frontmatter on every write; edits below this line are overwritten. Statuses: `lead` unchecked, `checked` passed on-chain checks, `verified` used in an approved broadcast, `demoted` failed after that.

## Deployments

| Id | Address | Status |
|---|---|---|
| 42161/token_messenger/v1 | `0x19330d10D9Cc8751218eaf51E8885D058642E08A` | verified |
| 8453/token_messenger/v1 | `0x1682Ae6375C4E4A97e4B583BC394c861A46D8962` | checked |
| 1/token_messenger/v1 | `0xBd3fa81B58Ba92a82136038B25aDec7066af3155` | checked |

## Destinations

| Chain id | Identifier | Status |
|---|---|---|
| 8453 | cctp_domain 6 | verified |
| 42161 | cctp_domain 3 | lead |
| 1 | cctp_domain 0 | lead |

## Tokens

| Id | Address | Decimals | Status |
|---|---|---|---|
| 42161/USDC | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` | 6 | verified |
| 8453/USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | 6 | checked |
| 1/USDC | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | 6 | checked |

## Functions

| Id | Signature | Status |
|---|---|---|
| token_messenger/depositForBurn | `depositForBurn(uint256,uint32,bytes32,address)` | verified |

## Changelog

- v2 (2026-09-24): cards check: 4 lead(s) passed on-chain checks
  - ~ deployments:8453/token_messenger/v1 lead -> checked
  - ~ deployments:1/token_messenger/v1 lead -> checked
  - ~ tokens:8453/USDC lead -> checked
  - ~ tokens:1/USDC lead -> checked
- v1 (2026-09-24): seed: CCTP v1; entries the arbitrum_to_base_usdc skill earned are verified, the rest are leads
  - + deployments:42161/token_messenger/v1 (verified)
  - + deployments:8453/token_messenger/v1 (lead)
  - + deployments:1/token_messenger/v1 (lead)
  - + destinations:8453 (verified)
  - + destinations:42161 (lead)
  - + destinations:1 (lead)
  - + tokens:42161/USDC (verified)
  - + tokens:8453/USDC (lead)
  - + tokens:1/USDC (lead)
  - + functions:token_messenger/depositForBurn (verified)
