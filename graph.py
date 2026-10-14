"""
LangGraph State Machine for Headless Web3 In-Transit Bridging Engine with Full Observability Telemetry.
"""

from typing import Dict, Any, Optional, TypedDict
import os
import re
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

from omnibridge.core.models import UserIntent, RouteQuote
from omnibridge.skills.manager import LOCAL_SKILLS_DIR, SkillManager, SkillMetadata
from omnibridge.discovery.rpc_manager import RPCManager
from omnibridge.discovery.route_finder import RouteFinder
from omnibridge.core.executor import CalldataExecutor
from omnibridge.core.simulator import Simulator, is_evm_address
from omnibridge.core.models import SimulationResult, SimulationStatus
from omnibridge.core.signer import Signer
from omnibridge.agent.contract import format_chain_ref
from omnibridge.cards.manager import CardManager


class GraphState(TypedDict):
    raw_input: str
    source_chain: str
    target_chain: str
    amount: float
    asset: str
    user_address: str
    # Where the bridged funds land. None means the sender, which is the overwhelmingly
    # common case and the only one that used to be expressible. Kept distinct from
    # user_address because they are different roles: one signs and pays, the other
    # receives, and conflating them is what made a third-party recipient impossible.
    recipient: Optional[str]
    slippage: str
    route: Optional[Dict[str, Any]]
    active_rpc: Optional[str]
    unsigned_transaction_hex: Optional[str]
    simulation_result: Optional[Dict[str, Any]]
    estimated_gas_usd: float
    status: str
    signed_transaction_hex: Optional[str]
    tx_hash: Optional[str]
    skill_path: Optional[str]
    # Why no skill was written, when the transfer itself succeeded. Kept separate from
    # `error`, which is about the transfer.
    skill_save_error: Optional[str]
    error: Optional[str]
    traces: Dict[str, Any]
    # Retry edge (plan 3.4): a revert is feedback, not a terminus.
    retry_count: int
    failure_context: Optional[str]
    budget: Optional[Dict[str, Any]]
    agent_evidence: Optional[Dict[str, Any]]
    # Set when this run replayed a cached skill, so a failure can demote that skill
    # rather than a fresh discovery silently overwriting it.
    replayed_skill: Optional[str]
    # The ordered transactions this transfer needs. A single-transaction
    # transfer is a plan of length one, so there is no second code path.
    plan: Optional[Dict[str, Any]]
    # What the preflight funding gate read off chain before discovery ran. Carries
    # UNCHECKED as a distinct outcome, so "could not read the balance" is never
    # rendered as "the balance is fine".
    funding: Optional[Dict[str, Any]]
    # The protocol card row this route's contract came from, matched in code on the
    # router address. Write-back uses it; the model's say-so is never consulted.
    card_match: Optional[Dict[str, Any]]
    # "mainnet" or "testnet". Every chain in the run is resolved on this family, and
    # resolution refuses to cross it; skills and cards are scoped to it as well.
    network: Optional[str]
    # The simulation failed on an Anvil fork *after* the plan's approval was applied
    # there, so an allowance revert means a wrong spender, not a missing approval.
    fork_failure: Optional[bool]


# Bounded by plan 3.4. Three attempts, then the run ends.
MAX_DISCOVERY_RETRIES = int(os.environ.get("OMNIBRIDGE_MAX_RETRIES", "3"))

rpc_mgr = RPCManager()
skill_mgr = SkillManager(repository_path=str(LOCAL_SKILLS_DIR))
finder = RouteFinder(skill_manager=skill_mgr, rpc_manager=rpc_mgr)
card_mgr = CardManager()


def _card_authority_enabled() -> bool:
    """A verified card row may stand in for the documentation source. Off by default."""
    return os.environ.get("OMNIBRIDGE_CARD_AUTHORITY", "").strip().lower() in ("1", "true", "yes")


def _agent_tools_disabled() -> bool:
    return os.environ.get("OMNIBRIDGE_DISABLE_AGENT_TOOLS", "").strip().lower() in ("1", "true", "yes")


def _network(state: Dict[str, Any]) -> str:
    """The run's network family. Mainnet unless the request said otherwise."""
    return state.get("network") or "mainnet"


def _superseded_skill(skill: Any) -> Optional[str]:
    """
    The newer card that supersedes the protocol version a cached skill routes through.

    "Always use the latest version": a verified skill for an older version is not a
    reason to keep using it. Matched on the skill's router address, like any route.
    """
    if skill is None:
        return None
    from omnibridge.cards.writeback import match_route

    match = match_route(card_mgr, skill.metadata.source_chain_id,
                        skill.metadata.router_address, None, None)
    return match.superseded_by if match else None


def _dest_chain_id(name: str, network: str = "mainnet") -> Optional[int]:
    try:
        return rpc_mgr.discover_chain_metadata(name, network=network).chain_id
    except Exception:
        return None


# --- NODE 1: Intent & Orchestrator Parser ---
def intent_parser_node(state: GraphState) -> Dict[str, Any]:
    raw = state.get("raw_input", "")
    # Extract entities dynamically from natural language or structured payload
    src = state.get("source_chain") or ""
    dst = state.get("target_chain") or ""
    amt = float(state.get("amount") or 0.0)
    tok = state.get("asset") or ""
    # No zero-address default. It reads as a valid EVM address everywhere downstream,
    # so a missing wallet would simulate from 0x0 and encode 0x0 as the recipient —
    # a burn, dressed as a transfer.
    user_addr = (state.get("user_address") or "").strip()
    if not is_evm_address(user_addr):
        raise ValueError(
            f"Intent Error: no valid sender address supplied (got {user_addr!r}). "
            "Connect a wallet — simulating from any other account would not describe "
            "this transfer."
        )
    slip = state.get("slippage") or "0.5%"
    # Refused, not defaulted: a typo'd network silently becoming mainnet would resolve
    # every chain in the run on the network holding real funds.
    if _network(state) not in ("mainnet", "testnet"):
        raise ValueError(
            f"Intent Error: network must be 'mainnet' or 'testnet', not {state.get('network')!r}."
        )

    # Optional, and validated as strictly as the sender. An unparseable recipient is
    # refused rather than quietly falling back to the sender: someone who names a
    # destination and gets their own address instead has been ignored on the one
    # decision that says where the money goes.
    recipient = (state.get("recipient") or "").strip()
    if recipient and not is_evm_address(recipient):
        raise ValueError(
            f"Intent Error: recipient {recipient!r} is not a valid EVM address. "
            "Supply a checksummed 0x address, or leave it empty to send to yourself."
        )

    if raw:
        # The verb set is widened beyond the literal word "bridge". Where no destination
        # asset is named, swap/send/move/transfer all denote the same operation.
        match = re.search(
            r"(?:bridge|swap|send|move|transfer)\s+(\d+(?:\.\d+)?)\s*([a-zA-Z0-9_-]+)?"
            r"\s+from\s+([a-zA-Z0-9_-]+)\s+to\s+([a-zA-Z0-9_-]+)",
            raw,
            re.IGNORECASE,
        )
        if match:
            amt = float(match.group(1))
            if match.group(2):
                tok = match.group(2).upper()
            # "arbitrum-sepolia" is how a two-word chain name survives a one-word
            # pattern. Resolved as "arbitrum sepolia": the registry's names use spaces,
            # and a hyphen matched nothing, so the testnet was unreachable by name.
            src = re.sub(r"[-_]+", " ", match.group(3)).strip()
            dst = re.sub(r"[-_]+", " ", match.group(4)).strip()

        # Only ever an explicit 0x address behind an explicit word. The chain regex above
        # already owns "to <name>", so nothing here can turn a destination chain into a
        # recipient, and a bare address floating in the sentence is not enough.
        if not recipient:
            named = re.search(
                r"(?:recipient|receiver|beneficiary|payable to|send(?:ing)?\s+to|"
                r"to\s+(?:address|wallet))\s+:?\s*(0x[a-fA-F0-9]{40})",
                raw,
                re.IGNORECASE,
            )
            if named:
                recipient = named.group(1)

    # No defaults block. This previously substituted "Unknown"/"Unknown"/1.0 NATIVE for
    # anything it could not parse and then reported status "PARSED" / "COMPLETED", so
    # "swap 100 usdc from base to stellar" silently became "1.0 NATIVE Unknown -> Unknown".
    # There is no such thing as a partially understood instruction to move money.
    missing = [
        label
        for label, value in (
            ("source chain", src),
            ("destination chain", dst),
            ("asset", tok),
        )
        if not value
    ]
    if missing or amt <= 0:
        if amt <= 0:
            missing.append("amount")
        raise ValueError(
            "Intent Error: could not determine "
            + ", ".join(missing)
            + f" from input {raw!r}. Expected something like "
            "'bridge 100 USDC from base to arbitrum', or supply the fields directly."
        )

    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    _console.print(Panel(
        f"[bold white]Raw Input:[/bold white] \"{raw}\"\n"
        f"[bold white]Source Chain:[/bold white] {src}\n"
        f"[bold white]Target Chain:[/bold white] {dst}\n"
        f"[bold white]Amount & Asset:[/bold white] {amt} {tok}\n"
        f"[bold white]Sender / Signer:[/bold white] {user_addr}\n"
        f"[bold white]Recipient:[/bold white] "
        f"{recipient + ' (NOT the sender)' if recipient and recipient.lower() != user_addr.lower() else user_addr}\n"
        f"[bold white]Slippage Tolerance:[/bold white] {slip}",
        title="[bold cyan]🧠 [STEP 1: INTENT & PARAMETER EXTRACTION][/bold cyan]",
        border_style="cyan"
    ))

    traces = state.get("traces", {})
    traces["step_1_intent"] = {
        "title": "Intent & Parameter Extraction",
        "raw_input": raw,
        "parsed_parameters": {
            "source_chain": src,
            "target_chain": dst,
            "amount": amt,
            "asset": tok,
            "user_address": user_addr,
            "recipient": recipient or user_addr,
            "recipient_is_sender": not recipient or recipient.lower() == user_addr.lower(),
            "slippage": slip,
        },
        "status": "COMPLETED",
    }

    return {
        "source_chain": src,
        "target_chain": dst,
        "amount": amt,
        "asset": tok,
        "user_address": user_addr,
        "recipient": recipient or None,
        "slippage": slip,
        "status": "PARSED",
        "traces": traces,
    }


