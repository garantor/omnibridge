"""
Smart-account signing: EIP-7702 (MetaMask DeleGator) + ERC-4337 via any bundler, gas in USDC.

The browser assembles and signs the UserOperation with MetaMask's kit, so the server's
job is to decide what it may contain and to refuse anything else. These tests pin that:
detection of a usable account, the batch encoding the kit also uses, every refusal the
proxy guard makes, the fee cap, and the batch really running through MetaMask's delegator
on a fork.
"""

import types

import pytest
from web3 import Web3

from omnibridge.aa import provider
from omnibridge.aa.account import (
    ENTRYPOINT_V07,
    METAMASK_7702_DELEGATOR,
    BatchDecodeError,
    Call,
    account_status,
    decode_batch,
    delegate_of,
    encode_batch,
)
from omnibridge.aa.guard import ApprovedBatch, build_batch, check_user_operation
from omnibridge.agent.provenance import selector_for
from omnibridge.core import erc20

SENDER = Web3.to_checksum_address("0x" + "ab" * 20)
USDC = "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d"
TM = "0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA"
PAYMASTER = "0x0000000000000039cd5e8aE05257CE51C473ddd1"
DESIGNATOR = bytes.fromhex("ef0100" + METAMASK_7702_DELEGATOR[2:].lower())


class _Eth:
    def __init__(self, codes):
        self.codes = codes

    def get_code(self, address):
        return self.codes.get(Web3.to_checksum_address(address), b"")

    def get_storage_at(self, *_):
        return b"\x00" * 32


def _w3(sender_code, delegator_code=None):
    codes = {SENDER: sender_code}
    codes[Web3.to_checksum_address(METAMASK_7702_DELEGATOR)] = (
        delegator_code if delegator_code is not None
        else bytes.fromhex(selector_for("execute(bytes32,bytes)")[2:] + "00" * 64))
    return types.SimpleNamespace(eth=_Eth(codes))


# --------------------------------------------------------------- account detection

def test_a_switched_metamask_account_is_usable():
    status = account_status(_w3(DESIGNATOR), SENDER)
    assert status.smart_account and status.delegate == Web3.to_checksum_address(METAMASK_7702_DELEGATOR)


@pytest.mark.parametrize("code, kind", [
    (b"", "eoa"),
    (bytes.fromhex("ef0100" + "11" * 20), "other_7702"),
    (bytes.fromhex("6080604052"), "contract"),
])
def test_anything_else_is_not(code, kind):
    status = account_status(_w3(code), SENDER)
    assert status.kind == kind and not status.smart_account


def test_a_delegator_without_execute_is_not_trusted():
    status = account_status(_w3(DESIGNATOR, delegator_code=b"\x60\x80"), SENDER)
    assert not status.smart_account and "execute(bytes32,bytes)" in status.reason


def test_the_designator_is_parsed_exactly():
    assert delegate_of(DESIGNATOR) == Web3.to_checksum_address(METAMASK_7702_DELEGATOR)
    assert delegate_of(DESIGNATOR + b"\x00") is None


# ------------------------------------------------------------------- the batch

def _calls():
    return [Call(USDC, 0, erc20.build_approve(PAYMASTER, 50_000)),
            Call(USDC, 0, erc20.build_approve(TM, 1_000_000)),
            Call(TM, 0, "0x8e0250ee" + "00" * 32)]


def test_the_batch_round_trips_in_the_kits_batch_mode():
    data = encode_batch(_calls())
    assert data.startswith("0xe9ae5c53")
    # MetaMask's kit uses BatchDefault = 0x0100…00 for more than one call.
    assert data[10:74] == "01" + "00" * 31
    assert [c.normalised() for c in decode_batch(data)] == [c.normalised() for c in _calls()]


def test_a_try_mode_batch_is_refused():
    data = encode_batch(_calls())
    try_mode = data[:10] + "0101" + data[14:]
    with pytest.raises(BatchDecodeError, match="not an atomic batch"):
        decode_batch(try_mode)


