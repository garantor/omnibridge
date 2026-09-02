"""
Autonomous Chain & RPC Discovery Manager.
Queries live chain registries for EVM and invokes live web search for non-EVM chains (Stellar, Solana, etc.).
Zero synthetic fake URLs.
"""

import asyncio
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
import aiohttp
import requests
from omnibridge.core.models import ChainInfo

CHAIN_REGISTRY_URL = "https://chainid.network/chains.json"

# Category words the registry uses to describe itself. This is not an enumeration of
# networks — no "sepolia"/"goerli"/"holesky" list to maintain — so a testnet launching
# tomorrow is classified correctly with no code change.
_TEST_NETWORK_WORDS = ("testnet", "devnet", "test network")

# Below this, the resolved chain's canonical name is too far from what was asked for
# to be the same thing. "solana" against "Neon EVM Mainnet" fails here without the
# code knowing what Solana is.
_MIN_NAME_SIMILARITY = 0.6


class ChainResolutionError(Exception):
    """Base class for a chain name that could not be resolved safely."""


class UnresolvedChainError(ChainResolutionError):
    """The registry does not know this chain. Not the same as 'it is non-EVM'."""


class AmbiguousChainError(ChainResolutionError):
    """Several chains matched. Refusing to pick one."""


def _self_describes_as_test_network(entry: Dict[str, Any]) -> bool:
    """True when the registry's own name/title calls this a test network."""
    blob = f"{entry.get('name', '')} {entry.get('title', '')}".lower()
    return any(word in blob for word in _TEST_NETWORK_WORDS)


def _network_family(entry: Dict[str, Any], by_id: Dict[int, Dict[str, Any]]) -> str:
    """
    Classify an entry as 'mainnet' or 'testnet' using only registry data.

    An L2 inherits its parent's family, which is how Arbitrum Sepolia (whose parent
    is Ethereum Sepolia) is classified without naming either of them.
    """
    if _self_describes_as_test_network(entry):
        return "testnet"

    parent_ref = (entry.get("parent") or {}).get("chain", "")
    if isinstance(parent_ref, str) and parent_ref.startswith("eip155-"):
        try:
            parent_id = int(parent_ref.split("-", 1)[1])
        except (ValueError, IndexError):
            return "mainnet"
        parent = by_id.get(parent_id)
        if parent is not None and _self_describes_as_test_network(parent):
            return "testnet"

    return "mainnet"