# --- NODE 1.5: Preflight Funding Gate ---
def preflight_node(state: GraphState) -> Dict[str, Any]:
    """
    Read the wallet before spending anything on its behalf.

    Discovery calls a paid model. A wallet that cannot fund the transfer cannot send
    the result of that call, whatever the result is, so the question is worth asking
    first — it needs no route, no protocol and no calldata.

    Both balances that can stop a run are read: the sending asset, which is what the
    user actually asked about, and the native balance, because gas is payable only in
    the chain's own currency.

    The gate refuses on INSUFFICIENT only. UNCHECKED continues, because an unreachable
    node — or a symbol no public token list carries — is not evidence that a wallet is
    empty; it is recorded as unchecked so nothing downstream can read it as a pass.
    """
    from omnibridge.core.balance import FundingStatus, preflight
    from omnibridge.discovery.rpc_manager import ChainResolutionError
    from omnibridge.steplog import StepLogger

    log = StepLogger("preflight")
    src_name = state["source_chain"]
    sender = state["user_address"]
    traces = state.get("traces", {})

    # Chain resolution used to happen first inside discovery, where a failure escaped
    # the graph as an uncaught exception. It runs here now, so an unresolvable chain
    # ends the run as a recorded refusal like every other honest "no".
    try:
        with log.timed(f"resolve chain '{src_name}'"):
            src_info = rpc_mgr.discover_chain_metadata(src_name, network=_network(state))
    except ChainResolutionError as exc:
        log.fail(str(exc))
        traces["step_1b_preflight"] = {
            "title": "Preflight Funding Check",
            "outcome": "UNRESOLVED_CHAIN",
            "reason": str(exc),
            "status": "FAILED",
        }
        return {"status": "UNRESOLVED_CHAIN", "error": str(exc), "traces": traces}

    try:
        rpc_url = rpc_mgr.get_fastest_rpc_sync(src_name, network=_network(state))
    except Exception as exc:
        # Not fatal. No balance can be read, which the report states as UNCHECKED.
        log.warn(f"no reachable RPC for {src_name}: {exc}")
        rpc_url = None

    w3 = _verified_web3(rpc_url, src_info.chain_id)
    report = preflight(
        w3,
        sender,
        native_symbol=src_info.native_symbol,
        chain_name=src_info.name,
        token_symbol=state["asset"],
        amount=float(state["amount"]),
        chain_id=src_info.chain_id,
    )

    for line in report.checked:
        log.value("read", line)
    for line in report.deferred:
        log.warn(line)

    traces["step_1b_preflight"] = {
        "title": "Preflight Funding Check",
        "method": (
            "eth_getBalance for gas, plus balanceOf on the sending token, against an "
            "identity-checked node. The token address comes from a public token list "
            "and is corroborated by the contract's own symbol() before its balance is "
            "believed — it is read-only and never reaches calldata."
        ),
        "scope": (
            "Source chain only, and funding only. Says nothing about whether a route "
            "exists or whether the call would succeed; the simulator gates that. The "
            "token the route actually approves is re-checked in build_plan."
        ),
        "funding": report.as_dict(),
        "status": "COMPLETED" if report.ok else "FAILED",
    }

    if report.status is FundingStatus.INSUFFICIENT:
        log.fail(report.reason or "insufficient funds")
        return {
            "status": "INSUFFICIENT_FUNDS",
            "error": report.reason,
            "funding": report.as_dict(),
            "active_rpc": rpc_url,
            "traces": traces,
        }

    log.ok(f"funding {report.status.value.lower()} — continuing to discovery")
    return {
        "status": "FUNDING_CHECKED",
        "funding": report.as_dict(),
        "active_rpc": rpc_url,
        "traces": traces,
    }


def route_after_preflight(state: GraphState) -> str:
    """
    An empty wallet ends the run before discovery is paid for.

    Not retried: `route_after_error` only retries when a `failure_context` was set, and
    nothing about a balance improves by asking a model again.
    """
    if state.get("status") in ("INSUFFICIENT_FUNDS", "UNRESOLVED_CHAIN"):
        return "error_node"
    return "discovery_node"


# --- NODE 2: Skill Replay or Agent Discovery ---
def discovery_node(state: GraphState) -> Dict[str, Any]:
    from rich.console import Console
    from rich.panel import Panel

    from omnibridge.discovery.route_finder import NoCachedRoute

    _console = Console()

    intent = UserIntent(
        token=state["asset"],
        amount=state["amount"],
        source_chain=state["source_chain"],
        dest_chain=state["target_chain"],
        recipient=state.get("recipient") or state["user_address"],
        network=_network(state),
    )

    # Tier 0: a verified skill short-circuits discovery entirely.
    # Tier 1: the reasoning core, unless the kill switch forces the cache-only path.
    agent_result = _run_discovery_agent(state, intent)
    if agent_result is not None:
        return agent_result

    cached = skill_mgr.find_skill(intent.source_chain, intent.dest_chain, intent.token,
                                  network=intent.network)
    newer = _superseded_skill(cached)
    if newer:
        reason = (
            f"The cached route for this transfer uses a protocol version that card {newer} "
            "supersedes, and the latest version is always used. No reasoning core is "
            "available to establish the route on the latest version."
        )
        traces = state.get("traces", {})
        traces["step_2_discovery"] = {
            "title": "Route & Contract Discovery", "source_type": "SKILL_MEMORY_CACHE",
            "outcome": "NO_ROUTE_FOUND", "reason": reason, "status": "COMPLETED",
        }
        return {"status": "NO_ROUTE_FOUND", "error": reason, "traces": traces}

    # Cache-only. There is no third tier: the web-scraping fallback that used to sit
    # here returned the first hex string it found on a search page as the router
    # address, and this node then printed it as a "Parsed Protocol". A route this
    # system has not verified is a route it does not have.
    #
    # "No route" is a correct answer, so it terminates as one — a recorded state with
    # its traces intact, rather than an exception thrown out of the graph. It is still
    # a refusal: NO_ROUTE_FOUND has no edge to the approval gate, and route_after_error
    # never retries it.
    try:
        quote = finder.discover_route(intent)
    except NoCachedRoute as exc:
        traces = state.get("traces", {})
        traces["step_2_discovery"] = {
            "title": "Route & Contract Discovery",
            "source_type": "SKILL_MEMORY_CACHE",
            "outcome": "NO_ROUTE_FOUND",
            "reason": str(exc),
            "status": "COMPLETED",
        }
        _console.print(Panel(
            f"[bold yellow]{exc}[/bold yellow]\n\n"
            "[dim]No verified route, and no reasoning core available to find one — set "
            "GEMINI_API_KEY or ANTHROPIC_API_KEY, or unset OMNIBRIDGE_DISABLE_AGENT.[/dim]",
            title="[bold yellow]📋 [STEP 2: NO ROUTE FOUND][/bold yellow]",
            border_style="yellow",
        ))
        return {
            "status": "NO_ROUTE_FOUND",
            "error": str(exc),
            "traces": traces,
        }
    fastest_rpc = rpc_mgr.get_fastest_rpc_sync(state["source_chain"], network=_network(state))
    skill_name = quote.extra_params.get("skill_name")

    _console.print(Panel(
        f"[bold green]Protocol:[/bold green] {quote.bridge_name}\n"
        f"[bold green]Router Contract:[/bold green] [white]`{quote.router_address}`[/white]\n"
        f"[bold green]Function:[/bold green] {quote.extra_params.get('function_signature')}\n"
        f"[bold green]Active Selected RPC:[/bold green] [cyan]{fastest_rpc}[/cyan]\n"
        f"[bold green]Source Chain ID:[/bold green] {quote.source_chain_id}\n"
        f"[bold green]Destination:[/bold green] "
        f"{format_chain_ref(quote.extra_params.get('dest_chain_ref')) or quote.dest_chain_id}\n"
        f"[dim]Replayed from skill memory. It still has to clear simulation — a skill is a "
        f"shortcut past discovery, never past the guardrails.[/dim]",
        title="[bold green]📋 [STEP 2: CACHED ROUTE REPLAYED FROM SKILL MEMORY][/bold green]",
        border_style="green"
    ))

    traces = state.get("traces", {})
    traces["step_2_discovery"] = {
        "title": "Route & Contract Discovery",
        "source_type": "SKILL_MEMORY_CACHE",
        "skill_name": skill_name,
        "skill_path": quote.extra_params.get("skill_path"),
        "discovered_route": quote.bridge_name,
        "router_contract": quote.router_address,
        "function_signature": quote.extra_params.get("function_signature"),
        "active_rpc": fastest_rpc,
        "status": "COMPLETED",
    }

    return {
        "route": {
            "source": state["source_chain"],
            "target": state["target_chain"],
            "bridge": quote.bridge_name,
            "router_address": quote.router_address,
            "quote_obj": quote.model_dump(),
        },
        "active_rpc": fastest_rpc,
        "replayed_skill": skill_name,
        "status": "ROUTE_DISCOVERED",
        "traces": traces,
    }


