// Omnibridge Web UI - Powered by Blocknative Web3-Onboard
import Onboard from "https://esm.sh/@web3-onboard/core@2.22.3";
import injectedModule from "https://esm.sh/@web3-onboard/injected-wallets@2.11.2";

// Initialize Blocknative Injected Wallets (MetaMask, Rabby, Coinbase, Brave, Phantom, Frame, etc.)
const injected = injectedModule();

// Initialize Blocknative Onboard instance
const onboard = Onboard({
  wallets: [injected],
  chains: [
    {
      id: "0x2105", // 8453 (Base)
      token: "ETH",
      label: "Base",
      rpcUrl: "https://mainnet.base.org",
    },
    {
      id: "0xa4b1", // 42161 (Arbitrum One)
      token: "ETH",
      label: "Arbitrum One",
      rpcUrl: "https://arb1.arbitrum.io/rpc",
    },
    {
      id: "0x1", // 1 (Ethereum)
      token: "ETH",
      label: "Ethereum Mainnet",
      rpcUrl: "https://eth.llamarpc.com",
    },
    {
      id: "0xa", // 10 (Optimism)
      token: "ETH",
      label: "Optimism",
      rpcUrl: "https://mainnet.optimism.io",
    },
    {
      id: "0x89", // 137 (Polygon)
      token: "POL",
      label: "Polygon",
      rpcUrl: "https://polygon-rpc.com",
    },
  ],
  appMetadata: {
    name: "Omnibridge",
    icon: "<svg width='40' height='40' viewBox='0 0 40 40' fill='none' xmlns='http://www.w3.org/2000/svg'><rect width='40' height='40' rx='8' fill='%236366F1'/><path d='M10 26L20 14L30 26' stroke='white' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/></svg>",
    description: "Autonomous In-Transit Web3 Cross-Chain Execution Gateway",
    recommendedInjectedWallets: [
      { name: "MetaMask", url: "https://metamask.io" },
      { name: "Coinbase", url: "https://wallet.coinbase.com/" },
      { name: "Rabby", url: "https://rabby.io" },
    ],
  },
  theme: "dark",
});

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("bridge-form");
  const intentInput = document.getElementById("intent-input");
  const executeBtn = document.getElementById("execute-btn");
  const outputSection = document.getElementById("output-section");
  const skillsGrid = document.getElementById("skills-grid");
  const refreshSkillsBtn = document.getElementById("refresh-skills-btn");
  const connectWalletBtn = document.getElementById("connect-wallet-btn");
  const walletBtnText = document.getElementById("wallet-btn-text");
  const chips = document.querySelectorAll(".chip");
  const logSection = document.getElementById("log-section");
  const logStream = document.getElementById("log-stream");
  const logAutoscroll = document.getElementById("log-autoscroll");
  const logCopyBtn = document.getElementById("log-copy-btn");
  const logDuration = document.getElementById("log-duration");

  let userConnectedAddress = null;
  let currentThreadId = null;
  let currentUnsignedHex = null;
  let currentRouterAddress = null;

  // Theme Management (Dual Mode: Dark & Light)
  const themeToggleBtn = document.getElementById("theme-toggle-btn");
  const themeIcon = document.getElementById("theme-icon");

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("omnibridge_theme", theme);
    if (themeIcon) {
      themeIcon.textContent = theme === "light" ? "☀️" : "🌙";
    }
  }

  const savedTheme = localStorage.getItem("omnibridge_theme") || "dark";
  setTheme(savedTheme);

  if (themeToggleBtn) {
    themeToggleBtn.addEventListener("click", () => {
      const currentTheme = document.documentElement.getAttribute("data-theme") || "dark";
      const newTheme = currentTheme === "dark" ? "light" : "dark";
      setTheme(newTheme);
    });
  }

  // ---------------------------------------------------------------- funding
  //
  // Read the wallet as soon as it connects. Discovery costs money and takes a minute,
  // and an empty wallet invalidates every route it could return — so the cheap
  // question is asked first. The server answers the same question again before
  // discovery runs; this is the early copy, not the authority.

  const fundingBanner = document.getElementById("funding-banner");

  /** The chain the transfer would leave from: what the intent says, else the wallet's. */
  function intendedSourceChain() {
    const typed = (intentInput.value || "").match(/\bfrom\s+([a-zA-Z0-9_-]+)\s+to\s+/i);
    if (typed) return typed[1];
    const wallets = onboard.state.get().wallets;
    const hexId = wallets && wallets[0] && wallets[0].chains && wallets[0].chains[0]
      ? wallets[0].chains[0].id
      : null;
    // The registry resolves a numeric chain id ahead of any name, so send the id.
    return hexId ? String(parseInt(hexId, 16)) : null;
  }

  function renderFunding(data) {
    // Three outcomes, three appearances. UNCHECKED is deliberately not green: the
    // balance was not read, and a reassuring banner would say otherwise.
    const tone = data.status === "INSUFFICIENT"
      ? "banner-rose"
      : data.status === "SUFFICIENT" ? "banner-green" : "banner-slate";

    // On a pass, lead with the asset the user is actually sending; the native
    // balance is the gas footnote, not the answer to the question they asked.
    const asset = data.asset_balance !== null && data.asset_balance !== undefined
      ? `${esc(data.asset_balance)} ${esc(data.asset_symbol)}`
      : null;
    const gas = `${esc(data.native_balance)} ${esc(data.native_symbol)} for gas`;

    const headline = data.status === "INSUFFICIENT"
      ? esc(data.reason)
      : data.status === "SUFFICIENT"
        ? `${asset ? `${asset}, ` : ""}${gas}, on ${esc(data.chain.name)}.`
        : `Balance not read on ${esc(data.chain.name)}.`;

    const notes = [...(data.status === "SUFFICIENT" ? [] : data.checked), ...data.deferred]
      .map((n) => `<span class="banner-note">${esc(n)}</span>`)
      .join("");

    fundingBanner.className = `banner ${tone}`;
    fundingBanner.innerHTML = `<strong>Funding:</strong> ${headline}${notes}`;
    fundingBanner.classList.remove("hidden");
  }

  async function checkFunding() {
    if (!userConnectedAddress) {
      fundingBanner.classList.add("hidden");
      return;
    }
    const sourceChain = intendedSourceChain();
    if (!sourceChain) return;

    try {
      const res = await fetch("/api/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          userAddress: userConnectedAddress,
          sourceChain,
          asset: assetFromIntent(),
          amount: amountFromIntent(),
        }),
      });
      if (!res.ok) {
        // A chain the registry does not carry is a real answer, but not a funding
        // one — say nothing here rather than implying the wallet was checked.
        fundingBanner.classList.add("hidden");
        return;
      }
      renderFunding(await res.json());
    } catch (err) {
      // Never let the early check block the run it exists to speed up.
      console.warn("preflight funding check failed:", err);
      fundingBanner.classList.add("hidden");
    }
  }

  function assetFromIntent() {
    const m = (intentInput.value || "").match(
      /(?:bridge|swap|send|move|transfer)\s+\d+(?:\.\d+)?\s*([a-zA-Z0-9_-]+)/i
    );
    return m ? m[1].toUpperCase() : null;
  }

  function amountFromIntent() {
    const m = (intentInput.value || "").match(
      /(?:bridge|swap|send|move|transfer)\s+(\d+(?:\.\d+)?)/i
    );
    return m ? parseFloat(m[1]) : null;
  }

  // Blocknative Subscription for reactive wallet state
  const state = onboard.state.select("wallets");
  state.subscribe((wallets) => {
    if (wallets && wallets.length > 0 && wallets[0].accounts.length > 0) {
      userConnectedAddress = wallets[0].accounts[0].address;
      walletBtnText.textContent = `${userConnectedAddress.slice(0, 6)}...${userConnectedAddress.slice(-4)}`;
      connectWalletBtn.style.borderColor = "var(--accent-green)";
      connectWalletBtn.style.color = "var(--text-primary)";
      connectWalletBtn.style.background = "rgba(16, 185, 129, 0.12)";
      checkFunding();
    } else {
      userConnectedAddress = null;
      walletBtnText.textContent = "Connect Wallet";
      connectWalletBtn.style.borderColor = "var(--border-color)";
      connectWalletBtn.style.background = "var(--bg-secondary)";
      fundingBanner.classList.add("hidden");
    }
  });

  // Connect Wallet via Blocknative Onboard
  async function triggerBlocknativeConnect() {
    try {
      const wallets = await onboard.connectWallet();
      if (wallets && wallets[0] && wallets[0].accounts.length > 0) {
        userConnectedAddress = wallets[0].accounts[0].address;
        return userConnectedAddress;
      }
    } catch (err) {
      console.warn("Blocknative connection error:", err);
    }
    return null;
  }

  connectWalletBtn.addEventListener("click", triggerBlocknativeConnect);

  // Pipeline step elements
  const steps = [
    document.getElementById("step-1"),
    document.getElementById("step-2"),
    document.getElementById("step-3"),
    document.getElementById("step-4"),
    document.getElementById("step-5"),
    document.getElementById("step-6"),
  ];

  function resetPipeline() {
    steps.forEach((s) => s.classList.remove("active", "completed"));
  }

  function setStepActive(stepNum) {
    for (let i = 0; i < stepNum - 1; i++) {
      steps[i].classList.remove("active");
      steps[i].classList.add("completed");
    }
    steps[stepNum - 1].classList.add("active");
  }

  function setAllCompleted() {
    steps.forEach((s) => {
      s.classList.remove("active");
      s.classList.add("completed");
    });
  }

  // Quick Chips
  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      intentInput.value = chip.getAttribute("data-intent");
      intentInput.focus();
      // The chain and asset just changed, so the banner is about the wrong transfer.
      checkFunding();
    });
  });

  // Re-read when the typed intent settles. Debounced: the check is a network call,
  // and a stale banner about a different chain is worse than none.
  let fundingDebounce = null;
  intentInput.addEventListener("input", () => {
    fundingBanner.classList.add("hidden");
    clearTimeout(fundingDebounce);
    fundingDebounce = setTimeout(checkFunding, 600);
  });

  // Fetch Skills from API
  async function loadSkills() {
    try {
      const res = await fetch("/api/skills");
      if (!res.ok) return;
      const skills = await res.json();
      skillsGrid.innerHTML = "";

      if (skills.length === 0) {
        skillsGrid.innerHTML = `<p class="subtitle">No persistent skills yet. Execute a bridging transfer to automatically save a procedural SKILL.md.</p>`;
        return;
      }

      skills.forEach((skill) => {
        const card = document.createElement("div");
        // A demoted skill is still on disk and still listed — the demotion is meant to
        // be reversible — but it must not be presented as though it were serving.
        card.className = `skill-card${skill.verified ? "" : " skill-demoted"}`;
        card.innerHTML = `
          <div class="skill-name">
            <span>${skill.verified ? "⚡" : "⚠"} ${esc(skill.name)}</span>
            <span class="skill-badge ${skill.verified ? "ok" : "bad"}">${skill.verified ? "verified" : "demoted"}</span>
          </div>
          <div class="skill-desc">${esc(skill.description) || "Cross-chain routine."}</div>
          <div class="skill-meta">
            <span>${esc(skill.source)} ➔ ${esc(skill.destination)}</span>
            <span>${esc(skill.token)}</span>
          </div>
          <div class="skill-meta">
            <span>${esc(skill.protocol) || "—"}</span>
            <span>${skill.verified
              ? `verified ${skill.verification_count}× · last ${esc(skill.last_verified) || "unknown"}`
              : "not served"}</span>
          </div>
          ${skill.last_failure ? `<div class="skill-failure">Last failure: ${esc(skill.last_failure)}</div>` : ""}
          ${skill.revisions ? `<div class="skill-meta"><span class="muted">${skill.revisions} earlier revision(s) retained</span></div>` : ""}
        `;
        skillsGrid.appendChild(card);
      });
    } catch (e) {
      console.error("Failed to load skills:", e);
    }
  }

  refreshSkillsBtn.addEventListener("click", loadSkills);
  loadSkills();

  // ------------------------------------------------------------- live log

  const LEVEL_CLASS = {
    green: "log-ok", red: "log-fail", yellow: "log-warn",
    cyan: "log-step", dim: "log-detail",
  };

  let logSource = null;
  const logLines = [];

  function resetLog() {
    logLines.length = 0;
    logStream.textContent = "";
    if (logDuration) logDuration.textContent = "";
    logSection.classList.remove("hidden");
  }

  function showRunDuration(seconds) {
    const pretty = formatDuration(seconds);
    if (logDuration) logDuration.textContent = pretty ? `— finished in ${pretty}` : "";
  }

  function appendLogRecord(rec) {
    const cls = LEVEL_CLASS[rec.level] || "log-detail";
    // Two clocks, because they answer different questions. `t` is how far into its own
    // stage a step happened — every stage restarts at zero — and `total` is how far
    // into the run. Reading elapsed time off `t` alone was impossible.
    const hasTotal = rec.total !== null && rec.total !== undefined;
    const line = `${String(rec.t).padStart(6)}s ${hasTotal ? `[${String(rec.total).padStart(6)}s] ` : ""}`
      + `${rec.marker} ${rec.scope}  ${rec.message}`;
    logLines.push(line);

    const el = document.createElement("div");
    el.className = `log-line ${cls}`;
    el.innerHTML =
      `<span class="log-t">${String(rec.t).padStart(6)}s</span>` +
      (hasTotal ? `<span class="log-total" title="elapsed since the run started">${String(rec.total).padStart(6)}s</span>` : `<span class="log-total"></span>`) +
      `<span class="log-marker">${esc(rec.marker)}</span>` +
      `<span class="log-scope">${esc(rec.scope)}</span>` +
      `<span class="log-msg">${esc(rec.message)}</span>`;
    logStream.appendChild(el);

    if (logAutoscroll.checked) logStream.scrollTop = logStream.scrollHeight;
  }

  function openLogStream(runId) {
    closeLogStream();
    // Opened before the run starts: /api/bridge/start does not return until the whole
    // graph has finished, so a stream attached afterwards would have nothing left to show.
    logSource = new EventSource(`/api/logs/${encodeURIComponent(runId)}`);
    logSource.onmessage = (e) => {
      try { appendLogRecord(JSON.parse(e.data)); } catch (_) { /* keep-alive */ }
    };
    logSource.addEventListener("end", closeLogStream);
    logSource.onerror = () => { /* the run may simply be over; the POST result decides */ };
  }

  function closeLogStream() {
    if (logSource) { logSource.close(); logSource = null; }
  }

  logCopyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(logLines.join("\n"));
      logCopyBtn.textContent = "Copied";
      setTimeout(() => { logCopyBtn.textContent = "Copy"; }, 1500);
    } catch (_) {
      logCopyBtn.textContent = "Copy failed";
      setTimeout(() => { logCopyBtn.textContent = "Copy"; }, 1500);
    }
  });

  // ---------------------------------------------------------------- helpers

  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // "not measured" is a real answer and must read as one. Rendering `undefined` or
  // silently substituting 0 is how an unmeasured figure passes for a measured one.
  function orUnmeasured(value, format) {
    if (value === null || value === undefined) return `<span class="muted">not measured</span>`;
    return esc(format ? format(value) : value);
  }

  const SIM_VERDICTS = {
    VERIFIED: {
      tag: "tag-green",
      label: "✓ eth_call completed, no revert",
      note: "Source leg only. No eth_call can prove funds arrive on the destination chain.",
    },
    REVERTED: {
      tag: "tag-rose",
      label: "✗ Reverted during eth_call",
      note: "The node rejected this transaction. It was not sent.",
    },
    UNAVAILABLE: {
      tag: "tag-amber",
      label: "⚠ Could not check",
      note: "No RPC endpoint could be reached and identity-checked. This is not a pass.",
    },
    UNSUPPORTED: {
      tag: "tag-amber",
      label: "⚠ No eth_call path",
      note: "The chain being submitted to has no simulation path. Nothing was verified.",
    },
  };

  function simVerdict(status) {
    return SIM_VERDICTS[status] || {
      tag: "tag-amber",
      label: `⚠ ${status || "Unknown"}`,
      note: "Simulation status is unknown. Treat this transaction as unverified.",
    };
  }

  // Chain ids the wallet is configured for, by id. Kept next to explorerTxUrl because
  // both answer "what is this chain" and drifting apart would be silent.
  const CHAIN_LABELS = { 1: "Ethereum Mainnet", 10: "Optimism", 137: "Polygon",
                         8453: "Base", 42161: "Arbitrum One" };

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return null;
    const n = Number(seconds);
    if (!isFinite(n)) return null;
    if (n < 10) return `${n.toFixed(2)}s`;
    if (n < 60) return `${n.toFixed(1)}s`;
    const m = Math.floor(Math.round(n) / 60);
    const r = Math.round(n) % 60;
    return `${m}m ${String(r).padStart(2, "0")}s`;
  }

  function chainLabel(chainId) {
    const id = Number(chainId);
    return CHAIN_LABELS[id] || (id ? `chain ${id}` : "an unknown network");
  }

  function explorerTxUrl(chainId, txHash) {
    // Only chains the wallet is configured for. An unknown chain gets no link rather
    // than a basescan.org link that would resolve to a different network's explorer.
    const byChain = { 1: "https://etherscan.io", 10: "https://optimistic.etherscan.io",
                      137: "https://polygonscan.com", 8453: "https://basescan.org",
                      42161: "https://arbiscan.io" };
    const base = byChain[Number(chainId)];
    return base ? `${base}/tx/${txHash}` : null;
  }

  // ---------------------------------------------------------------- telemetry

  function renderTelemetryCards(traces, data) {
    if (!traces) return "";
    let html = `<div class="telemetry-container">`;

    if (traces.step_1_intent) {
      const p = traces.step_1_intent.parsed_parameters || {};
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>🧠</span><span>Step 1: Intent & Parameter Parser</span></div>
            <span class="telemetry-tag tag-blue">Parsed</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">Raw Intent</div><div class="telemetry-val">"${esc(traces.step_1_intent.raw_input)}"</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Route Direction</div><div class="telemetry-val">${esc(p.source_chain)} ➔ ${esc(p.target_chain)}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Amount & Token</div><div class="telemetry-val">${esc(p.amount)} ${esc(p.asset)}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Sender</div><div class="telemetry-val">${esc(p.user_address)}</div></div>
          </div>
        </div>`;
    }

    if (traces.step_2_discovery) {
      const t2 = traces.step_2_discovery;
      const kind = {
        SKILL_MEMORY_CACHE: { tag: "tag-purple", label: "⚡ Skill cache hit" },
        AGENT_RUNTIME_DISCOVERY: { tag: "tag-green", label: "🤖 Discovered at runtime" },
        AGENT_NO_ROUTE: { tag: "tag-amber", label: "⚠ No route found" },
      }[t2.source_type] || { tag: "tag-blue", label: esc(t2.source_type) };

      const ev = data && data.agent_evidence;
      const prov = ev && ev.provenance;
      const proposal = ev && ev.proposal;

      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>🌐</span><span>Step 2: Route Discovery</span></div>
            <span class="telemetry-tag ${kind.tag}">${kind.label}</span>
          </div>
          <div class="telemetry-grid">
            ${t2.discovered_route ? `<div class="telemetry-item"><div class="telemetry-key">Protocol</div><div class="telemetry-val">${esc(t2.discovered_route)}</div></div>` : ""}
            ${t2.router_contract ? `<div class="telemetry-item"><div class="telemetry-key">Router Contract</div><div class="telemetry-val">${esc(t2.router_contract)}</div></div>` : ""}
            ${t2.function_signature || (proposal && proposal.function_signature) ? `<div class="telemetry-item"><div class="telemetry-key">Function</div><div class="telemetry-val">${esc(t2.function_signature || proposal.function_signature)}</div></div>` : ""}
            ${t2.skill_name ? `<div class="telemetry-item"><div class="telemetry-key">Replayed Skill</div><div class="telemetry-val">${esc(t2.skill_name)}</div></div>` : ""}
            ${t2.active_rpc ? `<div class="telemetry-item"><div class="telemetry-key">Active RPC</div><div class="telemetry-val">${esc(t2.active_rpc)}</div></div>` : ""}
            ${t2.model ? `<div class="telemetry-item"><div class="telemetry-key">Model</div><div class="telemetry-val">${esc(t2.model)}</div></div>` : ""}
          </div>
          ${t2.reason ? `<p class="telemetry-note">${esc(t2.reason)}</p>` : ""}
          ${prov ? renderProvenance(prov, proposal) : ""}
          ${t2.budget ? `<p class="telemetry-note">Budget: ${esc(t2.budget.model_calls)} model call(s), ${esc(t2.budget.input_tokens)} in / ${esc(t2.budget.output_tokens)} out tokens, ${orUnmeasured(t2.budget.wall_seconds, (v) => v.toFixed(1) + "s")}.</p>` : ""}
        </div>`;
    }

    if (traces.step_3_codegen) {
      const t3 = traces.step_3_codegen;
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>⚙️</span><span>Step 3: Calldata Synthesis</span></div>
            <span class="telemetry-tag tag-blue">Encoded</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">Selector</div><div class="telemetry-val">${esc(t3.calldata_selector)}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Payload Length</div><div class="telemetry-val">${esc(t3.calldata_length_bytes)} bytes</div></div>
          </div>
          ${t3.ephemeral_python_script ? `<div class="telemetry-subhead">Ephemeral script (generated, never executed server-side):</div><pre class="code-box">${esc(t3.ephemeral_python_script)}</pre>` : ""}
        </div>`;
    }

    if (traces.step_4_simulation) {
      const t4 = traces.step_4_simulation;
      const v = simVerdict(t4.simulation_status);
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>🧪</span><span>Step 4: Dry-Run Simulation (eth_call)</span></div>
            <span class="telemetry-tag ${v.tag}">${v.label}</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">Status</div><div class="telemetry-val">${esc(t4.simulation_status)}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Verified Chain ID</div><div class="telemetry-val">${orUnmeasured(t4.verified_chain_id)}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">RPC Node</div><div class="telemetry-val">${orUnmeasured(t4.rpc_endpoint)}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Gas Units</div><div class="telemetry-val">${orUnmeasured(t4.gas_units)}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Estimated Gas</div><div class="telemetry-val">${orUnmeasured(t4.estimated_gas_usd, (n) => "$" + Number(n).toFixed(4) + " USD")}</div></div>
          </div>
          <p class="telemetry-note">${v.note}</p>
          ${t4.revert_reason ? `<pre class="code-box code-error">${esc(t4.revert_reason)}</pre>` : ""}
        </div>`;
    }

    if (traces.step_error) {
      const te = traces.step_error;
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>🩺</span><span>Diagnostic</span></div>
            <span class="telemetry-tag ${te.will_retry ? "tag-amber" : "tag-rose"}">${te.will_retry ? "Retrying discovery" : "Run ended"}</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">Attempts</div><div class="telemetry-val">${esc(te.retry_count ?? 0)}</div></div>
            ${te.demoted_skill ? `<div class="telemetry-item"><div class="telemetry-key">Skill Demoted</div><div class="telemetry-val">${esc(te.demoted_skill)}</div></div>` : ""}
          </div>
          ${te.error ? `<pre class="code-box code-error">${esc(te.error)}</pre>` : ""}
          ${te.demoted_skill ? `<p class="telemetry-note">The cached route failed verification, so it is no longer served. It is retained on disk — a failure demotes a skill, it never rewrites one.</p>` : ""}
        </div>`;
    }

    html += `</div>`;
    return html;
  }

  function renderProvenance(prov, proposal) {
    if (prov.skipped) {
      return `<p class="telemetry-note">⚠ On-chain provenance not checked: ${esc(prov.skipped)}.</p>`;
    }
    const checks = [
      ["Router holds bytecode", prov.has_code],
      ["Bytecode implements the claimed selector", prov.selector_present],
      ["Sources corroborate", prov.sources_ok],
    ];
    return `
      <div class="provenance">
        <div class="telemetry-subhead">Provenance checks</div>
        ${checks.map(([label, ok]) => `
          <div class="provenance-row">
            <span class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"}</span>
            <span>${esc(label)}</span>
          </div>`).join("")}
        ${prov.implementation_address ? `<p class="telemetry-note">Resolved through EIP-1967 proxy to <code>${esc(prov.implementation_address)}</code>.</p>` : ""}
        ${proposal && proposal.sources && proposal.sources.length ? `<p class="telemetry-note">Sources: ${proposal.sources.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a>`).join(", ")}</p>` : ""}
      </div>`;
  }

  // ------------------------------------------------------- terminal outcomes

  function renderMissingPrerequisite(data) {
    return `
      <div class="verdict verdict-amber">
        <h3>A required step is missing</h3>
        <p>${esc(data.error)}</p>
        <p class="subtitle">
          The route itself is very likely correct — it was found, its router verified
          on chain, and it simulated as far as the token transfer. What is absent is a
          preceding transaction this pipeline does not yet build. Nothing was sent, and
          discovery was <em>not</em> retried: the route is not what failed.
        </p>
      </div>
      ${renderTelemetryCards(data.traces, data)}`;
  }

  function renderConfigError(data) {
    return `
      <div class="verdict verdict-rose">
        <h3>Agent not configured</h3>
        <p>${esc(data.error)}</p>
        <p class="subtitle">
          This is a credentials problem, not a routing one — the request reached
          Anthropic and was refused. An identity-linked API key must name the workspace
          it acts in:
        </p>
        <pre class="code-box">export ANTHROPIC_API_KEY=sk-ant-...
export OMNIBRIDGE_WORKSPACE_ID=wrkspc_...   # only for identity-linked keys</pre>
        <p class="subtitle">Restart the server after setting it. Nothing was sent on-chain.</p>
      </div>
      ${renderTelemetryCards(data.traces, data)}`;
  }

  function renderNoRoute(data) {
    return `
      <div class="verdict verdict-amber">
        <h3>No route found</h3>
        <p>${esc(data.error || "No verified route covers this transfer.")}</p>
        <p class="subtitle">
          This is an answer, not a crash. Nothing was fabricated and nothing was sent.
          A route the system has not verified is a route it does not have — set
          <code>ANTHROPIC_API_KEY</code> so the agent can discover one, or run a pair
          that already has a verified skill.
        </p>
      </div>
      ${renderTelemetryCards(data.traces, data)}`;
  }

  function renderInsufficientFunds(data) {
    // An answer about the wallet, not a failure of the pipeline — and one reached
    // before discovery was paid for, which is worth saying so the absent telemetry
    // does not read as a run that broke early.
    const f = data.funding || {};
    const read = [
      f.asset_balance !== null && f.asset_balance !== undefined
        ? `${esc(f.asset_balance)} ${esc(f.asset_symbol)}`
        : null,
      f.native_balance !== null && f.native_balance !== undefined
        ? `${esc(f.native_balance)} ${esc(f.native_symbol)} for gas`
        : null,
    ].filter(Boolean).join(", ");

    return `
      <div class="verdict verdict-amber">
        <h3>Not enough to send this</h3>
        <p>${esc(data.error || "This wallet cannot fund the transfer.")}</p>
        <p class="subtitle">
          Checked before any route was searched for, so nothing was spent finding one.
          ${read ? `Read on chain: ${read}.` : ""}
          ${f.asset_address_read_only
            ? `<br><span class="muted">${esc(f.asset_symbol)} balance read at
               ${esc(f.asset_address_read_only)}, corroborated by the contract's own
               symbol(). Read-only — this address is never part of a transaction.</span>`
            : ""}
        </p>
      </div>
      ${renderTelemetryCards(data.traces, data)}`;
  }

  function renderUnresolvedChain(data) {
    return `
      <div class="verdict verdict-amber">
        <h3>Chain not resolved</h3>
        <p>${esc(data.error || "That chain could not be resolved.")}</p>
        <p class="subtitle">
          The EVM chain registry does not carry it. That means this source does not
          know the chain — not that the chain does not exist, and not that no route
          exists. Nothing was guessed and nothing was sent.
        </p>
      </div>
      ${renderTelemetryCards(data.traces, data)}`;
  }

  function renderFailure(data) {
    const status = data.verification && data.verification.simulation_status;
    const v = simVerdict(status);
    return `
      <div class="verdict verdict-rose">
        <h3>Not verified — refusing to proceed</h3>
        <p>${esc(data.error || v.note)}</p>
        <p class="subtitle">
          The circuit breaker held. No transaction was offered for signature.
        </p>
      </div>
      ${renderTelemetryCards(data.traces, data)}`;
  }

  // ------------------------------------------------------------ approval gate

  function renderAmountCheck(amount) {
    if (!amount) return "";
    const token = esc(amount.token);

    if (amount.decoded_from_calldata === null || amount.decoded_from_calldata === undefined) {
      return `
        <div class="amount-check amount-warn">
          <div class="amount-label">Amount being sent</div>
          <div class="amount-value">${esc(amount.requested)} ${token}</div>
          <p class="amount-note">
            ⚠ This is the figure you asked for. The calldata is not ABI-decodable, so it
            could not be confirmed against the bytes you are about to sign.
          </p>
        </div>`;
    }

    if (!amount.matches) {
      return `
        <div class="amount-check amount-bad">
          <div class="amount-label">⛔ Amount mismatch — do not approve</div>
          <div class="amount-value">${esc(amount.decoded_from_calldata)} ${token}</div>
          <p class="amount-note">
            This is what the transaction actually moves, decoded from the calldata.
            You asked for ${esc(amount.requested)} ${token}.
          </p>
        </div>`;
    }

    return `
      <div class="amount-check amount-ok">
        <div class="amount-label">Amount being sent</div>
        <div class="amount-value">${esc(amount.decoded_from_calldata)} ${token}</div>
        <p class="amount-note">✓ Decoded from the calldata you are signing, and it matches your request.</p>
      </div>`;
  }

  const STEP_STATE = {
    VERIFIED: { icon: "✓", cls: "step-ok", label: "verified against a live node" },
    REVERTED: { icon: "✗", cls: "step-bad", label: "reverted in simulation" },
    UNAVAILABLE: { icon: "!", cls: "step-warn", label: "could not be checked" },
    UNSUPPORTED: { icon: "!", cls: "step-warn", label: "no simulation path" },
    PENDING_PREREQUISITE: {
      icon: "…", cls: "step-pending",
      label: "not checked yet — verified after the step before it lands",
    },
  };

  function renderPlan(plan) {
    if (!plan || !plan.steps || plan.steps.length < 2) return "";
    return `
      <div class="plan">
        <div class="plan-head">${plan.steps.length} transactions to sign, in order</div>
        ${plan.steps.map((st) => {
          const s = STEP_STATE[st.status] || STEP_STATE.UNAVAILABLE;
          const done = st.tx_hash ? " step-done" : "";
          return `
            <div class="plan-step ${s.cls}${done}">
              <div class="plan-step-head">
                <span class="plan-step-n">${st.index + 1}</span>
                <span class="plan-step-title">${esc(st.title)}</span>
                <span class="plan-step-state">${s.icon} ${st.tx_hash ? "signed" : esc(s.label)}</span>
              </div>
              <div class="plan-step-body">${esc(st.description)}</div>
              ${st.kind === "approve" ? `
                <div class="plan-step-body">
                  <strong>Token:</strong> <code>${esc(st.token_address)}</code><br>
                  <strong>Spender:</strong> <code>${esc(st.spender_address)}</code>
                </div>` : ""}
              ${st.revert_reason ? `<pre class="code-box code-error">${esc(st.revert_reason)}</pre>` : ""}
              ${st.gas_used ? `<div class="plan-step-body"><span class="muted">${st.gas_used} gas</span></div>` : ""}
            </div>`;
        }).join("")}
      </div>`;
  }

  function renderApproval(data) {
    const v = simVerdict(data.verification && data.verification.simulation_status);
    const route = data.route || {};
    const amount = data.amount || {};
    // A mismatch is the one condition where signing must not be one click away.
    const blocked = amount.matches === false;

    return `
      <div class="manifest-card">
        <div class="manifest-head">
          <h3>Step 5: Human Approval</h3>
          <span class="telemetry-tag ${v.tag}">${v.label}</span>
        </div>

        ${renderAmountCheck(amount)}
        ${renderPlan(data.plan)}

        <div class="manifest-grid">
          <div class="manifest-item"><div class="manifest-label">Route</div><div class="manifest-val">${esc(route.source)} ➔ ${esc(route.target)}</div></div>
          <div class="manifest-item"><div class="manifest-label">Protocol</div><div class="manifest-val">${esc(route.bridge)}</div></div>
          <div class="manifest-item"><div class="manifest-label">Router (you are calling this)</div><div class="manifest-val mono">${esc(route.router_address)}</div></div>
          <div class="manifest-item"><div class="manifest-label">Function</div><div class="manifest-val mono">${esc(route.function_signature) || '<span class="muted">unknown</span>'}</div></div>
          <div class="manifest-item"><div class="manifest-label">Verified on chain</div><div class="manifest-val">${orUnmeasured(data.verification && data.verification.simulated_on_chain_id)}</div></div>
          <div class="manifest-item"><div class="manifest-label">Estimated gas</div><div class="manifest-val">${orUnmeasured(data.estimated_gas_usd, (n) => "$" + Number(n).toFixed(4) + " USD")}</div></div>
        </div>

        <p class="scope-note">${esc((data.verification && data.verification.scope) || v.note)}</p>
        ${data.replayed_skill ? `<p class="scope-note">Replayed from cached skill <code>${esc(data.replayed_skill)}</code>. It was re-simulated for this request — a skill is a shortcut past discovery, not past the guardrails.</p>` : ""}

        <div class="manifest-actions">
          <button id="confirm-sign-btn" class="btn ${blocked ? "btn-danger" : "btn-success"}" ${blocked ? "disabled" : ""}>
            <span>${blocked
              ? "Blocked — amount does not match"
              : (currentPlan && currentPlan.steps.length > 1
                  ? `Sign step 1 of ${currentPlan.steps.length}: ${nextPlanStep(currentPlan) ? nextPlanStep(currentPlan).title : ""}`
                  : "Sign in Wallet & Broadcast")}</span>
            <span>${blocked ? "⛔" : "🚀"}</span>
          </button>
          <button id="cancel-btn" class="btn btn-secondary">Reject</button>
        </div>

        <details class="raw-calldata">
          <summary>Raw calldata (${((data.unsigned_transaction_hex || "").length - 2) / 2} bytes)</summary>
          <pre class="code-box">${esc(data.unsigned_transaction_hex)}</pre>
        </details>

        <h4 class="telemetry-heading">Execution traces</h4>
        ${renderTelemetryCards(data.traces, data)}
      </div>`;
  }

  // Drive the pipeline from what the graph actually recorded, not from a timer. The
  // steps used to light up on setTimeout at 300/700/1200ms, so a run that failed at
  // discovery still showed simulation as completed.
  function syncPipeline(data) {
    resetPipeline();
    const t = data.traces || {};
    const reached = [
      t.step_1_intent,
      t.step_2_discovery,
      t.step_3_codegen,
      t.step_4_simulation,
      data.status === "AWAITING_USER_APPROVAL" || t.step_6_broadcast,
      t.step_6_broadcast,
    ];
    const lastDone = reached.lastIndexOf(reached.filter(Boolean).pop());
    reached.forEach((done, i) => {
      if (!done) return;
      steps[i].classList.add(i === lastDone ? "active" : "completed");
    });
  }

  // ---------------------------------------------------------------- submit

  let currentAmount = null;
  let currentSourceChainId = null;
  let currentPlan = null;
  let currentRunId = null;

  function nextPlanStep(plan) {
    return plan && plan.steps ? plan.steps.find((s) => !s.tx_hash) : null;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const query = intentInput.value.trim();
    if (!query) return;

    resetPipeline();
    setStepActive(1);
    outputSection.classList.remove("hidden");
    outputSection.innerHTML = `
      <div class="working">
        <div class="working-title">Resolving route…</div>
        <p class="subtitle">Parsing intent, resolving the chain, discovering or replaying a route, encoding calldata, then simulating against a live node.</p>
      </div>`;

    executeBtn.disabled = true;

    try {
      // The wallet is required. It used to be omitted and defaulted server-side to a
      // hardcoded mainnet address, so an unconnected user got a simulation against a
      // stranger's balances — which tells them nothing about their own transfer.
      if (!userConnectedAddress) {
        const connected = await triggerBlocknativeConnect();
        if (!connected) {
          outputSection.innerHTML = `
            <div class="verdict verdict-amber">
              <h3>Connect a wallet first</h3>
              <p>Simulation runs from your address: balances and allowances are what
                 determine whether this transfer would succeed. Running it from anyone
                 else's account would not describe your transaction.</p>
            </div>`;
          resetPipeline();
          return;
        }
      }

      currentRunId = `run_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
      resetLog();
      openLogStream(currentRunId);

      const response = await fetch("/api/bridge/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: query, userAddress: userConnectedAddress, runId: currentRunId }),
      });

      const data = await response.json();
      closeLogStream();
      // Reported on every outcome, including failures — a run that took two minutes to
      // refuse is worth knowing about as much as one that took two minutes to succeed.
      showRunDuration(data.duration_seconds);
      syncPipeline(data);

      console.groupCollapsed("🌉 Omnibridge execution telemetry");
      console.log("status:", data.status);
      console.log("route:", data.route);
      console.log("verification:", data.verification);
      console.log("agent evidence:", data.agent_evidence);
      console.log("budget:", data.budget);
      console.log("traces:", data.traces);
      console.groupEnd();

      if (data.status === "AWAITING_USER_APPROVAL") {
        currentThreadId = data.thread_id;
        currentUnsignedHex = data.unsigned_transaction_hex;
        currentRouterAddress = data.route.router_address;
        currentSourceChainId = data.route.source_chain_id;
        currentAmount = data.amount;
        currentPlan = data.plan || null;

        outputSection.innerHTML = renderApproval(data);
        document.getElementById("confirm-sign-btn").addEventListener("click", handleBlocknativeSignAndBroadcast);
        document.getElementById("cancel-btn").addEventListener("click", () => {
          outputSection.innerHTML = `<div class="verdict verdict-amber"><h3>Rejected</h3><p>Nothing was signed and nothing was broadcast.</p></div>`;
          resetPipeline();
        });
      } else if (data.status === "INSUFFICIENT_FUNDS") {
        outputSection.innerHTML = renderInsufficientFunds(data);
      } else if (data.status === "UNRESOLVED_CHAIN") {
        outputSection.innerHTML = renderUnresolvedChain(data);
      } else if (data.status === "MISSING_PREREQUISITE") {
        outputSection.innerHTML = renderMissingPrerequisite(data);
      } else if (data.status === "CONFIG_ERROR") {
        outputSection.innerHTML = renderConfigError(data);
      } else if (data.status === "NO_ROUTE_FOUND") {
        outputSection.innerHTML = renderNoRoute(data);
      } else {
        outputSection.innerHTML = renderFailure(data);
      }

      // A failed run may have retired a cached skill; the library should reflect it.
      if (data.demoted_skill) loadSkills();
    } catch (err) {
      closeLogStream();
      outputSection.innerHTML = `<div class="verdict verdict-rose"><h3>Request failed</h3><p>${esc(err.message)}</p></div>`;
    } finally {
      executeBtn.disabled = false;
    }
  });

  // -------------------------------------------------- step 6: sign & broadcast

  async function handleBlocknativeSignAndBroadcast() {
    const signBtn = document.getElementById("confirm-sign-btn");

    // Defence in depth: the button is disabled on a mismatch, but a disabled button is
    // a UI state, not a guarantee. Nothing reaches a wallet without this check passing.
    if (currentAmount && currentAmount.matches === false) {
      outputSection.insertAdjacentHTML("afterbegin",
        `<div class="banner banner-rose"><strong>Blocked:</strong> the calldata moves
         ${esc(currentAmount.decoded_from_calldata)} ${esc(currentAmount.token)}, not the
         ${esc(currentAmount.requested)} ${esc(currentAmount.token)} you asked for.</div>`);
      return;
    }

    let connectedWallets = onboard.state.get().wallets;
    if (!connectedWallets || connectedWallets.length === 0) {
      connectedWallets = await onboard.connectWallet();
      if (!connectedWallets || connectedWallets.length === 0) {
        outputSection.insertAdjacentHTML("afterbegin",
          `<div class="banner banner-amber"><strong>Wallet required:</strong> connect a wallet to authorise this transaction.</div>`);
        return;
      }
    }

    const primaryWallet = connectedWallets[0];
    const fromAddress = primaryWallet.accounts[0].address;
    const provider = primaryWallet.provider;

    // The wallet's network decides where this lands, and nothing above here has
    // consulted it. eth_sendTransaction carries no chain id: it executes on whatever
    // network the wallet happens to be on, which for a fresh MetaMask is Ethereum
    // Mainnet regardless of the route. The router address, the calldata and the
    // simulation are all specific to the source chain, so signing on the wrong one
    // sends real funds to an address that on that chain is a different contract, or
    // no contract at all. Refuse rather than hope.
    if (!currentSourceChainId) {
      signBtn.disabled = false;
      signBtn.innerHTML = `<span>Sign in Wallet & Broadcast</span><span>🚀</span>`;
      outputSection.insertAdjacentHTML("afterbegin",
        `<div class="banner banner-rose"><strong>Not signable:</strong> the route did not
         report a source chain id, so the target network cannot be confirmed.
         Nothing was sent.</div>`);
      return;
    }

    const wantChainHex = "0x" + Number(currentSourceChainId).toString(16);
    const walletChainHex = (primaryWallet.chains && primaryWallet.chains[0]
      && primaryWallet.chains[0].id) || null;

    if (!walletChainHex || walletChainHex.toLowerCase() !== wantChainHex.toLowerCase()) {
      signBtn.innerHTML = `<span>Switching your wallet to ${esc(chainLabel(currentSourceChainId))}…</span>`;
      let switched = false;
      try {
        switched = await onboard.setChain({ chainId: wantChainHex });
      } catch (switchErr) {
        switched = false;
      }
      // setChain resolves false when the user declines or the wallet does not carry the
      // network. Either way the wallet is still on the wrong chain, so stop here.
      const nowHex = ((onboard.state.get().wallets[0] || {}).chains || [{}])[0].id || null;
      if (!switched || !nowHex || nowHex.toLowerCase() !== wantChainHex.toLowerCase()) {
        signBtn.disabled = false;
        signBtn.innerHTML = `<span>Sign in Wallet & Broadcast</span><span>🚀</span>`;
        setStepActive(5);
        outputSection.insertAdjacentHTML("afterbegin",
          `<div class="banner banner-rose"><strong>Wrong network:</strong> this route runs on
           ${esc(chainLabel(currentSourceChainId))} (chain ${esc(String(currentSourceChainId))}),
           but your wallet is on ${esc(chainLabel(parseInt(walletChainHex || "0", 16)))}.
           The router address and the simulation are specific to
           ${esc(chainLabel(currentSourceChainId))}. Switch networks in your wallet and
           sign again — nothing was sent.</div>`);
        return;
      }
    }

    signBtn.disabled = true;
    signBtn.innerHTML = `<span>Awaiting signature in ${esc(primaryWallet.label || "your wallet")}…</span>`;
    setStepActive(6);

    // Walk the plan in order. Each step is signed only after being simulated against
    // the chain as it is *now* — the bridge call cannot be checked before its approval
    // lands, so it is checked between the two signatures rather than optimistically
    // before either.
    const steps = (currentPlan && currentPlan.steps) || [
      { index: 0, kind: "bridge", title: "Bridge", to: currentRouterAddress,
        data: currentUnsignedHex, value_wei: 0, status: "VERIFIED", tx_hash: null },
    ];

    let realTxHash = null;

    for (let i = 0; i < steps.length; i++) {
      const step = steps[i];
      if (step.tx_hash) continue;

      if (step.status !== "VERIFIED") {
        signBtn.disabled = false;
        outputSection.insertAdjacentHTML("afterbegin",
          `<div class="banner banner-rose"><strong>Not signable:</strong> step ${step.index + 1}
           (${esc(step.title)}) is ${esc(step.status)}${step.revert_reason ? " — " + esc(step.revert_reason) : ""}.
           Nothing further was sent.</div>`);
        return;
      }

      signBtn.innerHTML = `<span>Step ${step.index + 1} of ${steps.length} — awaiting signature in ${esc(primaryWallet.label || "your wallet")}…</span>`;

      let hash;
      try {
        hash = await provider.request({
          method: "eth_sendTransaction",
          params: [{ from: fromAddress, to: step.to, data: step.data,
                     value: "0x" + Number(step.value_wei || 0).toString(16) }],
        });
      } catch (walletErr) {
        signBtn.disabled = false;
        signBtn.innerHTML = `<span>Sign in Wallet & Broadcast</span><span>🚀</span>`;
        setStepActive(5);
        const done = steps.filter((s2) => s2.tx_hash).length;
        outputSection.insertAdjacentHTML("afterbegin",
          `<div class="banner banner-rose"><strong>Not signed:</strong>
           ${esc(walletErr.message || "cancelled in wallet")}.
           ${done ? `Step${done > 1 ? "s" : ""} 1–${done} already went through; the transfer is incomplete.`
                  : "Nothing was broadcast."}</div>`);
        return;
      }

      step.tx_hash = hash;
      realTxHash = hash;

      // Not the last step: tell the server, and let it verify the next one against
      // live state before we ask for another signature.
      if (i < steps.length - 1) {
        signBtn.innerHTML = `<span>Step ${step.index + 1} sent — verifying the next against the chain…</span>`;
        try {
          const adv = await fetch("/api/bridge/advance", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ thread_id: currentThreadId, step_index: step.index,
                                   tx_hash: hash, runId: currentRunId }),
          });
          if (!adv.ok) throw new Error((await adv.json().catch(() => ({}))).detail || `server ${adv.status}`);
          const advData = await adv.json();
          currentPlan = advData.plan;
          steps.splice(0, steps.length, ...advData.plan.steps);
        } catch (err) {
          outputSection.innerHTML = `
            <div class="verdict verdict-rose">
              <h3>Could not verify the next step</h3>
              <p>${esc(err.message)}</p>
              <p class="subtitle">Step ${step.index + 1} was signed as <code>${esc(hash)}</code>.
                 The remaining steps were not sent, so the transfer is incomplete —
                 check that transaction on-chain before retrying.</p>
            </div>`;
          return;
        }
      }
    }

    try {
      signBtn.innerHTML = `<span>Recording broadcast and saving SKILL.md…</span>`;

      const resp = await fetch("/api/bridge/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thread_id: currentThreadId, signed_transaction_hex: realTxHash }),
      });

      // The old handler rendered "Broadcast Confirmed On-Chain!" unconditionally,
      // including when the server had refused the resume. Report what came back.
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || `server returned ${resp.status}`);
      }

      const resData = await resp.json();

      if (resData.status !== "CONFIRMED") {
        outputSection.innerHTML = `
          <div class="verdict verdict-rose">
            <h3>Broadcast not recorded — status ${esc(resData.status)}</h3>
            <p>${esc(resData.error || "The graph did not reach the broadcast node.")}</p>
            <p class="subtitle">Your wallet reported transaction <code>${esc(realTxHash)}</code>. Check it on-chain before retrying — it may already be in flight.</p>
          </div>
          ${renderTelemetryCards(resData.traces, resData)}`;
        return;
      }

      setAllCompleted();
      const url = resData.explorer_url || explorerTxUrl(currentSourceChainId, realTxHash);

      outputSection.innerHTML = `
        <div class="success-box">
          <div class="success-header"><span>✓ Broadcast submitted</span></div>
          <p>Submitted to the source chain. Cross-chain delivery is not confirmed by this
             step — the destination leg settles on the bridge's own schedule.</p>
          <div class="manifest-grid">
            <div class="manifest-item">
              <div class="manifest-label">Transaction hash</div>
              <div class="manifest-val mono">
                ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(realTxHash)} ↗</a>` : esc(realTxHash)}
              </div>
            </div>
            <div class="manifest-item">
              <div class="manifest-label">Skill persisted</div>
              <div class="manifest-val mono">${esc(resData.skill_persisted) || '<span class="muted">none written</span>'}</div>
            </div>
          </div>
        </div>
        ${renderTelemetryCards(resData.traces, resData)}`;

      loadSkills();
    } catch (err) {
      outputSection.innerHTML = `
        <div class="verdict verdict-rose">
          <h3>Could not record the broadcast</h3>
          <p>${esc(err.message)}</p>
          <p class="subtitle">Your wallet reported <code>${esc(realTxHash)}</code>. The transaction may be on-chain even though the server did not record it — verify before retrying.</p>
        </div>`;
    }
  }
});
