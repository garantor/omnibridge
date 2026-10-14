"""
The bundler that submits UserOperations and the ERC-20 paymaster that lets the account
pay gas in a token — any provider that speaks the standards.

Standard, so the same for every provider:
  - bundler: ERC-4337 JSON-RPC (`eth_sendUserOperation`, `eth_estimateUserOperationGas`,
    `eth_getUserOperationReceipt`, `eth_getUserOperationByHash`, `eth_supportedEntryPoints`);
  - paymaster: ERC-7677 (`pm_getPaymasterStubData`, `pm_getPaymasterData`), whose last
    parameter is a provider-specific context — configured, not assumed.

Not standard, so probed and never required:
  - a token quote (exchange rate, post-op gas) to size the gas-fee cap from. Pimlico's
    `pimlico_getTokenQuotes` is used when the paymaster answers it; otherwise the cap is
    the ceiling configured in AA_MAX_GAS_FEE, and the paymaster address must be configured;
  - a UserOperation gas price. `pimlico_getUserOperationGasPrice` when the bundler answers
    it; otherwise the chain's base fee and priority fee, read through the bundler.

Configuration (all server-side; URLs usually carry an API key and never reach the browser):
  AA_BUNDLER_URL        bundler endpoint; `{chain_id}` is replaced. Per chain:
                        AA_BUNDLER_URL_<chainId> takes precedence.
  AA_PAYMASTER_URL      paymaster endpoint, same rules; defaults to the bundler's.
  AA_BUNDLER_HEADERS, AA_PAYMASTER_HEADERS
                        optional JSON objects of extra HTTP headers (for key-in-header APIs).
  AA_PAYMASTER_CONTEXT  JSON object sent as the ERC-7677 context; the string "{token}"
                        anywhere in it becomes the gas token. Default {"token": "{token}"}.
  AA_PAYMASTER_ADDRESS  the ERC-20 paymaster, when the paymaster offers no quote naming it.
  AA_MAX_GAS_FEE        most the paymaster may take per transfer, in token units ("0.50").
                        Required without a quote; a ceiling on the quoted cap with one.
  PIMLICO_API_KEY       shorthand: with no AA_BUNDLER_URL, bundler and paymaster are Pimlico's.

Before use, the bundler is asked which chain it serves and which EntryPoints it supports:
a URL for another chain, or one without EntryPoint v0.7, is refused rather than trusted.
"""

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests
from web3 import Web3

from omnibridge.aa.account import ENTRYPOINT_V07

PIMLICO_URL = "https://api.pimlico.io/v2/{chain_id}/rpc?apikey={key}"

#: Gas the whole UserOperation may use, for sizing the fee cap. Generous on purpose: a cap
#: that is too low makes the paymaster refuse after the user has signed, while the unused
#: part of a generous cap is simply not charged.
FEE_CAP_GAS_ENVELOPE = 900_000
#: Headroom on the gas price between quoting and inclusion.
FEE_CAP_PRICE_BUFFER = 1.5

DEFAULT_CONTEXT = {"token": "{token}"}

#: The methods each endpoint answers. Anything else goes to the bundler, which also
#: serves the chain's standard eth_* reads.
PAYMASTER_METHODS = {"pm_getPaymasterStubData", "pm_getPaymasterData", "pimlico_getTokenQuotes"}


class AaError(RuntimeError):
    """The bundler or paymaster refused, could not be reached, or is misconfigured."""


class MethodUnsupported(AaError):
    """The endpoint does not implement this (non-standard) method."""


@dataclass
class AaConfig:
    bundler_url: str
    paymaster_url: str
    bundler_headers: Dict[str, str] = field(default_factory=dict)
    paymaster_headers: Dict[str, str] = field(default_factory=dict)
    paymaster_context: Any = field(default_factory=lambda: dict(DEFAULT_CONTEXT))
    paymaster_address: Optional[str] = None
    max_gas_fee: Optional[Decimal] = None
    #: Which setting supplied the endpoints, for messages. Never the URL itself.
    source: str = "AA_BUNDLER_URL"

    def context_for(self, token: str) -> Any:
        return _substitute(self.paymaster_context, Web3.to_checksum_address(token))


def _substitute(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return value.replace("{token}", token)
    if isinstance(value, dict):
        return {k: _substitute(v, token) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, token) for v in value]
    return value


def _env(name: str) -> Optional[str]:
    return (os.environ.get(name) or "").strip() or None


def _json_env(name: str, default: Any) -> Any:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise AaError(f"{name} is not valid JSON: {exc}") from exc


def _url(name: str, chain_id: int) -> Optional[str]:
    raw = _env(f"{name}_{chain_id}") or _env(name)
    return raw.replace("{chain_id}", str(chain_id)) if raw else None


