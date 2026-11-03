/* app.js — overlay bootstrap. Connects the bus, renders state, speaks replies (British
 * voice fallback in-browser), handles permission dialogs, 3D-space control, language
 * auto-switch, click-through, and the global-hotkey / tray hooks from the main process. */
import { bus } from "./bus.js";
import { setLang, getLang, applyI18n, t } from "./i18n.js";
import { initChat } from "./chat.js";
import { initButtons } from "./buttons.js";

let chat, buttons, space3d = null, backendHasVoice = false, wallpaperOn = false;

const ring = document.getElementById("state-ring");
const stateLabel = document.getElementById("state-label");
const connDot = document.getElementById("conn-dot");
const neuralReadout = document.getElementById("neural-readout");

function setState(s) {
  ring.dataset.state = s;
  stateLabel.textContent = t("state_" + s) || s;
}

/* ---------------- 3D project space (lazy-loaded so the HUD works without three.js) ---- */
async function ensureSpace3d() {
  if (space3d) return space3d;
  const host = document.getElementById("space3d-container");
  try {
    const mod = await import("./space3d/scene.js");
    space3d = new mod.Space3D(host);
    await space3d.init();
  } catch (e) {
    console.error("3D space failed to load (run npm install):", e);
    space3d = null;
  }
  return space3d;
}

async function openSpace3d() {
  const host = document.getElementById("space3d-container");
  host.classList.remove("hidden");
  window.jarvis && window.jarvis.openSpace3d();
  buttons.showDock();
  const s = await ensureSpace3d();
  if (s) s.open();
}

function closeSpace3d() {
  if (space3d) space3d.close();
  document.getElementById("space3d-container").classList.add("hidden");
  buttons.hideDock();
  if (wallpaperOn) toggleWallpaper(false);
  window.jarvis && window.jarvis.closeSpace3d();
}

function toggleWallpaper(force) {
  wallpaperOn = (force === undefined) ? !wallpaperOn : force;
  window.jarvis && window.jarvis.setWallpaper(wallpaperOn);
  if (space3d) space3d.setWallpaper(wallpaperOn);
}

async function handleUi(action, args = {}) {
  switch (action) {
    case "open_space3d": await openSpace3d(); break;
    case "close_space3d": closeSpace3d(); break;
    case "clone_object": (await ensureSpace3d()) && space3d.clone(); break;
    case "add_object": await openSpace3d(); space3d && space3d.addObject(); break;
    case "toggle_gesture": (await ensureSpace3d()) && space3d.toggleGesture(); break;
    case "wallpaper_toggle": await openSpace3d(); toggleWallpaper(); break;
    case "sleep": setState("idle"); buttons.reactor.classList.remove("awake"); break;
    default: break; // ai_listen etc. handled server-side
  }
}

/* ------------------------------- British-voice TTS fallback -------------------------- */
let voices = [];
function pickVoice(lang) {
  const want = lang === "ko" ? "ko" : "en-GB";
  return voices.find(v => v.lang && v.lang.toLowerCase().startsWith(want) &&
                          /(male|daniel|george|arthur|ryan|thomas)/i.test(v.name))
      || voices.find(v => v.lang && v.lang.toLowerCase().startsWith(want))
      || voices.find(v => v.lang && v.lang.toLowerCase().startsWith(lang))
      || voices[0];
}
function speak(text, lang) {
  if (backendHasVoice || !("speechSynthesis" in window) || !text) return;
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    const v = pickVoice(lang); if (v) { u.voice = v; u.lang = v.lang; }
    u.rate = 1.0; u.pitch = 1.0;
    speechSynthesis.speak(u);
  } catch (e) {}
}
if ("speechSynthesis" in window) {
  const load = () => { voices = speechSynthesis.getVoices(); };
  load(); speechSynthesis.onvoiceschanged = load;
}