# Substrings that mark a credentials or account problem rather than a routing one.
# Matched on the message because the SDK raises the same exception class for both.
_CONFIG_ERROR_MARKERS = (
    "anthropic-workspace-id",
    "authentication_error",
    "invalid x-api-key",
    "permission_error",
    "credit balance",
    "rate_limit_error",
    # A 404 from a model endpoint means the model name is wrong, not that no bridge
    # exists. It reached the page reserved for routing answers once already.
    "404",
    "not found",
    "not_found",
    "is not supported",
    "unsupported model",
)


def _is_configuration_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _CONFIG_ERROR_MARKERS)


def _run_discovery_agent(state: GraphState, intent: UserIntent) -> Optional[Dict[str, Any]]:
    """
    Tier 1 discovery: let the model establish the route, then check and encode it here.

    Returns None to fall through to the static path — when the kill switch is set, when
    no credentials are configured, or when a verified skill already covers this route.
    Raises when the agent runs but its proposal cannot be trusted, which the retry edge
    turns into another attempt.
    """
    from omnibridge.agent.budget import RequestBudget
    from omnibridge.agent.contract import NoRouteFound, ProviderUnavailable, agent_disabled
    from omnibridge.agent.provider import available_providers, get_provider
    from omnibridge.agent.synthesis import synthesize

    if agent_disabled():
        return None
    cached = skill_mgr.find_skill(intent.source_chain, intent.dest_chain, intent.token,
                                  network=intent.network)
    if cached is not None and not _superseded_skill(cached):
        return None

    # No provider configured is not an error — it is the cache-only mode, and the
    # cached path below reports its own honest "no route". A provider that is selected
    # but broken *is* an error, and says so.
    if not any(cls.configured() for cls in available_providers().values()):
        return None
    try:
        agent = get_provider()
    except ProviderUnavailable as exc:
        traces = state.get("traces", {})
        traces["step_2_discovery"] = {
            "title": "Route & Contract Discovery (agent)",
            "source_type": "AGENT_CONFIG_ERROR",
            "outcome": "CONFIG_ERROR",
            "reason": str(exc),
            "status": "FAILED",
        }
        return {"status": "CONFIG_ERROR", "error": str(exc), "traces": traces}

    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    from omnibridge.steplog import StepLogger

    log = StepLogger("discovery")
    log.step(f"provider [bold]{agent.name}[/bold] selected")
    log.value("allowed doc domains", f"{len(agent.domains)}")

    with log.timed(f"resolve chain '{intent.source_chain}'"):
        src_info = rpc_mgr.discover_chain_metadata(intent.source_chain, network=intent.network)
    log.value("resolved to", f"{src_info.name} (chain id {src_info.chain_id})")

    with log.timed(f"race {len(src_info.rpc_urls)} RPC endpoint(s)"):
        fastest_rpc = rpc_mgr.get_fastest_rpc_sync(intent.source_chain, network=intent.network)
    log.value("fastest RPC", fastest_rpc)

    # Counters carry across retries, not just the limits — see RequestBudget.restore.
    budget = RequestBudget.restore(state.get("budget"))
    agent_trace: Dict[str, Any] = {}

    # Stage 2: relevant protocol cards go into the question as hypotheses, and the agent
    # gets its toolkit so it can check them — and its own research — before proposing.
    from omnibridge.cards.context import render_for_agent, select_cards

    dest_chain_id = _dest_chain_id(state["target_chain"], intent.network)
    cards = select_cards(card_mgr, intent.token, src_info.chain_id, intent.source_chain,
                         state["target_chain"], dest_chain_id, network=intent.network)
    cards_context = render_for_agent(cards)
    log.value("protocol cards offered", ", ".join(c.protocol_id for c in cards) or "none")

    toolkit = None
    if not _agent_tools_disabled():
        from omnibridge.agent.tools import AgentToolkit, ToolContext
        from omnibridge.cards.validation import default_web3_for

        rpc_candidates = [fastest_rpc] + [u for u in src_info.rpc_urls if u != fastest_rpc]
        toolkit = AgentToolkit(ToolContext(
            sender=state["user_address"],
            source_chain_id=src_info.chain_id,
            simulator=Simulator(rpc_candidates, expected_chain_id=src_info.chain_id),
            cards=card_mgr,
            web3_for=default_web3_for(),
            domains=agent.domains,
            network_for=rpc_mgr.network_of,
        ))

    import inspect

    # Providers written before cards take neither argument; they are passed only to a
    # provider that declares them, so an older provider keeps working unchanged.
    accepted = inspect.signature(agent.discover).parameters
    extra: Dict[str, Any] = {}
    if "cards_context" in accepted:
        extra["cards_context"] = cards_context
    if "tools" in accepted:
        extra["tools"] = toolkit

    try:
        proposal = agent.discover(
            intent,
            src_info,
            state["target_chain"],
            budget,
            failure_context=state.get("failure_context"),
            trace=agent_trace,
            **extra,
        )
    except NoRouteFound as exc:
        # A correct answer, not a failure: terminates, and is never retried.
        traces = state.get("traces", {})
        traces["step_2_discovery"] = {
            "title": "Route & Contract Discovery (agent)",
            "source_type": "AGENT_NO_ROUTE",
            "outcome": "NO_ROUTE_FOUND",
            "reason": exc.reason,
            "sources": exc.sources,
            "budget": budget.as_dict(),
            "status": "COMPLETED",
        }
        # Terminal, and never retried: another attempt spends money to reach the same
        # answer. Returned as a state so the reasoning and the sources survive.
        return {
            "status": "NO_ROUTE_FOUND",
            "error": exc.reason,
            "budget": budget.as_dict(),
            "traces": traces,
        }
    except Exception as exc:
        # A misconfigured client is not a routing answer. Reported as CONFIG_ERROR so
        # the page says "fix your credentials" rather than "no bridge exists", which is
        # what a bare exception message looked like from the frontend.
        detail = str(exc)
        if isinstance(exc, ProviderUnavailable) or _is_configuration_error(detail):
            traces = state.get("traces", {})
            traces["step_2_discovery"] = {
                "title": "Route & Contract Discovery (agent)",
                "source_type": "AGENT_CONFIG_ERROR",
                "outcome": "CONFIG_ERROR",
                "reason": detail,
                "status": "FAILED",
            }
            return {
                "status": "CONFIG_ERROR",
                "error": detail,
                "traces": traces,
            }
        raise

    w3 = None
    try:
        from web3 import Web3

        candidate = Web3(Web3.HTTPProvider(fastest_rpc, request_kwargs={"timeout": 15}))
        if candidate.is_connected() and int(candidate.eth.chain_id) == src_info.chain_id:
            w3 = candidate
            log.ok(f"RPC identity confirmed as chain {src_info.chain_id}")
        else:
            log.warn("RPC did not confirm the expected chain id")
    except Exception as exc:
        w3 = None
        log.warn(f"no usable RPC for provenance checks: {exc}")
    if w3 is None:
        # Said plainly: without a node the bytecode check cannot run, and the proposal
        # is accepted on the model's word until the simulator gets a look at it.
        log.warn("skipping on-chain provenance — router bytecode will NOT be checked here")

    from omnibridge.agent.synthesis import ProposalRejected
    from omnibridge.cards.writeback import match_route

    match = match_route(card_mgr, src_info.chain_id, proposal.router_address,
                        dest_chain_id, proposal.dest_chain_ref)
    card_match = match.as_dict() if match else None
    if match:
        log.value("card row matched", f"{match.protocol_id}:{match.source_chain_id} ({match.source_status})")

    log.step("checking the proposal (Rule A boundary — the model stops choosing here)")
    try:
        if match and match.superseded_by:
            # The latest version is always used. A verified route on an older version is
            # still an older version.
            raise ProposalRejected(
                f"The router is on card {match.protocol_id} ({match.protocol_version}), which "
                f"card {match.superseded_by} supersedes. Always use the protocol's latest "
                f"version: establish the route from {match.superseded_by}."
            )
        if match and match.dest_agrees is False and match.card_dest_confirmed:
            # The plan's own example failure: a destination named by its EIP-155 id where
            # the protocol wants its own identifier. A broadcast confirmed the card's
            # value, so a different one is refused here rather than burned on chain.
            raise ProposalRejected(
                f"The proposal identifies the destination as {match.dest_id}, but card "
                f"{match.protocol_id} records {match.card_dest_id} for chain "
                f"{match.dest_chain_id}, confirmed by a broadcast."
            )
        with log.timed("shape, provenance and encoding"):
            calldata, quote, evidence = synthesize(
                proposal, intent, src_info, w3,
                allowed_domains=agent.domains,
                retrieved=getattr(proposal, "retrieved", None) or None,
                # Passed so synthesis can tell a third-party recipient from the default.
                sender=state["user_address"],
                card_authority=card_match if (
                    match and match.source_status == "verified" and _card_authority_enabled()
                ) else None,
            )
    except ProposalRejected as exc:
        # Feedback, not a crash. It used to escape the graph as an exception, so a
        # proposal the checks refused ended the run instead of informing the next try.
        log.fail(f"proposal refused: {exc}")
        traces = state.get("traces", {})
        traces["step_2_discovery"] = {
            "title": "Route & Contract Discovery (agent)",
            "source_type": "AGENT_RUNTIME_DISCOVERY",
            "outcome": "PROPOSAL_REJECTED",
            "reason": str(exc),
            "proposal": proposal.as_dict(),
            "card_match": card_match,
            "cards_offered": [c.protocol_id for c in cards],
            "agent_tools": toolkit.summary() if toolkit else None,
            "budget": budget.as_dict(),
            "status": "FAILED",
        }
        return {
            "status": "PROPOSAL_REJECTED",
            "error": str(exc),
            "failure_context": (
                f"Your proposal ({proposal.protocol}, router {proposal.router_address}, "
                f"{proposal.function_signature}) was refused before simulation: {exc}"
            ),
            "budget": budget.as_dict(),
            "card_match": card_match,
            "retry_count": state.get("retry_count", 0) + 1,
            "traces": traces,
        }
    evidence["card_match"] = card_match

    prov = evidence.get("provenance") or {}
    if prov.get("skipped"):
        log.warn(f"provenance skipped: {prov['skipped']}")
    else:
        log.value("router holds bytecode", f"{prov.get('is_contract')} ({prov.get('bytecode_size')} bytes)")
        if prov.get("implementation_address"):
            log.value("EIP-1967 implementation", prov["implementation_address"])
        log.value("selector present in bytecode", prov.get("selector_present"))
        log.value("documentation sources", ", ".join(prov.get("primary_domains") or []) or "none")
        log.value("explorer sources", ", ".join(prov.get("secondary_domains") or []) or "none")
        for note in prov.get("notes") or []:
            log.warn(note)
    # Where the money lands is worth a line of its own in the trail.
    if evidence.get("recipient_is_third_party"):
        log.warn(
            f"recipient is NOT the sender: {evidence.get('recipient_checked')} "
            "— confirmed present in the calldata"
        )
    elif not evidence.get("recipient_encoded"):
        log.warn(
            "the recipient could not be located in the calldata; the destination rests "
            "on the model's encoding alone"
        )
    log.value("selector recomputed", evidence.get("selector_recomputed"))
    log.ok(f"calldata built, {evidence.get('calldata_bytes')} bytes")

    _console.print(Panel(
        f"[bold green]Protocol:[/bold green] {proposal.protocol}  [dim](agent, {budget.model_calls} model call(s))[/dim]\n"
        f"[bold green]Router:[/bold green] [white]`{quote.router_address}`[/white]\n"
        f"[bold green]Function:[/bold green] {proposal.function_signature}\n"
        f"[bold green]Destination ref:[/bold green] {proposal.dest_chain_ref}\n"
        f"[bold green]Sources:[/bold green] {', '.join(proposal.sources)}\n"
        f"[bold green]Provenance:[/bold green] {'✓ verified on chain' if w3 is not None else '⚠ not checked (no usable RPC)'}\n"
        f"[bold green]Rationale:[/bold green] {proposal.rationale}",
        title="[bold green]📋 [STEP 2: AGENT-DISCOVERED ROUTE][/bold green]",
        border_style="green",
    ))

    traces = state.get("traces", {})
    traces["step_2_discovery"] = {
        "title": "Route & Contract Discovery (agent)",
        "source_type": "AGENT_RUNTIME_DISCOVERY",
        "provider": agent.name,
        "model": agent.model,
        "agent_turns": agent_trace.get("turns", []),
        "evidence": evidence,
        "cards_offered": [c.protocol_id for c in cards],
        "agent_tools": toolkit.summary() if toolkit else None,
        "budget": budget.as_dict(),
        "active_rpc": fastest_rpc,
        "status": "COMPLETED",
    }

    return {
        "route": {
            "bridge": quote.bridge_name,
            "router_address": quote.router_address,
            "source_chain_id": quote.source_chain_id,
            "dest_chain_id": quote.dest_chain_id,
            "quote_obj": quote.model_dump(),
        },
        "active_rpc": fastest_rpc,
        "unsigned_transaction_hex": calldata,
        "status": "ROUTE_DISCOVERED",
        "budget": budget.as_dict(),
        "agent_evidence": evidence,
        "card_match": card_match,
        "traces": traces,
    }


