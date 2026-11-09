/* chat.js -- holographic chat box (requirements 3, 6, 13).
 * Text input plus a mic button. The mic asks the backend's Whisper engine to listen
 * (push-to-talk); only if the backend has no microphone does it fall back to the browser's
 * speech recognition. Streamed replies grow in place, so text appears as JARVIS speaks. */
import { bus } from "./bus.js";
import { getLang } from "./i18n.js";

let panel, transcript, input, optionsBar, listenBtn, recog, listening = false;
const live = new Map();   // msg_id -> bubble element being streamed

export function initChat() {
  panel = document.getElementById("chat-panel");
  transcript = document.getElementById("transcript");
  input = document.getElementById("chat-input");
  optionsBar = document.getElementById("options-bar");
  listenBtn = document.getElementById("btn-listen");

  document.getElementById("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    let text = input.value.trim();
    if (!text) return;
    if (input.dataset.suffix && !text.endsWith(input.dataset.suffix.trim())) text += input.dataset.suffix;
    input.dataset.suffix = "";
    bus.command(text, "text");      // a new command interrupts any current reply (req 13)
    input.value = "";
  });
  document.getElementById("chat-close").addEventListener("click", hide);
  listenBtn.addEventListener("click", () => { listenBtn.classList.add("live"); bus.listen(); });

  return { addMessage, stream, finalize, setOptions, show, hide, toggle, browserListen,
           setListening: (on) => listenBtn.classList.toggle("live", !!on),
           focus: () => input && input.focus() };
}

function bubble(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + (role === "user" ? "user" : "assistant");
  el.textContent = text;
  transcript.appendChild(el);
  transcript.scrollTop = transcript.scrollHeight;
  show();
  return el;
}

function addMessage(role, text) { if (transcript) bubble(role, text); }

function stream(id, text) {
  if (!transcript || !id) return;
  let el = live.get(id);
  if (!el) { el = bubble("assistant", ""); el.classList.add("streaming"); live.set(id, el); }
  el.textContent = text;
  transcript.scrollTop = transcript.scrollHeight;
}

function finalize(id, text) {
  const el = id && live.get(id);
  if (el) { el.textContent = text; el.classList.remove("streaming"); live.delete(id); }
  else addMessage("assistant", text);
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
    if (isSug) { input.value = label.replace(/, sir\.?$/i, ""); input.focus(); }
    else bus.command(label, "text");
  });
  return c;
}

/* Fallback only: the browser's recognizer (used when the backend has no microphone). */
function browserListen() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  listenBtn.classList.remove("live");
  if (!SR) { input.placeholder = "No microphone available to JARVIS - please type, sir."; return; }
  if (!recog) {
    recog = new SR();
    recog.continuous = false; recog.interimResults = false;
    recog.onresult = (ev) => { const t = ev.results[0][0].transcript.trim(); if (t) bus.command(t, "voice"); };
    recog.onend = recog.onerror = () => { listening = false; listenBtn.classList.remove("live"); };
  }
  if (listening) { recog.stop(); return; }
  try { recog.lang = getLang() === "ko" ? "ko-KR" : "en-GB"; recog.start(); listening = true;
        listenBtn.classList.add("live"); } catch (e) {}
}

function show() { panel && panel.classList.remove("hidden"); }
function hide() { panel && panel.classList.add("hidden"); }
function toggle() { panel && panel.classList.toggle("hidden"); }
