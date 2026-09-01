"""
High-Velocity Route Discovery Engine.
Implements multi-tiered route resolution (Skill Cache -> Verified Native Protocols -> JIT Live Discovery).
"""

from typing import Optional
from omnibridge.core.models import UserIntent, RouteQuote
from omnibridge.skills.manager import SkillManager
from omnibridge.discovery.rpc_manager import RPCManager

# Standard verified router endpoints and contract registries for high-speed fallback
VERIFIED_PROTOCOLS = {
    # Circle CCTP (Cross-Chain Transfer Protocol)
    "cctp": {
        "name": "Circle CCTP (TokenMessenger)",
        "routers": {
            8453: "0x12ade2554a39A27dd2465376F4302cCc8004ce38",   # Base TokenMessenger
            42161: "0x19330d10D9CcC875121528212165944645c946DD",  # Arbitrum TokenMessenger
            1: "0xbd3fa81b58ba92d82136038b25adec7066af3155",      # Ethereum TokenMessenger
            10: "0x2B4069517957735bE00ceE0fadAE88a26365528f",     # Optimism TokenMessenger
            137: "0x9daF8c91AEFAE50b9c0E69629D3F6Ca40cA3B3FE",    # Polygon TokenMessenger
        },
        "usdc": {
            8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Base Native USDC
            42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # Arbitrum Native USDC
            1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",      # Ethereum USDC
            10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",     # Optimism USDC
            137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",    # Polygon USDC
        },
        "domains": {
            1: 0,       # Ethereum
            10: 2,      # Optimism
            42161: 3,   # Arbitrum
            8453: 6,    # Base
            137: 7,     # Polygon
        },
    },
    # Hyperliquid Bridge (Deposit Contract on Arbitrum / Base bridge relay)
    "hyperliquid": {
        "name": "Hyperliquid Native Bridge",
        "routers": {
            42161: "0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7",  # Arbitrum Deposit Vault
        },
        "usdc": {
            42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        },
    },
    # Across Protocol V3
    "across": {
        "name": "Across Protocol V3",
        "routers": {
            8453: "0x09aea4b2242abC8bb4BB78D537A67a245A7bEC64",   # Base SpokePool
            42161: "0xe35e9842fceaCA96570B734083f4a58e8F7C5f2A",  # Arbitrum SpokePool
            1: "0x5c7BCd6E7De5423a257D81B442095A1a6ced35C5",      # Ethereum SpokePool
            10: "0x6f26Bf09B1C792e3228e5467807a900A503c0281",     # Optimism SpokePool
        },
    },
}