def route_after_discovery(state: GraphState) -> str:
    """
    NO_ROUTE_FOUND and CONFIG_ERROR both end the run.

    Nothing downstream improves on either: the first is a correct answer, and the
    second will fail identically on every retry against the same credentials.
    """
    if state.get("status") in ("NO_ROUTE_FOUND", "CONFIG_ERROR", "MISSING_PREREQUISITE"):
        return "error_node"
    if state.get("status") == "PROPOSAL_REJECTED":
        # Straight back to discovery with the refusal as context, within the same
        # retry bound as a revert. The budget's attempt counter bounds it as well.
        if state.get("retry_count", 0) < MAX_DISCOVERY_RETRIES:
            return "discovery_node"
        return "error_node"
    return "code_builder_node"


# Reverts that say a prerequisite is missing, not that the route is wrong.
#
# An allowance revert means the router was never approved to move the token — a step
# this pipeline does not yet perform at all. Retrying it sends a correct route back to
# discovery with "do not repeat the same router and arguments", so the only way for the
# model to comply is to invent a different answer. Observed doing exactly that:
# V1 -> V2 -> V1, with a freshly confabulated reason each time, because the model cannot
# observe allowance state and we asked it to explain a failure it had no access to.
_PREREQUISITE_REVERTS = (
    "exceeds allowance",
    "insufficient allowance",
    "transfer amount exceeds balance",
    "insufficient balance",
    "erc20: transfer amount exceeds",
)


def _missing_prerequisite(reason: str) -> Optional[str]:
    """Name the missing step a revert points at, or None if it is a routing failure."""
    lowered = (reason or "").lower()
    if any(marker in lowered for marker in ("exceeds allowance", "insufficient allowance")):
        return (
            "The router has no allowance to move this token. An ERC-20 bridge needs two "
            "transactions — approve(router, amount) on the token contract, then the bridge "
            "call — and this pipeline only builds the second. The route itself simulated as "
            "far as the transfer, so it is very likely correct."
        )
    if any(marker in lowered for marker in ("exceeds balance", "insufficient balance")):
        return (
            "The sender does not hold enough of this token. Nothing about the route is "
            "wrong; retrying would reach the same result."
        )
    return None


def route_after_code_builder(state: GraphState) -> str:
    """
    A plan that could not be built must not be simulated anyway.

    `build_plan` raises PrerequisiteUnsatisfiable when the sender cannot cover the
    transfer, and code_builder_node turns that into MISSING_PREREQUISITE *without a
    plan*. The edge into the simulator used to be unconditional, so the simulator found
    no plan, fell back to simulating the bare bridge calldata — the one call the planner
    documents as uncheckable before its approval lands — and reported
    `ERC20: transfer amount exceeds allowance`. The balance diagnosis was overwritten by
    a revert caused by the missing approval the plan existed to add.
    """
    if state.get("status") == "MISSING_PREREQUISITE":
        return "error_node"
    return "simulator_node"


def route_after_error(state: GraphState) -> str:
    """
    A revert is the highest-quality signal this system produces. Feed it back.

    Bounded at three attempts, and only for failures another attempt could plausibly fix
    — a NO_ROUTE_FOUND or a budget exhaustion is not retried, because retrying it just
    spends money to reach the same answer.
    """
    if not state.get("failure_context"):
        return END
    if state.get("retry_count", 0) >= MAX_DISCOVERY_RETRIES:
        return END
    return "discovery_node"


