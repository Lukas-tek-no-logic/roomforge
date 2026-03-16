/**
 * RoomForge — AI Interior Designer Frontend
 */

const state = {
  sessionId: null,
  polling: null,
  annotating: false,
  rect: null,
  dragging: false,
  dragStart: null,
  draftsInterval: null,
  finalizeInterval: null,
  // New state for features
  renderHistory: [],          // 3.1: history of renders
  lastBlenderUrl: null,       // 2.3: for before/after
  lightboxImages: [],         // 2.2: images for lightbox nav
  lightboxIndex: 0,           // 2.2: current lightbox index
  soundEnabled: true,         // 3.2: sound toggle
  audioCtx: null,             // 3.2: Web Audio context
  zoomScale: 1,               // 4.2: image zoom
  zoomTranslateX: 0,          // 4.2: image pan
  zoomTranslateY: 0,          // 4.2: image pan
  isPanning: false,           // 4.2: panning state
  panStart: null,             // 4.2: pan start coords
  lastCommand: "",            // 3.1: last chat command for history
};

// ── Elements ───────────────────────────────────────────────────────────────

const fileInput       = document.getElementById("file-input");
const inspInput       = document.getElementById("inspiration-input");
const inspoStatus     = document.getElementById("inspo-status");
const templateSelect  = document.getElementById("template-select");
const loadTemplateBtn = document.getElementById("load-template-btn");
const describeInput   = document.getElementById("describe-input");
const describeBtn     = document.getElementById("describe-btn");
const statusBar       = document.getElementById("status-bar");
const chatArea        = document.getElementById("chat-area");
const chatInput       = document.getElementById("chat-input");
const sendBtn         = document.getElementById("send-btn");
const placeholder     = document.getElementById("placeholder");
const renderImg       = document.getElementById("render-img");
const renderCanvas    = document.getElementById("render-canvas");
const renderSpinner   = document.getElementById("render-spinner");
const spinnerLabel    = document.getElementById("spinner-label");
const renderLabel     = document.getElementById("render-label");
const roomTypeSelect  = document.getElementById("room-type-select");
const draftsBtn       = document.getElementById("drafts-btn");
const finalizeBtn     = document.getElementById("finalize-btn");
const annotateBtn     = document.getElementById("annotate-btn");
const annotPanel      = document.getElementById("annotation-panel");
const annotInput      = document.getElementById("annotation-input");
const annotApply      = document.getElementById("annotation-apply");
const annotCancel     = document.getElementById("annotation-cancel");
const draftsGallery   = document.getElementById("drafts-gallery");
const draftsGrid      = document.getElementById("drafts-grid");
const draftsLabel     = document.getElementById("drafts-label");
const draftsClose     = document.getElementById("drafts-close");
const progressBar     = document.getElementById("progress-bar");
const progressFill    = document.getElementById("progress-fill");
const toastContainer  = document.getElementById("toast-container");

// ── Helpers ─────────────────────────────────────────────────────────────

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// 1.2: Toast notifications
function showToast(message, type = "info", duration = 4000) {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("toast-out");
    toast.addEventListener("animationend", () => toast.remove());
  }, duration);
}

function setStatus(text, type = "") {
  statusBar.textContent = text;
  statusBar.className = "status-bar " + type;
  // Toast for important events
  if (type === "error") showToast(text, "error");
  else if (type === "ok" && text.includes("ready")) showToast(text, "success");
}

// 4.1: Enhanced chat messages with time + copy + basic markdown
function addMsg(who, text) {
  // Remove typing indicator if present
  const typing = chatArea.querySelector(".typing-indicator-msg");
  if (typing) typing.remove();

  const div = document.createElement("div");
  div.className = `msg ${who}`;

  // Basic markdown: **bold** and `code`
  let html = escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>");

  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  html += `<span class="msg-time">${time}</span>`;

  div.innerHTML = html;

  // Copy button on AI messages
  if (who === "ai") {
    const copyBtn = document.createElement("button");
    copyBtn.className = "msg-copy";
    copyBtn.textContent = "\u2398";
    copyBtn.addEventListener("click", () => {
      navigator.clipboard.writeText(text);
      copyBtn.textContent = "\u2713";
      setTimeout(() => { copyBtn.textContent = "\u2398"; }, 1500);
    });
    div.appendChild(copyBtn);
  }

  chatArea.appendChild(div);
  chatArea.scrollTop = chatArea.scrollHeight;
}