class RouteFinder:
    """Discovers and constructs optimal execution routes for cross-chain intents."""

    def __init__(self, skill_manager: Optional[SkillManager] = None, rpc_manager: Optional[RPCManager] = None):
        self.skill_manager = skill_manager or SkillManager()
        self.rpc_manager = rpc_manager or RPCManager()

    def discover_route(self, intent: UserIntent) -> RouteQuote:
        """Resolves the best route via Skill Cache or Dynamic Protocol Resolution."""
        src_chain = self.rpc_manager.get_chain_info(intent.source_chain)
        dst_chain = self.rpc_manager.get_chain_info(intent.dest_chain)

        if not src_chain:
            raise ValueError(f"Unsupported source chain: {intent.source_chain}")
        if not dst_chain:
            raise ValueError(f"Unsupported destination chain: {intent.dest_chain}")

        raw_amount = int(intent.amount * (10 ** 6))  # Default 6 decimals for USDC/USDT

        # Check Skill Cache (Tier 0)
        cached_skill = self.skill_manager.find_skill(intent.source_chain, intent.dest_chain, intent.token)
        if cached_skill:
            meta = cached_skill.metadata
            return RouteQuote(
                bridge_name=f"{meta.bridge_protocol.upper()} (Skill Cache)",
                source_chain_id=meta.source_chain_id,
                dest_chain_id=meta.dest_chain_id,
                source_token_address=meta.source_token_address or "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                dest_token_address=meta.dest_token_address or "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                router_address=meta.router_address,
                deposit_amount_raw=raw_amount,
                estimated_output_raw=raw_amount,
                decimals=6,
                fee_amount_usd=0.05,
                gas_estimate_units=180000,
                calldata_template="skill_verified",
                extra_params={"skill_name": meta.name, "cctp_domain": meta.cctp_destination_domain},
            )

        # Dynamic Route Resolution (Tier 1-4)
        token_sym = intent.token.upper()

        # Route Case 1: Base to Hyperliquid
        if (src_chain.name.lower() == "base" and dst_chain.name.lower().startswith("hyperliquid")) and token_sym == "USDC":
            # Direct/Across to Arbitrum -> Hyperliquid deposit bridge router
            arb_chain = self.rpc_manager.get_chain_info("arbitrum")
            return RouteQuote(
                bridge_name="Hyperliquid Bridge via CCTP/Arbitrum Relay",
                source_chain_id=src_chain.chain_id,
                dest_chain_id=dst_chain.chain_id,
                source_token_address=VERIFIED_PROTOCOLS["cctp"]["usdc"][src_chain.chain_id],
                dest_token_address=VERIFIED_PROTOCOLS["cctp"]["usdc"][arb_chain.chain_id],
                router_address=VERIFIED_PROTOCOLS["cctp"]["routers"][src_chain.chain_id],
                deposit_amount_raw=raw_amount,
                estimated_output_raw=raw_amount,  # CCTP 1:1 burn-and-mint
                decimals=6,
                fee_amount_usd=0.10,
                gas_estimate_units=210000,
                calldata_template="cctp_deposit_for_burn",
                extra_params={
                    "destination_domain": VERIFIED_PROTOCOLS["cctp"]["domains"][arb_chain.chain_id],
                    "target_vault": VERIFIED_PROTOCOLS["hyperliquid"]["routers"][42161],
                },
            )

        # Route Case 2: Circle CCTP native transfer for USDC between supported EVM chains
        if token_sym == "USDC" and src_chain.cctp_domain is not None and dst_chain.cctp_domain is not None:
            cctp_data = VERIFIED_PROTOCOLS["cctp"]
            src_router = cctp_data["routers"].get(src_chain.chain_id)
            src_usdc = cctp_data["usdc"].get(src_chain.chain_id)
            dst_usdc = cctp_data["usdc"].get(dst_chain.chain_id, "")
            dst_domain = cctp_data["domains"].get(dst_chain.chain_id)

            if src_router and src_usdc and dst_domain is not None:
                return RouteQuote(
                    bridge_name=cctp_data["name"],
                    source_chain_id=src_chain.chain_id,
                    dest_chain_id=dst_chain.chain_id,
                    source_token_address=src_usdc,
                    dest_token_address=dst_usdc,
                    router_address=src_router,
                    deposit_amount_raw=raw_amount,
                    estimated_output_raw=raw_amount,
                    decimals=6,
                    fee_amount_usd=0.00,  # 0% protocol fee for CCTP
                    gas_estimate_units=190000,
                    calldata_template="cctp_deposit_for_burn",
                    extra_params={"destination_domain": dst_domain},
                )

        # Route Case 3: Across Protocol fallback
        across_data = VERIFIED_PROTOCOLS["across"]
        src_router = across_data["routers"].get(src_chain.chain_id)
        if src_router:
            fee_deduction = int(raw_amount * 0.0005)  # ~0.05% fee
            return RouteQuote(
                bridge_name=across_data["name"],
                source_chain_id=src_chain.chain_id,
                dest_chain_id=dst_chain.chain_id,
                source_token_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                dest_token_address="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                router_address=src_router,
                deposit_amount_raw=raw_amount,
                estimated_output_raw=raw_amount - fee_deduction,
                decimals=6,
                fee_amount_usd=intent.amount * 0.0005,
                gas_estimate_units=220000,
                calldata_template="across_deposit_v3",
                extra_params={"relayer_fee_pct": 0.05},
            )

        raise RuntimeError(f"No viable bridge route found for {intent.token} from {intent.source_chain} to {intent.dest_chain}")
