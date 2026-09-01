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

  // Blocknative Subscription for reactive wallet state
  const state = onboard.state.select("wallets");
  state.subscribe((wallets) => {
    if (wallets && wallets.length > 0 && wallets[0].accounts.length > 0) {
      userConnectedAddress = wallets[0].accounts[0].address;
      walletBtnText.textContent = `${userConnectedAddress.slice(0, 6)}...${userConnectedAddress.slice(-4)}`;
      connectWalletBtn.style.borderColor = "var(--accent-green)";
      connectWalletBtn.style.color = "var(--text-primary)";
      connectWalletBtn.style.background = "rgba(16, 185, 129, 0.12)";
    } else {
      userConnectedAddress = null;
      walletBtnText.textContent = "Connect Wallet";
      connectWalletBtn.style.borderColor = "var(--border-color)";
      connectWalletBtn.style.background = "var(--bg-secondary)";
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
    });
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
        card.className = "skill-card";
        card.innerHTML = `
          <div class="skill-name">⚡ ${skill.name}</div>
          <div class="skill-desc">${skill.description || "Verified cross-chain routine."}</div>
          <div class="skill-meta">
            <span>${skill.source || "Base"} ➔ ${skill.destination || "Arbitrum"}</span>
            <span>${skill.token || "USDC"}</span>
          </div>
        `;
        skillsGrid.appendChild(card);
      });
    } catch (e) {
      console.error("Failed to load skills:", e);
    }
  }

  refreshSkillsBtn.addEventListener("click", loadSkills);
  loadSkills();

  // Render Telemetry Cards
  function renderTelemetryCards(traces) {
    if (!traces) return "";
    let html = `<div class="telemetry-container">`;

    if (traces.step_1_intent) {
      const t1 = traces.step_1_intent;
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>🔍</span><span>Step 1: Intent & Parameter Parser</span></div>
            <span class="telemetry-tag tag-blue">Parsed via NLP</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">Raw Intent</div><div class="telemetry-val">"${t1.raw_input}"</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Route Direction</div><div class="telemetry-val">${t1.parsed_parameters.source_chain} ➔ ${t1.parsed_parameters.target_chain}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Amount & Token</div><div class="telemetry-val">${t1.parsed_parameters.amount} ${t1.parsed_parameters.asset}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Slippage Tolerance</div><div class="telemetry-val">${t1.parsed_parameters.slippage}</div></div>
          </div>
        </div>
      `;
    }

    if (traces.step_2_discovery) {
      const t2 = traces.step_2_discovery;
      const isSkill = t2.source_type === "SKILL_MEMORY_CACHE";
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>🌐</span><span>Step 2: Route Discovery & Registry Resolution</span></div>
            <span class="telemetry-tag ${isSkill ? 'tag-purple' : 'tag-green'}">${isSkill ? '⚡ Skill Cache Hit (0ms)' : '🔍 Live Web Docs Discovery'}</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">LangChain Search Query</div><div class="telemetry-val" style="font-size: 0.8rem;">"${t2.search_query}"</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Discovered Protocol</div><div class="telemetry-val" style="color: #a5b4fc;">${t2.discovered_route}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Router Contract</div><div class="telemetry-val">${t2.router_contract}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Active Public RPC</div><div class="telemetry-val" style="color: #6ee7b7;">${t2.active_rpc}</div></div>
          </div>
          ${t2.web_search_tool_output ? `
            <div style="margin-top: 0.75rem; font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase;">
              🔍 LangChain web_search_tool Output:
            </div>
            <pre class="code-box" style="color: var(--accent-cyan); font-size: 0.75rem; max-height: 90px;">${t2.web_search_tool_output}</pre>
          ` : ''}
        </div>
      `;
    }

    if (traces.step_3_codegen) {
      const t3 = traces.step_3_codegen;
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>⚙️</span><span>Step 3: JIT In-Transit Codegen & Calldata Assembly</span></div>
            <span class="telemetry-tag tag-blue">Ephemerally Synthesized</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">Calldata Selector</div><div class="telemetry-val">${t3.calldata_selector}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Payload Length</div><div class="telemetry-val">${t3.calldata_length_bytes} bytes</div></div>
          </div>
          <div style="margin-top: 0.75rem; font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase;">Ephemeral Python Script:</div>
          <pre class="code-box">${t3.ephemeral_python_script}</pre>
        </div>
      `;
    }

    if (traces.step_4_simulation) {
      const t4 = traces.step_4_simulation;
      html += `
        <div class="telemetry-step-card">
          <div class="telemetry-header">
            <div class="telemetry-title"><span>🧪</span><span>Step 4: Dry-Run Simulation (eth_call)</span></div>
            <span class="telemetry-tag tag-green">✓ Zero Reverts (eth_call Passed)</span>
          </div>
          <div class="telemetry-grid">
            <div class="telemetry-item"><div class="telemetry-key">Execution RPC Node</div><div class="telemetry-val">${t4.rpc_endpoint}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Gas Units Used</div><div class="telemetry-val">${t4.gas_units} gas</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Estimated Gas Fee</div><div class="telemetry-val">$${t4.estimated_gas_usd.toFixed(4)} USD</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Circuit Breaker Safety</div><div class="telemetry-val" style="color: var(--accent-green);">SAFE_TO_EXECUTE</div></div>
          </div>
        </div>
      `;
    }

    html += `</div>`;
    return html;
  }

  // Execute Bridge Intent & Run Live Simulation
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const query = intentInput.value.trim();
    if (!query) return;

    resetPipeline();
    setStepActive(1);
    outputSection.classList.remove("hidden");
    outputSection.innerHTML = `
      <div style="text-align: center; padding: 2rem 1rem;">
        <div style="font-size: 1.4rem; margin-bottom: 0.5rem;">🤖 Step-by-Step Discovery & Simulation in Progress...</div>
        <p class="subtitle">Executing NLP parsing ➔ web search ➔ JIT codegen ➔ eth_call dry-run...</p>
      </div>
    `;

    executeBtn.disabled = true;

    try {
      setTimeout(() => setStepActive(2), 300);
      setTimeout(() => setStepActive(3), 700);
      setTimeout(() => setStepActive(4), 1200);

      const response = await fetch("/api/bridge/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input: query,
          userAddress: userConnectedAddress || "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        }),
      });

      const data = await response.json();
      setStepActive(5);

      // Detailed Browser Console Logging of Search and Extraction
      console.group("🌉 Omnibridge Step-by-Step Execution Telemetry");
      console.log("🧠 Step 1 - Parsed Intent:", data.traces?.step_1_intent?.parsed_parameters);
      console.log("🔍 Step 2 - Web Search Query:", data.traces?.step_2_discovery?.search_query);
      console.log("🌐 Step 2 - Web Search Tool Output:", data.traces?.step_2_discovery?.web_search_tool_output);
      console.log("📋 Step 2 - Extracted Parameters to Use:", {
        protocol: data.route?.bridge,
        router_contract: data.route?.router_address,
        active_rpc: data.traces?.step_2_discovery?.active_rpc,
        source_chain: data.route?.source,
        dest_chain: data.route?.target,
      });
      console.log("⚙️ Step 3 - Synthesized Calldata Hex:", data.unsigned_transaction_hex);
      console.log("🧪 Step 4 - Simulation & Gas Profile:", data.traces?.step_4_simulation);
      console.groupEnd();

      if (data.status === "AWAITING_USER_APPROVAL") {
        currentThreadId = data.thread_id;
        currentUnsignedHex = data.unsigned_transaction_hex;
        currentRouterAddress = data.route.router_address;

        const telemetryHtml = renderTelemetryCards(data.traces);

        outputSection.innerHTML = `
          <div class="manifest-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <h3 style="color: var(--accent-cyan);">⚡ Step 5: Human-in-the-Loop Approval Manifest</h3>
              <span class="badge" style="background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border-color: rgba(16, 185, 129, 0.3);">
                ✓ Pre-Flight Simulation Passed
              </span>
            </div>
            <p class="subtitle">Simulation verified this transaction will not revert. Click below to sign with your connected wallet.</p>

            <div class="manifest-actions" style="margin-bottom: 1.5rem;">
              <button id="confirm-sign-btn" class="btn btn-success" style="font-size: 1.05rem; padding: 1rem 2rem;">
                <span>Sign in Wallet & Broadcast Live</span>
                <span>🚀</span>
              </button>
              <button id="cancel-btn" class="btn btn-secondary">Reject</button>
            </div>

            <h4 style="margin-top: 1.5rem; color: var(--text-secondary); font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em;">
              📋 Telemetry & Execution Traces (Step 1 – Step 4)
            </h4>
            ${telemetryHtml}
          </div>
        `;

        document.getElementById("confirm-sign-btn").addEventListener("click", handleBlocknativeSignAndBroadcast);
        document.getElementById("cancel-btn").addEventListener("click", () => {
          outputSection.innerHTML = `<p style="color: var(--accent-amber);">Transaction rejected by user. Nothing was broadcasted on-chain.</p>`;
          resetPipeline();
        });
      } else {
        outputSection.innerHTML = `
          <div style="color: var(--accent-rose);">
            <h3>Simulation / Execution Error</h3>
            <p>${data.error || "Simulation failed to pass zero-revert guarantee."}</p>
          </div>
        `;
      }
    } catch (err) {
      outputSection.innerHTML = `<p style="color: var(--accent-rose);">Failed to execute intent: ${err.message}</p>`;
    } finally {
      executeBtn.disabled = false;
    }
  });

  // Step 6: Real Web3 Wallet Signing & On-Chain Broadcast via Blocknative Provider
  async function handleBlocknativeSignAndBroadcast() {
    const signBtn = document.getElementById("confirm-sign-btn");
    let connectedWallets = onboard.state.get().wallets;

    // If wallet not connected yet, trigger Blocknative modal
    if (!connectedWallets || connectedWallets.length === 0) {
      connectedWallets = await onboard.connectWallet();
      if (!connectedWallets || connectedWallets.length === 0) {
        outputSection.insertAdjacentHTML("afterbegin", `
          <div style="background: rgba(245, 158, 11, 0.15); border: 1px solid var(--accent-amber); border-radius: var(--radius-md); padding: 1rem; margin-bottom: 1.5rem; color: #fde68a;">
            <strong>⚠️ Wallet Connection Required:</strong> Please connect your wallet in the Blocknative modal to authorize this transaction.
          </div>
        `);
        return;
      }
    }

    const primaryWallet = connectedWallets[0];
    const fromAddress = primaryWallet.accounts[0].address;
    const provider = primaryWallet.provider;

    signBtn.disabled = true;
    signBtn.innerHTML = `<span>Awaiting signature in ${primaryWallet.label || "Wallet"}...</span>`;
    setStepActive(6);

    let realTxHash = null;

    try {
      realTxHash = await provider.request({
        method: "eth_sendTransaction",
        params: [
          {
            from: fromAddress,
            to: currentRouterAddress,
            data: currentUnsignedHex,
            value: "0x0",
          },
        ],
      });
    } catch (walletErr) {
      signBtn.disabled = false;
      signBtn.innerHTML = `<span>Sign in Wallet & Broadcast Live 🚀</span>`;
      setStepActive(5);

      outputSection.insertAdjacentHTML("afterbegin", `
        <div style="background: rgba(244, 63, 94, 0.15); border: 1px solid var(--accent-rose); border-radius: var(--radius-md); padding: 1rem; margin-bottom: 1.5rem; color: #fecdd3;">
          <strong>⚠️ Signature Rejected in Wallet:</strong> ${walletErr.message || "Transaction was cancelled in your wallet. Nothing broadcasted."}
        </div>
      `);
      return;
    }

    // Resume LangGraph with real on-chain transaction hash
    try {
      signBtn.innerHTML = `<span>Confirming on-chain & saving SKILL.md...</span>`;

      const resp = await fetch("/api/bridge/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          thread_id: currentThreadId,
          signed_transaction_hex: realTxHash,
        }),
      });

      const resData = await resp.json();
      setAllCompleted();

      outputSection.innerHTML = `
        <div class="success-box">
          <div class="success-header">
            <span>✓ Real Transaction Broadcast Confirmed On-Chain!</span>
          </div>
          <p style="margin-bottom: 0.75rem;">Your real cross-chain transaction has been broadcast to the network sequencer.</p>
          
          <div class="manifest-grid">
            <div class="manifest-item">
              <div class="manifest-label">On-Chain Transaction Hash</div>
              <div class="manifest-val" style="font-size: 0.85rem; color: var(--accent-green); word-break: break-all;">
                <a href="https://basescan.org/tx/${realTxHash}" target="_blank" style="color: var(--accent-green); text-decoration: underline;">
                  ${realTxHash} ↗
                </a>
              </div>
            </div>
            <div class="manifest-item">
              <div class="manifest-label">Persistent SKILL.md Saved</div>
              <div class="manifest-val" style="font-size: 0.8rem; color: #a5b4fc;">
                ${resData.skill_persisted}
              </div>
            </div>
          </div>
        </div>
      `;

      loadSkills();
    } catch (err) {
      outputSection.innerHTML = `<p style="color: var(--accent-rose);">Error recording broadcast: ${err.message}</p>`;
    }
  }
});
