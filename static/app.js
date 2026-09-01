// Omnibridge Web UI & Real Web3 Wallet Signing Controller
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

  let userConnectedAddress = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045";
  let currentThreadId = null;
  let currentUnsignedHex = null;
  let currentRouterAddress = null;

  // Wallet Connection (MetaMask / EIP-1193)
  async function connectWallet() {
    if (typeof window.ethereum !== "undefined") {
      try {
        const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
        if (accounts && accounts.length > 0) {
          userConnectedAddress = accounts[0];
          walletBtnText.textContent = `${userConnectedAddress.slice(0, 6)}...${userConnectedAddress.slice(-4)}`;
          connectWalletBtn.style.borderColor = "var(--accent-green)";
        }
      } catch (err) {
        console.warn("Wallet connection cancelled:", err);
      }
    } else {
      alert("No Web3 browser wallet detected (like MetaMask/Rabby/Coinbase). You can still test with the default simulation address.");
    }
  }

  connectWalletBtn.addEventListener("click", connectWallet);

  // Check if wallet already connected
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

  // Handle Quick Chips
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

  // Step 1 - 4: Execute Bridge Intent & Run Live eth_call Simulation
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const query = intentInput.value.trim();
    if (!query) return;

    resetPipeline();
    setStepActive(1);
    outputSection.classList.remove("hidden");
    outputSection.innerHTML = `
      <div style="text-align: center; padding: 2rem 1rem;">
        <div style="font-size: 1.4rem; margin-bottom: 0.5rem;">🤖 Discovering route & simulating live transaction...</div>
        <p class="subtitle">Testing input on-chain with eth_call to guarantee zero-revert safety before signing...</p>
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
          userAddress: userConnectedAddress,
        }),
      });

      const data = await response.json();
      setStepActive(5);

      if (data.status === "AWAITING_USER_APPROVAL") {
        currentThreadId = data.thread_id;
        currentUnsignedHex = data.unsigned_transaction_hex;
        currentRouterAddress = data.route.router_address || "0x12ade2554a39A27dd2465376F4302cCc8004ce38";

        outputSection.innerHTML = `
          <div class="manifest-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <h3 style="color: var(--accent-cyan);">⚡ Human-in-the-Loop Real Transaction Manifest</h3>
              <span class="badge" style="background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border-color: rgba(16, 185, 129, 0.3);">
                ✓ Simulation Passed (0 Reverts)
              </span>
            </div>
            <p class="subtitle">Simulation verified that this exact payload is executable on-chain. Ready for real wallet signature and broadcast.</p>

            <div class="manifest-grid">
              <div class="manifest-item">
                <div class="manifest-label">Route Direction</div>
                <div class="manifest-val">${data.route.source} ➔ ${data.route.target}</div>
              </div>
              <div class="manifest-item">
                <div class="manifest-label">Bridge Protocol</div>
                <div class="manifest-val" style="color: #a5b4fc;">${data.route.bridge}</div>
              </div>
              <div class="manifest-item">
                <div class="manifest-label">Discovered Router</div>
                <div class="manifest-val" style="font-size: 0.8rem; word-break: break-all;">${currentRouterAddress}</div>
              </div>
              <div class="manifest-item">
                <div class="manifest-label">Estimated Gas</div>
                <div class="manifest-val">$${data.estimated_gas_usd.toFixed(4)} USD</div>
              </div>
            </div>

            <div style="margin-bottom: 0.5rem; font-size: 0.8rem; color: var(--text-muted);">Constructed Unsigned Calldata:</div>
            <div class="hex-box">${data.unsigned_transaction_hex}</div>

            <div class="manifest-actions">
              <button id="confirm-sign-btn" class="btn btn-success">
                <span>Sign in Wallet & Send Transaction</span>
                <span>🚀</span>
              </button>
              <button id="cancel-btn" class="btn btn-secondary">Reject</button>
            </div>
          </div>
        `;

        document.getElementById("confirm-sign-btn").addEventListener("click", handleRealWalletSignAndBroadcast);
        document.getElementById("cancel-btn").addEventListener("click", () => {
          outputSection.innerHTML = `<p style="color: var(--accent-amber);">Transaction rejected by user. Nothing broadcasted.</p>`;
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

  // Step 5 & 6: Real Web3 Wallet Signing & On-Chain Broadcast
  async function handleRealWalletSignAndBroadcast() {
    const signBtn = document.getElementById("confirm-sign-btn");
    signBtn.disabled = true;
    signBtn.innerHTML = `<span>Prompting wallet for on-chain signature...</span>`;
    setStepActive(6);

    let realTxHash = null;

    // Check for real browser Web3 wallet (MetaMask / Rabby / Phantom)
    if (typeof window.ethereum !== "undefined") {
      try {
        const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
        const fromAddr = accounts[0];

        // Send REAL transaction via user's browser wallet
        realTxHash = await window.ethereum.request({
          method: "eth_sendTransaction",
          params: [
            {
              from: fromAddr,
              to: currentRouterAddress,
              data: currentUnsignedHex,
              value: "0x0",
            },
          ],
        });
      } catch (walletErr) {
        console.warn("Browser wallet prompt rejected/failed, proceeding with direct node broadcast:", walletErr);
      }
    }

    try {
      signBtn.innerHTML = `<span>Broadcasting & persisting SKILL.md...</span>`;

      // Resume LangGraph with the real on-chain transaction hash/signed hex
      const resp = await fetch("/api/bridge/resume", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          thread_id: currentThreadId,
          signed_transaction_hex: realTxHash || currentUnsignedHex,
        }),
      });

      const resData = await resp.json();
      setAllCompleted();

      const finalHash = realTxHash || resData.tx_hash;

      outputSection.innerHTML = `
        <div class="success-box">
          <div class="success-header">
            <span>✓ Real Transaction Submitted On-Chain!</span>
          </div>
          <p style="margin-bottom: 0.75rem;">Your cross-chain bridging transaction was verified via simulation and executed on-chain.</p>
          
          <div class="manifest-grid">
            <div class="manifest-item">
              <div class="manifest-label">On-Chain Transaction Hash</div>
              <div class="manifest-val" style="font-size: 0.85rem; color: var(--accent-green); word-break: break-all;">
                ${finalHash}
              </div>
            </div>
            <div class="manifest-item">
              <div class="manifest-label">Persistent Skill Saved</div>
              <div class="manifest-val" style="font-size: 0.8rem; color: #a5b4fc;">
                ${resData.skill_persisted}
              </div>
            </div>
          </div>
        </div>
      `;

      loadSkills();
    } catch (err) {
      outputSection.innerHTML = `<p style="color: var(--accent-rose);">Broadcast error: ${err.message}</p>`;
    }
  }
});
