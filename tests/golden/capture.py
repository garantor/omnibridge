"""
Regenerate the golden fixtures from live chain data.

Run from the repo root:  python tests/golden/capture.py

Reads DepositForBurn logs off the CCTP router on Base, fetches each transaction, and
records the exact calldata alongside its hash and block so every fixture stays checkable
on a block explorer. Nothing here is written by hand.

Adding coverage is the reason to run this. Refreshing existing fixtures is usually the
wrong move — their value is in being stable references.
"""

import itertools
import json
import time
from pathlib import Path

from eth_abi import decode
from web3 import Web3

from omnibridge.discovery.rpc_manager import RPCManager

ROUTER = "0x1682Ae6375C4E4A97e4B583BC394c861A46D8962"
SIGNATURE = "depositForBurn(uint256,uint32,bytes32,address)"
PARAM_TYPES = ["uint256", "uint32", "bytes32", "address"]

# Circle-issued domain identifiers, unrelated to EIP-155 chain IDs.
# https://developers.circle.com/cctp/concepts/supported-chains-and-domains
DOMAINS = {
    0: "Ethereum", 1: "Avalanche", 2: "OP Mainnet", 3: "Arbitrum", 4: "Noble",
    5: "Solana", 6: "Base", 7: "Polygon PoS", 8: "Sui", 9: "Aptos", 10: "Unichain",
    11: "Linea", 14: "World Chain", 16: "Sei", 27: "Stellar",
}
PREFERRED = (0, 3, 5, 8)   # Ethereum, Arbitrum, then two non-EVM recipients
BLOCK_SPAN, WINDOW = 54_000, 9_000


def main() -> None:
    endpoints = RPCManager().discover_chain_metadata("base").rpc_urls
    pool = itertools.cycle(endpoints)

    def call(fn, tries=8):
        last = None
        for _ in range(tries):
            try:
                return fn(Web3(Web3.HTTPProvider(next(pool), request_kwargs={"timeout": 25})))
            except Exception as exc:  # public endpoints rate-limit; rotate and retry
                last = exc
                time.sleep(0.4)
        raise last

    router = Web3.to_checksum_address(ROUTER)
    selector = "0x" + Web3.keccak(text=SIGNATURE)[:4].hex()
    latest = call(lambda c: c.eth.block_number)

    code = call(lambda c: c.eth.get_code(router))
    assert len(code) > 0, "router holds no bytecode"
    assert selector[2:] in code.hex(), "selector absent from deployed bytecode"

    picked, seen = {}, set()
    for lo in range(latest - BLOCK_SPAN, latest, WINDOW):
        if set(PREFERRED) <= set(picked):
            break
        try:
            logs = call(lambda c, lo=lo: c.eth.get_logs(
                {"address": router, "fromBlock": lo, "toBlock": min(lo + WINDOW - 1, latest)}
            ))
        except Exception:
            continue
        for log in logs:
            tx_hash = log["transactionHash"].hex()
            if tx_hash in seen:
                continue
            seen.add(tx_hash)
            try:
                tx = call(lambda c, h=tx_hash: c.eth.get_transaction(h))
            except Exception:
                continue
            data = tx["input"].hex() if hasattr(tx["input"], "hex") else str(tx["input"])
            data = data if data.startswith("0x") else "0x" + data
            if not data.startswith(selector):
                continue
            amount, domain, recipient, burn_token = decode(
                PARAM_TYPES, bytes.fromhex(data[10:])
            )
            if domain in picked:
                continue
            picked[domain] = {
                "tx_hash": tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash,
                "block": log["blockNumber"],
                "source_chain": "Base",
                "source_chain_id": 8453,
                "dest_domain": domain,
                "dest_chain": DOMAINS.get(domain, f"CCTP domain {domain}"),
                "router_address": router,
                "function_signature": SIGNATURE,
                "selector": selector,
                "param_types": PARAM_TYPES,
                "param_values": [str(amount), domain, "0x" + recipient.hex(), burn_token],
                "amount_raw": str(amount),
                "amount_human": amount / 1e6,
                "token": "USDC",
                "decimals": 6,
                "expected_calldata": data,
            }
            print(f"  captured domain {domain} ({DOMAINS.get(domain, '?')})")

    order = [d for d in PREFERRED if d in picked] + [d for d in picked if d not in PREFERRED]
    fixtures = [picked[d] for d in order][:4]
    out = Path(__file__).parent / "cctp_base.json"
    out.write_text(json.dumps(fixtures, indent=2) + "\n")
    print(f"\nwrote {len(fixtures)} fixtures to {out}")


if __name__ == "__main__":
    main()
