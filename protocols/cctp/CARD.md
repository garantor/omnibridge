---
protocol_id: cctp
protocol_name: Circle CCTP
docs:
- https://developers.circle.com/stablecoins/cctp-getting-started
- https://developers.circle.com/llms.txt
- https://developers.circle.com/cctp-v1/docs/evm-smart-contracts
supported_chains:
- ethereum
- arbitrum
- base
supported_tokens:
- USDC
network: mainnet
protocol_family: cctp
protocol_version: v1
card_version: 2
last_updated: '2026-09-24'
---

# Protocol Knowledge: Circle CCTP

## Fast-Path Interfaces
- **Deposit For Burn:** `depositForBurn(uint256,uint32,bytes32,address)`

## Learned Chain Cache (Agent Proposes, System Verifies)
| Network | Chain ID | CCTP Domain | Token Messenger Address | Status | Verified Date | Evidence |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Arbitrum One | 42161 | 3 | `0x19330d10D9Cc8751218eaf51E8885D058642E08A` | `verified` | 2026-09-11 | `0x14f1f5bf7306df19e600411550bfc30f14a3d80e84553549e986d1fc0ba9661e` |
| Base | 8453 | 6 (confirmed) | `0x1682Ae6375C4E4A97e4B583BC394c861A46D8962` | `unverified` | — | https://developers.circle.com/cctp-v1/docs/evm-smart-contracts |
| Ethereum | 1 | 0 | `0xBd3fa81B58Ba92a82136038B25aDec7066af3155` | `unverified` | — | https://developers.circle.com/cctp-v1/docs/evm-smart-contracts |

## Learned Failure Modes & Revert Fixes
- *Revert Reason:* `No TokenMessenger for domain`.
- *Fix:* The destination is a CCTP domain, not an EIP-155 chain id: Base is `6`, not `8453`. Take it from the chain cache.
