"""
High-Speed Asynchronous Public RPC Manager & Benchmark Engine.
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
import aiohttp
from omnibridge.core.models import ChainInfo

# Default public RPC registry with multiple redundant fallbacks
DEFAULT_CHAINS: Dict[str, ChainInfo] = {
    "base": ChainInfo(
        chain_id=8453,
        name="Base",
        native_symbol="ETH",
        block_explorer="https://basescan.org",
        cctp_domain=6,
        rpc_urls=[
            "https://mainnet.base.org",
            "https://base-rpc.publicnode.com",
            "https://1rpc.io/base",
            "https://base.meowrpc.com",
            "https://base.drpc.org",
        ],
    ),
    "arbitrum": ChainInfo(
        chain_id=42161,
        name="Arbitrum One",
        native_symbol="ETH",
        block_explorer="https://arbiscan.io",
        cctp_domain=3,
        rpc_urls=[
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum-one-rpc.publicnode.com",
            "https://1rpc.io/arb",
            "https://arbitrum.meowrpc.com",
            "https://arbitrum.drpc.org",
        ],
    ),
    "ethereum": ChainInfo(
        chain_id=1,
        name="Ethereum Mainnet",
        native_symbol="ETH",
        block_explorer="https://etherscan.io",
        cctp_domain=0,
        rpc_urls=[
            "https://eth.llamarpc.com",
            "https://ethereum-rpc.publicnode.com",
            "https://1rpc.io/eth",
            "https://eth.meowrpc.com",
            "https://rpc.ankr.com/eth",
        ],
    ),
    "optimism": ChainInfo(
        chain_id=10,
        name="Optimism (OP Mainnet)",
        native_symbol="ETH",
        block_explorer="https://optimistic.etherscan.io",
        cctp_domain=2,
        rpc_urls=[
            "https://mainnet.optimism.io",
            "https://optimism-rpc.publicnode.com",
            "https://1rpc.io/op",
            "https://optimism.drpc.org",
        ],
    ),
    "polygon": ChainInfo(
        chain_id=137,
        name="Polygon PoS",
        native_symbol="POL",
        block_explorer="https://polygonscan.com",
        cctp_domain=7,
        rpc_urls=[
            "https://polygon-rpc.com",
            "https://polygon-bor-rpc.publicnode.com",
            "https://1rpc.io/matic",
        ],
    ),
    "hyperliquid": ChainInfo(
        chain_id=999,
        name="Hyperliquid L1",
        native_symbol="HYPE",
        block_explorer="https://hyperscan.xyz",
        rpc_urls=[
            "https://api.hyperliquid.xyz/evm",
            "https://rpc.hyperliquid.xyz/evm",
        ],
    ),
}


class RPCManager:
    """Manages RPC selection, health testing, and high-speed concurrent racing."""

    def __init__(self, chains: Optional[Dict[str, ChainInfo]] = None):
        self.chains = chains or DEFAULT_CHAINS

    def get_chain_info(self, chain_name: str) -> Optional[ChainInfo]:
        key = chain_name.lower().strip()
        return self.chains.get(key)

    async def _probe_rpc(
        self, session: aiohttp.ClientSession, rpc_url: str, expected_chain_id: Optional[int] = None
    ) -> Tuple[str, float, bool]:
        """Probes a single RPC endpoint with a lightweight JSON-RPC call and measures latency."""
        payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
        start_time = time.perf_counter()
        try:
            async with session.post(
                rpc_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                    if "result" in data:
                        return (rpc_url, elapsed_ms, True)
        except Exception:
            pass
        return (rpc_url, 9999.0, False)

    async def race_rpcs(self, chain_name: str) -> str:
        """Concurrently races all known public RPCs for a chain and returns the fastest active endpoint."""
        chain = self.get_chain_info(chain_name)
        if not chain:
            raise ValueError(f"Unknown chain '{chain_name}'. Please specify custom RPC.")

        urls = chain.rpc_urls
        if not urls:
            raise ValueError(f"No RPC URLs configured for chain '{chain_name}'")

        async with aiohttp.ClientSession() as session:
            tasks = [self._probe_rpc(session, url, chain.chain_id) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_results = []
        for r in results:
            if isinstance(r, tuple) and r[2]:  # url, latency, success
                valid_results.append(r)

        if not valid_results:
            # Fallback to first configured URL if all race tests failed/timed out
            return urls[0]

        valid_results.sort(key=lambda x: x[1])
        return valid_results[0][0]

    def get_fastest_rpc_sync(self, chain_name: str) -> str:
        """Synchronous wrapper around race_rpcs."""
        try:
            return asyncio.run(self.race_rpcs(chain_name))
        except Exception:
            chain = self.get_chain_info(chain_name)
            return chain.rpc_urls[0] if chain and chain.rpc_urls else ""
