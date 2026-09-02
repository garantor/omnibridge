"""
Data models and type definitions for Omnibridge.
"""

from enum import Enum
from typing import Dict, List, Optional, Any
from typing_extensions import Literal
from pydantic import BaseModel, Field, computed_field


class UserIntent(BaseModel):
    token: str = Field(description="Token symbol (e.g. USDC, ETH, WBTC)")
    amount: float = Field(description="Human-readable amount (e.g. 100.0)")
    source_chain: str = Field(description="Source chain name (e.g. base, arbitrum, ethereum)")
    dest_chain: str = Field(description="Destination chain name (e.g. hyperliquid, arbitrum, optimism)")
    recipient: Optional[str] = Field(default=None, description="Destination recipient address (defaults to sender)")
    max_slippage_bps: int = Field(default=50, description="Max slippage in basis points (50 = 0.5%)")
    priority: str = Field(default="low_fee", description="Routing priority: 'low_fee', 'fastest', 'native'")
    network: Literal["mainnet", "testnet"] = Field(
        default="mainnet",
        description="Which network family to resolve chains on. Chain resolution refuses to "
                    "cross this boundary, so a testnet is never selected for a mainnet intent.",
    )


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


class SimulationStatus(str, Enum):
    """
    Outcome of a pre-flight simulation.

    A boolean cannot express "I could not check", which is how an unverified
    transaction ends up presented as a verified one. Only VERIFIED may reach the
    approval gate.
    """

    VERIFIED = "VERIFIED"        # eth_call completed against a live, identity-checked node
    REVERTED = "REVERTED"        # eth_call reverted — circuit breaker
    UNAVAILABLE = "UNAVAILABLE"  # no reachable, identity-checked RPC — could not check
    UNSUPPORTED = "UNSUPPORTED"  # no eth_call path on the chain being submitted to


class SimulationResult(BaseModel):
    status: SimulationStatus
    revert_reason: Optional[str] = None

    # None means "not measured". Never substitute a plausible-looking default:
    # a fabricated gas figure is exactly how an unverified transaction passes
    # for a verified one.
    gas_used: Optional[int] = None
    gas_price_gwei: Optional[float] = None
    estimated_gas_cost_usd: Optional[float] = None

    output_amount_valid: bool = True
    calldata_preview: str = ""
    logs_count: int = 0

    # What was actually checked, so the manifest can state the scope of verification
    # rather than letting silence imply end-to-end assurance.
    simulated_on_chain_id: Optional[int] = None
    simulated_via_rpc: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success(self) -> bool:
        """Only a completed eth_call counts. Everything else is a refusal."""
        return self.status is SimulationStatus.VERIFIED


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

    # Verification provenance — rendered by the manifest so the human sees what was
    # actually checked, and on which chain.
    simulation_status: str = "UNKNOWN"
    simulated_on_chain_id: Optional[int] = None

    # Decoded back out of the calldata rather than echoed from the intent. If the
    # display and the transaction read from the same source, a decimal-scaling error
    # is invisible to the human too.
    amount_in_decoded: Optional[float] = None


class BroadcastReceipt(BaseModel):
    tx_hash: str
    chain_id: int
    block_number: Optional[int] = None
    explorer_url: str
    status: str = "submitted"
    timestamp: str = ""
