/* app.js -- overlay bootstrap. Connects the bus, renders state, streams replies into the
 * chat, speaks with a British browser voice only when the backend has no voice engine,
 * handles permission dialogs, the 3D space (save / export / neural view / wallpaper),
 * Settings, language auto-switch, click-through, and the hotkey / tray hooks. */
import { bus } from "./bus.js";
import { setLang, getLang, applyI18n, t } from "./i18n.js";
import { initChat } from "./chat.js";
import { initButtons } from "./buttons.js";
import { initSettings } from "./settings.js";

let chat, buttons, settings, space3d = null;
let cfg = {}, status = {}, backendHasVoice = false, wallpaperOn = false;
let neuralTimer = null, projectRequested = false;

const ring = document.getElementById("state-ring");
const stateLabel = document.getElementById("state-label");
const connDot = document.getElementById("conn-dot");
const neuralReadout = document.getElementById("neural-readout");

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

async function openSpace3d() {
  document.getElementById("space3d-container").classList.remove("hidden");
  if (window.jarvis) window.jarvis.openSpace3d();
  buttons.showDock();
  const s = await ensureSpace3d();
  if (!s) return null;
  s.open();
  if (cfg.space3d && cfg.space3d.gesture_control && !s.gesturesOn()) s.toggleGesture();
  return s;
}

function closeSpace3d() {
  stopNeural();
  if (space3d) { space3d.hideNeural(); space3d.close(); }
  document.getElementById("space3d-container").classList.add("hidden");
  buttons.hideDock();
  if (window.jarvis) window.jarvis.closeSpace3d();
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

async function handleUi(action, args = {}) {
  switch (action) {
    case "open_space3d": await openSpace3d(); break;
    case "close_space3d": closeSpace3d(); break;
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

function toast(title, body, kind = "", ms = 6000) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<h4>${esc(title)}</h4><p>${esc(body)}</p>`;
  toastHost().appendChild(el); makeInteractive(el);
  setTimeout(() => el.remove(), ms);
}

function permissionToast(msg) {
  const el = document.createElement("div");
  el.className = "toast" + ((msg.risk || 0) >= 0.7 ? " danger" : "");
  el.innerHTML = `<h4>${esc(t("perm_title"))}</h4>
    <p><b>${esc(msg.action || msg.category)}</b><br>${esc(msg.detail)}</p>
    <div class="row"><button class="no">${esc(t("deny"))}</button><button class="ok">${esc(t("allow"))}</button></div>`;
  const done = (granted) => { bus.permission(msg.request_id, granted); el.remove();
    if (window.jarvis) window.jarvis.setClickThrough(true); };
  el.querySelector(".ok").addEventListener("click", () => done(true));
  el.querySelector(".no").addEventListener("click", () => done(false));
  toastHost().appendChild(el); makeInteractive(el);
  setTimeout(() => { if (el.isConnected) done(false); }, 85000);   // auto-deny before the server's timeout
}

/* ------------------------------------------------------------ bus events ---- */
function maybeSwitchLang(lang) {
  if (lang && lang !== getLang()) {
    setLang(lang);
    if (buttons) buttons.rerenderLabels();
    if (disposition) renderDisposition(disposition);
  }
}
let disposition = null;
function renderDisposition(state) {
  disposition = state || disposition;
  const el = document.getElementById("disposition");
  if (!el || !disposition) return;
  el.textContent = t("mood." + (disposition.mood || "calm"));
  el.title = `${disposition.purpose || ""}\n${disposition.drive || ""}`.trim();
  el.classList.remove("hidden");
  if (ring) ring.title = `${disposition.label || "JARVIS"} — ${disposition.drive || ""}`;
}

function renderStatus() {
  const act = Number(status.neurons_active || 0).toLocaleString();
  const nom = Number(status.neurons_nominal || 2e11).toExponential(1);
  neuralReadout.textContent = `neural mesh · ${act} / ${nom}` + (status.llm_online === false ? ` · ${t("llm_offline")}` : "");
}

bus.on("hello", (m) => {
  cfg = m.config || {}; status = m.status || {};
  backendHasVoice = !!status.voice;
  setLang((cfg.language && cfg.language.default) || "en");
  buttons.rerenderLabels();
  renderStatus();
});
bus.on("_open", () => connDot.classList.add("ok"));
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
bus.on("control", (m) => { if (m.state) renderDisposition(m.state); });
bus.on("options", (m) => chat.setOptions(m.options || [], m.suggestions || []));
bus.on("ui", (m) => handleUi(m.action, m.args || {}));
bus.on("permission_request", permissionToast);
bus.on("listen_unavailable", () => chat.browserListen());
bus.on("space3d_project", (m) => { if (m.project && space3d) space3d.load(m.project); });
bus.on("neural_points", (m) => { if (space3d && neuralTimer) space3d.showNeural(m); });
bus.on("error", (m) => toast("JARVIS", m.message || "error", "danger"));

/* --------------------------------------------------------- preload hooks ---- */
function wirePreload() {
  if (!window.jarvis) return;
  window.jarvis.onWake(() => { bus.wake(); buttons.reactor.classList.add("awake"); });
  window.jarvis.onOpenSpace3d(() => openSpace3d());
  window.jarvis.onToggleChat(() => chat.toggle());
  window.jarvis.onToggleGesture(() => handleUi("toggle_gesture"));
  window.jarvis.onOpenSettings(() => settings.open());
  window.jarvis.onWallpaperChanged((on) => { wallpaperOn = !!on; });
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
  });
  settings = initSettings({ makeInteractive });
  setLang("en");
  applyI18n(document);
  setState("idle");
  document.querySelectorAll(".interactive").forEach(makeInteractive);
  wirePreload();
}

window.addEventListener("DOMContentLoaded", main);