def name_similarity(requested: str, resolved_name: str) -> float:
    """
    How plausibly `resolved_name` is what the user meant by `requested`.

    A whole-word appearance counts as a match ("arbitrum" in "Arbitrum One"), which
    is why a legitimate suffix does not get penalised while an unrelated name does.
    """
    a = (requested or "").lower().strip()
    b = (resolved_name or "").lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b.split():
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class RPCManager:
    """Dynamically discovers, queries, and benchmarks RPC endpoints for ANY network on the fly."""

    def __init__(self):
        self.dynamic_chains: Dict[str, ChainInfo] = {}
        self._registry_cache: Optional[List[Dict[str, Any]]] = None

    def register_discovered_chain(self, chain_info: ChainInfo) -> None:
        """Registers dynamically discovered chain metadata into the runtime session cache."""
        self.dynamic_chains[chain_info.name.lower()] = chain_info

    def get_chain_info(self, chain_name: str) -> Optional[ChainInfo]:
        """Looks up dynamically discovered or cached chain info."""
        return self.dynamic_chains.get(chain_name.lower().strip())

    def _fetch_registry(self) -> List[Dict[str, Any]]:
        """Fetch the EVM chain registry once per manager instance."""
        if self._registry_cache is None:
            resp = requests.get(CHAIN_REGISTRY_URL, timeout=10.0)
            resp.raise_for_status()
            self._registry_cache = resp.json()
        return self._registry_cache

    def discover_chain_metadata(self, chain_name: str, network: str = "mainnet") -> ChainInfo:
        """
        Resolve a chain name to verified metadata, or refuse.

        Never guesses. Several matches raise AmbiguousChainError; an unknown name raises
        UnresolvedChainError, which means exactly that and *not* "this chain is non-EVM".

        The previous implementation fell through to a web search and, on any hit,
        constructed ChainInfo(chain_id=1) — Ethereum mainnet's id — for whatever chain
        had been asked for. That path is gone: RPC URLs scraped from search text cannot
        establish a chain identity, and inventing one puts a wrong chainId into a signed
        transaction. Resolving a chain the registry does not carry is discovery work
        (plan Phase 3), not something to guess at here.
        """
        key = (chain_name or "").lower().strip()
        if not key:
            raise UnresolvedChainError("No chain name was supplied.")

        cache_key = f"{network}:{key}"
        if cache_key in self.dynamic_chains:
            return self.dynamic_chains[cache_key]

        try:
            chains = self._fetch_registry()
        except Exception as exc:
            raise UnresolvedChainError(
                f"Chain registry at {CHAIN_REGISTRY_URL} is unreachable ({exc}); "
                f"cannot resolve '{chain_name}'."
            ) from exc

        entry = self._match_registry_entry(key, chains, network)
        info = self._to_chain_info(entry)
        self.dynamic_chains[cache_key] = info
        self.dynamic_chains[info.name.lower()] = info
        return info

    def _match_registry_entry(
        self, key: str, chains: List[Dict[str, Any]], network: str
    ) -> Dict[str, Any]:
        by_id = {c["chainId"]: c for c in chains if isinstance(c.get("chainId"), int)}

        # A chain ID beats a name, always. 137 is unambiguous in a way "Polygon" is not.
        if key.isdigit():
            entry = by_id.get(int(key))
            if entry is None:
                raise UnresolvedChainError(f"No chain with id {key} in the registry.")
            return entry

        candidates = [
            c
            for c in chains
            if c.get("status") != "deprecated" and _network_family(c, by_id) == network
        ]

        def pick(field_label: str, hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                listed = ", ".join(f"{h.get('name')} ({h.get('chainId')})" for h in hits[:6])
                raise AmbiguousChainError(
                    f"'{key}' matches {len(hits)} {network} chains on {field_label}: {listed}. "
                    "Refusing to guess — supply a numeric chain ID instead."
                )
            return None

        # Ranked by field, not by list order. `chain` is a family label rather than an
        # identity — four Neon EVM networks declare chain == "Solana" — so it ranks last.
        for field in ("name", "shortName"):
            hit = pick(field, [c for c in candidates if str(c.get(field, "")).lower().strip() == key])
            if hit is not None:
                return self._guard_name(key, hit)

        suffixed = [
            c
            for c in candidates
            if str(c.get("name", "")).lower().strip()
            in (f"{key} mainnet", f"{key} one", f"{key} network", f"{key}-mainnet")
        ]
        hit = pick("name", suffixed)
        if hit is not None:
            return self._guard_name(key, hit)

        hit = pick("chain", [c for c in candidates if str(c.get("chain", "")).lower().strip() == key])
        if hit is not None:
            return self._guard_name(key, hit)

        prefixed = [
            c for c in candidates if str(c.get("name", "")).lower().strip().startswith(f"{key} ")
        ]
        hit = pick("name prefix", prefixed)
        if hit is not None:
            return self._guard_name(key, hit)

        raise UnresolvedChainError(
            f"'{key}' is not a {network} chain in the EVM registry. That means this source "
            "does not know it — not that it is non-EVM. Resolving it is discovery work."
        )

    @staticmethod
    def _guard_name(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        """
        Refuse a match whose canonical name is not plausibly what was asked for.

        This is the check that catches 'solana' resolving to a Neon EVM network, and it
        does so without the code knowing what Solana is.
        """
        resolved = str(entry.get("name", ""))
        score = name_similarity(key, resolved)
        if score < _MIN_NAME_SIMILARITY:
            raise UnresolvedChainError(
                f"Refusing to resolve '{key}' to '{resolved}' (chain {entry.get('chainId')}): "
                f"name similarity {score:.2f} is below {_MIN_NAME_SIMILARITY}. "
                "These are almost certainly different chains."
            )
        return entry

    @staticmethod
    def _to_chain_info(entry: Dict[str, Any]) -> ChainInfo:
        rpcs = [
            u
            for u in entry.get("rpc", [])
            if isinstance(u, str) and u.startswith("http") and "${" not in u
        ]
        explorers = entry.get("explorers") or []
        explorer = explorers[0].get("url", "") if explorers else ""
        return ChainInfo(
            chain_id=int(entry["chainId"]),
            name=str(entry.get("name", "")),
            native_symbol=(entry.get("nativeCurrency") or {}).get("symbol", "ETH"),
            # Empty rather than a fabricated f"https://{key}scan.io".
            block_explorer=explorer,
            rpc_urls=rpcs[:8],
        )

    async def _probe_rpc(self, session: aiohttp.ClientSession, rpc_url: str) -> Tuple[str, float, bool]:
        """Probes an RPC endpoint for latency and availability."""
        start_time = time.perf_counter()
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1,
        }
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
            return ""

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
        except Exception as e:
            chain = self.discover_chain_metadata(chain_name)
            if chain.rpc_urls:
                return chain.rpc_urls[0]
            raise ConnectionError(f"RPC Failure: Could not establish connection to '{chain_name}' RPC: {e}")