def test_the_batch_is_the_fee_allowance_then_the_unsigned_steps():
    steps = [types.SimpleNamespace(to=USDC, value_wei=0, data=erc20.build_approve(TM, 1_000_000), tx_hash=None),
             types.SimpleNamespace(to=TM, value_wei=0, data="0x8e0250ee", tx_hash=None),
             types.SimpleNamespace(to=TM, value_wei=0, data="0xdone", tx_hash="0x" + "11" * 32)]
    batch = build_batch(SENDER, 421614, steps, USDC, PAYMASTER, 50_000)
    assert [c.to for c in batch.calls] == [Web3.to_checksum_address(USDC)] * 2 + [TM]
    assert batch.calls[0].data == erc20.build_approve(PAYMASTER, 50_000)


# ---------------------------------------------------------------------- the guard

def _approved():
    return ApprovedBatch(SENDER, 421614, Web3.to_checksum_address(USDC), PAYMASTER, 50_000, _calls())


def _op(**overrides):
    op = {"sender": SENDER, "nonce": "0x0", "callData": encode_batch(_calls()),
          "paymaster": PAYMASTER, "signature": "0x"}
    op.update(overrides)
    return op


def test_the_approved_batch_passes():
    assert check_user_operation(_op(), _approved(), ENTRYPOINT_V07) == []


@pytest.mark.parametrize("overrides, message", [
    (dict(sender="0x" + "cd" * 20), "not the run's wallet"),
    (dict(callData=encode_batch(_calls()[:2])), "differ from the 3 approved"),
    (dict(callData=encode_batch(_calls()[:2] + [Call(TM, 0, "0xdeadbeef")])), "differ"),
    (dict(callData=encode_batch(list(reversed(_calls())))), "differ"),
    (dict(paymaster="0x" + "99" * 20), "not the approved ERC-20 paymaster"),
    (dict(eip7702Auth={"address": "0x" + "66" * 20}), "re-delegating the account"),
    (dict(factory="0x" + "77" * 20), "deploys an account"),
    (dict(callData="0xa9059cbb" + "00" * 64), "not an execute"),
])
def test_anything_else_is_refused(overrides, message):
    problems = check_user_operation(_op(**overrides), _approved(), ENTRYPOINT_V07)
    assert any(message in p for p in problems), problems


def test_another_entry_point_is_refused():
    assert any("not v0.7" in p for p in check_user_operation(_op(), _approved(), "0x" + "55" * 20))


# ----------------------------------------------------------------------- fee cap

def test_the_fee_cap_is_the_paymasters_conversion_rounded_up():
    quote = provider.TokenQuote(token=USDC, paymaster=PAYMASTER, exchange_rate=3_000 * 10**6, post_op_gas=40_000)
    # exchange_rate 3000e6 = 3,000 USDC per ETH. 1 gwei, no buffer: (900k + 40k) gas
    # * 1e9 wei = 9.4e14 wei = 0.00094 ETH = 2.82 USDC = 2,820,000 base units.
    assert provider.token_fee_cap(quote, 10**9, buffer=1.0) == 2_820_000
    # A price buffer raises it proportionally.
    assert provider.token_fee_cap(quote, 10**9) == 4_230_000
    # Rounded up: a cap one unit short would make the paymaster refuse after signing.
    tiny = provider.TokenQuote(token=USDC, paymaster=PAYMASTER, exchange_rate=1, post_op_gas=0)
    assert provider.token_fee_cap(tiny, 1, gas=1, buffer=1.0) == 1


# --------------------------------------------------------------------- providers

_AA_ENV = ["PIMLICO_API_KEY", "AA_BUNDLER_URL", "AA_PAYMASTER_URL", "AA_BUNDLER_HEADERS",
           "AA_PAYMASTER_HEADERS", "AA_PAYMASTER_CONTEXT", "AA_PAYMASTER_ADDRESS", "AA_MAX_GAS_FEE",
           "AA_BUNDLER_URL_421614", "AA_PAYMASTER_URL_421614"]


