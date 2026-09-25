---
protocol_id: cctp_v2_testnet
protocol_name: Circle CCTP V2
docs:
- https://developers.circle.com/llms.txt
- https://developers.circle.com/cctp/references/contract-addresses.md
- https://developers.circle.com/cctp/concepts/supported-chains-and-domains.md
- https://developers.circle.com/cctp/migration-from-v1-to-v2.md
supported_chains:
- ethereum sepolia
- arbitrum sepolia
supported_tokens:
- USDC
network: testnet
protocol_family: cctp
protocol_version: v2
card_version: 3
last_updated: '2026-09-24'
---

# Protocol Knowledge: Circle CCTP V2

## Fast-Path Interfaces
- **Deposit For Burn:** `depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)`

## Learned Chain Cache (Agent Proposes, System Verifies)
| Network | Chain ID | CCTP Domain | Token Messenger Address | Status | Verified Date | Evidence |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Ethereum Sepolia | 11155111 | 0 | `0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA` | `unverified` | — | https://developers.circle.com/cctp/references/contract-addresses |
| Arbitrum Sepolia | 421614 | 3 | `0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA` | `unverified` | — | https://developers.circle.com/cctp/references/contract-addresses |

## Learned Failure Modes & Revert Fixes
