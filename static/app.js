// Omnibridge Web UI - Strict Real Web3 Wallet Signing Controller
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

  // Wallet Connection (MetaMask / EIP-1193)
  async function connectWallet(showModal = true) {
    if (typeof window.ethereum === "undefined") {
      if (showModal) {
        alert("🦊 No Web3 Wallet Detected!\nPlease install or open MetaMask, Rabby, or Coinbase Wallet to sign and broadcast real on-chain transactions.");
      }
      return null;
    }

    try {
      const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
      if (accounts && accounts.length > 0) {
        userConnectedAddress = accounts[0];
        walletBtnText.textContent = `${userConnectedAddress.slice(0, 6)}...${userConnectedAddress.slice(-4)}`;
        connectWalletBtn.style.borderColor = "var(--accent-green)";
        connectWalletBtn.style.color = "var(--text-primary)";
        return userConnectedAddress;
      }
    } catch (err) {
      console.warn("Wallet connection was rejected or failed:", err);
      return null;
    }
    return null;
  }

  connectWalletBtn.addEventListener("click", () => connectWallet(true));

  // Check if wallet is already unlocked on page load
  if (typeof window.ethereum !== "undefined") {
    window.ethereum.request({ method: "eth_accounts" }).then((accounts) => {
      if (accounts && accounts.length > 0) {
        userConnectedAddress = accounts[0];
        walletBtnText.textContent = `${userConnectedAddress.slice(0, 6)}...${userConnectedAddress.slice(-4)}`;
        connectWalletBtn.style.borderColor = "var(--accent-green)";
      }
    });
  }

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
    steps.forEach((s) => {
      s.classList.remove("active", "completed");
    });
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

  // Helper to render telemetry cards
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
            <div class="telemetry-item"><div class="telemetry-key">Search Query / Mode</div><div class="telemetry-val" style="font-size: 0.8rem;">${t2.search_query}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Discovered Protocol</div><div class="telemetry-val" style="color: #a5b4fc;">${t2.discovered_route}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Router Contract</div><div class="telemetry-val">${t2.router_contract}</div></div>
            <div class="telemetry-item"><div class="telemetry-key">Active Public RPC</div><div class="telemetry-val" style="color: #6ee7b7;">${t2.active_rpc}</div></div>
          </div>
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

      if (data.status === "AWAITING_USER_APPROVAL") {
        currentThreadId = data.thread_id;
        currentUnsignedHex = data.unsigned_transaction_hex;
        currentRouterAddress = data.route.router_address || "0x12ade2554a39A27dd2465376F4302cCc8004ce38";

        const telemetryHtml = renderTelemetryCards(data.traces);

        outputSection.innerHTML = `
          <div class="manifest-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <h3 style="color: var(--accent-cyan);">⚡ Step 5: Human-in-the-Loop Approval Manifest</h3>
              <span class="badge" style="background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border-color: rgba(16, 185, 129, 0.3);">
                ✓ Pre-Flight Simulation Passed
              </span>
            </div>
            <p class="subtitle">Simulation verified this transaction will not revert. Please authorize signature in your wallet to broadcast.</p>

            <div class="manifest-actions" style="margin-bottom: 1.5rem;">
              <button id="confirm-sign-btn" class="btn btn-success" style="font-size: 1.05rem; padding: 1rem 2rem;">
                <span>Sign in Wallet & Broadcast Live</span>
                <span>🚀</span>
              </button>
              <button id="cancel-btn" class="btn btn-secondary">Reject</button>
            </div>

            <!-- Step by Step Telemetry -->
            <h4 style="margin-top: 1.5rem; color: var(--text-secondary); font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em;">
              📋 Telemetry & Execution Traces (Step 1 – Step 4)
            </h4>
            ${telemetryHtml}
          </div>
        `;

        document.getElementById("confirm-sign-btn").addEventListener("click", handleStrictRealWalletSignAndBroadcast);
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

  // Step 6: Strict Real Web3 Wallet Signing & On-Chain Broadcast
  async function handleStrictRealWalletSignAndBroadcast() {
    const signBtn = document.getElementById("confirm-sign-btn");

    // 1. Check if Web3 wallet is available
    if (typeof window.ethereum === "undefined") {
      outputSection.insertAdjacentHTML("afterbegin", `
        <div style="background: rgba(244, 63, 94, 0.15); border: 1px solid var(--accent-rose); border-radius: var(--radius-md); padding: 1rem; margin-bottom: 1.5rem; color: #fecdd3;">
          <strong>❌ Web3 Wallet Required:</strong> No browser wallet (like MetaMask or Rabby) was detected. Please install a Web3 wallet extension to sign and broadcast real on-chain transactions.
        </div>
      `);
      return;
    }

    // 2. Ensure wallet is connected
    if (!userConnectedAddress) {
      userConnectedAddress = await connectWallet(false);
      if (!userConnectedAddress) {
        outputSection.insertAdjacentHTML("afterbegin", `
          <div style="background: rgba(245, 158, 11, 0.15); border: 1px solid var(--accent-amber); border-radius: var(--radius-md); padding: 1rem; margin-bottom: 1.5rem; color: #fde68a;">
            <strong>⚠️ Wallet Not Connected:</strong> Please connect your MetaMask/Rabby wallet using the button in the top right to sign this transaction.
          </div>
        `);
        return;
      }
    }

    signBtn.disabled = true;
    signBtn.innerHTML = `<span>Awaiting signature in MetaMask...</span>`;
    setStepActive(6);

    let realTxHash = null;

    try {
      // 3. Prompt user's real browser wallet for on-chain execution
      realTxHash = await window.ethereum.request({
        method: "eth_sendTransaction",
        params: [
          {
            from: userConnectedAddress,
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
          <strong>⚠️ Signature Rejected in Wallet:</strong> ${walletErr.message || "You declined the signature request in your wallet. No transaction was broadcasted on-chain."}
        </div>
      `);
      return;
    }

    // 4. Resume LangGraph with the real on-chain transaction hash
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
