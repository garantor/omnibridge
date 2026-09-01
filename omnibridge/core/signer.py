"""
Secure Signer and Live On-Chain Broadcast Interface.
Enforces real on-chain execution with clean dry-run test mode separation.
"""

from typing import Dict, Any, Optional
import time
from eth_account import Account
from web3 import Web3
from omnibridge.core.models import BroadcastReceipt


class Signer:
    """Handles strictly real on-chain broadcasting and wallet signing integration."""

    def __init__(self, rpc_url: str, explorer_url: str = ""):
        self.rpc_url = rpc_url
        self.explorer_url = explorer_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url)) if rpc_url and rpc_url.startswith("http") else None

    def broadcast_raw_transaction(self, signed_tx_hex: str, chain_id: int = 1) -> BroadcastReceipt:
        """Submits an externally signed raw transaction directly on-chain."""
        clean_hex = signed_tx_hex.strip() if signed_tx_hex else ""
        if not clean_hex.startswith("0x"):
            clean_hex = f"0x{clean_hex}"

        # Case 1: Transaction was already broadcast by user's browser wallet (e.g. MetaMask eth_sendTransaction)
        # and returned a valid 32-byte (66-char hex) on-chain transaction hash
        if len(clean_hex) == 66 and not clean_hex.startswith("0x6fd3504e") and not clean_hex.startswith("0x095ea7b3"):
            return BroadcastReceipt(
                tx_hash=clean_hex,
                chain_id=chain_id,
                explorer_url=f"{self.explorer_url}/tx/{clean_hex}" if self.explorer_url else f"https://blockscan.com/tx/{clean_hex}",
                status="submitted_on_chain",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

        # Case 2: Signed raw RLP transaction payload ready for eth_sendRawTransaction
        if self.w3 and self.w3.is_connected() and len(clean_hex) > 66:
            try:
                raw_bytes = bytes.fromhex(clean_hex.replace("0x", ""))
                tx_hash_bytes = self.w3.eth.send_raw_transaction(raw_bytes)
                tx_hash = tx_hash_bytes.hex()
                if not tx_hash.startswith("0x"):
                    tx_hash = f"0x{tx_hash}"

                return BroadcastReceipt(
                    tx_hash=tx_hash,
                    chain_id=chain_id,
                    explorer_url=f"{self.explorer_url}/tx/{tx_hash}" if self.explorer_url else f"https://basescan.org/tx/{tx_hash}",
                    status="submitted_on_chain",
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            except Exception as e:
                raise RuntimeError(f"On-chain broadcast failed on node {self.rpc_url}: {e}")

        raise ValueError(
            "Signing Required: No valid signed transaction payload or on-chain transaction hash was provided. "
            "Please sign the transaction using your connected Web3 wallet (MetaMask/Rabby) or provide a private key."
        )

    def broadcast_transaction(
        self,
        raw_tx_payload: Dict[str, Any],
        private_key: Optional[str] = None,
        dry_run: bool = False,
    ) -> BroadcastReceipt:
        """Signs locally if private key provided, or handles dry_run / external wallet signature."""
        if dry_run:
            mock_hash = "0x" + "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
            return BroadcastReceipt(
                tx_hash=mock_hash,
                chain_id=raw_tx_payload.get("chainId", 8453),
                explorer_url=f"{self.explorer_url}/tx/{mock_hash}" if self.explorer_url else f"https://basescan.org/tx/{mock_hash}",
                status="dry_run_verified",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

        if private_key:
            account = Account.from_key(private_key)
            if not self.w3 or not self.w3.is_connected():
                raise ConnectionError(f"Cannot broadcast transaction: RPC endpoint {self.rpc_url} is unreachable.")

            if "nonce" not in raw_tx_payload:
                raw_tx_payload["nonce"] = self.w3.eth.get_transaction_count(account.address)
            if "gas" not in raw_tx_payload:
                try:
                    raw_tx_payload["gas"] = self.w3.eth.estimate_gas(raw_tx_payload)
                except Exception:
                    raw_tx_payload["gas"] = 250000
            if "gasPrice" not in raw_tx_payload and "maxFeePerGas" not in raw_tx_payload:
                raw_tx_payload["gasPrice"] = self.w3.eth.gas_price

            signed_tx = account.sign_transaction(raw_tx_payload)
            tx_hash_bytes = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            tx_hash = tx_hash_bytes.hex()
            return BroadcastReceipt(
                tx_hash=tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}",
                chain_id=raw_tx_payload.get("chainId", self.w3.eth.chain_id),
                explorer_url=f"{self.explorer_url}/tx/{tx_hash}",
                status="submitted_on_chain",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

        signed_hex = raw_tx_payload.get("signed_hex") or raw_tx_payload.get("tx_hash") or raw_tx_payload.get("data")
        dyn_chain_id = raw_tx_payload.get("chainId") or (self.w3.eth.chain_id if (self.w3 and self.w3.is_connected()) else 1)
        return self.broadcast_raw_transaction(signed_hex, chain_id=dyn_chain_id)