def _verified_web3(rpc_url: Optional[str], expected_chain_id: int):
    """A Web3 whose node confirmed the chain we expect, or None."""
    if not rpc_url:
        return None
    try:
        from web3 import Web3

        candidate = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        if candidate.is_connected() and int(candidate.eth.chain_id) == int(expected_chain_id):
            return candidate
    except Exception:
        pass
    return None


# --- NODE 3: In-Transit Code Builder ---
def code_builder_node(state: GraphState) -> Dict[str, Any]:
    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    quote_data = state["route"]["quote_obj"]
    quote = RouteQuote(**quote_data)

    intent = UserIntent(
        token=state["asset"],
        amount=state["amount"],
        source_chain=state["source_chain"],
        dest_chain=state["target_chain"],
        recipient=state.get("recipient") or state["user_address"],
        network=_network(state),
    )

    # An agent proposal arrives already encoded, and already checked: synthesis
    # recomputed the selector, verified the router's bytecode carries it, and packed the
    # parameters. Re-encoding it here would silently discard all of that and run the
    # values back through a different path — so the agent's bytes pass through unchanged.
    existing = state.get("unsigned_transaction_hex")
    if quote.calldata_template == "agent_proposed" and existing:
        calldata = existing
    else:
        calldata = CalldataExecutor.build_calldata_for_route(quote, intent, state["user_address"])
    ephemeral_script = CalldataExecutor.generate_ephemeral_python_script(
        rpc_url=state.get("active_rpc", ""),
        router_address=quote.router_address,
        calldata=calldata,
        value_wei=0,
        chain_id=quote.source_chain_id,
    )

    # PRINT 3: Then print In-Transit Codegen & Calldata
    _console.print(Panel(
        f"[bold cyan]Function Selector:[/bold cyan] {calldata[:10]}\n"
        f"[bold cyan]Calldata Byte Length:[/bold cyan] {len(calldata)} bytes\n"
        f"[bold cyan]Raw Calldata Hex Preview:[/bold cyan] {calldata[:66]}...\n\n"
        f"[bold yellow]Ephemeral In-Transit Python Script Generated in Memory:[/bold yellow]\n{ephemeral_script.strip()}",
        title="[bold cyan]⚙️ [STEP 3: JIT IN-TRANSIT CODEGEN & CALLDATA SYNTHESIS][/bold cyan]",
        border_style="cyan"
    ))

    traces = state.get("traces", {})
    traces["step_3_codegen"] = {
        "title": "In-Transit Calldata Synthesis",
        "method": "JIT Dynamic ABI Assembly (No Static Bridge SDKs)",
        "calldata_selector": calldata[:10],
        "calldata_length_bytes": len(calldata),
        "ephemeral_python_script": ephemeral_script,
        "status": "COMPLETED",
    }

    # Build the ordered steps. Most ERC-20 bridges need an approval first; whether one
    # is actually required is read off chain rather than taken from the model.
    from omnibridge.core.planner import PrerequisiteUnsatisfiable, build_plan
    from omnibridge.steplog import StepLogger

    plog = StepLogger("plan")
    src_info = rpc_mgr.discover_chain_metadata(state["source_chain"], network=_network(state))
    w3 = _verified_web3(state.get("active_rpc"), src_info.chain_id)
    spend = (quote.extra_params or {}).get("spend")
    plan_notes: list = []

    try:
        plan = build_plan(
            quote, intent, state["user_address"], calldata, src_info.chain_id,
            w3=w3, spend=spend, notes=plan_notes,
        )
    except PrerequisiteUnsatisfiable as exc:
        plog.fail(str(exc))
        traces["step_3_codegen"]["status"] = "FAILED"
        return {"status": "MISSING_PREREQUISITE", "error": str(exc), "traces": traces}

    for note in plan_notes:
        plog.warn(note)
    plog.step(f"{len(plan.steps)} transaction(s) to sign")
    for step in plan.steps:
        plog.value(f"  step {step.index + 1} ({step.kind})", step.title)

    traces["step_3_codegen"]["plan"] = [s.model_dump() for s in plan.steps]
    traces["step_3_codegen"]["plan_notes"] = plan_notes

    return {
        "unsigned_transaction_hex": calldata,
        "plan": plan.model_dump(),
        "status": "PAYLOAD_BUILT",
        "traces": traces,
    }


# --- NODE 4: Dry-Run Simulator ---
def simulator_node(state: GraphState) -> Dict[str, Any]:
    from rich.console import Console
    from rich.panel import Panel
    _console = Console()

    from omnibridge.steplog import StepLogger

    log = StepLogger("simulate")
    src_name = state["source_chain"]
    src_info = rpc_mgr.discover_chain_metadata(src_name, network=_network(state))
    rpc_url = state.get("active_rpc") or rpc_mgr.get_fastest_rpc_sync(src_name, network=_network(state))
    log.step(f"eth_call to {state['route']['router_address']} on chain {src_info.chain_id}")
    log.value("from", state["user_address"])
    log.value("calldata", state["unsigned_transaction_hex"], limit=90)

    # Every endpoint, preferred order, plus the chain these are supposed to be so an
    # impostor node cannot answer the only question that gates this graph.
    rpc_candidates = [rpc_url] + [u for u in src_info.rpc_urls if u != rpc_url]
    sim = Simulator(rpc_candidates, expected_chain_id=src_info.chain_id)

    # Simulate the first unsigned step of the plan, not "the transaction". Where an
    # approval comes first, that is what gets verified now; the bridge call is checked
    # again after the approval lands, because before then its revert is guaranteed and
    # says nothing about whether the route is right.
    from omnibridge.core.models import ExecutionPlan
    from omnibridge.core.planner import simulate_next

    plan = ExecutionPlan(**state["plan"]) if state.get("plan") else None
    plan_out = None
    fork_run = None
    fork_failed = False

    if plan is not None and any(st.tx_hash is None for st in plan.steps):
        pending = [st for st in plan.steps if st.tx_hash is None][0]
        log.value("step", f"{pending.index + 1} of {len(plan.steps)} — {pending.kind}")
        with log.timed(f"simulate {pending.kind}"):
            plan, sim_res, _ = simulate_next(plan, sim, state["user_address"])
        fork_run, fork_note = _fork_check_plan(plan, sim, state["user_address"], sim_res, log)
        if sim_res.success and not (fork_run is not None and not fork_run.success and fork_note is None):
            _prepare_smart_account(plan, sim, state, src_info, log)
        plan_out = plan.model_dump()
        for later in plan.steps:
            if later.status.value == "PENDING_PREREQUISITE":
                forked = (" — passed on a fork with the earlier steps applied"
                          if later.fork_verified else "")
                log.value(
                    f"  step {later.index + 1} ({later.kind})",
                    "re-simulated live after the step before it lands" + forked,
                )
        if fork_run is not None and not fork_run.success and fork_note is None:
            failure = fork_run.first_failure
            sim_res = SimulationResult(
                status=SimulationStatus.REVERTED,
                revert_reason=(
                    f"On a fork of chain {src_info.chain_id}, with the earlier step(s) applied, "
                    f"the {failure.label} step reverted: {failure.revert_reason}"
                ),
                calldata_preview=sim_res.calldata_preview,
                output_amount_valid=False,
                simulated_on_chain_id=src_info.chain_id,
                simulated_via_rpc="anvil fork of " + (fork_run.fork_url or ""),
            )
            fork_failed = True
    else:
        with log.timed("simulate"):
            sim_res = sim.simulate_call(
                from_address=state["user_address"],
                to_address=state["route"]["router_address"],
                calldata=state["unsigned_transaction_hex"],
                value_wei=0,
            )

    if sim_res.success:
        log.ok(f"VERIFIED on chain {sim_res.simulated_on_chain_id}, {sim_res.gas_used} gas")
    else:
        log.fail(f"{sim_res.status.value}: {sim_res.revert_reason}")

    # PRINT 4: Then print Simulation Result
    status_color = "green" if sim_res.success else "red"
    gas_line = f"{sim_res.gas_used} gas" if sim_res.gas_used is not None else "not measured"
    usd_line = (
        f"${sim_res.estimated_gas_cost_usd:.4f}"
        if sim_res.estimated_gas_cost_usd is not None
        else "not measured"
    )
    _console.print(Panel(
        f"[bold {status_color}]Simulation Status:[/bold {status_color}] {sim_res.status.value}\n"
        f"[bold white]Target RPC Node:[/bold white] {sim_res.simulated_via_rpc or rpc_url}\n"
        f"[bold white]Verified Chain ID:[/bold white] {sim_res.simulated_on_chain_id}\n"
        f"[bold white]Gas Units Used:[/bold white] {gas_line}\n"
        f"[bold white]Estimated Gas Cost USD:[/bold white] {usd_line}\n"
        f"[bold white]Verdict:[/bold white] "
        f"{'✓ VERIFIED against a live node' if sim_res.success else '❌ NOT VERIFIED — refusing to proceed'}\n"
        + (f"[bold red]Reason:[/bold red] {sim_res.revert_reason}" if not sim_res.success else ""),
        title=f"[bold {status_color}]🧪 [STEP 4: LIVE DRY-RUN SIMULATION (eth_call)][/bold {status_color}]",
        border_style=status_color
    ))

    traces = state.get("traces", {})
    traces["step_4_simulation"] = {
        "title": "Dry-Run Simulation (eth_call)",
        "rpc_endpoint": rpc_url,
        "method": "eth_call / static gas estimation"
        + (" + Anvil fork of the whole plan" if fork_run is not None else ""),
        "fork": fork_run.as_dict() if fork_run is not None else None,
        "simulation_status": sim_res.status.value,
        "simulation_passed": sim_res.success,
        "verified_chain_id": sim_res.simulated_on_chain_id,
        "scope": "source leg only — the destination leg is not verifiable by eth_call",
        "gas_units": sim_res.gas_used,
        "estimated_gas_usd": sim_res.estimated_gas_cost_usd,
        "revert_reason": sim_res.revert_reason if not sim_res.success else None,
        "status": "COMPLETED" if sim_res.success else "FAILED",
    }

    return {
        "simulation_result": sim_res.model_dump(),
        "plan": plan_out if plan_out is not None else state.get("plan"),
        "estimated_gas_usd": sim_res.estimated_gas_cost_usd or 0.0,
        "status": "SIMULATION_PASSED" if sim_res.success else "SIMULATION_FAILED",
        "error": sim_res.revert_reason if not sim_res.success else None,
        "fork_failure": fork_failed,
        "traces": traces,
    }


