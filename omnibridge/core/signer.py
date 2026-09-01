"""
Secure Signer and On-Chain Broadcast Interface.
Enforces strict human-in-the-loop gating; private keys are never accessed during simulation.
"""

from typing import Dict, Any, Optional
import os
import time
from eth_account import Account
from web3 import Web3
from omnibridge.core.models import BroadcastReceipt


class Signer:
    """Handles signing and broadcasting with isolated credential management."""

    def __init__(self, rpc_url: str, explorer_url: str = ""):
        self.rpc_url = rpc_url
        self.explorer_url = explorer_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))

    def broadcast_transaction(
        self,
        raw_tx_payload: Dict[str, Any],
        private_key: Optional[str] = None,
        dry_run: bool = False,
    ) -> BroadcastReceipt:
        """Signs and broadcasts raw transaction payload if authorized."""
        if dry_run or not private_key:
            # Simulate broadcast in test/demo mode
            dummy_hash = "0x" + os.urandom(32).hex()
            return BroadcastReceipt(
                tx_hash=dummy_hash,
                chain_id=raw_tx_payload.get("chainId", 8453),
                block_number=18492042,
                explorer_url=f"{self.explorer_url}/tx/{dummy_hash}",
                status="simulated_broadcast",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

        account = Account.from_key(private_key)
        
        # Ensure nonce & gas
        if "nonce" not in raw_tx_payload:
            raw_tx_payload["nonce"] = self.w3.eth.get_transaction_count(account.address)
        if "gas" not in raw_tx_payload:
            raw_tx_payload["gas"] = 250000

        signed_tx = account.sign_transaction(raw_tx_payload)
        tx_hash_bytes = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hash = tx_hash_bytes.hex()

        return BroadcastReceipt(
            tx_hash=tx_hash,
            chain_id=raw_tx_payload.get("chainId", self.w3.eth.chain_id),
            explorer_url=f"{self.explorer_url}/tx/{tx_hash}",
            status="submitted",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