// 2.6: Typing indicator
function showTypingIndicator() {
  const existing = chatArea.querySelector(".typing-indicator-msg");
  if (existing) return;
  const div = document.createElement("div");
  div.className = "msg ai typing-indicator-msg";
  div.innerHTML = '<div class="typing-indicator"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div>';
  chatArea.appendChild(div);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function removeTypingIndicator() {
  const el = chatArea.querySelector(".typing-indicator-msg");
  if (el) el.remove();
}

function disableChat() { chatInput.disabled = true; sendBtn.disabled = true; }
function enableChat() { chatInput.disabled = false; sendBtn.disabled = false; chatInput.focus(); }
function showSpinner(text = "Rendering...") { spinnerLabel.textContent = text; renderSpinner.classList.add("active"); }
function hideSpinner() { renderSpinner.classList.remove("active"); }

function stopDraftsPolling() {
  if (state.draftsInterval) { clearInterval(state.draftsInterval); state.draftsInterval = null; }
}
function stopFinalizePolling() {
  if (state.finalizeInterval) { clearInterval(state.finalizeInterval); state.finalizeInterval = null; }
}

// 3.6: Progress bar
function setProgress(pct) {
  progressBar.classList.toggle("active", pct > 0 && pct < 100);
  progressFill.style.width = pct + "%";
  if (pct >= 100) setTimeout(() => { progressBar.classList.remove("active"); progressFill.style.width = "0%"; }, 1000);
}

// 1.3: Button ripple effect
document.addEventListener("click", e => {
  const btn = e.target.closest(".btn");
  if (!btn || btn.disabled) return;
  const rect = btn.getBoundingClientRect();
  const ripple = document.createElement("span");
  ripple.className = "ripple-effect";
  const size = Math.max(rect.width, rect.height);
  ripple.style.width = ripple.style.height = size + "px";
  ripple.style.left = (e.clientX - rect.left - size / 2) + "px";
  ripple.style.top = (e.clientY - rect.top - size / 2) + "px";
  btn.appendChild(ripple);
  ripple.addEventListener("animationend", () => ripple.remove());
});

// 3.2: Sound system (Web Audio API)
function initAudio() {
  if (state.audioCtx) return;
  try { state.audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { /* no audio */ }
}

function playSound(type) {
  if (!state.soundEnabled || !state.audioCtx) return;
  try {
    const ctx = state.audioCtx;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    const now = ctx.currentTime;

    if (type === "success") {
      osc.type = "sine";
      osc.frequency.setValueAtTime(660, now);
      osc.frequency.setValueAtTime(880, now + 0.08);
      gain.gain.setValueAtTime(0.08, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
      osc.start(now);
      osc.stop(now + 0.25);
    } else if (type === "error") {
      osc.type = "sine";
      osc.frequency.setValueAtTime(280, now);
      gain.gain.setValueAtTime(0.06, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.3);
      osc.start(now);
      osc.stop(now + 0.3);
    } else if (type === "click") {
      osc.type = "sine";
      osc.frequency.setValueAtTime(1100, now);
      gain.gain.setValueAtTime(0.03, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.05);
      osc.start(now);
      osc.stop(now + 0.05);
    }
  } catch (e) { /* silent fail */ }
}

// Init audio on first user interaction
document.addEventListener("click", function audioInit() {
  initAudio();
  document.removeEventListener("click", audioInit);
}, { once: true });

// 3.2: Sound toggle
const soundToggle = document.getElementById("sound-toggle");
const soundIconOn = document.getElementById("sound-icon-on");
const soundIconOff = document.getElementById("sound-icon-off");

state.soundEnabled = localStorage.getItem("rf_sound") !== "off";
if (!state.soundEnabled) { soundIconOn.style.display = "none"; soundIconOff.style.display = "block"; }

soundToggle.addEventListener("click", () => {
  state.soundEnabled = !state.soundEnabled;
  localStorage.setItem("rf_sound", state.soundEnabled ? "on" : "off");
  soundIconOn.style.display = state.soundEnabled ? "block" : "none";
  soundIconOff.style.display = state.soundEnabled ? "none" : "block";
});

// 3.3: Theme toggle
const themeToggle = document.getElementById("theme-toggle");
const themeIconDark = document.getElementById("theme-icon-dark");
const themeIconLight = document.getElementById("theme-icon-light");

if (localStorage.getItem("rf_theme") === "light") {
  document.documentElement.classList.add("light");
  themeIconDark.style.display = "none";
  themeIconLight.style.display = "block";
}

themeToggle.addEventListener("click", () => {
  document.documentElement.classList.toggle("light");
  const isLight = document.documentElement.classList.contains("light");
  localStorage.setItem("rf_theme", isLight ? "light" : "dark");
  themeIconDark.style.display = isLight ? "none" : "block";
  themeIconLight.style.display = isLight ? "block" : "none";
  document.querySelector('meta[name="theme-color"]').content = isLight ? "#f0f2f5" : "#0b0e17";
});

// ── Init ───────────────────────────────────────────────────────────────────

async function init() {
  const res = await fetch("/sessions", { method: "POST" });
  const data = await res.json();
  state.sessionId = data.session_id;
  setStatus(`Session: ${state.sessionId}`, "ok");

  // Load room templates
  try {
    const tRes = await fetch("/room-templates");
    const tData = await tRes.json();
    tData.templates.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.label;
      templateSelect.appendChild(opt);
    });
  } catch (e) { /* templates not available */ }

  // 3.5: Onboarding on first visit
  if (!localStorage.getItem("rf_onboarded")) {
    setTimeout(startOnboarding, 1000);
  }
}

// ── Template selection ──────────────────────────────────────────────────

templateSelect.addEventListener("change", () => {
  loadTemplateBtn.disabled = !templateSelect.value;
});

loadTemplateBtn.addEventListener("click", async () => {
  const id = templateSelect.value;
  if (!id) return;

  const label = templateSelect.options[templateSelect.selectedIndex].text;
  addMsg("system", `Loading: ${label}`);
  setStatus("Loading & rendering...", "rendering");
  showSpinner("Building 3D scene...");
  setProgress(10);
  disableChat();
  loadTemplateBtn.disabled = true;
  showTypingIndicator();
  playSound("click");

  try {
    let res, data;
    if (id.startsWith("_extracted_")) {
      const realId = id.replace("_extracted_", "");
      res = await fetch(`/sessions/${state.sessionId}/load-extracted-room/${realId}`, { method: "POST" });
      data = await res.json();
    } else {
      res = await fetch(`/sessions/${state.sessionId}/load-template/${id}`, { method: "POST" });
      data = await res.json();
    }
    if (!res.ok) throw new Error(data.detail);
    state.lastCommand = `Loaded: ${data.room_label || label}`;
    addMsg("ai", `Loaded: ${data.room_label || label}. Rendering...`);
    roomTypeSelect.value = data.room_type || "";
    setProgress(30);
    startPolling();
  } catch (e) {
    setStatus(`Error: ${e.message}`, "error");
    hideSpinner();
    enableChat();
    loadTemplateBtn.disabled = false;
    removeTypingIndicator();
    setProgress(0);
    playSound("error");
  }
});

// ── Describe room ──────────────────────────────────────────────────────

describeBtn.addEventListener("click", generateFromDescription);
describeInput.addEventListener("keydown", e => { if (e.key === "Enter") generateFromDescription(); });

async function generateFromDescription() {
  const desc = describeInput.value.trim();
  if (!desc) return;

  addMsg("user", desc);
  describeInput.value = "";
  setStatus("Claude is designing the room...", "rendering");
  showSpinner("Generating room layout...");
  setProgress(10);
  disableChat();
  describeBtn.disabled = true;
  showTypingIndicator();
  playSound("click");
  state.lastCommand = desc;

  const form = new FormData();
  form.append("description", desc);

  try {
    const res = await fetch(`/sessions/${state.sessionId}/generate-room`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    const furn = data.room_data?.furniture?.length || 0;
    addMsg("ai", `Room created with ${furn} items. Rendering...`);
    setProgress(40);
    startPolling();
  } catch (e) {
    setStatus(`Error: ${e.message}`, "error");
    addMsg("system", `Error: ${e.message}`);
    hideSpinner();
    enableChat();
    describeBtn.disabled = false;
    removeTypingIndicator();
    setProgress(0);
    playSound("error");
  }
}

// ── Upload photo ──────────────────────────────────────────────────────

fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file) return;
  await handlePhotoUpload(file);
});