def _prepare_smart_account(plan: Any, sim: Simulator, state: Dict[str, Any], src_info: Any, log: Any) -> None:
    """
    Offer the plan as one gasless-in-ETH signature, when the sender can take it.

    Needs: a bundler configured for this chain (any ERC-4337 bundler serving EntryPoint
    v0.7, see `omnibridge.aa.provider`); the sender switched to a MetaMask smart account on
    this chain; an ERC-20 paymaster accepting the token being bridged; enough of it for the
    transfer plus the gas-fee cap; and the exact batch passing on a fork. Any miss leaves
    `plan.aa` unset and says why in `plan.aa_unavailable` — the step-by-step path still
    works, so none of this can stop a transfer, only offer a better one.
    """
    from omnibridge.aa import provider
    from omnibridge.aa.account import account_status
    from omnibridge.aa.guard import build_batch
    from omnibridge.core import erc20

    def unavailable(reason: str) -> None:
        plan.aa_unavailable = reason
        log.value("smart account", f"not offered: {reason}")

    try:
        if not provider.configured(src_info.chain_id):
            return unavailable("no bundler is configured (set AA_BUNDLER_URL, or PIMLICO_API_KEY)")
    except provider.AaError as exc:
        return unavailable(str(exc))
    w3, _url = sim.connect_verified()
    if w3 is None:
        return unavailable("no identity-checked node to read the account from")
    sender = state["user_address"]
    status = account_status(w3, sender)
    if not status.smart_account:
        return unavailable(status.reason or status.kind)

    pending = [st for st in plan.steps if st.tx_hash is None]
    token = next((st.token_address for st in pending if st.kind == "approve" and st.token_address), None)
    if token is None:
        spend = ((state.get("route") or {}).get("quote_obj") or {}).get("extra_params", {}).get("spend") or {}
        token = spend.get("token")
    if not token:
        return unavailable("the route pulls no ERC-20, so there is no token to pay gas in")

    decimals = erc20.read_decimals(w3, token) or 6
    try:
        client = provider.AaClient(src_info.chain_id)
        client.check_bundler()
        fees = client.fee_plan(token, decimals)
    except provider.AaError as exc:
        return unavailable(str(exc))
    fee_cap = fees.fee_cap

    amount = sum(int(st.amount_raw or 0) for st in pending if st.kind != "approve")
    balance = erc20.read_balance(w3, token, sender)
    if balance is not None and balance < amount + fee_cap:
        return unavailable(
            f"the wallet holds {balance} base units; the transfer needs {amount} plus up to "
            f"{fee_cap} for gas"
        )

    batch = build_batch(sender, src_info.chain_id, pending, token, fees.paymaster, fee_cap, fees.context)
    with log.timed(f"fork-simulate the smart-account batch ({len(batch.calls)} calls)"):
        run, why_not = sim.simulate_batch_on_fork(sender, batch.calls)
    if run is None:
        return unavailable(f"the batch could not be simulated on a fork: {why_not}")
    if not run.success:
        failure = run.first_failure
        return unavailable(f"the batch reverted on a fork: {failure.revert_reason if failure else 'unknown'}")

    plan.aa = {
        **batch.as_dict(),
        "token_decimals": decimals,
        "fee_cap_display": f"{fee_cap / 10**decimals:.4f}",
        "fork_gas_used": run.steps[-1].gas_used if run.steps else None,
        "fee_basis": fees.basis,
        "kind": status.kind,
        "delegate": status.delegate,
    }
    log.ok(f"smart account: one signature, gas paid in the token (up to {plan.aa['fee_cap_display']}), "
           "batch passed on a fork")


def _fork_check_plan(plan: Any, sim: Simulator, sender: str, first_result: Any, log: Any):
    """
    Run every unsigned step of the plan, in order, on an Anvil fork.

    Only worth doing when there is more than one step and the first passed live: then
    the fork is the one place the later steps can be checked before anything is signed.
    Results are written onto the steps as evidence. Returns (ForkRun or None, note), where
    a note means the fork itself could not answer — never read as a pass or a revert.
    """
    from omnibridge.core.fork import ForkStep

    pending = [s for s in plan.steps if s.tx_hash is None]
    if len(pending) < 2 or not first_result.success:
        return None, "not needed"
    steps = [ForkStep(to=s.to, data=s.data, value_wei=s.value_wei, label=s.kind) for s in pending]
    with log.timed(f"fork-simulate all {len(steps)} steps"):
        run, why_not = sim.simulate_sequence_on_fork(sender, steps)
    if run is None:
        log.warn(f"no fork ({why_not}); later steps are checked live after the earlier ones land")
        return None, why_not
    for step, result in zip(pending, run.steps):
        step.fork_verified = result.success
        step.fork_gas_used = result.gas_used
        step.fork_revert_reason = result.revert_reason
    failure = run.first_failure
    if failure is not None and failure.infrastructure_error:
        log.warn(f"the fork could not answer for the {failure.label} step: {failure.revert_reason}")
        return run, "fork infrastructure error"
    if run.success:
        log.ok("every step passed on a fork, in order")
    else:
        log.fail(f"{failure.label} step reverted on the fork: {failure.revert_reason}")
    return run, None


def route_after_simulation(state: GraphState) -> str:
    """
    The only branch in the graph, so it has to be able to say no.

    VERIFIED means an eth_call completed against a node whose identity was checked.
    REVERTED, UNAVAILABLE and UNSUPPORTED are all refusals — in particular UNAVAILABLE
    ("could not check") must never read as a pass.
    """
    result = state.get("simulation_result") or {}
    if result.get("status") == SimulationStatus.VERIFIED.value:
        return "hitl_gate_node"
    return "error_node"


def validate_resume_payload(resume_data: Any, unsigned_hex: Optional[str]) -> str:
    """
    Turn a resume payload into a signed transaction, or refuse.

    A malformed resume is a rejection, not an approval. The gate previously fell back to
    state["unsigned_transaction_hex"] whenever resume_data was not a dict, and still
    returned USER_APPROVED — so a malformed payload carried an *unsigned* transaction
    forward as though a human had signed it.
    """
    if not isinstance(resume_data, dict):
        raise ValueError(
            f"Approval Error: resume payload was {type(resume_data).__name__}, expected "
            "an object containing 'signed_transaction_hex'. Treating this as a rejection."
        )

    signed_hex = resume_data.get("signed_transaction_hex")
    if not isinstance(signed_hex, str) or not signed_hex.strip():
        raise ValueError(
            "Approval Error: resume payload carried no 'signed_transaction_hex'. "
            "Nothing was signed, so nothing is approved."
        )

    signed_hex = signed_hex.strip()
    if signed_hex == (unsigned_hex or "").strip():
        raise ValueError(
            "Approval Error: the resumed payload is the unsigned calldata, not a signed "
            "transaction. Refusing to treat it as approved."
        )
    return signed_hex


