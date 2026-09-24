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
    # `priority` ("low_fee" | "fastest" | "native") was removed. The CLI accepted it and
    # passed it through, and nothing ever read it — a flag that silently did nothing.
    # Route selection is the agent's judgement; when it becomes steerable it should be
    # steerable in the prompt, not by a field no code consults.
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
    # `cctp_domain` was removed: never set, never read, and a protocol-specific
    # field on a generic chain model is the curated-table shape Rule B rules out.
    # A destination's identity travels as `dest_chain_ref`, which names its scheme.


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
    # `gas_estimate_units` was removed. It was only ever written as 0 and never read,
    # and its 200000 default was an invented figure that would have reached a manifest
    # as though measured. Real gas comes from the simulator or is reported as absent.
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

    # What was actually checked, so the manifest can state the scope of verification
    # rather than letting silence imply end-to-end assurance.
    simulated_on_chain_id: Optional[int] = None
    simulated_via_rpc: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success(self) -> bool:
        """Only a completed eth_call counts. Everything else is a refusal."""
        return self.status is SimulationStatus.VERIFIED


class StepStatus(str, Enum):
    """
    Whether a step in a plan has been checked, and if not, why not.

    PENDING_PREREQUISITE is the one that matters. A bridge call cannot be simulated
    before its approval lands — the allowance genuinely is not there yet — and there is
    no honest way to report it as verified in advance. It is re-simulated against live
    state once the approval confirms, immediately before it is signed.
    """

    VERIFIED = "VERIFIED"
    REVERTED = "REVERTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    PENDING_PREREQUISITE = "PENDING_PREREQUISITE"


class TransactionStep(BaseModel):
    """One transaction the user will be asked to sign."""

    index: int
    kind: str                      # "approve" | "bridge"
    title: str
    description: str
    to: str
    data: str
    value_wei: int = 0
    chain_id: int

    status: StepStatus = StepStatus.PENDING_PREREQUISITE
    revert_reason: Optional[str] = None
    gas_used: Optional[int] = None
    simulated_on_chain_id: Optional[int] = None

    # For an approve: what is being granted, to whom. Rendered so the human sees the
    # standing permission they are giving, not just a hex blob.
    token_address: Optional[str] = None
    spender_address: Optional[str] = None
    amount_raw: Optional[int] = None
    amount_decoded: Optional[float] = None

    tx_hash: Optional[str] = None

    # What a forked simulation of the whole plan said about this step, run with every
    # earlier step applied first. Evidence only: it never makes a step signable, because
    # the fork is a snapshot and the live re-check before signing is what counts.
    fork_verified: Optional[bool] = None
    fork_gas_used: Optional[int] = None
    fork_revert_reason: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def signable(self) -> bool:
        """Only a step verified against live state may be signed."""
        return self.status is StepStatus.VERIFIED


class ExecutionPlan(BaseModel):
    """
    The ordered transactions that make up one transfer.

    A single-transaction transfer is a plan of length one, so there is no separate code
    path for it — the thing that used to be `unsigned_transaction_hex` is now step 0.
    """

    steps: List[TransactionStep] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        """True when the first unsigned step has been verified and can be signed."""
        pending = [s for s in self.steps if s.tx_hash is None]
        return bool(pending) and pending[0].signable


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