async function handlePhotoUpload(file) {
  addMsg("system", `Uploading ${file.name}...`);
  setStatus("Analyzing photo...", "rendering");
  showSpinner("Analyzing photo...");
  setProgress(10);
  disableChat();
  showTypingIndicator();
  playSound("click");
  state.lastCommand = `Photo: ${file.name}`;

  const form = new FormData();
  form.append("file", file);

  try {
    const res = await fetch(`/sessions/${state.sessionId}/upload`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    addMsg("ai", "Photo analyzed! Rendering 3D scene...");
    setProgress(40);
    startPolling();
  } catch (e) {
    setStatus(`Error: ${e.message}`, "error");
    addMsg("system", `Error: ${e.message}`);
    hideSpinner();
    enableChat();
    removeTypingIndicator();
    setProgress(0);
    playSound("error");
  }
}

// ── Upload floor plan ──────────────────────────────────────────────────

const floorplanInput = document.getElementById("floorplan-input");

floorplanInput.addEventListener("change", async () => {
  const file = floorplanInput.files[0];
  if (!file) return;
  await handleFloorplanUpload(file);
});

async function handleFloorplanUpload(file) {
  addMsg("system", `Uploading floor plan: ${file.name}`);
  setStatus("Analyzing floor plan...", "rendering");
  showSpinner("Claude is reading the floor plan dimensions...");
  setProgress(10);
  disableChat();
  showTypingIndicator();
  playSound("click");
  state.lastCommand = `Floor plan: ${file.name}`;

  const form = new FormData();
  form.append("floorplan", file);

  try {
    const res = await fetch(`/sessions/${state.sessionId}/upload-floorplan`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);

    if (data.mode === "multi_room" && data.rooms) {
      addMsg("ai", `Found ${data.rooms.length} rooms in floor plan:`);
      data.rooms.forEach(r => addMsg("ai", `  - ${r.label} (${r.dimensions.width}x${r.dimensions.depth}m)`));
      if (data.loaded) addMsg("ai", `Loaded "${data.loaded}" — rendering...`);

      data.rooms.forEach(r => {
        const opt = document.createElement("option");
        opt.value = `_extracted_${r.id}`;
        opt.textContent = `[Plan] ${r.label}`;
        opt.dataset.roomData = JSON.stringify(r);
        templateSelect.appendChild(opt);
      });
    } else if (data.mode === "single_room") {
      addMsg("ai", `Room created: ${data.room_data?.room_label || "Room"}. Rendering...`);
    }
    setProgress(40);
    startPolling();
  } catch (e) {
    setStatus(`Error: ${e.message}`, "error");
    addMsg("system", `Floor plan error: ${e.message}`);
    hideSpinner();
    enableChat();
    removeTypingIndicator();
    setProgress(0);
    playSound("error");
  }
}

// ── Upload inspiration ────────────────────────────────────────────────

inspInput.addEventListener("change", async () => {
  const files = inspInput.files;
  if (!files.length) return;

  inspoStatus.textContent = "Analyzing...";
  playSound("click");
  const form = new FormData();
  for (const f of files) form.append("files", f);
  form.append("style_name", "Custom Style");

  try {
    const res = await fetch(`/sessions/${state.sessionId}/create-style`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    inspoStatus.textContent = `Style: ${data.num_references} images analyzed`;
    addMsg("ai", `Style profile created from ${data.num_references} images.`);
    showToast(`Style profile created from ${data.num_references} images`, "success");
    playSound("success");
  } catch (e) {
    inspoStatus.textContent = "Failed";
    addMsg("system", `Inspiration error: ${e.message}`);
    playSound("error");
  }
});

// ── Chat ───────────────────────────────────────────────────────────────

sendBtn.addEventListener("click", sendChat);
chatInput.addEventListener("keydown", e => { if (e.key === "Enter") sendChat(); });

async function sendChat() {
  const command = chatInput.value.trim();
  if (!command || !state.sessionId) return;

  addMsg("user", command);
  chatInput.value = "";
  disableChat();
  setStatus("Applying changes...", "rendering");
  showSpinner("Applying changes & re-rendering...");
  setProgress(10);
  showTypingIndicator();
  playSound("click");
  state.lastCommand = command;

  const form = new FormData();
  form.append("command", command);

  try {
    const res = await fetch(`/sessions/${state.sessionId}/chat`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    addMsg("ai", "Changes applied! Re-rendering...");
    setProgress(40);
    startPolling();
  } catch (e) {
    setStatus(`Error: ${e.message}`, "error");
    enableChat();
    hideSpinner();
    removeTypingIndicator();
    setProgress(0);
    playSound("error");
  }
}

// ── Drafts ─────────────────────────────────────────────────────────────

draftsBtn.addEventListener("click", generateDrafts);
draftsClose.addEventListener("click", () => { draftsGallery.style.display = "none"; stopDraftsPolling(); });

async function generateDrafts() {
  const roomType = roomTypeSelect.value;
  draftsGallery.style.display = "block";
  draftsGrid.innerHTML = '<div style="padding:20px;color:var(--text-muted)">Generating 4 drafts from different angles...</div>';
  draftsLabel.textContent = "Generating drafts...";
  draftsBtn.disabled = true;
  playSound("click");
  showToast("Generating 4 angle drafts...", "info");

  const params = new URLSearchParams({ count: "4" });
  if (roomType) params.set("room_type", roomType);

  try {
    await fetch(`/sessions/${state.sessionId}/drafts?${params}`, { method: "POST" });
    pollDrafts();
  } catch (e) {
    draftsGrid.innerHTML = `<div style="color:var(--accent);padding:20px">Error: ${escapeHtml(e.message)}</div>`;
    draftsBtn.disabled = false;
    playSound("error");
  }
}

function pollDrafts() {
  stopDraftsPolling();
  state.draftsInterval = setInterval(async () => {
    try {
      const res = await fetch(`/sessions/${state.sessionId}/drafts`);
      const data = await res.json();
      renderDraftsGrid(data);
      if (data.status === "drafts_ready" || data.status === "error" || data.status === "done") {
        stopDraftsPolling();
        draftsBtn.disabled = false;
        if (data.status !== "error") {
          showToast("Drafts ready!", "success");
          playSound("success");
        }
      }
    } catch (e) { /* keep polling */ }
  }, 5000);
}

function renderDraftsGrid(data) {
  const drafts = data.drafts || [];
  const total = data.total || 4;
  const done = data.done || 0;
  const angles = ["Front", "Corner Left", "Corner Right", "Side"];

  draftsLabel.textContent = done < total ? `Generating ${done}/${total}...` : `${total} drafts ready`;

  draftsGrid.innerHTML = "";
  for (let i = 0; i < total; i++) {
    const div = document.createElement("div");
    div.className = "draft-card";

    if (i < drafts.length && drafts[i]) {
      const url = `/sessions/${state.sessionId}/drafts/${i}?t=${Date.now()}`;
      const img = document.createElement("img");
      img.src = url;
      img.alt = `Draft ${i}`;
      img.loading = "lazy";
      const info = document.createElement("div");
      info.className = "draft-info";
      info.textContent = angles[i % 4] || `Draft ${i}`;
      div.appendChild(img);
      div.appendChild(info);
      div.addEventListener("click", () => {
        renderImg.src = url;
        renderImg.style.display = "block";
        placeholder.style.display = "none";
        renderLabel.textContent = `Draft ${i} — ${angles[i % 4] || ""}`;
      });
      // Double-click opens lightbox
      div.addEventListener("dblclick", () => {
        state.lightboxImages = drafts.filter(Boolean).map((_, idx) =>
          `/sessions/${state.sessionId}/drafts/${idx}?t=${Date.now()}`
        );
        state.lightboxIndex = i;
        openLightbox(url);
      });
    } else {
      const ph = document.createElement("div");
      ph.className = "draft-placeholder";
      ph.innerHTML = '<div class="mini-spinner"></div>';
      const info = document.createElement("div");
      info.className = "draft-info";
      info.textContent = angles[i % 4] || `Draft ${i}`;
      div.appendChild(ph);
      div.appendChild(info);
    }
    draftsGrid.appendChild(div);
  }
}

// ── Finalize ───────────────────────────────────────────────────────────

const finalizeModal  = document.getElementById("finalize-modal");
const finalizeClose  = document.getElementById("finalize-close");
const finalizeGo     = document.getElementById("finalize-go");
const finalizeResult = document.getElementById("finalize-result");
const finalizePrompt = document.getElementById("finalize-prompt");
const finalizeStyle  = document.getElementById("finalize-style-input");
const finalizeCamera = document.getElementById("finalize-camera");

finalizeBtn.addEventListener("click", () => { finalizeModal.style.display = "flex"; });
finalizeClose.addEventListener("click", () => { finalizeModal.style.display = "none"; stopFinalizePolling(); });
finalizeModal.addEventListener("click", e => {
  if (e.target === finalizeModal) { finalizeModal.style.display = "none"; stopFinalizePolling(); }
});

finalizeGo.addEventListener("click", triggerFinalize);

async function triggerFinalize() {
  const style = finalizeStyle.value.trim();
  const camera = finalizeCamera.value;
  const roomType = roomTypeSelect.value;
  finalizeGo.disabled = true;
  playSound("click");

  // Store the current blender render URL for before/after
  if (renderImg.src && renderImg.style.display !== "none") {
    state.lastBlenderUrl = renderImg.src;
  }

  finalizeResult.innerHTML = `
    <div style="display:flex;flex-direction:column;align-items:center;gap:10px;padding:40px;color:var(--warning)">
      <div class="spinner-ring" style="width:32px;height:32px;border-color:rgba(60,90,140,0.2);border-top-color:var(--warning)"></div>
      Generating high-resolution render (2048px)...
    </div>`;

  const params = new URLSearchParams();
  if (style) params.set("style", style);
  if (camera) params.set("camera", camera);
  if (roomType) params.set("room_type", roomType);

  try {
    const res = await fetch(`/sessions/${state.sessionId}/finalize?${params}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    if (data.prompt) finalizePrompt.textContent = `${data.depth_guided ? "[Depth-guided] " : ""}${data.prompt}`;
    pollFinalize();
  } catch (e) {
    finalizeResult.innerHTML = `<div style="color:var(--accent);padding:20px">Error: ${escapeHtml(e.message)}</div>`;
    finalizeGo.disabled = false;
    playSound("error");
  }
}

function pollFinalize() {
  stopFinalizePolling();
  state.finalizeInterval = setInterval(async () => {
    try {
      const status = await fetch(`/sessions/${state.sessionId}/status`).then(r => r.json());
      if (status.status === "finalized") {
        stopFinalizePolling();
        const url = `/sessions/${state.sessionId}/finalize?t=${Date.now()}`;
        const img = document.createElement("img");
        img.src = url;
        img.style.cssText = "width:100%;border-radius:6px;display:block;cursor:pointer";
        img.addEventListener("dblclick", () => openLightbox(url));
        finalizeResult.innerHTML = "";
        finalizeResult.appendChild(img);
        finalizeGo.disabled = false;
        addMsg("ai", "High-resolution render ready!");
        showToast("HD render complete!", "success");
        playSound("success");
        triggerConfetti();

        // 2.3: Show before/after compare if we have a blender render
        if (state.lastBlenderUrl) {
          showCompare(state.lastBlenderUrl, url);
        }
      } else if (status.status === "error") {
        stopFinalizePolling();
        const errDiv = document.createElement("div");
        errDiv.style.cssText = "color:var(--accent);padding:20px";
        errDiv.textContent = `Error: ${status.error || "Unknown error"}`;
        finalizeResult.innerHTML = "";
        finalizeResult.appendChild(errDiv);
        finalizeGo.disabled = false;
        playSound("error");
      }
    } catch (e) { /* keep polling */ }
  }, 3000);
}

// ── Polling ────────────────────────────────────────────────────────────

function startPolling() {
  if (state.polling) clearInterval(state.polling);
  state.polling = setInterval(pollStatus, 2000);
}

async function pollStatus() {
  if (!state.sessionId) return;
  try {
    const res = await fetch(`/sessions/${state.sessionId}/status`);
    const data = await res.json();

    if (data.status === "done" || data.status === "drafts_ready") {
      clearInterval(state.polling);
      state.polling = null;
      hideSpinner();
      setProgress(95);
      await refreshRender();
      enableChat();
      loadTemplateBtn.disabled = !templateSelect.value;
      describeBtn.disabled = false;
      setStatus(`Render ready (iteration ${data.iterations || 0})`, "ok");
      setProgress(100);
      removeTypingIndicator();
      playSound("success");
      if (data.status === "done") {
        addMsg("ai", "Render complete! Use **Drafts** for AI visualization or chat to modify.");
      }
    } else if (data.status === "error") {
      clearInterval(state.polling);
      state.polling = null;
      hideSpinner();
      enableChat();
      loadTemplateBtn.disabled = !templateSelect.value;
      describeBtn.disabled = false;
      setStatus(`Error: ${(data.error || "").slice(0, 100)}`, "error");
      setProgress(0);
      removeTypingIndicator();
      playSound("error");
    } else if (data.status === "generating_room") {
      spinnerLabel.textContent = "Claude is designing the room...";
      setProgress(25);
    } else if (data.status === "analyzing") {
      spinnerLabel.textContent = "Analyzing photo...";
      setProgress(40);
    } else if (data.status === "rendering") {
      spinnerLabel.textContent = "Rendering with Blender...";
      setProgress(70);
    }
  } catch (e) { /* network glitch */ }
}

// ── Render display ─────────────────────────────────────────────────────

async function refreshRender() {
  const url = `/sessions/${state.sessionId}/render?t=${Date.now()}`;

  // 4.3: Crossfade from old render
  const oldSrc = renderImg.src;
  const hadOldRender = renderImg.style.display === "block" && oldSrc;

  await new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      if (hadOldRender) {
        // Create crossfade clone
        const clone = document.createElement("img");
        clone.src = oldSrc;
        clone.className = "render-crossfade";
        clone.style.opacity = "1";
        const wrapper = document.getElementById("canvas-wrapper");
        wrapper.appendChild(clone);
        requestAnimationFrame(() => { clone.style.opacity = "0"; });
        clone.addEventListener("transitionend", () => clone.remove());
      }

      renderImg.src = img.src;
      renderImg.style.display = "block";
      placeholder.style.display = "none";
      renderLabel.textContent = "Blender render";
      resetZoom();
      syncCanvas();
      resolve();
    };
    img.onerror = reject;
    img.src = url;
  });
  draftsBtn.disabled = false;
  finalizeBtn.disabled = false;
  annotateBtn.disabled = false;
  document.getElementById("history-btn").disabled = false;

  // 3.1: Add to render history
  addToHistory(url, state.lastCommand);
}

// ── 3.1: Render History ────────────────────────────────────────────────

const historyBtn = document.getElementById("history-btn");
const historyDrawer = document.getElementById("history-drawer");
const historyClose = document.getElementById("history-close");
const historyList = document.getElementById("history-list");

historyBtn.addEventListener("click", toggleHistory);
historyClose.addEventListener("click", () => { historyDrawer.style.display = "none"; });

function toggleHistory() {
  const open = historyDrawer.style.display === "none";
  historyDrawer.style.display = open ? "flex" : "none";
}

function addToHistory(url, command) {
  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  state.renderHistory.push({ url, command: command || "", time });

  // Create blob for permanent storage
  const item = document.createElement("div");
  item.className = "history-item";

  const img = document.createElement("img");
  img.src = url;
  img.alt = "Render";
  img.loading = "lazy";

  const info = document.createElement("div");
  info.className = "history-item-info";
  const cmdSpan = document.createElement("span");
  cmdSpan.textContent = (command || "Initial render").slice(0, 30);
  const timeSpan = document.createElement("span");
  timeSpan.textContent = time;
  info.appendChild(cmdSpan);
  info.appendChild(timeSpan);

  item.appendChild(img);
  item.appendChild(info);
  item.addEventListener("click", () => {
    renderImg.src = url;
    renderImg.style.display = "block";
    placeholder.style.display = "none";
    renderLabel.textContent = `History: ${(command || "").slice(0, 40)}`;
  });

  // Remove "No renders yet" empty state
  const empty = historyList.querySelector(".history-empty");
  if (empty) empty.remove();

  historyList.prepend(item);
}

// ── Annotation ─────────────────────────────────────────────────────────

annotateBtn.addEventListener("click", toggleAnnotateMode);
annotCancel.addEventListener("click", cancelAnnotation);
annotApply.addEventListener("click", applyAnnotation);
annotInput.addEventListener("input", () => {
  annotApply.disabled = !annotInput.value.trim() || !state.rect;
});

function toggleAnnotateMode() {
  state.annotating = !state.annotating;
  if (state.annotating) {
    annotateBtn.classList.add("active");
    renderCanvas.style.display = "block";
    renderCanvas.style.cursor = "crosshair";
    state.rect = null;
    annotPanel.classList.remove("active");
  } else {
    cancelAnnotation();
  }
}

function cancelAnnotation() {
  state.annotating = false;
  state.rect = null;
  annotateBtn.classList.remove("active");
  renderCanvas.style.display = "none";
  annotPanel.classList.remove("active");
  annotInput.value = "";
  annotApply.disabled = true;
  clearCanvasRect();
}

function syncCanvas() {
  const rect = renderImg.getBoundingClientRect();
  renderCanvas.width = rect.width;
  renderCanvas.height = rect.height;
  renderCanvas.style.width = rect.width + "px";
  renderCanvas.style.height = rect.height + "px";
  const wrapper = document.getElementById("canvas-wrapper");
  const wRect = wrapper.getBoundingClientRect();
  renderCanvas.style.left = (rect.left - wRect.left) + "px";
  renderCanvas.style.top = (rect.top - wRect.top) + "px";
}

window.addEventListener("resize", () => {
  if (renderImg.style.display === "block") syncCanvas();
});

renderCanvas.addEventListener("mousedown", e => {
  if (!state.annotating) return;
  state.dragging = true;
  state.dragStart = canvasPos(e);
  state.rect = null;
});

renderCanvas.addEventListener("mousemove", e => {
  if (!state.dragging) return;
  drawRect(normalizeRect(state.dragStart, canvasPos(e)));
});

renderCanvas.addEventListener("mouseup", e => {
  if (!state.dragging) return;
  state.dragging = false;
  const r = normalizeRect(state.dragStart, canvasPos(e));
  if (r.x2 - r.x1 > 5 && r.y2 - r.y1 > 5) {
    state.rect = r;
    annotPanel.classList.add("active");
    annotInput.focus();
  }
});

function canvasPos(e) {
  const r = renderCanvas.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}
function normalizeRect(a, b) {
  return { x1: Math.min(a.x, b.x)|0, y1: Math.min(a.y, b.y)|0, x2: Math.max(a.x, b.x)|0, y2: Math.max(a.y, b.y)|0 };
}
function drawRect(r) {
  const ctx = renderCanvas.getContext("2d");
  ctx.clearRect(0, 0, renderCanvas.width, renderCanvas.height);
  ctx.fillStyle = "rgba(233, 69, 96, 0.2)";
  ctx.fillRect(r.x1, r.y1, r.x2-r.x1, r.y2-r.y1);
  ctx.strokeStyle = "#e94560";
  ctx.lineWidth = 2;
  ctx.setLineDash([6, 3]);
  ctx.strokeRect(r.x1, r.y1, r.x2-r.x1, r.y2-r.y1);
}
function clearCanvasRect() {
  renderCanvas.getContext("2d").clearRect(0, 0, renderCanvas.width, renderCanvas.height);
}

async function applyAnnotation() {
  const comment = annotInput.value.trim();
  if (!comment || !state.rect) return;

  const combined = document.createElement("canvas");
  combined.width = renderImg.naturalWidth;
  combined.height = renderImg.naturalHeight;
  const ctx = combined.getContext("2d");
  ctx.drawImage(renderImg, 0, 0);
  const scaleX = renderImg.naturalWidth / renderCanvas.width;
  const scaleY = renderImg.naturalHeight / renderCanvas.height;
  const sr = {
    x1: (state.rect.x1 * scaleX)|0, y1: (state.rect.y1 * scaleY)|0,
    x2: (state.rect.x2 * scaleX)|0, y2: (state.rect.y2 * scaleY)|0,
  };
  const imageB64 = combined.toDataURL("image/png");

  addMsg("user", `[Annotation] ${comment}`);
  disableChat();
  showSpinner("Applying annotation...");
  setProgress(10);
  cancelAnnotation();
  showTypingIndicator();
  playSound("click");
  state.lastCommand = `[Annotation] ${comment}`;

  const form = new FormData();
  form.append("image_b64", imageB64);
  form.append("comment", comment);
  form.append("x1", sr.x1); form.append("y1", sr.y1);
  form.append("x2", sr.x2); form.append("y2", sr.y2);
  form.append("render_w", renderImg.naturalWidth);
  form.append("render_h", renderImg.naturalHeight);

  try {
    const res = await fetch(`/sessions/${state.sessionId}/annotate`, { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail);
    addMsg("ai", "Annotation applied! Re-rendering...");
    setProgress(40);
    startPolling();
  } catch (e) {
    setStatus(`Error: ${e.message}`, "error");
    enableChat();
    hideSpinner();
    removeTypingIndicator();
    setProgress(0);
    playSound("error");
  }
}

// ── 4.2: Image Zoom + Pan ──────────────────────────────────────────────

function resetZoom() {
  state.zoomScale = 1;
  state.zoomTranslateX = 0;
  state.zoomTranslateY = 0;
  applyZoom();
}

function applyZoom() {
  renderImg.style.transform = `scale(${state.zoomScale}) translate(${state.zoomTranslateX}px, ${state.zoomTranslateY}px)`;
}

document.getElementById("canvas-wrapper").addEventListener("wheel", e => {
  if (state.annotating || renderImg.style.display !== "block") return;
  e.preventDefault();
  const delta = e.deltaY > 0 ? 0.9 : 1.1;
  state.zoomScale = Math.max(0.5, Math.min(5, state.zoomScale * delta));
  applyZoom();
}, { passive: false });

document.getElementById("canvas-wrapper").addEventListener("mousedown", e => {
  if (state.annotating || renderImg.style.display !== "block" || state.zoomScale <= 1) return;
  if (e.button !== 0) return;
  state.isPanning = true;
  state.panStart = { x: e.clientX, y: e.clientY, tx: state.zoomTranslateX, ty: state.zoomTranslateY };
  e.preventDefault();
});

document.addEventListener("mousemove", e => {
  if (!state.isPanning) return;
  const dx = (e.clientX - state.panStart.x) / state.zoomScale;
  const dy = (e.clientY - state.panStart.y) / state.zoomScale;
  state.zoomTranslateX = state.panStart.tx + dx;
  state.zoomTranslateY = state.panStart.ty + dy;
  applyZoom();
});

document.addEventListener("mouseup", () => { state.isPanning = false; });

// Double-click to reset zoom or open lightbox
renderImg.addEventListener("dblclick", () => {
  if (state.zoomScale > 1.05) {
    resetZoom();
  } else {
    openLightbox(renderImg.src);
  }
});

// ── 2.2: Lightbox ──────────────────────────────────────────────────────

const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-img");
const lightboxClose = document.getElementById("lightbox-close");

function openLightbox(url) {
  lightboxImg.src = url;
  lightbox.style.display = "flex";
}

lightboxClose.addEventListener("click", closeLightbox);
lightbox.addEventListener("click", e => { if (e.target === lightbox) closeLightbox(); });

function closeLightbox() {
  lightbox.style.display = "none";
}

document.getElementById("lightbox-prev").addEventListener("click", () => {
  if (state.lightboxImages.length < 2) return;
  state.lightboxIndex = (state.lightboxIndex - 1 + state.lightboxImages.length) % state.lightboxImages.length;
  lightboxImg.src = state.lightboxImages[state.lightboxIndex];
});

document.getElementById("lightbox-next").addEventListener("click", () => {
  if (state.lightboxImages.length < 2) return;
  state.lightboxIndex = (state.lightboxIndex + 1) % state.lightboxImages.length;
  lightboxImg.src = state.lightboxImages[state.lightboxIndex];
});

// ── 2.3: Before/After Compare ──────────────────────────────────────────

const compareContainer = document.getElementById("compare-container");
const compareWrapper = document.getElementById("compare-wrapper");
const compareBefore = document.getElementById("compare-before");
const compareAfter = document.getElementById("compare-after");
const compareSlider = document.getElementById("compare-slider");

function showCompare(beforeUrl, afterUrl) {
  compareBefore.src = beforeUrl;
  compareAfter.src = afterUrl;
  compareContainer.style.display = "block";
  // Reset slider to middle
  setComparePosition(50);
}

function setComparePosition(pct) {
  compareAfter.style.clipPath = `inset(0 0 0 ${pct}%)`;
  compareSlider.style.left = pct + "%";
}

let compareActive = false;
compareWrapper.addEventListener("mousedown", e => {
  compareActive = true;
  updateCompare(e);
});
document.addEventListener("mousemove", e => { if (compareActive) updateCompare(e); });
document.addEventListener("mouseup", () => { compareActive = false; });
// Touch support
compareWrapper.addEventListener("touchstart", e => { compareActive = true; updateCompare(e.touches[0]); }, { passive: true });
document.addEventListener("touchmove", e => { if (compareActive) updateCompare(e.touches[0]); }, { passive: true });
document.addEventListener("touchend", () => { compareActive = false; });

function updateCompare(e) {
  const rect = compareWrapper.getBoundingClientRect();
  const pct = Math.max(0, Math.min(100, ((e.clientX - rect.left) / rect.width) * 100));
  setComparePosition(pct);
}

// ── 2.4: Drag and Drop ────────────────────────────────────────────────

const dropOverlay = document.getElementById("drop-overlay");
let dragCounter = 0;

document.addEventListener("dragenter", e => {
  e.preventDefault();
  dragCounter++;
  if (dragCounter === 1) dropOverlay.classList.add("active");
});

document.addEventListener("dragleave", e => {
  e.preventDefault();
  dragCounter--;
  if (dragCounter <= 0) { dropOverlay.classList.remove("active"); dragCounter = 0; }
});

document.addEventListener("dragover", e => e.preventDefault());

document.addEventListener("drop", e => {
  e.preventDefault();
  dragCounter = 0;
  dropOverlay.classList.remove("active");

  const files = e.dataTransfer.files;
  if (!files.length || !state.sessionId) return;

  const file = files[0];
  const isImage = file.type.startsWith("image/");
  const isPdf = file.type === "application/pdf";

  if (isPdf || (isImage && file.name.toLowerCase().includes("plan"))) {
    handleFloorplanUpload(file);
  } else if (isImage) {
    // If no room loaded yet, treat as room photo; otherwise as photo too
    handlePhotoUpload(file);
  }
});

// ── 2.1: Keyboard Shortcuts ───────────────────────────────────────────

const shortcutsModal = document.getElementById("shortcuts-modal");

document.getElementById("shortcuts-btn").addEventListener("click", () => {
  shortcutsModal.style.display = "flex";
});
document.getElementById("shortcuts-close").addEventListener("click", () => {
  shortcutsModal.style.display = "none";
});

document.addEventListener("keydown", e => {
  const active = document.activeElement;
  const inInput = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT");

  // Escape: close any open modal/panel
  if (e.key === "Escape") {
    if (lightbox.style.display === "flex") { closeLightbox(); return; }
    if (shortcutsModal.style.display === "flex") { shortcutsModal.style.display = "none"; return; }
    if (finalizeModal.style.display === "flex") { finalizeModal.style.display = "none"; stopFinalizePolling(); return; }
    if (historyDrawer.style.display === "flex") { historyDrawer.style.display = "none"; return; }
    if (state.annotating) { cancelAnnotation(); return; }
    return;
  }

  // Arrow keys in lightbox
  if (lightbox.style.display === "flex") {
    if (e.key === "ArrowLeft") document.getElementById("lightbox-prev").click();
    else if (e.key === "ArrowRight") document.getElementById("lightbox-next").click();
    return;
  }

  // Ctrl+Enter: finalize
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !finalizeBtn.disabled) {
    e.preventDefault();
    finalizeBtn.click();
    return;
  }

  // Shortcuts only when not focused on input
  if (inInput) return;

  if (e.key === "?" || (e.shiftKey && e.key === "/")) {
    shortcutsModal.style.display = shortcutsModal.style.display === "flex" ? "none" : "flex";
  } else if (e.key === "d" || e.key === "D") {
    if (!draftsBtn.disabled) draftsBtn.click();
  } else if (e.key === "a" || e.key === "A") {
    if (!annotateBtn.disabled) annotateBtn.click();
  } else if (e.key === "f" || e.key === "F") {
    if (renderImg.style.display === "block") openLightbox(renderImg.src);
  } else if (e.key === "h" || e.key === "H") {
    toggleHistory();
  }
});

// ── 3.5: Onboarding ───────────────────────────────────────────────────

const onboardingOverlay = document.getElementById("onboarding-overlay");
const onboardingSpotlight = document.getElementById("onboarding-spotlight");
const onboardingTooltip = document.getElementById("onboarding-tooltip");
const onboardingText = document.getElementById("onboarding-text");
const onboardingStep = document.getElementById("onboarding-step");

const ONBOARDING_STEPS = [
  { selector: "#start-options", text: "Start here: pick a template, describe your dream room, or upload a photo." },
  { selector: "#chat-input", text: "Chat with AI to modify your room: move furniture, change materials, add elements." },
  { selector: "#drafts-btn", text: "Generate 4 photorealistic previews from different angles." },
  { selector: "#finalize-btn", text: "When you're happy, create a high-resolution HD render." },
];

let onboardingIdx = 0;

function startOnboarding() {
  onboardingIdx = 0;
  onboardingOverlay.style.display = "block";
  showOnboardingStep();
}

function showOnboardingStep() {
  if (onboardingIdx >= ONBOARDING_STEPS.length) {
    endOnboarding();
    return;
  }
  const step = ONBOARDING_STEPS[onboardingIdx];
  const el = document.querySelector(step.selector);
  if (!el) { onboardingIdx++; showOnboardingStep(); return; }

  const rect = el.getBoundingClientRect();
  const pad = 8;
  onboardingSpotlight.style.left = (rect.left - pad) + "px";
  onboardingSpotlight.style.top = (rect.top - pad) + "px";
  onboardingSpotlight.style.width = (rect.width + pad * 2) + "px";
  onboardingSpotlight.style.height = (rect.height + pad * 2) + "px";

  onboardingText.textContent = step.text;
  onboardingStep.textContent = `${onboardingIdx + 1} / ${ONBOARDING_STEPS.length}`;

  // Position tooltip near the spotlight
  const tooltipTop = rect.bottom + 16;
  const tooltipLeft = Math.max(16, Math.min(rect.left, window.innerWidth - 340));
  onboardingTooltip.style.top = tooltipTop + "px";
  onboardingTooltip.style.left = tooltipLeft + "px";
}

document.getElementById("onboarding-next").addEventListener("click", () => {
  onboardingIdx++;
  showOnboardingStep();
});

document.getElementById("onboarding-skip").addEventListener("click", endOnboarding);
onboardingOverlay.addEventListener("click", e => {
  if (e.target === onboardingOverlay) endOnboarding();
});

function endOnboarding() {
  onboardingOverlay.style.display = "none";
  localStorage.setItem("rf_onboarded", "1");
}

// ── 4.4: Collapsible Panel ─────────────────────────────────────────────

const collapseToggle = document.getElementById("collapse-toggle");
const mainLayout = document.getElementById("main-layout");

if (localStorage.getItem("rf_collapsed") === "1") {
  mainLayout.classList.add("collapsed");
}

collapseToggle.addEventListener("click", () => {
  mainLayout.classList.toggle("collapsed");
  localStorage.setItem("rf_collapsed", mainLayout.classList.contains("collapsed") ? "1" : "0");
});

// ── 4.6: Confetti ──────────────────────────────────────────────────────

const confettiCanvas = document.getElementById("confetti-canvas");
const confettiCtx = confettiCanvas.getContext("2d");

function triggerConfetti() {
  confettiCanvas.width = window.innerWidth;
  confettiCanvas.height = window.innerHeight;

  const particles = [];
  const colors = ["#e94560", "#ff6b81", "#4caf50", "#ff9800", "#2196f3", "#e0e0e0"];
  const cx = confettiCanvas.width / 2;
  const cy = confettiCanvas.height / 3;

  for (let i = 0; i < 40; i++) {
    particles.push({
      x: cx,
      y: cy,
      vx: (Math.random() - 0.5) * 12,
      vy: (Math.random() - 1) * 10,
      w: Math.random() * 8 + 4,
      h: Math.random() * 6 + 3,
      color: colors[Math.floor(Math.random() * colors.length)],
      rotation: Math.random() * 360,
      rotSpeed: (Math.random() - 0.5) * 10,
      gravity: 0.15,
      opacity: 1,
    });
  }

  let frame = 0;
  function animate() {
    frame++;
    confettiCtx.clearRect(0, 0, confettiCanvas.width, confettiCanvas.height);

    let alive = false;
    for (const p of particles) {
      p.vy += p.gravity;
      p.x += p.vx;
      p.y += p.vy;
      p.rotation += p.rotSpeed;
      p.opacity -= 0.008;
      if (p.opacity <= 0) continue;
      alive = true;

      confettiCtx.save();
      confettiCtx.translate(p.x, p.y);
      confettiCtx.rotate(p.rotation * Math.PI / 180);
      confettiCtx.globalAlpha = p.opacity;
      confettiCtx.fillStyle = p.color;
      confettiCtx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      confettiCtx.restore();
    }

    if (alive && frame < 180) {
      requestAnimationFrame(animate);
    } else {
      confettiCtx.clearRect(0, 0, confettiCanvas.width, confettiCanvas.height);
    }
  }
  requestAnimationFrame(animate);
}

// ── DGX Services ───────────────────────────────────────────────────────

const dgxToggle = document.getElementById("dgx-toggle");
const dgxBody   = document.getElementById("dgx-body");
const dgxList   = document.getElementById("dgx-list");
let dgxOpen = false, dgxPoll = null;

dgxToggle.addEventListener("click", () => {
  dgxOpen = !dgxOpen;
  dgxBody.style.display = dgxOpen ? "block" : "none";
  if (dgxOpen) { refreshDgx(); dgxPoll = setInterval(refreshDgx, 10000); }
  else if (dgxPoll) { clearInterval(dgxPoll); dgxPoll = null; }
});

document.getElementById("dgx-start-all").addEventListener("click", () => toggleAllDgx("start"));
document.getElementById("dgx-stop-all").addEventListener("click", () => toggleAllDgx("stop"));

async function refreshDgx() {
  try {
    const svcs = await fetch("/dgx/services").then(r => r.json());
    dgxList.innerHTML = "";
    svcs.forEach(s => {
      const row = document.createElement("div");
      row.className = "dgx-row";

      const dot = document.createElement("span");
      dot.className = `dgx-dot ${s.status === "running" ? "running" : ""}`;

      const name = document.createElement("span");
      name.className = "dgx-name";
      name.textContent = s.name;

      const port = document.createElement("span");
      port.className = "dgx-port";
      port.textContent = `:${s.port}`;

      const btn = document.createElement("button");
      btn.className = "btn btn-sm btn-secondary";
      btn.textContent = s.status === "running" ? "Stop" : "Start";
      btn.addEventListener("click", () => {
        const action = s.status === "running" ? "stop" : "start";
        fetch(`/dgx/services/${s.name}/${action}`, { method: "POST" }).then(() => refreshDgx());
      });

      row.appendChild(dot);
      row.appendChild(name);
      row.appendChild(port);
      row.appendChild(btn);
      dgxList.appendChild(row);
    });
  } catch (e) {
    dgxList.innerHTML = "";
    const errDiv = document.createElement("div");
    errDiv.style.color = "var(--accent)";
    errDiv.textContent = "Cannot reach backend";
    dgxList.appendChild(errDiv);
  }
}

async function toggleAllDgx(action) {
  try {
    const svcs = await fetch("/dgx/services").then(r => r.json());
    await Promise.allSettled(svcs.map(s => fetch(`/dgx/services/${s.name}/${action}`, { method: "POST" })));
    refreshDgx();
  } catch (e) { /* ignore */ }
}

// ── 3.4: PWA — handled by manifest.json ────────────────────────────────

// ── Bootstrap ──────────────────────────────────────────────────────────

init().catch(e => setStatus(`Init error: ${e.message}`, "error"));