# --- NODE 5: Human-in-the-Loop Interrupt Gate ---
def hitl_gate_node(state: GraphState) -> Dict[str, Any]:
    quote_data = (state.get("route") or {}).get("quote_obj") or {}
    calldata = state["unsigned_transaction_hex"]
    sim = state.get("simulation_result") or {}

    # Recover the amount from the bytes that will actually be signed. The CLI manifest
    # has done this since Phase 1; the browser path never did, so the only number a web
    # user ever saw was the one they typed. If the display and the transaction read from
    # the same source, a scaling error is invisible to the human too.
    decimals = quote_data.get("decimals") or 6
    decoded_raw = CalldataExecutor.decode_leading_uint256(calldata)
    decoded_amount = decoded_raw / (10 ** decimals) if decoded_raw is not None else None

    # What the browser needs to put the wallet on the right chain: a readable name, and
    # for a chain the wallet does not know yet, the parameters wallet_addEthereumChain
    # takes. The RPC offered is the one this run identity-checked, not a registry guess.
    source_chain_meta = None
    try:
        info = rpc_mgr.discover_chain_metadata(state["source_chain"], network=_network(state))
        source_chain_meta = {
            "chain_id": info.chain_id,
            "name": info.name,
            "native_symbol": info.native_symbol,
            "explorer": info.block_explorer or None,
            "rpc_url": state.get("active_rpc") or (info.rpc_urls[0] if info.rpc_urls else None),
        }
    except Exception:
        source_chain_meta = None

    approval_payload = {
        "status": "AWAITING_USER_APPROVAL",
        "network": _network(state),
        "source_chain_meta": source_chain_meta,
        "route": {
            "source": state["source_chain"],
            "target": state["target_chain"],
            "bridge": state["route"]["bridge"],
            "router_address": state["route"]["router_address"],
            "source_chain_id": quote_data.get("source_chain_id"),
            "dest_chain_ref": (quote_data.get("extra_params") or {}).get("dest_chain_ref"),
            "function_signature": (quote_data.get("extra_params") or {}).get("function_signature"),
        },
        # Who signs and who receives, always both, so the approval screen can say when
        # they differ instead of leaving the user to assume they do not.
        "parties": {
            "sender": state["user_address"],
            "recipient": state.get("recipient") or state["user_address"],
            "recipient_is_sender": not state.get("recipient")
            or str(state["recipient"]).lower() == str(state["user_address"]).lower(),
        },
        # What the human is actually authorising, and whether it matches what they asked
        # for. `decoded` is None when the calldata is not ABI-decodable — stated as
        # unverified rather than silently falling back to the requested figure.
        "amount": {
            "requested": state["amount"],
            "decoded_from_calldata": decoded_amount,
            "token": state["asset"],
            "matches": (
                decoded_amount is not None
                and abs(decoded_amount - float(state["amount"])) < 1e-9
            ),
        },
        # The scope of what was checked, so the page cannot imply more than happened.
        "verification": {
            "simulation_status": sim.get("status"),
            "simulated_on_chain_id": sim.get("simulated_on_chain_id"),
            "simulated_via_rpc": sim.get("simulated_via_rpc"),
            "gas_used": sim.get("gas_used"),
            "revert_reason": sim.get("revert_reason"),
            "scope": (
                "Source leg only. No eth_call can prove that funds arrive on the "
                "destination chain."
            ),
        },
        "unsigned_transaction_hex": calldata,
        # The ordered transactions. A single-transaction transfer is a plan of one, so
        # the browser has one code path rather than a special case.
        "plan": state.get("plan"),
        "estimated_gas_usd": state["estimated_gas_usd"],
        "slippage": state["slippage"],
        "agent_evidence": state.get("agent_evidence"),
        "budget": state.get("budget"),
        "replayed_skill": state.get("replayed_skill"),
        "traces": state.get("traces", {}),
    }

    # Native LangGraph Interrupt: Pauses graph and yields telemetry + approval payload
    resume_data = interrupt(approval_payload)

    signed_hex = validate_resume_payload(resume_data, state.get("unsigned_transaction_hex"))

    return {
        "signed_transaction_hex": signed_hex,
        "status": "USER_APPROVED",
    }


# --- NODE 6: Broadcast & Skill Save ---
def broadcast_skill_node(state: GraphState) -> Dict[str, Any]:
    src_chain_info = rpc_mgr.discover_chain_metadata(state["source_chain"], network=_network(state))
    signer = Signer(state["active_rpc"], src_chain_info.block_explorer)

    tx_payload = {
        "chainId": src_chain_info.chain_id,
        "to": state["route"]["router_address"],
        "signed_hex": state.get("signed_transaction_hex"),
        "data": state["unsigned_transaction_hex"],
        "value": 0,
    }

    receipt = signer.broadcast_transaction(tx_payload, private_key=None, dry_run=False)

    # Past this line the transfer has happened. Everything below is bookkeeping, and
    # bookkeeping is not allowed to decide whether a broadcast counts: an exception here
    # used to propagate out of the node and land the run in REJECTED with the user's
    # transaction already mined. The status reports what the chain did; a failed
    # write-back is reported alongside it, not instead of it.
    try:
        skill_path = _persist_route_as_skill(state, receipt)
        skill_error = None
    except Exception as exc:
        from omnibridge.steplog import StepLogger

        skill_path = None
        skill_error = f"{type(exc).__name__}: {exc}"
        StepLogger("broadcast").warn(
            f"transaction {receipt.tx_hash} is on chain, but the skill was not saved "
            f"({skill_error}). The route will be rediscovered next time."
        )

    # Stage 3 of the card lifecycle: an approved broadcast verifies the card row whose
    # contract it called. Bookkeeping, so it can never change the outcome above.
    card_path, card_error = None, None
    if state.get("card_match"):
        try:
            from omnibridge.cards.writeback import record_success

            card_path = record_success(card_mgr, state["card_match"], receipt.tx_hash)
        except Exception as exc:
            card_error = f"{type(exc).__name__}: {exc}"

    traces = state.get("traces", {})
    traces["step_6_broadcast"] = {
        "title": "On-Chain Broadcast & SKILL.md Registration",
        "tx_hash": receipt.tx_hash,
        "explorer_url": receipt.explorer_url,
        "persisted_skill_path": skill_path,
        # Named rather than implied by a null path, so "not saved" is distinguishable
        # from "saved somewhere this response did not carry".
        "skill_save_error": skill_error,
        "card_path": card_path,
        "card_save_error": card_error,
        "status": "COMPLETED" if skill_error is None else "COMPLETED_WITHOUT_SKILL",
    }

    return {
        "status": "CONFIRMED",
        "tx_hash": receipt.tx_hash,
        "skill_path": skill_path,
        "skill_save_error": skill_error,
        "traces": traces,
    }


def _persist_route_as_skill(state: GraphState, receipt: Any) -> str:
    """
    Write the SKILL.md that makes the second identical request free.

    Split out of the broadcast node so its failures are catchable there. It runs after
    the transaction is already on chain, so nothing it does may raise past its caller.
    """
    src_chain_info = rpc_mgr.discover_chain_metadata(state["source_chain"], network=_network(state))
    quote = RouteQuote(**state["route"]["quote_obj"])
    params = quote.extra_params or {}
    src_name = state["source_chain"]
    dst_name = state["target_chain"]
    skill_name = f"{src_name.lower()}_to_{dst_name.lower()}_{state['asset'].lower()}"

    # Store the parameters with this run's amount and addresses swapped for placeholders,
    # so a replay encodes the next caller's transfer rather than a frozen copy of this one.
    stored_values = CalldataExecutor.generalise_param_values(
        list(params.get("param_values") or []),
        amount_raw=quote.deposit_amount_raw,
        recipient=state.get("recipient") or state["user_address"],
        sender=state["user_address"],
    )

    # The destination chain id was hardcoded to 200 here, for every route, regardless of
    # where the transfer was actually going. A destination that is not EIP-155 addressable
    # has no chain id at all, so it is left unset and named by dest_chain_ref instead.
    dest_chain_id: Optional[int] = None
    dest_chain_ref = params.get("dest_chain_ref")
    try:
        dest_chain_id = rpc_mgr.discover_chain_metadata(dst_name, network=_network(state)).chain_id
    except Exception:
        dest_chain_id = None

    sim = state.get("simulation_result") or {}
    metadata = SkillMetadata(
        name=skill_name,
        description=(
            f"Verified bridge workflow for {state['asset']} from {src_name} to "
            f"{dst_name} via {state['route']['bridge']}"
        ),
        source_chain=src_chain_info.name,
        source_chain_id=src_chain_info.chain_id,
        dest_chain=dst_name,
        dest_chain_id=dest_chain_id,
        dest_chain_ref=dest_chain_ref,
        token=state["asset"],
        bridge_protocol=state["route"]["bridge"],
        router_address=state["route"]["router_address"],
        source_token_address=quote.source_token_address or None,
        dest_token_address=quote.dest_token_address or None,
        network=_network(state),
        function_signature=params.get("function_signature"),
        param_types=list(params.get("param_types") or []),
        param_values=stored_values,
        spend=params.get("spend"),
        # Earned, not enumerated: the strings that actually resolved to this route —
        # and kept per end, because a route has a direction. Pooling them made every
        # skill match its own reverse.
        source_aliases=sorted({src_name.lower(), src_chain_info.name.lower()}),
        dest_aliases=sorted({dst_name.lower()}),
        verified=True,
        last_verified=receipt.timestamp[:10],
        sources=list(params.get("sources") or []),
        discovered_by=(state.get("agent_evidence") or {}).get("model") or "skill_replay",
        rpc_fallbacks=src_chain_info.rpc_urls,
        tags=["cross-chain", state["asset"].lower(), src_name.lower(), dst_name.lower()],
    )

    skill_body = f"""# {skill_name.upper()} Execution Routine

## Route
- **Protocol:** {metadata.bridge_protocol}
- **Router:** `{metadata.router_address}`
- **Function:** `{metadata.function_signature}`
- **Destination:** {format_chain_ref(dest_chain_ref) or dest_chain_id or dst_name}

## Parameters
Encoded through `CalldataExecutor.encode_dynamic_calldata`. Placeholders are bound per
request by `CalldataExecutor.bind_param_values`; everything else is fixed for this route.

| # | Type | Value |
|---|------|-------|
""" + "\n".join(
        f"| {i} | `{t}` | `{v}` |"
        for i, (t, v) in enumerate(zip(metadata.param_types, metadata.param_values))
    ) + f"""

## Verification
- Simulation: {sim.get('status')} on chain {sim.get('simulated_on_chain_id')} via {sim.get('simulated_via_rpc')}
- Gas: {sim.get('gas_used')} units
- Approved by a human and broadcast as `{receipt.tx_hash}`
- Sources: {', '.join(metadata.sources) if metadata.sources else 'skill replay (no new sources)'}

A cache hit skips discovery, not the guardrails: a replay is re-simulated and
re-approved before it can send anything.
"""

    return skill_mgr.save_skill(
        metadata, skill_body, target_dir=str(LOCAL_SKILLS_DIR / skill_name)
    )