def config_for(chain_id: int) -> Optional[AaConfig]:
    """This chain's bundler and paymaster settings, or None when none are configured."""
    chain_id = int(chain_id)
    bundler = _url("AA_BUNDLER_URL", chain_id)
    source = "AA_BUNDLER_URL"
    if bundler is None and _env("PIMLICO_API_KEY"):
        bundler = PIMLICO_URL.format(chain_id=chain_id, key=_env("PIMLICO_API_KEY"))
        source = "PIMLICO_API_KEY"
    if bundler is None:
        return None
    paymaster = _url("AA_PAYMASTER_URL", chain_id)
    bundler_headers = _json_env("AA_BUNDLER_HEADERS", {})
    paymaster_headers = _json_env("AA_PAYMASTER_HEADERS", None)
    if paymaster_headers is None:
        paymaster_headers = bundler_headers if paymaster is None else {}
    context = _json_env("AA_PAYMASTER_CONTEXT", dict(DEFAULT_CONTEXT))
    address = _env("AA_PAYMASTER_ADDRESS")
    if address is not None:
        if not Web3.is_address(address):
            raise AaError("AA_PAYMASTER_ADDRESS is not an address.")
        address = Web3.to_checksum_address(address)
    ceiling = None
    if _env("AA_MAX_GAS_FEE") is not None:
        try:
            ceiling = Decimal(_env("AA_MAX_GAS_FEE"))
        except InvalidOperation as exc:
            raise AaError("AA_MAX_GAS_FEE must be a token amount such as 0.50.") from exc
        if ceiling <= 0:
            raise AaError("AA_MAX_GAS_FEE must be more than zero.")
    return AaConfig(bundler_url=bundler, paymaster_url=paymaster or bundler,
                    bundler_headers=bundler_headers, paymaster_headers=paymaster_headers,
                    paymaster_context=context, paymaster_address=address,
                    max_gas_fee=ceiling, source=source)


def configured(chain_id: Optional[int] = None) -> bool:
    if chain_id is not None:
        return config_for(chain_id) is not None
    return any(k == "PIMLICO_API_KEY" or k.startswith("AA_BUNDLER_URL")
               for k, v in os.environ.items() if v.strip())


@dataclass
class TokenQuote:
    token: str
    paymaster: str
    #: Token base units per 1e18 wei of gas.
    exchange_rate: int
    post_op_gas: int


@dataclass
class GasPrice:
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    #: Which method it came from, for the log.
    basis: str

    def as_rpc(self) -> Dict[str, str]:
        return {"maxFeePerGas": hex(self.max_fee_per_gas),
                "maxPriorityFeePerGas": hex(self.max_priority_fee_per_gas)}


@dataclass
class FeePlan:
    paymaster: str
    fee_cap: int
    context: Any
    #: "quote" (sized from the paymaster's quote) or "ceiling" (AA_MAX_GAS_FEE).
    basis: str


_UNSUPPORTED = re.compile(r"not (supported|found|available|implemented)|does not exist|unknown method|"
                          r"method .*not|unsupported", re.I)


