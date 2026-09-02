"""
Skill-cache route lookup.

This module answers one question: has this exact route already been discovered,
simulated and approved? If so it replays it. If not it says so, and discovery is
someone else's job — the agent's (omnibridge.agent).

It used to also attempt discovery itself, by regex-scraping hex strings out of
search-result text and returning the first `0x…` it found as the router contract.
That is gone: an address that appears near the words "bridge" and "router" on a
web page is not a verified router, and the function had no way to identify the
protocol, its ABI or its fees, so it filled those in as literal unknowns that then
flowed into calldata assembly.
"""

from typing import Optional
from omnibridge.core.models import UserIntent, RouteQuote
from omnibridge.skills.manager import SkillManager
from omnibridge.discovery.rpc_manager import RPCManager


class NoCachedRoute(Exception):
    """No verified skill covers this route. Not a failure — an input to discovery."""


class RouteFinder:
    """Resolves a route from verified procedural memory, or reports that it cannot."""

    def __init__(self, skill_manager: Optional[SkillManager] = None, rpc_manager: Optional[RPCManager] = None):
        self.skill_manager = skill_manager or SkillManager()
        self.rpc_manager = rpc_manager or RPCManager()

    def discover_route(self, intent: UserIntent) -> RouteQuote:
        """
        Replay the cached route for this intent, or raise NoCachedRoute.

        Chain metadata is still resolved for both legs, because a skill recorded under
        one name must not be replayed for a request that resolves somewhere else.
        """
        src_name = intent.source_chain.lower().strip()
        dst_name = intent.dest_chain.lower().strip()
        token_sym = intent.token.upper().strip()

        if not src_name or not dst_name:
            raise ValueError(
                "Intent Error: Source chain and destination chain must be provided. "
                f"Got: from '{src_name}' to '{dst_name}'"
            )

        # The intent's network family is carried through so a mainnet request can never
        # resolve onto a testnet.
        src_chain = self.rpc_manager.discover_chain_metadata(src_name, network=intent.network)

        cached_skill = self.skill_manager.find_skill(
            src_name, dst_name, token_sym, network=intent.network
        )
        if cached_skill is None:
            raise NoCachedRoute(
                f"No verified skill covers {token_sym} from {src_name} to {dst_name} on "
                f"{intent.network}. This route has to be discovered."
            )

        meta = cached_skill.metadata
        if meta.source_chain_id != src_chain.chain_id:
            raise NoCachedRoute(
                f"Skill {meta.name!r} was verified on chain {meta.source_chain_id}, but "
                f"'{src_name}' now resolves to {src_chain.chain_id}. Refusing to replay it."
            )

        decimals = 6 if token_sym in ("USDC", "USDT") else 18
        raw_amount = int(intent.amount * (10 ** decimals))

        return RouteQuote(
            bridge_name=f"{meta.bridge_protocol} (Verified Skill)",
            source_chain_id=meta.source_chain_id,
            # A destination without an EIP-155 id keeps the source id rather than a
            # fabricated one; dest_chain_ref carries the real identity.
            dest_chain_id=meta.dest_chain_id if meta.dest_chain_id is not None else src_chain.chain_id,
            source_token_address=meta.source_token_address or "",
            dest_token_address=meta.dest_token_address or "",
            router_address=meta.router_address,
            deposit_amount_raw=raw_amount,
            estimated_output_raw=raw_amount,
            decimals=decimals,
            # Not measured on a cache hit. Zero here means "not measured"; the previous
            # 0.05 / 190000 pair were invented numbers that reached the manifest as facts.
            fee_amount_usd=0.0,
            calldata_template="skill_verified",
            extra_params={
                "skill_name": meta.name,
                "skill_path": cached_skill.file_path,
                "function_signature": meta.function_signature,
                "param_types": list(meta.param_types),
                "param_values": list(meta.param_values),
                "spend": meta.spend,
                "dest_chain_ref": meta.dest_chain_ref,
                "destination_domain": meta.cctp_destination_domain,
            },
        )
