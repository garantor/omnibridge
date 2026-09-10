"""
Resolving a token symbol to a contract address, from live public registries.

Why this is allowed to exist
----------------------------
Rule B rules out a curated route table baked into this repo — a dict of "USDC on Base
is 0x8335…" that ages badly and that nobody re-verifies. It does not rule out asking a
live public registry, which is exactly what `RPCManager` already does for chains via
`chainid.network/chains.json`. This module is that same pattern for tokens.

What the answer may and may not be used for
-------------------------------------------
**Read-only.** An address resolved here is used to call `balanceOf` and nothing else.
It never reaches calldata, never becomes an approval's `spender` or `token`, and is
never stored on a route. The token that actually gets approved comes from the model's
`RouteProposal.spend.token`, which `planner.build_plan` checks against the chain.

That boundary is what makes a third-party token list an acceptable dependency. The
worst a wrong entry can do is make an early funding check read the wrong balance —
which is why a mismatch downgrades the check to UNCHECKED rather than refusing, and
why `balance.check_token` corroborates every candidate by asking the contract for its
own `symbol()` before believing it.

Ambiguity is refused, not guessed. Two different addresses claiming the same symbol on
the same chain is precisely the case where picking one is worse than saying so.
"""

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

#: Public token lists, tried in order. Both follow the tokenlists.org schema.
#: Overridable with OMNIBRIDGE_TOKEN_LISTS for testing or for an internal mirror.
DEFAULT_TOKEN_LISTS = [
    "https://ipfs.io/ipns/tokens.uniswap.org",
    "https://static.optimism.io/optimism.tokenlist.json",
]

_FETCH_TIMEOUT = 12


class TokenResolutionError(Exception):
    """Base for "this symbol did not resolve", which is never "you hold none of it"."""


class UnknownToken(TokenResolutionError):
    """No list carries this symbol on this chain. Says nothing about the balance."""


class AmbiguousToken(TokenResolutionError):
    """Several different contracts claim this symbol on this chain. Refusing to pick."""


@dataclass(frozen=True)
class TokenRef:
    """A candidate address for a symbol. Not yet corroborated against the chain."""

    address: str
    symbol: str
    chain_id: int
    #: The list's claim. `balance.check_token` prefers the contract's own answer.
    decimals: int
    #: Which list said so, so a surprising answer can be traced to its source.
    source: str


def token_list_urls() -> List[str]:
    override = os.environ.get("OMNIBRIDGE_TOKEN_LISTS")
    if override:
        return [u.strip() for u in override.split(",") if u.strip()]
    return list(DEFAULT_TOKEN_LISTS)


class TokenRegistry:
    """
    Symbol to address, from public lists, cached for the life of the process.

    Cached because the funding gate runs on every request and these lists are ~1 MB.
    Not cached across processes: a stale address on disk is the curated table this
    module exists to avoid, just with extra steps.
    """

    def __init__(self, urls: Optional[List[str]] = None):
        self._urls = urls if urls is not None else token_list_urls()
        self._lists: Dict[str, List[Dict[str, Any]]] = {}
        self._failed: Dict[str, str] = {}
        self._lock = threading.Lock()

    def _fetch(self, url: str) -> List[Dict[str, Any]]:
        """One list's tokens, or [] with the reason recorded."""
        if url in self._lists:
            return self._lists[url]
        if url in self._failed:
            return []
        try:
            response = requests.get(url, timeout=_FETCH_TIMEOUT)
            response.raise_for_status()
            tokens = response.json().get("tokens") or []
            if not isinstance(tokens, list):
                raise ValueError("list has no 'tokens' array")
        except Exception as exc:
            self._failed[url] = str(exc)
            return []
        self._lists[url] = tokens
        return tokens

    @property
    def failures(self) -> Dict[str, str]:
        """Lists that could not be fetched, and why. Rendered rather than swallowed."""
        return dict(self._failed)

    def resolve(self, chain_id: int, symbol: str) -> TokenRef:
        """
        The contract that calls itself `symbol` on `chain_id`.

        Raises UnknownToken when no list carries it and AmbiguousToken when they
        disagree within a single list. Both mean "not established" — never "absent".
        """
        wanted = (symbol or "").strip().upper()
        if not wanted:
            raise UnknownToken("No token symbol was supplied.")

        with self._lock:
            for url in self._urls:
                hits = [
                    t
                    for t in self._fetch(url)
                    if int(t.get("chainId", -1)) == int(chain_id)
                    and str(t.get("symbol", "")).strip().upper() == wanted
                ]
                if not hits:
                    continue

                addresses = {str(t["address"]).lower() for t in hits}
                if len(addresses) > 1:
                    raise AmbiguousToken(
                        f"{wanted} on chain {chain_id} resolves to {len(addresses)} "
                        f"different contracts in {url}: {', '.join(sorted(addresses))}. "
                        "Refusing to pick one — a balance read against the wrong "
                        "contract is worse than no balance read."
                    )

                hit = hits[0]
                return TokenRef(
                    address=str(hit["address"]),
                    symbol=str(hit.get("symbol", wanted)),
                    chain_id=int(chain_id),
                    decimals=int(hit.get("decimals", 18)),
                    source=url,
                )

        detail = ""
        if self._failed:
            detail = " (" + "; ".join(f"{u}: {e}" for u, e in self._failed.items()) + ")"
        raise UnknownToken(
            f"No public token list carries {wanted} on chain {chain_id}{detail}. "
            "That is a gap in the lists, not evidence about the balance."
        )
