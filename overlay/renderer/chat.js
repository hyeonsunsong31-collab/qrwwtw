/* chat.js — holographic chat box (requirements 3, 6, 13).
 * Text input + a push-to-talk mic (browser SpeechRecognition) that both feed the same
 * command bus, so every feature is reachable by text or voice from the overlay too. */
import { bus } from "./bus.js";
import { t, getLang } from "./i18n.js";

let panel, transcript, input, optionsBar, listenBtn, recog, listening = false;

export function initChat() {
  panel = document.getElementById("chat-panel");
  transcript = document.getElementById("transcript");
  input = document.getElementById("chat-input");
  optionsBar = document.getElementById("options-bar");
  listenBtn = document.getElementById("btn-listen");

  document.getElementById("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    bus.interrupt();          // a new typed command cuts off any current reply (req 13)
    bus.command(text, "text");
    input.value = "";
  });
  document.getElementById("chat-close").addEventListener("click", hide);
  listenBtn.addEventListener("click", toggleListen);

  return { addMessage, setOptions, show, hide, toggle, focus: () => input && input.focus() };
}

function addMessage(role, text) {
  if (!transcript) return;
  const el = document.createElement("div");
  el.className = "msg " + (role === "user" ? "user" : "assistant");
  el.textContent = text;
  transcript.appendChild(el);
  transcript.scrollTop = transcript.scrollHeight;
  show();
}

function setOptions(options = [], suggestions = []) {
  if (!optionsBar) return;
  optionsBar.innerHTML = "";
  options.forEach((o) => optionsBar.appendChild(chip(o, false)));
  suggestions.forEach((s) => optionsBar.appendChild(chip(s, true)));
}

function chip(label, isSug) {
  const c = document.createElement("button");
  c.className = "chip" + (isSug ? " sug" : "");
  c.textContent = label;
  c.title = isSug ? "suggestion" : "option";
  c.addEventListener("click", () => {
    // suggestions are advice; options are actionable — send options straight to the bus
    if (isSug) { input.value = label.replace(/, sir\.?$/i, ""); input.focus(); }
    else { bus.interrupt(); bus.command(label, "text"); }
  });
  return c;
}

/* --- browser speech-to-text (push to talk) --- */
function ensureRecog() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return null;
  if (recog) return recog;
  recog = new SR();
  recog.continuous = false;
  recog.interimResults = false;
  recog.onresult = (ev) => {
    const text = ev.results[0][0].transcript.trim();
    if (text) { bus.interrupt(); bus.command(text, "voice"); }
  };
  recog.onend = () => { listening = false; listenBtn.classList.remove("live"); };
  recog.onerror = () => { listening = false; listenBtn.classList.remove("live"); };
  return recog;
}

function toggleListen() {
  const r = ensureRecog();
  if (!r) { input.placeholder = "Voice input needs the backend mic, sir — type instead."; return; }
  if (listening) { r.stop(); return; }
  try {
    r.lang = getLang() === "ko" ? "ko-KR" : "en-GB";
    r.start(); listening = true; listenBtn.classList.add("live");
  } catch (e) { /* already started */ }
}

function show() { panel && panel.classList.remove("hidden"); }
function hide() { panel && panel.classList.add("hidden"); }
function toggle() { panel && panel.classList.toggle("hidden"); }
