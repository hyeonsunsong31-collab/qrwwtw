/* app.js -- overlay bootstrap. Connects the bus, renders state, streams replies into the
 * chat, speaks with a British browser voice only when the backend has no voice engine,
 * handles permission dialogs, the 3D space (save / export / neural view / wallpaper),
 * Settings, language auto-switch, click-through, and the hotkey / tray hooks. */
import { bus } from "./bus.js";
import { setLang, getLang, applyI18n, t } from "./i18n.js";
import { initChat } from "./chat.js";
import { initButtons } from "./buttons.js";
import { initSettings } from "./settings.js";
import { initWorkspaces, openProduct } from "./workspaces.js";

let chat, buttons, settings, workspaces, space3d = null;
let cfg = {}, status = {}, backendHasVoice = false, wallpaperOn = false;
let neuralTimer = null, projectRequested = false;

const ring = document.getElementById("state-ring");
const stateLabel = document.getElementById("state-label");
const connDot = document.getElementById("conn-dot");
const neuralReadout = document.getElementById("neural-readout");
const voiceBadge = document.getElementById("voice-badge");
const voiceBadgeText = document.getElementById("voice-badge-text");
const micLevel = document.getElementById("mic-level");

function setState(s) {
  ring.dataset.state = s === "capture" ? "listening" : s;
  stateLabel.textContent = t("state_" + s) || s;
  if (chat) chat.setListening(s === "capture");
}

/* ------------------------------------------------- click-through for widgets ---- */
function makeInteractive(el) {
  if (!window.jarvis || el.dataset.ct) return;
  el.dataset.ct = "1";
  el.addEventListener("mouseenter", () => window.jarvis.setClickThrough(false));
  el.addEventListener("mouseleave", () => window.jarvis.setClickThrough(true));
}

/* ------------------------------------------------------------- 3D space ---- */
async function ensureSpace3d() {
  if (space3d) return space3d;
  try {
    const mod = await import("./space3d/scene.js");
    space3d = new mod.Space3D(document.getElementById("space3d-container"));
    await space3d.init();
  } catch (e) {
    console.error("3D space failed to load (run INSTALL_JARVIS.bat to install three.js):", e);
    space3d = null;
  }
  if (space3d && !projectRequested) { projectRequested = true; bus.send({ type: "space3d_load" }); }
  return space3d;
}

const space3dOpen = () => !document.getElementById("space3d-container").classList.contains("hidden");

/* The brain is told what is on screen, so a bare "close it" / "exit" / "go back" (voice or
 * text) closes the 3D space while it is open (v1.4.4). Sent again after every reconnect. */
function reportUiState() {
  bus.send({ type: "ui_state", state: { space3d: space3dOpen() } });
  if (buttons) buttons.rerenderLabels();             // the dock's 3D button becomes "Close 3D"
}

async function openSpace3d() {
  const wasOpen = space3dOpen();
  document.getElementById("space3d-container").classList.remove("hidden");
  if (window.jarvis) window.jarvis.openSpace3d();
  buttons.showDock();
  if (!wasOpen) reportUiState();
  const s = await ensureSpace3d();
  if (!s) return null;
  s.open();
  if (cfg.space3d && cfg.space3d.gesture_control && !s.gesturesOn()) s.toggleGesture();
  return s;
}

function closeSpace3d() {
  const wasOpen = space3dOpen();
  stopNeural();
  if (space3d) { space3d.hideNeural(); space3d.close(); }
  document.getElementById("space3d-container").classList.add("hidden");
  buttons.hideDock();
  if (window.jarvis) window.jarvis.closeSpace3d();
  if (wasOpen) reportUiState();
}

async function setWallpaper(on) {
  if (on) {
    if (cfg.space3d && cfg.space3d.replace_wallpaper === false) return;
    const s = await ensureSpace3d();
    const state = s ? s.serialize() : null;
    closeSpace3d();                                   // leave immersive mode; desktop shows the live scene
    if (window.jarvis) window.jarvis.setWallpaper(true, state);
  } else if (window.jarvis) {
    window.jarvis.setWallpaper(false);
  }
  wallpaperOn = on;
}