@pytest.fixture
def aa_env(monkeypatch):
    for name in _AA_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class _Endpoint:
    """A fake JSON-RPC server per URL: {url: {method: result or Exception-shaped error}}."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def post(self, url, json, headers=None, timeout=None):
        self.seen.append((url, json["method"], headers))
        table = self.routes.get(url.split("?")[0], {})
        if json["method"] not in table:
            body = {"error": {"code": -32601, "message": f"Method {json['method']} not found"}}
        else:
            answer = table[json["method"]]
            body = answer if isinstance(answer, dict) and "error" in answer else {"result": answer}
        return types.SimpleNamespace(json=lambda: body)


BUNDLER = "https://bundler.test/421614"
PM = "https://paymaster.test/421614"


def _client(aa_env, routes, **env):
    aa_env.setenv("AA_BUNDLER_URL", "https://bundler.test/{chain_id}?key=secret")
    for k, v in env.items():
        aa_env.setenv(k, v)
    session = _Endpoint(routes)
    return provider.AaClient(421614, session=session), session


def test_any_bundler_url_is_used_per_chain(aa_env):
    assert provider.config_for(421614) is None and not provider.configured()
    aa_env.setenv("AA_BUNDLER_URL", "https://b.test/{chain_id}/rpc")
    cfg = provider.config_for(421614)
    assert cfg.bundler_url == "https://b.test/421614/rpc" and cfg.paymaster_url == cfg.bundler_url
    aa_env.setenv("AA_BUNDLER_URL_421614", "https://arb-sepolia.other.test/v2/k")
    aa_env.setenv("AA_PAYMASTER_URL", "https://pm.test/{chain_id}")
    cfg = provider.config_for(421614)
    assert cfg.bundler_url == "https://arb-sepolia.other.test/v2/k" and cfg.paymaster_url == "https://pm.test/421614"
    assert provider.config_for(84532).bundler_url == "https://b.test/84532/rpc"


def test_pimlico_is_only_a_shorthand(aa_env):
    aa_env.setenv("PIMLICO_API_KEY", "k")
    assert provider.config_for(421614).bundler_url.startswith("https://api.pimlico.io/v2/421614/")
    aa_env.setenv("AA_BUNDLER_URL", "https://b.test/{chain_id}")
    assert provider.config_for(421614).bundler_url == "https://b.test/421614"


def test_paymaster_context_and_headers_are_configured(aa_env):
    aa_env.setenv("AA_BUNDLER_URL", "https://b.test")
    aa_env.setenv("AA_BUNDLER_HEADERS", '{"x-api-key": "h"}')
    aa_env.setenv("AA_PAYMASTER_CONTEXT", '{"erc20": {"token": "{token}"}, "policyId": "p1"}')
    cfg = provider.config_for(421614)
    assert cfg.bundler_headers == {"x-api-key": "h"} and cfg.paymaster_headers == {"x-api-key": "h"}
    assert cfg.context_for(USDC.lower()) == {"erc20": {"token": USDC}, "policyId": "p1"}
    aa_env.setenv("AA_PAYMASTER_CONTEXT", "{not json")
    with pytest.raises(provider.AaError, match="AA_PAYMASTER_CONTEXT is not valid JSON"):
        provider.config_for(421614)


def test_a_bundler_for_another_chain_or_entry_point_is_refused(aa_env):
    client, _ = _client(aa_env, {BUNDLER: {"eth_chainId": "0x1", "eth_supportedEntryPoints": [ENTRYPOINT_V07]}})
    with pytest.raises(provider.AaError, match="serves chain 0x1, not 421614"):
        client.check_bundler()
    client, _ = _client(aa_env, {BUNDLER: {"eth_chainId": hex(421614), "eth_supportedEntryPoints": ["0x" + "11" * 20]}})
    with pytest.raises(provider.AaError, match="EntryPoint v0.7"):
        client.check_bundler()


def test_calls_are_routed_to_the_endpoint_that_answers_them(aa_env):
    client, session = _client(aa_env, {BUNDLER: {"eth_sendUserOperation": "0xop"},
                                       PM: {"pm_getPaymasterData": {"paymaster": PAYMASTER}}},
                              AA_PAYMASTER_URL="https://paymaster.test/{chain_id}")
    client.rpc("eth_sendUserOperation", [])
    client.rpc("pm_getPaymasterData", [])
    assert [(u.split("?")[0], m) for u, m, _ in session.seen] == [(BUNDLER, "eth_sendUserOperation"),
                                                                  (PM, "pm_getPaymasterData")]


def test_errors_never_carry_the_url_or_its_key(aa_env):
    class Down:
        def post(self, url, **_):
            raise ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")

    aa_env.setenv("AA_BUNDLER_URL", "https://bundler.test/{chain_id}?key=secret")
    with pytest.raises(provider.AaError) as info:
        provider.AaClient(421614, session=Down()).rpc("eth_sendUserOperation", [])
    assert "secret" not in str(info.value) and "bundler.test" not in str(info.value)


def test_gas_price_uses_the_bundlers_quote_when_it_has_one(aa_env):
    fast = {"fast": {"maxFeePerGas": hex(30), "maxPriorityFeePerGas": hex(3)}}
    client, _ = _client(aa_env, {BUNDLER: {"pimlico_getUserOperationGasPrice": fast}})
    assert client.gas_price().as_rpc() == {"maxFeePerGas": "0x1e", "maxPriorityFeePerGas": "0x3"}
    client, _ = _client(aa_env, {BUNDLER: {"eth_getBlockByNumber": {"baseFeePerGas": hex(10)},
                                           "eth_maxPriorityFeePerGas": hex(2)}})
    price = client.gas_price()
    assert (price.max_fee_per_gas, price.max_priority_fee_per_gas) == (22, 2) and "base fee" in price.basis


def _quote_routes(paymaster=PAYMASTER):
    return {BUNDLER: {"pimlico_getUserOperationGasPrice": {"fast": {"maxFeePerGas": hex(10**7), "maxPriorityFeePerGas": "0x1"}},
                      "pimlico_getTokenQuotes": {"quotes": [{"token": USDC, "paymaster": paymaster,
                                                             "exchangeRate": hex(3_000 * 10**6), "postOpGas": hex(40_000)}]}}}


def test_a_quoting_paymaster_sizes_the_cap(aa_env):
    client, _ = _client(aa_env, _quote_routes())
    plan = client.fee_plan(USDC, 6)
    assert (plan.paymaster, plan.fee_cap, plan.basis) == (PAYMASTER, 42_300, "quote")
    assert plan.context == {"token": Web3.to_checksum_address(USDC)}


def test_a_quote_above_the_ceiling_or_from_another_paymaster_is_refused(aa_env):
    client, _ = _client(aa_env, _quote_routes(), AA_MAX_GAS_FEE="0.01")
    with pytest.raises(provider.AaError, match="above AA_MAX_GAS_FEE"):
        client.fee_plan(USDC, 6)
    client, _ = _client(aa_env, _quote_routes(), AA_PAYMASTER_ADDRESS="0x" + "99" * 20)
    with pytest.raises(provider.AaError, match="not the configured AA_PAYMASTER_ADDRESS"):
        client.fee_plan(USDC, 6)


def test_the_quote_must_include_the_token(aa_env):
    client, _ = _client(aa_env, {BUNDLER: {"pimlico_getTokenQuotes": {"quotes": []}}})
    with pytest.raises(provider.AaError, match="does not accept"):
        client.fee_plan(USDC, 6)


def test_a_paymaster_without_quotes_needs_an_address_and_a_ceiling(aa_env):
    client, _ = _client(aa_env, {BUNDLER: {}})
    with pytest.raises(provider.AaError, match="AA_PAYMASTER_ADDRESS and AA_MAX_GAS_FEE"):
        client.fee_plan(USDC, 6)
    client, _ = _client(aa_env, {BUNDLER: {}}, AA_PAYMASTER_ADDRESS=PAYMASTER, AA_MAX_GAS_FEE="0.25")
    plan = client.fee_plan(USDC, 6)
    assert (plan.paymaster, plan.fee_cap, plan.basis) == (PAYMASTER, 250_000, "ceiling")


# ------------------------------------------------------------------------ proxy

def _proxy(monkeypatch, plan, calls_seen):
    from fastapi.testclient import TestClient

    import server

    monkeypatch.setattr(server.graph, "get_state",
                        lambda config: types.SimpleNamespace(values={"plan": plan}))

    class Client:
        def __init__(self, chain_id):
            self.chain_id = chain_id

        def rpc(self, method, params):
            calls_seen.append(method)
            return {"ok": method}

        def gas_price(self):
            calls_seen.append("gas_price")
            return provider.GasPrice(30, 3, "test")

    monkeypatch.setattr(provider, "AaClient", Client)
    return TestClient(server.server_app)


def test_the_proxy_forwards_the_approved_batch(monkeypatch):
    seen = []
    client = _proxy(monkeypatch, {"aa": _approved().as_dict()}, seen)
    res = client.post("/api/aa/rpc", json={"thread_id": "t", "method": "eth_sendUserOperation",
                                           "params": [_op(), ENTRYPOINT_V07]}).json()
    assert res["result"] == {"ok": "eth_sendUserOperation"} and seen == ["eth_sendUserOperation"]


def test_the_proxy_refuses_a_swapped_call_before_the_bundler_sees_it(monkeypatch):
    seen = []
    client = _proxy(monkeypatch, {"aa": _approved().as_dict()}, seen)
    swapped = _op(callData=encode_batch(_calls()[:2] + [Call("0x" + "ee" * 20, 0, "0x")]))
    res = client.post("/api/aa/rpc", json={"thread_id": "t", "method": "eth_sendUserOperation",
                                           "params": [swapped, ENTRYPOINT_V07]}).json()
    assert "Refused" in res["error"]["message"] and seen == []


def test_the_proxy_requires_the_approved_paymaster_context(monkeypatch):
    seen = []
    approved = _approved()
    approved.paymaster_context = {"erc20": {"token": approved.token}, "policyId": "p1"}
    client = _proxy(monkeypatch, {"aa": approved.as_dict()}, seen)
    for context in ({"token": "0x" + "12" * 20}, {"erc20": {"token": approved.token}}):
        res = client.post("/api/aa/rpc", json={"thread_id": "t", "method": "pm_getPaymasterData",
                                               "params": [_op(), ENTRYPOINT_V07, "0x66eee", context]}).json()
        assert "approved gas token" in res["error"]["message"]
    assert seen == []
    res = client.post("/api/aa/rpc", json={"thread_id": "t", "method": "pm_getPaymasterData",
                                           "params": [_op(), ENTRYPOINT_V07, "0x66eee", approved.paymaster_context]}).json()
    assert res["result"] == {"ok": "pm_getPaymasterData"}


def test_the_proxy_answers_the_gas_price_itself(monkeypatch):
    seen = []
    client = _proxy(monkeypatch, {"aa": _approved().as_dict()}, seen)
    res = client.post("/api/aa/rpc", json={"thread_id": "t", "method": "omnibridge_getUserOperationGasPrice"}).json()
    assert res["result"] == {"maxFeePerGas": "0x1e", "maxPriorityFeePerGas": "0x3"} and seen == ["gas_price"]


def test_the_proxy_serves_nothing_for_a_run_without_a_batch_or_other_methods(monkeypatch):
    seen = []
    client = _proxy(monkeypatch, {"aa": None}, seen)
    assert "no approved smart-account batch" in client.post(
        "/api/aa/rpc", json={"thread_id": "t", "method": "eth_chainId"}).json()["error"]["message"]
    client = _proxy(monkeypatch, {"aa": _approved().as_dict()}, seen)
    for method in ("eth_sendRawTransaction", "pimlico_getUserOperationGasPrice"):
        res = client.post("/api/aa/rpc", json={"thread_id": "t", "method": method, "params": ["0x"]}).json()
        assert res["error"]["code"] == -32601
    assert seen == []


# --------------------------------------------------------------- on a live fork

@pytest.mark.network
def test_the_batch_runs_through_metamasks_delegator_on_a_fork():
    """
    What MetaMask's "Switch to smart account" leaves on chain, then the batch: approve and
    bridge in one call, exactly the amount burned, nothing left approved, and the same
    call from anyone else refused.
    """
    from omnibridge.cards.validation import default_web3_for
    from omnibridge.core.executor import CalldataExecutor
    from omnibridge.core.fork import ForkStep, anvil_available, anvil_fork, run_on_fork

    if not anvil_available():
        pytest.skip("anvil is not installed")
    live = default_web3_for()(421614)
    if live is None:
        pytest.skip("no Arbitrum Sepolia node reachable")
    sender = Web3.to_checksum_address("0x00000000000000000000000000000000000ab777")
    burn = CalldataExecutor.encode_dynamic_calldata(
        "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)",
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        [1_000_000, 6, CalldataExecutor.pad_bytes32_address(sender), USDC, b"\x00" * 32, 0, 2000])
    data = encode_batch([Call(USDC, 0, erc20.build_approve(TM, 1_000_000)), Call(TM, 0, burn)])

    with anvil_fork(live.provider.endpoint_uri, 421614) as w3:
        w3.provider.make_request("anvil_setBalance", [sender, hex(10**18)])
        w3.provider.make_request("anvil_dealERC20", [sender, USDC, hex(5_000_000)])
        w3.provider.make_request("anvil_setCode", [sender, "0x" + DESIGNATOR.hex()])
        assert account_status(w3, sender).smart_account

        ok = run_on_fork(w3, sender, [ForkStep(sender, data, label="batch")])
        assert ok[0].success, ok[0].revert_reason
        assert erc20.read_balance(w3, USDC, sender) == 4_000_000
        assert erc20.read_allowance(w3, USDC, sender, TM) == 0

        outsider = run_on_fork(w3, "0x000000000000000000000000000000000000dEaD", [ForkStep(sender, data, label="x")])
        assert not outsider[0].success


# ------------------------------------------------------- when the graph offers it

def _plan():
    from omnibridge.core.models import ExecutionPlan, TransactionStep

    return ExecutionPlan(steps=[
        TransactionStep(index=0, kind="approve", title="a", description="", to=USDC, data=erc20.build_approve(TM, 1_000_000),
                        chain_id=421614, token_address=USDC, spender_address=TM, amount_raw=1_000_000),
        TransactionStep(index=1, kind="bridge", title="b", description="", to=TM, data="0x8e0250ee",
                        chain_id=421614, amount_raw=1_000_000),
    ])


class _Sim:
    def __init__(self, w3, batch_ok=True):
        self.w3, self.batch_ok, self.batches = w3, batch_ok, []

    def connect_verified(self):
        return self.w3, "http://node"

    def simulate_batch_on_fork(self, sender, calls):
        from omnibridge.core.fork import ForkRun, ForkStepResult

        self.batches.append(calls)
        step = ForkStepResult("smart-account batch", self.batch_ok, 150_000, None if self.batch_ok else "execution reverted: boom")
        return ForkRun(chain_id=421614, fork_url="x", steps=[step]), None


def _offer(monkeypatch, sender_code=DESIGNATOR, balance=10_000_000, batch_ok=True, key=True):
    import graph

    for name in _AA_ENV:
        monkeypatch.delenv(name, raising=False)
    if key:
        monkeypatch.setenv("AA_BUNDLER_URL", "https://bundler.test/{chain_id}")

    class Client(provider.AaClient):
        def __init__(self, chain_id):
            super().__init__(chain_id, session=_Endpoint(_quote_routes()))

        def check_bundler(self):
            pass

    monkeypatch.setattr(provider, "AaClient", Client)
    monkeypatch.setattr(erc20, "read_balance", lambda w3, t, o: balance)
    monkeypatch.setattr(erc20, "read_decimals", lambda w3, t: 6)
    plan, sim = _plan(), _Sim(_w3(sender_code), batch_ok)
    log = types.SimpleNamespace(value=lambda *a, **k: None, ok=lambda *a, **k: None,
                                timed=lambda *a, **k: __import__("contextlib").nullcontext())
    graph._prepare_smart_account(plan, sim, {"user_address": SENDER}, types.SimpleNamespace(chain_id=421614), log)
    return plan, sim


def test_a_switched_account_with_a_key_gets_the_one_signature_batch(monkeypatch):
    plan, sim = _offer(monkeypatch)
    assert plan.aa is not None and plan.aa_unavailable is None
    assert [c["to"] for c in plan.aa["calls"]] == [Web3.to_checksum_address(USDC)] * 2 + [TM]
    # (940k gas) * 0.015 gwei = 1.41e13 wei = 0.0423 USDC at 3,000 USDC/ETH.
    assert plan.aa["fee_cap"] == 42_300 and plan.aa["fee_cap_display"] == "0.0423"
    assert len(sim.batches) == 1, "the exact batch was fork-simulated"


@pytest.mark.parametrize("kwargs, reason", [
    (dict(key=False), "no bundler is configured"),
    (dict(sender_code=b""), "not switched to a smart account"),
    (dict(balance=1_000_000), "plus up to"),
    (dict(batch_ok=False), "reverted on a fork"),
])
def test_otherwise_it_falls_back_and_says_why(monkeypatch, kwargs, reason):
    plan, _ = _offer(monkeypatch, **kwargs)
    assert plan.aa is None and reason in plan.aa_unavailable
