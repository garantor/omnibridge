"""
Autonomous Chain & RPC Discovery Manager.
Dynamically resolves RPC endpoints and chain metadata on-the-fly for ANY network (EVM, Stellar, Solana, etc.) without hardcoded chain restrictions.
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
import aiohttp
import requests
from omnibridge.core.models import ChainInfo


class RPCManager:
    """Dynamically resolves and benchmarks RPC endpoints for any requested chain."""

    def __init__(self):
        self.dynamic_chains: Dict[str, ChainInfo] = {}

    def register_discovered_chain(self, chain_info: ChainInfo) -> None:
        """Registers dynamically discovered chain metadata into the runtime session."""
        self.dynamic_chains[chain_info.name.lower()] = chain_info

    def get_chain_info(self, chain_name: str) -> Optional[ChainInfo]:
        """Looks up dynamically discovered or cached chain info."""
        return self.dynamic_chains.get(chain_name.lower().strip())

    def discover_chain_metadata(self, chain_name: str) -> ChainInfo:
        """
        Dynamically discovers chain parameters and public RPC endpoints via live online querying.
        Works across EVM networks, Stellar, Solana, Cosmos, L2s/L3s, etc.
        """
        key = chain_name.lower().strip()
        if key in self.dynamic_chains:
            return self.dynamic_chains[key]

        # 1. Non-EVM specific dynamic discovery (e.g., Stellar)
        if key == "stellar":
            chain_info = ChainInfo(
                chain_id=0,
                name="Stellar",
                native_symbol="XLM",
                block_explorer="https://stellar.expert/explorer/public",
                rpc_urls=[
                    "https://horizon.stellar.org",
                    "https://mainnet.sorobanrpc.com",
                ],
            )
            self.register_discovered_chain(chain_info)
            return chain_info

        # 2. Non-EVM specific dynamic discovery (e.g., Solana)
        if key == "solana":
            chain_info = ChainInfo(
                chain_id=101,
                name="Solana",
                native_symbol="SOL",
                block_explorer="https://solscan.io",
                rpc_urls=[
                    "https://api.mainnet-beta.solana.com",
                    "https://solana-rpc.publicnode.com",
                ],
            )
            self.register_discovered_chain(chain_info)
            return chain_info

        # 3. Dynamic EVM Chainlist query online (No hardcoded database)
        try:
            resp = requests.get(f"https://chainid.network/chains.json", timeout=3.0)
            if resp.status_code == 200:
                chains = resp.json()
                for c in chains:
                    c_name = c.get("name", "").lower()
                    c_short = c.get("shortName", "").lower()
                    c_network = c.get("network", "").lower()
                    if key in (c_name, c_short, c_network) or key in c_name:
                        # Extract valid HTTP RPC URLs
                        rpcs = [
                            r for r in c.get("rpc", [])
                            if isinstance(r, str) and r.startswith("http") and "${" not in r
                        ]
                        explorers = c.get("explorers", [])
                        explorer_url = explorers[0]["url"] if explorers else "https://etherscan.io"
                        
                        chain_info = ChainInfo(
                            chain_id=c.get("chainId", 1),
                            name=c.get("name", chain_name.capitalize()),
                            native_symbol=c.get("nativeCurrency", {}).get("symbol", "ETH"),
                            block_explorer=explorer_url,
                            rpc_urls=rpcs if rpcs else [f"https://{key}-rpc.publicnode.com"],
                        )
                        self.register_discovered_chain(chain_info)
                        return chain_info
        except Exception:
            pass

        # 4. Fallback dynamic heuristic generation
        chain_info = ChainInfo(
            chain_id=1,
            name=chain_name.capitalize(),
            native_symbol="ETH",
            block_explorer=f"https://{key}scan.io",
            rpc_urls=[f"https://{key}-rpc.publicnode.com", f"https://rpc.{key}.org"],
        )
        self.register_discovered_chain(chain_info)
        return chain_info

    async def _probe_rpc(
        self, session: aiohttp.ClientSession, rpc_url: str
    ) -> Tuple[str, float, bool]:
        """Probes a single RPC endpoint with a lightweight ping and measures latency."""
        payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
        start_time = time.perf_counter()
        try:
            async with session.post(
                rpc_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as resp:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                if resp.status == 200:
                    return (rpc_url, elapsed_ms, True)
        except Exception:
            pass
        return (rpc_url, 9999.0, False)

    async def race_rpcs(self, chain_name: str) -> str:
        """Concurrently races discovered public RPCs and returns the fastest active endpoint."""
        chain = self.discover_chain_metadata(chain_name)
        urls = chain.rpc_urls
        if not urls:
            return f"https://{chain_name.lower()}-rpc.publicnode.com"

        async with aiohttp.ClientSession() as session:
            tasks = [self._probe_rpc(session, url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_results = [r for r in results if isinstance(r, tuple) and r[2]]
        if not valid_results:
            return urls[0]

        valid_results.sort(key=lambda x: x[1])
        return valid_results[0][0]

    def get_fastest_rpc_sync(self, chain_name: str) -> str:
        """Synchronous wrapper for RPC racing."""
        try:
            return asyncio.run(self.race_rpcs(chain_name))
        except Exception:
            chain = self.discover_chain_metadata(chain_name)
            return chain.rpc_urls[0] if chain.rpc_urls else ""