function toBase64(buf) {
  const bytes = new Uint8Array(buf); let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
const stamp = () => new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);

async function exportModel() {
  const s = await ensureSpace3d();
  if (!s) return;
  const glb = await s.exportGLB();
  bus.send({ type: "space3d_export", filename: `jarvis_model_${stamp()}.glb`, data_b64: toBase64(glb) });
}

function stopNeural() { clearInterval(neuralTimer); neuralTimer = null; }

async function toggleNeural() {
  const s = await openSpace3d();
  if (!s) return;
  if (s.neuralVisible()) { stopNeural(); s.hideNeural(); return; }
  bus.send({ type: "neural_points", max_points: 3000 });
  neuralTimer = setInterval(() => bus.send({ type: "neural_points", max_points: 3000 }), 2500);
}

/* ----------------------------------------------- start-up / "show yourself" banner ---- */
let announceTimer = null;
function announceText() {
  const el = document.getElementById("announce-status");
  el.textContent = t(bus.connected ? "announce_online" : "announce_starting");
  el.classList.toggle("ok", bus.connected);
}
function hideAnnounce() {
  const el = document.getElementById("announce");
  el.classList.add("leaving");
  buttons.reactor.classList.remove("attention");
  setTimeout(() => { el.classList.add("hidden"); el.classList.remove("leaving"); }, 700);
}
function announce() {
  const el = document.getElementById("announce");
  announceText();
  el.classList.remove("hidden", "leaving");
  buttons.reactor.classList.add("attention");        // draw the eye to the WAKE button too
  clearTimeout(announceTimer);
  // stays until the brain is connected (then a few seconds more), but never forever
  announceTimer = setTimeout(hideAnnounce, bus.connected ? 7000 : 60000);
}
function announceVisible() { return !document.getElementById("announce").classList.contains("hidden"); }

async function handleUi(action, args = {}) {
  switch (action) {
    case "announce": announce(); break;
    case "open_space3d": await openSpace3d(); break;
    case "close_space3d": closeSpace3d(); break;
    case "workspaces": if (workspaces) workspaces.open(); break;
    case "clone_object": if (await openSpace3d()) space3d.clone(); break;
    case "add_object": if (await openSpace3d()) space3d.addObject(args.shape); break;
    case "delete_object": if (await openSpace3d()) space3d.deleteSelected(); break;
    case "save_project": { const s = await ensureSpace3d(); if (s) bus.send({ type: "space3d_save", project: s.serialize() }); break; }
    case "export_model": await exportModel(); break;
    case "toggle_gesture": {
      const s = await ensureSpace3d(); if (!s) break;
      const on = s.gesturesOn();
      if (!args.state || (args.state === "off" && on) || (args.state === "on" && !on)) await s.toggleGesture();
      break;
    }
    case "neural_view": await toggleNeural(); break;
    case "wallpaper_on": await setWallpaper(true); break;
    case "wallpaper_off": await setWallpaper(false); break;
    case "wallpaper_toggle": await setWallpaper(!wallpaperOn); break;
    case "open_settings_panel": settings.open(); break;
    case "sleep": setState("idle"); buttons.reactor.classList.remove("awake"); break;
    default: break;
  }
}

/* ------------------------------------- British-voice fallback (no backend voice) ---- */
let voices = [];
function pickVoice(lang) {
  const want = lang === "ko" ? "ko" : "en-gb";
  const l = (v) => (v.lang || "").toLowerCase();
  return voices.find(v => l(v).startsWith(want) && /(male|george|daniel|arthur|ryan|thomas)/i.test(v.name))
      || voices.find(v => l(v).startsWith(want))
      || voices.find(v => l(v).startsWith(lang)) || voices[0];
}
function speak(text, lang) {
  if (backendHasVoice || !("speechSynthesis" in window) || !text) return;
  try {   // utterances queue natively, so streamed sentences play back-to-back
    const u = new SpeechSynthesisUtterance(text);
    const v = pickVoice(lang); if (v) { u.voice = v; u.lang = v.lang; }
    speechSynthesis.speak(u);
  } catch (e) {}
}
function stopSpeaking() { if ("speechSynthesis" in window) speechSynthesis.cancel(); }
if ("speechSynthesis" in window) {
  const load = () => { voices = speechSynthesis.getVoices(); };
  load(); speechSynthesis.onvoiceschanged = load;
}

