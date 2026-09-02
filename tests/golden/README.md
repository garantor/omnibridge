# Golden route fixtures

Real, confirmed mainnet transactions, captured from chain data rather than written by hand.

These are the only hardcoded chain values in the repository, and they are deliberate. A
fixture's whole worth is being a fixed reference the implementation cannot argue with —
the opposite of a production lookup table, which exists to be consulted and therefore to
rot. See Rule B in `docs/IMPLEMENTATION_PLAN.md`.

## Provenance

Every field was verified against the chain, not taken from documentation or memory:

- **Router** `0x1682Ae6375C4E4A97e4B583BC394c861A46D8962` (Base). Confirmed with `eth_getCode`
  to be a contract (13,497 bytes), and confirmed to contain the 4-byte selector `0x6fd3504e`
  in its deployed bytecode.
- **Selector** `0x6fd3504e` = `keccak("depositForBurn(uint256,uint32,bytes32,address)")[:4]`,
  computed, not looked up.
- **Calldata** taken verbatim from confirmed transactions found by reading `DepositForBurn`
  logs off the router and fetching each transaction's input. Each fixture records its
  transaction hash and block, so any claim here can be checked on a block explorer.
- **Destination domains** cross-referenced against Circle's own table at
  <https://developers.circle.com/cctp/concepts/supported-chains-and-domains>. Domains are
  Circle-issued identifiers and bear no relation to EIP-155 chain IDs — domain 5 is Solana,
  domain 6 is Base, domain 27 is Stellar.

Two fixtures carry a `mintRecipient` that is a genuine 32-byte non-EVM key (Solana, Sui)
rather than a left-padded EVM address. That is deliberate coverage: it is the case where an
encoder that assumes 20-byte addresses silently produces the wrong recipient.

## What they are for

`tests/test_golden_routes.py` asserts that `encode_dynamic_calldata` — the generic encoder
the agent will drive in Phase 3 — reproduces these bytes exactly. This is what makes
Phase 4's deletion of the per-protocol template chain provable rather than hopeful: both
paths are held against the same real transactions, so removing one cannot change behaviour
undetected.

## Files

- `cctp_base.json` — four CCTP `depositForBurn` transfers from Base: to Ethereum, Arbitrum,
  Solana and Sui. The last two carry genuine 32-byte non-EVM `mintRecipient` keys.
- `across_base.json` — one Across V3 `depositV3` transfer, Base to Ethereum, through the
  SpokePool at `0x09aea4b2242abC8bb4BB78D537A67a245A7bEC64`.

Two things the Across fixture records that the CCTP ones do not:

- **`router_is_proxy`.** The SpokePool holds ~680 bytes of bytecode and the `depositV3`
  selector is *absent* from it; the EIP-1967 implementation at
  `0xf23C6C04A2B88e8651FE99bbDccbB5C9D306e6B0` (23,048 bytes) has it. A provenance check
  that reads only the address's own code returns a false negative on every proxied router.
- **`calldata_suffix`.** The on-chain calldata ends in nine bytes (`0x1dc0de00410000000c`)
  appended by the submitting integrator as an attribution tag. It is not part of the ABI
  encoding and is not derivable from the parameters, so the encoder is right to omit it
  and the test accounts for it rather than treating correct behaviour as a mismatch.

## Regenerating

`python tests/golden/capture.py` re-reads recent CCTP router logs and rewrites
`cctp_base.json`. Run it only to add coverage; existing fixtures should not be refreshed
casually, since their value comes from being stable references.