class AaClient:
    def __init__(self, chain_id: int, config: Optional[AaConfig] = None, session: Any = None,
                 timeout: float = 20.0):
        self.chain_id = int(chain_id)
        self.config = config or config_for(self.chain_id)
        if self.config is None:
            raise AaError("no bundler is configured (set AA_BUNDLER_URL, or PIMLICO_API_KEY).")
        self.session = session or requests
        self.timeout = timeout

    # ----------------------------------------------------------------- transport

    def _post(self, url: str, headers: Dict[str, str], label: str, method: str, params: List[Any]) -> Any:
        try:
            resp = self.session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                     headers=headers or None, timeout=self.timeout)
            body = resp.json()
        except Exception as exc:
            # Exception text from HTTP libraries includes the URL, and the URL the key.
            raise AaError(f"the {label} could not be reached for {method} ({type(exc).__name__}).") from None
        err = body.get("error") if isinstance(body, dict) else None
        if err:
            message = str(err.get("message", err)) if isinstance(err, dict) else str(err)
            message = message.replace(url, f"<{label}>")
            code = err.get("code") if isinstance(err, dict) else None
            if code == -32601 or _UNSUPPORTED.search(message) and method not in _STANDARD:
                raise MethodUnsupported(f"the {label} does not support {method}: {message}")
            raise AaError(f"the {label} refused {method}: {message}")
        return body.get("result") if isinstance(body, dict) else None

    def bundler_rpc(self, method: str, params: List[Any]) -> Any:
        return self._post(self.config.bundler_url, self.config.bundler_headers, "bundler", method, params)

    def paymaster_rpc(self, method: str, params: List[Any]) -> Any:
        return self._post(self.config.paymaster_url, self.config.paymaster_headers, "paymaster", method, params)

    def rpc(self, method: str, params: List[Any]) -> Any:
        """Route a call to the endpoint that answers it."""
        if method in PAYMASTER_METHODS:
            return self.paymaster_rpc(method, params)
        return self.bundler_rpc(method, params)

    # ------------------------------------------------------------------- checks

    def check_bundler(self) -> None:
        """Refuse a bundler that serves another chain or not EntryPoint v0.7."""
        served = self.bundler_rpc("eth_chainId", [])
        if served is None or int(str(served), 16) != self.chain_id:
            raise AaError(f"the bundler from {self.config.source} serves chain {served}, not {self.chain_id}; "
                          f"set AA_BUNDLER_URL_{self.chain_id} or use {{chain_id}} in the URL.")
        entry_points = self.bundler_rpc("eth_supportedEntryPoints", []) or []
        if ENTRYPOINT_V07.lower() not in {str(e).lower() for e in entry_points}:
            raise AaError(f"the bundler does not support EntryPoint v0.7 ({ENTRYPOINT_V07}), "
                          "which MetaMask's smart account validates against.")

    # --------------------------------------------------------------- gas & fees

    def token_quote(self, token: str) -> Optional[TokenQuote]:
        """The paymaster's quote for this token, or None when it offers no quote method."""
        try:
            result = self.paymaster_rpc("pimlico_getTokenQuotes",
                                        [{"tokens": [token]}, ENTRYPOINT_V07, hex(self.chain_id)])
        except MethodUnsupported:
            return None
        quotes = (result or {}).get("quotes") or []
        match = next((q for q in quotes if str(q.get("token", "")).lower() == token.lower()), None)
        if match is None:
            raise AaError(f"the paymaster does not accept {token} on chain {self.chain_id}.")
        return TokenQuote(token=token, paymaster=Web3.to_checksum_address(match["paymaster"]),
                          exchange_rate=int(match["exchangeRate"], 16), post_op_gas=int(match["postOpGas"], 16))

    def gas_price(self) -> GasPrice:
        """What a UserOperation should bid. The bundler's own quote when it has one."""
        try:
            fast = (self.bundler_rpc("pimlico_getUserOperationGasPrice", []) or {}).get("fast") or {}
            return GasPrice(int(fast["maxFeePerGas"], 16), int(fast["maxPriorityFeePerGas"], 16),
                            "pimlico_getUserOperationGasPrice")
        except MethodUnsupported:
            pass
        block = self.bundler_rpc("eth_getBlockByNumber", ["latest", False]) or {}
        base = int(block.get("baseFeePerGas") or "0x0", 16)
        try:
            tip = int(self.bundler_rpc("eth_maxPriorityFeePerGas", []), 16)
        except MethodUnsupported:
            tip = 0
        return GasPrice(2 * base + tip, tip, "base fee + eth_maxPriorityFeePerGas")

    def fee_plan(self, token: str, decimals: int) -> FeePlan:
        """
        Which paymaster, how much it may take, and the context to send it.

        The cap is always code's: from the paymaster's quote when there is one, otherwise
        the configured ceiling. Never the model's, and never unbounded.
        """
        cfg = self.config
        ceiling = int(cfg.max_gas_fee * 10**decimals) if cfg.max_gas_fee is not None else None
        context = cfg.context_for(token)
        quote = self.token_quote(token)
        if quote is None:
            if cfg.paymaster_address is None or ceiling is None:
                raise AaError("the paymaster offers no token quote, so AA_PAYMASTER_ADDRESS and "
                              "AA_MAX_GAS_FEE must be set to bound what it may take.")
            return FeePlan(cfg.paymaster_address, ceiling, context, "ceiling")
        if cfg.paymaster_address and cfg.paymaster_address.lower() != quote.paymaster.lower():
            raise AaError(f"the paymaster quoted {quote.paymaster}, not the configured "
                          f"AA_PAYMASTER_ADDRESS {cfg.paymaster_address}.")
        cap = token_fee_cap(quote, self.gas_price().max_fee_per_gas)
        if ceiling is not None and cap > ceiling:
            raise AaError(f"the quoted gas-fee cap {cap / 10**decimals:.4f} is above AA_MAX_GAS_FEE "
                          f"{cfg.max_gas_fee}.")
        return FeePlan(quote.paymaster, cap, context, "quote")


#: Standard methods: an error from one of these is a refusal, never "unsupported".
_STANDARD = {"eth_chainId", "eth_supportedEntryPoints", "eth_sendUserOperation",
             "eth_estimateUserOperationGas", "eth_getUserOperationReceipt",
             "eth_getUserOperationByHash", "pm_getPaymasterStubData", "pm_getPaymasterData"}


def token_fee_cap(quote: TokenQuote, max_fee_per_gas: int,
                  gas: int = FEE_CAP_GAS_ENVELOPE, buffer: float = FEE_CAP_PRICE_BUFFER) -> int:
    """
    The most the paymaster may take, in token base units.

    The conversion an ERC-20 paymaster applies to a UserOperation's maximum cost: wei of
    gas (including the paymaster's own post-op gas) times the exchange rate, over 1e18.
    Rounded up, so the cap is never a unit short.
    """
    wei = (gas + quote.post_op_gas) * int(max_fee_per_gas * buffer)
    return -(-(wei * quote.exchange_rate) // 10**18)