/* ---------------------------------------------------------------- toasts ---- */
function toastHost() {
  let h = document.getElementById("toast-host");
  if (!h) { h = document.createElement("div"); h.id = "toast-host"; document.body.appendChild(h); }
  return h;
}
const esc = (s) => String(s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function toast(title, body, kind = "", ms = 6000, opts = {}) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<h4>${esc(title)}</h4><p>${esc(body)}</p>` + (opts.fix ? `<p class="fix">${esc(opts.fix)}</p>` : "");
  const actions = [];
  const page = { "ms-settings:privacy-microphone": "privacy-microphone", "ms-settings:sound": "sound" }[opts.action];
  if (page) actions.push([t("open_fix"), () => bus.send({ type: "open_settings_page", page })]);
  if (opts.check) actions.push([t("check_mic"), () => { chat.show(); bus.command(getLang() === "ko" ? "마이크 확인해 줘" : "check the microphone", "text"); }]);
  (opts.buttons || []).forEach((b) => actions.push(b));
  if (actions.length) {
    const row = document.createElement("div"); row.className = "row";
    actions.forEach(([label, fn]) => { const b = document.createElement("button"); b.textContent = label;
      b.addEventListener("click", () => { fn(); el.remove(); if (window.jarvis) window.jarvis.setClickThrough(true); });
      row.appendChild(b); });
    el.appendChild(row);
  }
  toastHost().appendChild(el); makeInteractive(el);
  setTimeout(() => el.remove(), ms);
  return el;
}

const permToasts = new Map();          // request id -> toast (removed when answered by voice / text too)
function permissionToast(msg) {
  const el = document.createElement("div");
  el.className = "toast" + ((msg.risk || 0) >= 0.7 ? " danger" : "");
  // the plain question ("Shall I close Spotify, sir?"), then the exact detail (v1.4.4)
  const head = msg.question ? `<p class="perm-q"><b>${esc(msg.question)}</b></p>` :
                              `<p><b>${esc(msg.action || msg.category)}</b></p>`;
  el.innerHTML = `<h4>${esc(t("perm_title"))}</h4>${head}
    <p class="perm-detail">${esc(msg.detail)}</p>
    <p class="perm-hint">${esc(t("perm_hint"))}</p>
    <div class="row"><button class="no">${esc(t("deny"))}</button><button class="ok">${esc(t("allow"))}</button></div>`;
  const close = () => { permToasts.delete(msg.request_id); if (el.isConnected) el.remove();
    if (window.jarvis) window.jarvis.setClickThrough(true); };
  const done = (granted) => { bus.permission(msg.request_id, granted); close(); };
  el.querySelector(".ok").addEventListener("click", () => done(true));
  el.querySelector(".no").addEventListener("click", () => done(false));
  toastHost().appendChild(el); makeInteractive(el);
  permToasts.set(msg.request_id, close);
  setTimeout(() => { if (el.isConnected) done(false); }, 85000);   // auto-deny before the server's timeout
}
function permissionResolved(msg) { const close = permToasts.get(msg.request_id); if (close) close(); }

/* ------------------------------------------------------------ bus events ---- */
function maybeSwitchLang(lang) {
  if (lang && lang !== getLang()) {
    setLang(lang);
    if (buttons) buttons.rerenderLabels();
    if (disposition) renderDisposition(disposition);
    if (announceVisible()) announceText();
  }
}
let disposition = null, selfSummary = null;
function renderDisposition(state) {
  disposition = state || disposition;
  const el = document.getElementById("disposition");
  if (!el || !disposition) return;
  el.textContent = t("mood." + (disposition.mood || "calm"));
  // tooltip: the fixed directive + what the self-model has learned, then the drive layer
  const self = selfSummary ? (selfSummary[getLang()] || selfSummary.en || "") : "";
  el.title = `${self}\n${disposition.purpose || ""}\n${disposition.drive || ""}`.trim();
  el.classList.remove("hidden");
  if (ring) ring.title = `${disposition.label || "JARVIS"} — ${disposition.drive || ""}`;
}

/* ---------------------------------------------- voice status + microphone (v1.4.2) ---- */
function renderVoice() {
  if (!voiceBadge) return;
  const d = status.voice_detail || null, ko = getLang() === "ko";
  let cls = "off", label = t("voice_off");
  const tip = [t("voice_check_tip")];
  if (!status.voice) {
    if (status.voice_state === "loading") { cls = "loading"; label = t("voice_loading"); }
    else if (status.voice_state === "failed" || status.voice_state === "unavailable") {
      cls = "bad"; label = t("voice_bad"); tip.push(status.voice_error || status.voice_state);
    }
  } else if (d) {
    const blocking = (d.problems || []).find((p) => p.blocking);
    const wakeOk = d.wake && d.wake.ready, stt = (d.stt && d.stt.state) || "loading";
    if (d.listening === false) { cls = "off"; label = t("voice_muted"); }
    else if (blocking) { cls = "bad"; label = t("voice_bad"); tip.push(ko ? blocking.message_ko : blocking.message); }
    else if (stt === "failed" || (!wakeOk && d.wake && d.wake.error && d.wake.error !== "loading")) {
      cls = "warn"; label = t("voice_warn");
      (d.problems || []).forEach((p) => tip.push(ko ? p.message_ko : p.message));
    }
    else if (!wakeOk || stt !== "ready") { cls = "loading"; label = t("voice_stt"); }
    else { cls = "ok"; label = t("voice_ok"); }
    if (d.mic && d.mic.device) tip.push(`\u{1F399} ${d.mic.device}${d.mic.rate ? ` \u00b7 ${d.mic.rate} Hz` : ""}`);
    else if (d.mic && d.mic.reason) tip.push(`\u{1F399} ${d.mic.reason}`);
    if (d.last_heard && d.last_heard.text) tip.push(`\u201c${d.last_heard.text}\u201d`);
    const ep = d.mic && d.mic.endpoint;
    if (ep && typeof ep.muted === "boolean" && buttons && buttons.setUserMuted) buttons.setUserMuted(ep.muted);
  }
  voiceBadge.className = "badge voice " + cls;
  voiceBadgeText.textContent = label;
  voiceBadge.title = tip.join("\n");
}

const problemToasts = new Map();
function onVoiceProblem(m) {
  const p = m.problem || {};
  const old = problemToasts.get(p.code);
  if (old) { old.remove(); problemToasts.delete(p.code); }
  if (m.active) {
    const ko = getLang() === "ko";
    problemToasts.set(p.code, toast(t("toast_mic"), ko ? p.message_ko : p.message, p.blocking ? "danger" : "info",
      p.blocking ? 45000 : 12000, { fix: ko ? p.fix_ko : p.fix, action: p.action, check: true }));
  }
  renderVoice();
}

function onVoiceHint(m) {
  if (m.code === "near_miss") toast(t("toast_voice"), t("hint_near_miss").replace("{heard}", m.heard || "Jarvis"), "info", 8000);
  else if (m.code === "stt_loading") toast(t("toast_voice"), t("hint_stt_loading"), "info", 5000);
  else if (m.code === "unheard" && m.reason !== "mic_problem") {
    const key = m.reason === "no_speech" ? "hint_unheard_no_speech"
              : m.reason === "stt_unavailable" ? "hint_stt_unavailable" : "hint_unheard_unclear";
    toast(t("toast_voice"), t(key) + (m.detail ? ` (${m.detail})` : ""), "info", 4500, { check: m.reason !== "no_speech" });
  }
}

let micRaf = 0, micPending = null;
function onMic(m) {
  micPending = m;
  if (micRaf || !micLevel) return;
  micRaf = requestAnimationFrame(() => {
    micRaf = 0;
    const v = micPending || {};
    micLevel.style.width = Math.round(Math.max(0, Math.min(1, v.level || 0)) * 100) + "%";
    micLevel.classList.toggle("speech", !!v.speech);
    micLevel.classList.toggle("silent", !!v.silent);
  });
}

function backendSpeaks() {
  // the backend speaks unless its voice engine is missing or its voice failed to load
  const d = status.voice_detail;
  return !!status.voice && !(d && d.tts && d.tts.state === "failed");
}

/* ------------------------------------------------- language engine (v1.4.3) ---- */
function llmText() {
  const s = status.llm_state, prog = status.llm_progress;
  const pct = prog ? ` ${Math.round(prog.percent || 0)}%` : "";
  if (s === "pulling" && prog) return t("llm_downloading") + pct;
  if (s === "starting") return t("llm_starting");
  if (s === "installing") return t("llm_installing");
  if (s === "not_installed") return t("llm_not_installed");
  if (status.llm_online === false) return t("llm_offline");
  if (s === "loading") return t("llm_warming");
  if (s === "error") return t("llm_error");
  if (prog) return t("llm_downloading") + pct;          // working on an installed model meanwhile
  return "";
}

let llmProblemCode = null, llmToast = null;
function renderLlmProblem() {
  const p = status.llm_problem || null;
  const code = p ? p.code : null;
  if (code === llmProblemCode) return;
  llmProblemCode = code;
  if (llmToast) { llmToast.remove(); llmToast = null; }
  if (!p || code === "model_missing" || code === "installing") return;    // the readout shows progress
  const ko = getLang() === "ko";
  const buttons = [[t("llm_check"), () => { chat.show(); bus.command(ko ? "언어 엔진 확인해 줘" : "check the language engine", "text"); }]];
  if (p.action === "install") buttons.push([t("llm_install_btn"), () => { chat.show(); bus.command(ko ? "언어 엔진 설치해 줘" : "install the language engine", "text"); }]);
  llmToast = toast(t("toast_llm"), ko ? p.message_ko : p.message, "danger", 45000, { fix: ko ? p.fix_ko : p.fix, buttons });
}

function renderStatus() {
  const act = Number(status.neurons_active || 0).toLocaleString();
  const nom = Number(status.neurons_nominal || 2e11).toExponential(1);
  const txt = llmText();
  neuralReadout.textContent = `neural mesh · ${act} / ${nom}` + (txt ? ` · ${txt}` : "");
  const tip = [];
  if (status.llm && status.llm !== "offline-fallback") tip.push(`model: ${status.llm}`);
  if (status.llm_host) tip.push(`engine: Ollama ${status.llm_version || ""} at ${status.llm_host}`.replace("  ", " "));
  if (status.llm_note) tip.push(status.llm_note);
  if (status.llm_problem) tip.push((getLang() === "ko" ? status.llm_problem.message_ko : status.llm_problem.message) || "");
  if (status.llm_proxy_bypassed) tip.push("system proxy bypassed for the local engine");
  neuralReadout.title = tip.join("\n");
  renderLlmProblem();
}

bus.on("hello", (m) => {
  cfg = m.config || {}; status = m.status || {};
  if (m.self) selfSummary = m.self;
  backendHasVoice = backendSpeaks();
  setLang((cfg.language && cfg.language.default) || "en");
  buttons.rerenderLabels();
  renderStatus();
  renderVoice();
  if (announceVisible()) announceText();
});
bus.on("_open", () => { connDot.classList.add("ok"); if (announceVisible()) announce(); });
bus.on("status", (m) => {                    // e.g. the voice engine finished loading
  status = m.status || status;
  backendHasVoice = backendSpeaks();
  renderStatus();
  renderVoice();
});
bus.on("mic", onMic);
bus.on("voice_problem", onVoiceProblem);
bus.on("voice_hint", onVoiceHint);
bus.on("_close", () => { connDot.classList.remove("ok"); neuralReadout.textContent = "neural mesh · offline"; });
bus.on("state", (m) => setState(m.state || "idle"));
bus.on("transcript", (m) => {
  maybeSwitchLang(m.lang);
  if (m.role === "user") { stopSpeaking(); chat.addMessage("user", m.text); }
  else if (m.msg_id) chat.finalize(m.msg_id, m.text);
  else chat.addMessage("assistant", m.text);
});
bus.on("transcript_delta", (m) => { maybeSwitchLang(m.lang); chat.stream(m.msg_id, m.text); });
bus.on("say", (m) => {
  maybeSwitchLang(m.lang);
  if (!backendHasVoice) { setState("speaking"); speak(m.text, m.lang); }
});
bus.on("stop_tts", stopSpeaking);
bus.on("wake_response", () => { buttons.reactor.classList.add("awake"); chat.show(); });
bus.on("control", (m) => {
  if (m.self) selfSummary = m.self;
  if (m.state || m.self) renderDisposition(m.state);
});
bus.on("options", (m) => chat.setOptions(m.options || [], m.suggestions || []));
bus.on("ui", (m) => handleUi(m.action, m.args || {}));
bus.on("permission_request", permissionToast);
bus.on("permission_resolved", permissionResolved);
bus.on("timer_due", (m) => toast(t("toast_timer"), m.text || "", "", 15000));
bus.on("listen_unavailable", (m) => {
  chat.setListening(false);
  toast(t("toast_voice"), t("listen_unavailable").replace("{reason}", m.reason || m.state || "?"), "danger", 9000);
});
bus.on("space3d_project", (m) => { if (m.project && space3d) space3d.load(m.project); });
bus.on("neural_points", (m) => { if (space3d && neuralTimer) space3d.showNeural(m); });
bus.on("error", (m) => toast("JARVIS", m.message || "error", "danger"));

/* --------------------------------------------------------- preload hooks ---- */
function wirePreload() {
  if (!window.jarvis) return;
  window.jarvis.onWake(() => { bus.wake(); buttons.reactor.classList.add("awake"); });
  if (window.jarvis.onAnnounce) window.jarvis.onAnnounce(() => announce());
  window.jarvis.onOpenSpace3d(() => openSpace3d());
  window.jarvis.onToggleChat(() => chat.toggle());
  window.jarvis.onToggleGesture(() => handleUi("toggle_gesture"));
  window.jarvis.onOpenSettings(() => settings.open());
  window.jarvis.onWallpaperChanged((on) => { wallpaperOn = !!on; });
  if (window.jarvis.onToggleWorkspaces) window.jarvis.onToggleWorkspaces(() => workspaces && workspaces.toggle());
}

/* ------------------------------------------------------------------ init ---- */
function main() {
  chat = initChat();
  buttons = initButtons({
    onUi: handleUi,
    toggleChat: () => chat.toggle(),
    openSettings: () => settings.open(),
    focusChat: (prefill, suffix) => {
      chat.show(); chat.focus();
      const inp = document.getElementById("chat-input");
      inp.value = prefill; inp.dataset.suffix = suffix || "";
    },
    wake: () => { bus.wake(); buttons.reactor.classList.add("awake"); },
    space3dOpen,
  });
  settings = initSettings({ makeInteractive });
  workspaces = initWorkspaces({
    makeInteractive,
    // a finished product: a card in the chat and a toast
    onProduct: (m) => {
      const p = m.product || {};
      chat.addCard(p, { open_link: t("ws_open_link"), show_folder: t("ws_show_folder") }, openProduct);
      toast(t("ws_ready_title"), (p.title || "") + (p.links && p.links.local ? " · " + p.links.local : ""), "", 9000);
    },
  });
  if (voiceBadge) voiceBadge.addEventListener("click", () => {
    chat.show();
    bus.command(getLang() === "ko" ? "마이크 확인해 줘" : "check the microphone", "text");
  });
  setLang("en");
  applyI18n(document);
  setState("idle");
  document.querySelectorAll(".interactive").forEach(makeInteractive);
  wirePreload();
  bus.on("_open", reportUiState);                    // a restarted brain learns what is on screen
  // Escape leaves the 3D space (it fills the screen; voice "close the 3D space" works too)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && space3dOpen()) { e.preventDefault(); closeSpace3d(); }
  });
  if (window.jarvis && window.jarvis.ready) window.jarvis.ready();   // main shows the HUD and announces it
}

window.addEventListener("DOMContentLoaded", main);