/* ----------------------------------- permission toasts ------------------------------ */
function toastHost() {
  let h = document.getElementById("toast-host");
  if (!h) { h = document.createElement("div"); h.id = "toast-host"; document.body.appendChild(h); }
  return h;
}
function permissionToast(msg) {
  const h = toastHost();
  const el = document.createElement("div");
  el.className = "toast" + ((msg.risk || 0) >= 0.7 ? " danger" : "");
  el.innerHTML = `<h4>${t("perm_title")}</h4>
    <p><b>${escapeHtml(msg.action || msg.category)}</b><br>${escapeHtml(msg.detail || "")}</p>
    <div class="row">
      <button class="no">${t("deny")}</button>
      <button class="ok">${t("allow")}</button>
    </div>`;
  const done = (granted) => { bus.permission(msg.request_id, granted); el.remove(); };
  el.querySelector(".ok").addEventListener("click", () => done(true));
  el.querySelector(".no").addEventListener("click", () => done(false));
  h.appendChild(el);
  setTimeout(() => { if (el.isConnected) done(false); }, 85000); // auto-deny before server timeout
}
function escapeHtml(s) { return (s || "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* --------------------------------------- bus events --------------------------------- */
function maybeSwitchLang(lang) {
  if (lang && lang !== getLang()) {
    setLang(lang);                 // flips <html> labels + lang badge
    if (buttons) buttons.rerenderLabels();   // feature dock labels follow the language
  }
}

bus.on("hello", (m) => {
  const def = (m.config && m.config.language && m.config.language.default) || "en";
  setLang(def);
});
bus.on("_open", () => connDot.classList.add("ok"));
bus.on("_close", () => connDot.classList.remove("ok"));
bus.on("state", (m) => setState(m.state || "idle"));
bus.on("transcript", (m) => {
  maybeSwitchLang(m.lang);
  if (m.role === "user" && "speechSynthesis" in window) speechSynthesis.cancel(); // barge-in
  chat.addMessage(m.role, m.text);
});
bus.on("say", (m) => { maybeSwitchLang(m.lang); setState("speaking"); speak(m.text, m.lang);
  setTimeout(() => { if (ring.dataset.state === "speaking") setState("idle"); }, 400 + (m.text || "").length * 45); });
bus.on("stop_tts", () => { if ("speechSynthesis" in window) speechSynthesis.cancel(); });
bus.on("wake_response", () => { buttons.reactor.classList.add("awake"); chat.show(); setState("listening"); });
bus.on("options", (m) => chat.setOptions(m.options || [], m.suggestions || []));
bus.on("ui", (m) => handleUi(m.action, m.args || {}));
bus.on("permission_request", (m) => permissionToast(m));

/* ---------------------------------------- health ------------------------------------ */
async function health() {
  try {
    const r = await fetch("http://127.0.0.1:8765/health");
    const h = await r.json();
    backendHasVoice = !!h.voice;
    const act = (h.neurons_active || 0).toLocaleString();
    const nom = Number(h.neurons_nominal || 2e11).toExponential(1);
    neuralReadout.textContent = `neural mesh · ${act} / ${nom}`;
  } catch (e) {
    neuralReadout.textContent = "neural mesh · offline";
  }
}

/* ------------------------------------- click-through -------------------------------- */
function wireClickThrough() {
  if (!window.jarvis) return;
  document.querySelectorAll(".interactive").forEach((el) => {
    el.addEventListener("mouseenter", () => window.jarvis.setClickThrough(false));
    el.addEventListener("mouseleave", () => window.jarvis.setClickThrough(true));
  });
}

/* ------------------------------------- preload hooks -------------------------------- */
function wirePreload() {
  if (!window.jarvis) return;
  window.jarvis.onWake(() => { bus.wake(); buttons.reactor.classList.add("awake"); });
  window.jarvis.onOpenSpace3d(() => openSpace3d());
  window.jarvis.onToggleChat(() => chat.toggle());
  window.jarvis.onToggleGesture(() => handleUi("toggle_gesture"));
}

/* ------------------------------------------ init ------------------------------------ */
function main() {
  chat = initChat();
  buttons = initButtons({
    onUi: handleUi,
    toggleChat: () => chat.toggle(),
    focusChat: (prefill, suffix) => { chat.show(); chat.focus();
      const inp = document.getElementById("chat-input"); inp.value = prefill; inp.dataset.suffix = suffix || ""; },
    wake: () => { bus.wake(); buttons.reactor.classList.add("awake"); },
  });
  setLang("en");
  applyI18n(document);
  setState("idle");
  wireClickThrough();
  wirePreload();
  health();
  setInterval(health, 15000);
}

window.addEventListener("DOMContentLoaded", main);
