"""
Execution Simulator & Revert Circuit Breaker.

Validates transaction payloads via eth_call and gas estimation before any user
signature. This is the only branch in the execution graph, so it must be able to
say no: a result is VERIFIED only when a real call completed against a node whose
identity was checked. Everything else is a refusal, never a plausible default.

Scope: the source leg only. No eth_call can prove that funds arrive on the far
side of a bridge.
"""

from typing import Any, Dict, List, Optional, Tuple, Union

from web3 import Web3

from omnibridge.core.models import RouteQuote, SimulationResult, SimulationStatus

# Rough USD/ETH used only to annotate the gas estimate. Not a price oracle.
_ETH_USD_ESTIMATE = 2500.0


def is_evm_address(value: Optional[str]) -> bool:
    """True for a syntactically valid 20-byte hex address."""
    if not value or not isinstance(value, str):
        return False
    if not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


class Simulator:
    """Runs dry-run simulations against identity-checked RPCs and trips on failure."""

    def __init__(
        self,
        rpc_urls: Union[str, List[str]],
        expected_chain_id: Optional[int] = None,
    ):
        if isinstance(rpc_urls, str):
            rpc_urls = [rpc_urls] if rpc_urls else []
        self.rpc_urls: List[str] = [
            u for u in (rpc_urls or []) if isinstance(u, str) and u.startswith("http")
        ]
        self.expected_chain_id = expected_chain_id
        # Endpoints that answered but claimed to be a different chain.
        self.rejected_endpoints: List[Tuple[str, Any]] = []

    @property
    def rpc_url(self) -> str:
        """The preferred endpoint, kept for callers that only want one URL."""
        return self.rpc_urls[0] if self.rpc_urls else ""

    def _connect_verified(self) -> Tuple[Optional[Web3], Optional[str]]:
        """
        Return the first endpoint that is reachable *and* reports the chain we expect.

        The fallback list is not trusted infrastructure: RPCManager builds part of it
        by regex-extracting URLs out of web search results. An endpoint pointing at a
        fork, a testnet, or an attacker's node would answer eth_call confidently and
        wrongly, so identity is checked before anything is asked of it.
        """
        for url in self.rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 6}))
                if not w3.is_connected():
                    continue
                if self.expected_chain_id is not None:
                    reported = int(w3.eth.chain_id)
                    if reported != int(self.expected_chain_id):
                        self.rejected_endpoints.append((url, reported))
                        continue
                return w3, url
            except Exception:
                continue
        return None, None

    def simulate_call(
        self,
        from_address: str,
        to_address: str,
        calldata: str,
        value_wei: int = 0,
        min_output_expected: Optional[int] = None,
        quote: Optional[RouteQuote] = None,
    ) -> SimulationResult:
        """Perform an eth_call and gas estimate, or explain why it could not be done."""
        preview = calldata[:66] + ("..." if len(calldata) > 66 else "")

        # The sender is never substituted. Simulating from a different account reports
        # that account's balances and allowances, which says nothing about this user's
        # transaction.
        if not is_evm_address(from_address):
            return SimulationResult(
                status=SimulationStatus.UNSUPPORTED,
                revert_reason=(
                    f"Cannot simulate: sender '{from_address}' is not a valid EVM address. "
                    "A simulation from any other account would not describe this transfer."
                ),
                calldata_preview=preview,
                output_amount_valid=False,
            )

        # UNSUPPORTED keys on the chain being *submitted to* — the router we are calling —
        # not on the bridge's destination. A Base to Stellar transfer is an ordinary Base
        # transaction and simulates normally.
        if not is_evm_address(to_address):
            return SimulationResult(
                status=SimulationStatus.UNSUPPORTED,
                revert_reason=(
                    f"Cannot simulate: no eth_call path for submission target '{to_address}'. "
                    "Non-EVM preflight is not implemented; this transfer is unverified."
                ),
                calldata_preview=preview,
                output_amount_valid=False,
            )

        w3, rpc_url = self._connect_verified()
        if w3 is None:
            if self.rejected_endpoints:
                detail = "; ".join(
                    f"{url} reported chain {cid}, expected {self.expected_chain_id}"
                    for url, cid in self.rejected_endpoints
                )
                reason = f"No endpoint confirmed the expected chain identity. {detail}"
            elif not self.rpc_urls:
                reason = "No RPC endpoint was supplied for this chain."
            else:
                reason = (
                    f"None of {len(self.rpc_urls)} RPC endpoint(s) were reachable: "
                    + ", ".join(self.rpc_urls)
                )
            return SimulationResult(
                status=SimulationStatus.UNAVAILABLE,
                revert_reason=f"Simulation could not be performed. {reason}",
                calldata_preview=preview,
                output_amount_valid=False,
            )

        tx_payload: Dict[str, Any] = {
            "from": Web3.to_checksum_address(from_address),
            "to": Web3.to_checksum_address(to_address),
            "data": calldata if calldata.startswith("0x") else f"0x{calldata}",
            "value": value_wei,
        }

        try:
            gas_price_wei = w3.eth.gas_price
            gas_price_gwei = float(w3.from_wei(gas_price_wei, "gwei"))

            w3.eth.call(tx_payload)
            gas_estimate = int(w3.eth.estimate_gas(tx_payload))

            eth_cost = w3.from_wei(gas_estimate * gas_price_wei, "ether")
            gas_cost_usd = float(eth_cost) * _ETH_USD_ESTIMATE

            output_valid = True
            if quote and min_output_expected:
                output_valid = quote.estimated_output_raw >= min_output_expected

            return SimulationResult(
                status=SimulationStatus.VERIFIED,
                gas_used=gas_estimate,
                gas_price_gwei=round(gas_price_gwei, 2),
                estimated_gas_cost_usd=round(gas_cost_usd, 4),
                calldata_preview=preview,
                output_amount_valid=output_valid,
                simulated_on_chain_id=int(w3.eth.chain_id),
                simulated_via_rpc=rpc_url,
            )

        except Exception as err:
            # Report what the node said. Naming a probable cause the node did not
            # give would be a guess presented as a diagnosis.
            return SimulationResult(
                status=SimulationStatus.REVERTED,
                revert_reason=f"Transaction reverted during eth_call: {err}",
                calldata_preview=preview,
                output_amount_valid=False,
                simulated_on_chain_id=self.expected_chain_id,
                simulated_via_rpc=rpc_url,
            )
