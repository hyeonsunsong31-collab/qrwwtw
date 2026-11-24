/* chat.js -- holographic chat box (requirements 3, 6, 13).
 * Text input plus a mic button. The mic asks the backend's Whisper engine to listen
 * (push-to-talk). If the backend cannot listen, app.js shows why (v1.4.2: the browser's own
 * speech recognition was the fallback, but inside Electron it needs Google keys and silently
 * did nothing). Streamed replies grow in place, so text appears as JARVIS speaks. */
import { bus } from "./bus.js";

let panel, transcript, input, optionsBar, listenBtn, stagedEl;
const live = new Map();   // msg_id -> bubble element being streamed
let staged = [];          // files attached to the next message (v1.5.0): it becomes a workspace request

export function initChat() {
  panel = document.getElementById("chat-panel");
  transcript = document.getElementById("transcript");
  input = document.getElementById("chat-input");
  optionsBar = document.getElementById("options-bar");
  listenBtn = document.getElementById("btn-listen");

  stagedEl = document.getElementById("chat-staged");
  document.getElementById("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    let text = input.value.trim();
    if (!text && !staged.length) return;
    if (input.dataset.suffix && !text.endsWith(input.dataset.suffix.trim())) text += input.dataset.suffix;
    input.dataset.suffix = "";
    if (staged.length) {
      // with files: a developer request ("make this game faster", "convert it to ico", ...)
      bubble("user", (text || "") + "  📎 " + staged.map(base).join(", "));
      bus.send({ type: "ws_request", text, attachments: staged.slice() });
      staged = [];
      renderStaged();
    } else {
      bus.command(text, "text");    // a new command interrupts any current reply (req 13)
    }
    input.value = "";
  });
  const attach = document.getElementById("btn-attach");
  if (attach) attach.addEventListener("click", async () => {
    if (window.jarvis && window.jarvis.pickFiles) stage(await window.jarvis.pickFiles());
  });
  panel.addEventListener("dragover", (e) => { e.preventDefault(); panel.classList.add("drop"); });
  panel.addEventListener("dragleave", () => panel.classList.remove("drop"));
  panel.addEventListener("drop", (e) => {
    e.preventDefault(); panel.classList.remove("drop");
    const paths = [];
    for (const f of e.dataTransfer.files || []) {
      try { const p = window.jarvis && window.jarvis.getPathForFile ? window.jarvis.getPathForFile(f) : ""; if (p) paths.push(p); } catch (err) {}
    }
    stage(paths);
  });
  document.getElementById("chat-close").addEventListener("click", hide);
  listenBtn.addEventListener("click", () => { listenBtn.classList.add("live"); bus.listen(); });

  return { addMessage, stream, finalize, setOptions, show, hide, toggle, stage, addCard,
           setListening: (on) => listenBtn.classList.toggle("live", !!on),
           focus: () => input && input.focus() };
}

const base = (p) => String(p || "").split(/[\\/]/).pop();

function stage(paths) {
  (paths || []).filter(Boolean).forEach((p) => { if (!staged.includes(p)) staged.push(p); });
  renderStaged();
  if (staged.length) { show(); input.focus(); }
}

function renderStaged() {
  if (!stagedEl) return;
  stagedEl.classList.toggle("hidden", !staged.length);
  stagedEl.innerHTML = "";
  staged.forEach((p, i) => {
    const chip = document.createElement("span");
    chip.className = "ws-file";
    chip.title = p;
    chip.textContent = "📎 " + base(p) + " ";
    const x = document.createElement("button");
    x.textContent = "×";
    x.addEventListener("click", () => { staged.splice(i, 1); renderStaged(); });
    chip.appendChild(x);
    stagedEl.appendChild(chip);
  });
}

/* A finished developer product in the chat: open it, show it in its folder, open its link. */
function addCard(product, labels, open) {
  if (!transcript || !product) return;
  const el = document.createElement("div");
  el.className = "msg assistant product";
  const title = document.createElement("div");
  title.className = "prod-title";
  title.textContent = "✓ " + (product.title || base(product.path) || "");
  el.appendChild(title);
  const row = document.createElement("div");
  row.className = "prod-row";
  const add = (label, fn) => { const b = document.createElement("button"); b.textContent = label; b.addEventListener("click", fn); row.appendChild(b); };
  const links = product.links || {};
  if (links.local) add("🌐 " + labels.open_link, () => open(links.local, "url"));
  if (links.public) add("🌍 " + links.public, () => open(links.public, "copy"));
  if (product.path) {
    add("📄 " + base(product.path), () => open(product.path, "file"));
    add("📁 " + labels.show_folder, () => open(product.path, "folder"));
  }
  el.appendChild(row);
  transcript.appendChild(el);
  transcript.scrollTop = transcript.scrollHeight;
  show();
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

function show() { panel && panel.classList.remove("hidden"); }
function hide() { panel && panel.classList.add("hidden"); }
function toggle() { panel && panel.classList.toggle("hidden"); }
