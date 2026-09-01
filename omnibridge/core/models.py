"""
Data models and type definitions for Omnibridge.
"""

from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class UserIntent(BaseModel):
    token: str = Field(description="Token symbol (e.g. USDC, ETH, WBTC)")
    amount: float = Field(description="Human-readable amount (e.g. 100.0)")
    source_chain: str = Field(description="Source chain name (e.g. base, arbitrum, ethereum)")
    dest_chain: str = Field(description="Destination chain name (e.g. hyperliquid, arbitrum, optimism)")
    recipient: Optional[str] = Field(default=None, description="Destination recipient address (defaults to sender)")
    max_slippage_bps: int = Field(default=50, description="Max slippage in basis points (50 = 0.5%)")
    priority: str = Field(default="low_fee", description="Routing priority: 'low_fee', 'fastest', 'native'")


class ChainInfo(BaseModel):
    chain_id: int
    name: str
    native_symbol: str = "ETH"
    block_explorer: str
    rpc_urls: List[str]
    cctp_domain: Optional[int] = None


class RouteQuote(BaseModel):
    bridge_name: str
    source_chain_id: int
    dest_chain_id: int
    source_token_address: str
    dest_token_address: str
    router_address: str
    deposit_amount_raw: int
    estimated_output_raw: int
    decimals: int = 6
    fee_amount_usd: float = 0.0
    gas_estimate_units: int = 200000
    calldata_template: str = ""
    extra_params: Dict[str, Any] = Field(default_factory=dict)


class SimulationResult(BaseModel):
    success: bool
    revert_reason: Optional[str] = None
    gas_used: int = 0
    gas_price_gwei: float = 0.0
    estimated_gas_cost_usd: float = 0.0
    output_amount_valid: bool = True
    calldata_preview: str = ""
    logs_count: int = 0


class ExecutionManifest(BaseModel):
    source_chain: str
    source_chain_id: int
    dest_chain: str
    dest_chain_id: int
    bridge_name: str
    router_address: str
    token_symbol: str
    amount_in: float
    amount_out_est: float
    min_amount_out: float
    slippage_percent: float
    gas_cost_usd: float
    bridge_fee_usd: float
    total_cost_usd: float
    raw_to: str
    raw_value_wei: int
    raw_data: str


class BroadcastReceipt(BaseModel):
    tx_hash: str
    chain_id: int
    block_number: Optional[int] = None
    explorer_url: str
    status: str = "submitted"
    timestamp: str = ""
