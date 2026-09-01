"""
Execution Simulator & Revert Circuit Breaker.
Validates transaction payloads via static eth_call and gas estimations prior to any user signature.
"""

from typing import Dict, Any, Optional
from web3 import Web3
from omnibridge.core.models import RouteQuote, SimulationResult


class Simulator:
    """Executes dry-run simulations against live RPCs and triggers circuit breakers on failure."""

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))

    def simulate_call(
        self,
        from_address: str,
        to_address: str,
        calldata: str,
        value_wei: int = 0,
        min_output_expected: Optional[int] = None,
        quote: Optional[RouteQuote] = None,
    ) -> SimulationResult:
        """Performs static call and gas estimation with circuit breaker enforcement."""
        tx_payload: Dict[str, Any] = {
            "from": Web3.to_checksum_address(from_address),
            "to": Web3.to_checksum_address(to_address),
            "data": calldata if calldata.startswith("0x") else f"0x{calldata}",
            "value": value_wei,
        }

        # Check if RPC connection is active
        if not self.w3.is_connected():
            return SimulationResult(
                success=False,
                revert_reason=f"RPC connection failed for {self.rpc_url}",
                output_amount_valid=False,
            )

        try:
            # 1. Fetch current gas price
            gas_price_wei = self.w3.eth.gas_price
            gas_price_gwei = float(self.w3.from_wei(gas_price_wei, "gwei"))

            # 2. Attempt eth_call simulation
            return_bytes = self.w3.eth.call(tx_payload)
            hex_return = return_bytes.hex() if return_bytes else "0x"

            # 3. Estimate gas units
            try:
                gas_estimate = self.w3.eth.estimate_gas(tx_payload)
            except Exception:
                gas_estimate = quote.gas_estimate_units if quote else 200000

            # 4. Calculate estimated gas cost in USD (assuming default ETH price ~$2500)
            eth_cost = self.w3.from_wei(gas_estimate * gas_price_wei, "ether")
            gas_cost_usd = float(eth_cost) * 2500.0

            # 5. Validate output amount if quote exists
            output_valid = True
            if quote and min_output_expected:
                output_valid = quote.estimated_output_raw >= min_output_expected

            return SimulationResult(
                success=True,
                gas_used=gas_estimate,
                gas_price_gwei=round(gas_price_gwei, 2),
                estimated_gas_cost_usd=round(gas_cost_usd, 4),
                calldata_preview=calldata[:66] + ("..." if len(calldata) > 66 else ""),
                output_amount_valid=output_valid,
            )

        except Exception as err:
            err_str = str(err)
            # Circuit breaker: format readable revert reason
            revert_reason = "Transaction Reverted: " + (
                "Insufficient token balance or allowance"
                if "execution reverted" in err_str or "transfer amount exceeds balance" in err_str
                else err_str
            )
            return SimulationResult(
                success=False,
                revert_reason=revert_reason,
                gas_used=0,
                gas_price_gwei=0.0,
                estimated_gas_cost_usd=0.0,
                output_amount_valid=False,
                calldata_preview=calldata[:66],
            )