def error_node(state: GraphState) -> Dict[str, Any]:
    """
    Terminal for an unrecoverable failure, and the hinge of the retry edge.

    Where the failure is one the agent could learn from — a revert, a rejected proposal —
    it is packaged as context for another attempt rather than discarded.
    """
    sim = state.get("simulation_result") or {}
    reason = state.get("error") or sim.get("revert_reason")
    # A missing prerequisite is not a routing error, so it never goes round the retry
    # edge. Three attempts against the same allowance revert cost ~170s of model time
    # and produced three contradictory explanations of a failure the model could not see.
    # After a fork applied the plan's approval, an allowance revert is not a missing
    # approval — the approval was there. It means the route approved the wrong spender
    # or pulls a different token, which is a routing error another attempt can fix.
    prerequisite = None if state.get("fork_failure") else _missing_prerequisite(reason or "")
    retryable = (
        sim.get("status") in ("REVERTED", "UNAVAILABLE")
        and bool(reason)
        and state.get("status") != "NO_ROUTE_FOUND"
        and prerequisite is None
    )

    # Demote before patching. If this run replayed a cached skill and it failed, the
    # skill stops being served — but it is not rewritten here. The replacement lands
    # only once it has itself cleared simulation and the human gate, which is what
    # stops a transient RPC error from overwriting a known-good route with whatever a
    # cold search turned up.
    demoted = None
    replayed = state.get("replayed_skill")
    if replayed and reason:
        cached = next(
            (sk for sk in skill_mgr.list_skills() if sk.metadata.name == replayed), None
        )
        if cached is not None and cached.metadata.verified:
            try:
                demoted = skill_mgr.mark_unverified(
                    cached, f"{sim.get('status') or 'FAILED'}: {reason}"
                )
            except Exception as exc:  # never let bookkeeping mask the original failure
                demoted = f"demotion failed: {exc}"

    # Stage 4: a revert is written onto the card the route used, from what the node
    # said, and an unverified row it relied on is marked failed. Bookkeeping: it must
    # never mask the failure being reported.
    card_match = state.get("card_match")
    card_note, fix = None, None
    if sim.get("status") == "REVERTED" and reason and card_match and prerequisite is None:
        try:
            from omnibridge.cards.writeback import known_fix, record_failure

            record_failure(card_mgr, card_match, reason)
            fix = known_fix(card_mgr, card_match.get("protocol_id"), reason)
            card_note = (
                f"This route used card {card_match.get('protocol_id')} row "
                f"{card_match.get('source_chain_id')} (was {card_match.get('source_status')})."
            )
        except Exception as exc:
            card_note = f"card write-back failed: {exc}"

    failure_context = None
    if retryable and state.get("retry_count", 0) < MAX_DISCOVERY_RETRIES:
        route = state.get("route") or {}
        failure_context = (
            f"Router {route.get('router_address')} with the parameters you gave was "
            f"rejected at simulation: {reason}. Establish what was wrong before proposing "
            "again, and do not repeat the same router and arguments."
        )
        if card_note:
            failure_context += " " + card_note
        if fix:
            failure_context += f" The card records this fix for that revert (unverified advice): {fix}"

    traces = state.get("traces", {})
    # This node runs once per attempt, so the second pass must not erase what the first
    # recorded. The demotion happens on the pass that saw the failure; by the time the
    # retry comes back through, the skill is already unverified and `demoted` is None.
    previous = traces.get("step_error") or {}

    # Report the failure that started this, not the one it caused. A reverted skill is
    # demoted, so the retry finds an empty cache and reports "no verified skill covers
    # this route" — true, but only because of the demotion, and it reads as though the
    # route was never known. The first substantive attempt is the honest headline.
    root_cause = next(
        (a for a in previous.get("attempts", []) if a.get("simulation_status")), None
    )
    if root_cause and state.get("status") in ("NO_ROUTE_FOUND", "CONFIG_ERROR"):
        reason = (
            f"{root_cause['error']} "
            f"(after this failed, the cached route was retired, so the retry found "
            f"nothing left to try.)"
        )
    traces["step_error"] = {
        "title": "Diagnostic",
        "error": reason,
        "missing_prerequisite": prerequisite,
        "simulation_status": sim.get("status"),
        "retry_count": state.get("retry_count", 0),
        "will_retry": failure_context is not None,
        "demoted_skill": demoted or previous.get("demoted_skill"),
        "card_note": card_note,
        "known_fix": fix,
        "attempts": [*previous.get("attempts", []), {
            "simulation_status": sim.get("status"),
            "error": reason,
            "demoted_skill": demoted,
        }],
    }

    if prerequisite:
        # Report what is actually missing. "The circuit breaker held" is true and
        # useless; the user needs to know the route was fine and a step is absent.
        return {
            "status": "MISSING_PREREQUISITE",
            "error": prerequisite,
            "failure_context": None,
            "retry_count": state.get("retry_count", 0),
            "traces": traces,
        }

    return {
        # A no-route keeps its own name through the diagnostic. Flattening every
        # outcome to ERROR forced callers to pattern-match the message to tell "no
        # bridge exists for this pair" from "the bridge rejected this transaction".
        # INSUFFICIENT_FUNDS and UNRESOLVED_CHAIN are here for the same reason: both
        # are answers about the request, not failures of the pipeline, and both are
        # things the user can act on.
        "status": state["status"]
        if state.get("status")
        in (
            "NO_ROUTE_FOUND",
            "CONFIG_ERROR",
            "MISSING_PREREQUISITE",
            "INSUFFICIENT_FUNDS",
            "UNRESOLVED_CHAIN",
        )
        else "ERROR",
        "error": reason or "Unknown execution error occurred",
        "failure_context": failure_context,
        "retry_count": state.get("retry_count", 0) + (1 if failure_context else 0),
        "traces": traces,
    }


# Build LangGraph State Machine
builder = StateGraph(GraphState)

builder.add_node("intent_parser_node", intent_parser_node)
builder.add_node("preflight_node", preflight_node)
builder.add_node("discovery_node", discovery_node)
builder.add_node("code_builder_node", code_builder_node)
builder.add_node("simulator_node", simulator_node)
builder.add_node("hitl_gate_node", hitl_gate_node)
builder.add_node("broadcast_skill_node", broadcast_skill_node)
builder.add_node("error_node", error_node)

builder.add_edge(START, "intent_parser_node")
# Funding is checked before discovery, because discovery is the expensive step and an
# empty wallet invalidates every route it could return.
builder.add_edge("intent_parser_node", "preflight_node")
builder.add_conditional_edges(
    "preflight_node", route_after_preflight, ["discovery_node", "error_node"]
)
builder.add_conditional_edges(
    "discovery_node", route_after_discovery, ["code_builder_node", "error_node", "discovery_node"]
)
builder.add_conditional_edges(
    "code_builder_node", route_after_code_builder, ["simulator_node", "error_node"]
)
builder.add_conditional_edges("simulator_node", route_after_simulation, ["hitl_gate_node", "error_node"])
builder.add_edge("hitl_gate_node", "broadcast_skill_node")
builder.add_edge("broadcast_skill_node", END)
# The Diagnostic node is no longer a dead end: a revert returns to Discovery with the
# reason in context, bounded by MAX_DISCOVERY_RETRIES.
builder.add_conditional_edges("error_node", route_after_error, ["discovery_node", END])

checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)
