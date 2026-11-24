/* workspaces.js -- the developer's Workspaces panel (v1.5.0).
 * Session slots on the left; the selected one on the right: what it delivered (open the file,
 * show it in its folder, open the link, share it on Wi-Fi or publicly), what JARVIS is doing
 * right now, the acceptance checklist, the stacked requests, and a box to add a request with
 * files (attach or drop them). Everything here is also reachable by voice or text. */
import { bus } from "./bus.js";
import { t } from "./i18n.js";

let panel, listEl, detailEl, statusEl, makeInteractive, onProduct;
let state = { workspaces: [], selected: null, busy: null };
let detail = null, staged = [], lastProgress = {}, detailTimer = null, pendingDelete = null;

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const base = (p) => String(p || "").split(/[\\/]/).pop();
const STATUS_ICON = { done: "✓", failed: "✖", stopped: "■", undone: "↶", working: "⟳", queued: "…", paused: "❚❚" };

export function initWorkspaces(opts = {}) {
  makeInteractive = opts.makeInteractive || (() => {});
  onProduct = opts.onProduct || (() => {});
  panel = document.getElementById("ws-panel");
  listEl = document.getElementById("ws-list");
  detailEl = document.getElementById("ws-detail");
  statusEl = document.getElementById("ws-status");
  document.getElementById("ws-close").addEventListener("click", close);
  // "+ New": an inline name field (Electron has no prompt())
  const newBtn = document.getElementById("ws-new"), newName = document.getElementById("ws-new-name");
  newBtn.addEventListener("click", () => {
    if (newName.classList.contains("hidden")) { newName.classList.remove("hidden"); newName.value = ""; newName.focus(); return; }
    bus.send({ type: "ws_create", name: newName.value.trim() });
    newName.classList.add("hidden");
  });
  newName.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { bus.send({ type: "ws_create", name: newName.value.trim() }); newName.classList.add("hidden"); }
    if (e.key === "Escape") newName.classList.add("hidden");
  });
  bus.on("ws_state", (m) => { state = m; renderList(); requestDetail(); });
  bus.on("ws_detail", (m) => { detail = m.detail; if (m.links && detail) detail.links = m.links; renderDetail(); });
  bus.on("ws_created", (m) => { bus.send({ type: "ws_select", id: m.workspace.id }); });
  bus.on("ws_queued", (m) => { say(t("ws_queued").replace("{name}", m.workspace.name)); staged = []; renderDetail(); });
  bus.on("ws_error", (m) => say("✖ " + (m.message || "error"), true));
  bus.on("ws_links", () => requestDetail(true));
  bus.on("ws_undone", () => { say(t("ws_undone")); requestDetail(true); });
  bus.on("dev_progress", (m) => {
    if (m.workspace) lastProgress[m.workspace] = m;
    if (m.phase === "downloading") say(m.activity || "");
    if (panel && !panel.classList.contains("hidden") && m.workspace === state.selected) { renderProgress(); requestDetail(); }
  });
  bus.on("dev_product", (m) => { onProduct(m); requestDetail(true); });
  bus.on("_open", () => { if (!panel.classList.contains("hidden")) bus.send({ type: "ws_list" }); });
  // drop files anywhere on the panel
  panel.addEventListener("dragover", (e) => { e.preventDefault(); panel.classList.add("drop"); });
  panel.addEventListener("dragleave", () => panel.classList.remove("drop"));
  panel.addEventListener("drop", (e) => { e.preventDefault(); panel.classList.remove("drop"); stageFiles(e.dataTransfer.files); });
  return { open, close, toggle: () => (panel.classList.contains("hidden") ? open() : close()), stage: stageFiles,
           isOpen: () => !panel.classList.contains("hidden") };
}

function open() { panel.classList.remove("hidden"); bus.send({ type: "ws_list" }); }
function close() { panel.classList.add("hidden"); }
function say(text, bad = false) { statusEl.textContent = text; statusEl.classList.toggle("bad", !!bad); }

function requestDetail(now = false) {
  if (!state.selected || panel.classList.contains("hidden")) return;
  clearTimeout(detailTimer);
  detailTimer = setTimeout(() => bus.send({ type: "ws_get", id: state.selected }), now ? 0 : 700);
}

export function stagePaths(paths) {
  paths.filter(Boolean).forEach((p) => { if (!staged.includes(p)) staged.push(p); });
  renderStaged();
}

function stageFiles(fileList) {
  const paths = [];
  for (const f of fileList || []) {
    let p = "";
    try { p = window.jarvis && window.jarvis.getPathForFile ? window.jarvis.getPathForFile(f) : (f.path || ""); } catch (e) {}
    if (p) paths.push(p);
  }
  stagePaths(paths);
}

/* ------------------------------------------------------------------ list */
function renderList() {
  listEl.innerHTML = "";
  if (!state.workspaces.length) {
    listEl.innerHTML = `<div class="ws-empty">${esc(t("ws_none"))}</div>`;
  }
  state.workspaces.forEach((w) => {
    const b = document.createElement("button");
    const st = w.busy ? "working" : (w.status || "idle");
    b.className = "ws-slot" + (w.id === state.selected ? " sel" : "");
    b.innerHTML = `<span class="ws-dot ${esc(st)}"></span><span class="ws-name">${esc(w.name)}</span>
      <span class="ws-count">${w.requests || 0}</span>`;
    b.title = `${w.name} -- ${st}`;
    b.addEventListener("click", () => bus.send({ type: "ws_select", id: w.id }));
    listEl.appendChild(b);
  });
  if (!state.selected) { detailEl.innerHTML = `<div class="ws-empty">${esc(t("ws_pick"))}</div>`; }
  if (state.pull) say(`${t("ws_downloading")} ${state.pull.model}: ${Math.round(state.pull.percent || 0)}%`);
}

/* ------------------------------------------------------------------ detail */
function renderDetail() {
  const d = detail;
  if (!d || d.id !== state.selected) return;
  // the panel is redrawn while a job runs: keep what you are typing (and where), and an open log
  const oldTa = document.getElementById("ws-text");
  const keep = oldTa ? { value: oldTa.value, focus: document.activeElement === oldTa,
                         a: oldTa.selectionStart, b: oldTa.selectionEnd, wid: detailEl.dataset.wid } : null;
  const logOpen = !!detailEl.querySelector(".ws-log[open]");
  const reqs = d.request_list || [];
  const active = reqs.find((r) => r.status === "working") || reqs[reqs.length - 1];
  const crit = (active && active.checklist) || [];
  detailEl.innerHTML = `
    <div class="ws-title"><b>${esc(d.name)}</b> <span class="ws-kind">${esc(d.kind && d.kind !== "auto" ? d.kind : "")}</span>
      <span class="ws-ctl">
        <button data-a="pause" title="${esc(t("ws_pause"))}">❚❚</button>
        <button data-a="resume" title="${esc(t("ws_resume"))}">▶</button>
        <button data-a="stop" title="${esc(t("ws_stop"))}">■</button>
        <button data-a="undo" title="${esc(t("ws_undo"))}">↶</button>
        <button data-a="delete" class="danger" title="${esc(t("ws_delete"))}">🗑</button>
      </span></div>
    ${productHtml(d.product, d.links)}
    <div id="ws-progress"></div>
    ${crit.length ? `<h5>${esc(t("ws_checklist"))}</h5><ul class="ws-crit">${crit.map((c) =>
      `<li class="${esc(c.status || "pending")}"><span>${c.status === "pass" ? "✓" : c.status === "fail" ? "✖" : "…"}</span>
       ${esc(c.text)}</li>`).join("")}</ul>` : ""}
    <h5>${esc(t("ws_requests"))}</h5>
    <ol class="ws-reqs">${reqs.slice(-8).map((r) => `<li class="${esc(r.status)}" title="${esc(r.summary || "")}">
      <span class="st">${STATUS_ICON[r.status] || "·"}</span> ${esc(r.text.slice(0, 160))}
      ${(r.attachments || []).length ? `<em>📎 ${r.attachments.map((a) => esc(a.name)).join(", ")}</em>` : ""}</li>`).join("")}</ol>
    ${(d.log || []).length ? `<details class="ws-log"><summary>${esc(t("ws_log"))}</summary><pre>${(d.log || [])
      .filter((l) => l.kind !== "reply").slice(-14).map((l) => esc(`${l.kind}: ${l.text}`.slice(0, 240))).join("\n")}</pre></details>` : ""}
    <div class="ws-add">
      <textarea id="ws-text" rows="2" placeholder="${esc(t("ws_ph"))}"></textarea>
      <div id="ws-staged"></div>
      <div class="ws-add-row">
        <button id="ws-attach" type="button">📎 ${esc(t("ws_attach"))}</button>
        <span class="ws-drop">${esc(t("ws_drop"))}</span>
        <button id="ws-send" type="button" class="primary">${esc(t("ws_send"))}</button>
      </div>
    </div>`;
  detailEl.querySelectorAll(".ws-ctl button").forEach((b) => b.addEventListener("click", () => control(b.dataset.a)));
  detailEl.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openProduct(b.dataset.open, b.dataset.how)));
  detailEl.querySelectorAll("[data-share]").forEach((b) => b.addEventListener("click", () =>
    bus.send({ type: "ws_share", id: d.id, mode: b.dataset.share })));
  detailEl.querySelector("#ws-attach").addEventListener("click", async () => {
    if (window.jarvis && window.jarvis.pickFiles) stagePaths(await window.jarvis.pickFiles());
  });
  detailEl.querySelector("#ws-send").addEventListener("click", submit);
  detailEl.querySelector("#ws-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
  });
  const ta = detailEl.querySelector("#ws-text");
  if (keep && keep.wid === d.id && keep.value) {
    ta.value = keep.value;
    if (keep.focus) { ta.focus(); try { ta.setSelectionRange(keep.a, keep.b); } catch (e) {} }
  } else if (keep && keep.focus && keep.wid === d.id) ta.focus();
  if (logOpen) { const lg = detailEl.querySelector(".ws-log"); if (lg) lg.open = true; }
  detailEl.dataset.wid = d.id;
  renderStaged();
  renderProgress();
  detailEl.querySelectorAll("button, textarea").forEach((el) => makeInteractive(el));
}

function productHtml(p, links) {
  if (!p) return "";
  const l = Object.assign({}, p.links || {}, links || {});
  const rows = [];
  if (l.local) rows.push(`<button data-open="${esc(l.local)}" data-how="url">🌐 ${esc(t("ws_open_link"))}</button>`);
  if (l.lan) rows.push(`<button data-open="${esc(l.lan)}" data-how="copy" title="${esc(l.lan)}">📶 ${esc(l.lan)}</button>`);
  if (l.public) rows.push(`<button data-open="${esc(l.public)}" data-how="copy" title="${esc(l.public)}">🌍 ${esc(l.public)}</button>`);
  if (p.path) {
    rows.push(`<button data-open="${esc(p.path)}" data-how="file">📄 ${esc(base(p.path))}</button>`);
    rows.push(`<button data-open="${esc(p.path)}" data-how="folder">📁 ${esc(t("ws_show_folder"))}</button>`);
  }
  if (p.web) {
    rows.push(l.lan ? "" : `<button data-share="lan">📶 ${esc(t("ws_share_lan"))}</button>`);
    rows.push(l.public ? `<button data-share="stop">✖ ${esc(t("ws_share_stop"))}</button>` :
                         `<button data-share="public">🌍 ${esc(t("ws_share_public"))}</button>`);
  }
  return `<div class="ws-product"><div class="ws-prod-title">${esc(t("ws_product"))}: ${esc(p.title || "")}</div>
    <div class="ws-prod-row">${rows.join("")}</div></div>`;
}

function renderProgress() {
  const el = document.getElementById("ws-progress");
  if (!el) return;
  const m = lastProgress[state.selected];
  const busy = state.busy && state.busy.workspace === state.selected;
  if (!m || !busy) { el.innerHTML = ""; return; }
  const pct = m.total ? Math.round(100 * (m.passed || 0) / m.total) : 0;
  const mins = Math.floor((m.elapsed || 0) / 60);
  el.innerHTML = `<div class="ws-activity">${state.busy.paused ? "❚❚ " : "⟳ "}${esc(m.activity || m.phase || "")}</div>
    <div class="ws-bar"><i style="width:${pct}%"></i></div>
    <div class="ws-meta">${esc(t("ws_passing").replace("{p}", m.passed || 0).replace("{n}", m.total || 0))} · ${mins} min · ${esc(t("ws_step"))} ${m.step || 0}</div>`;
}

function renderStaged() {
  const el = document.getElementById("ws-staged");
  if (!el) return;
  el.innerHTML = staged.map((p, i) => `<span class="ws-file" title="${esc(p)}">📎 ${esc(base(p))}
    <button data-i="${i}" title="remove">×</button></span>`).join("");
  el.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { staged.splice(Number(b.dataset.i), 1); renderStaged(); }));
}

function submit() {
  const ta = document.getElementById("ws-text");
  const text = (ta && ta.value || "").trim();
  if (!text && !staged.length) return;
  bus.send({ type: "ws_request", workspace: state.selected || "", text, attachments: staged.slice() });
  if (ta) ta.value = "";
  say(t("ws_sending"));
}

function control(action) {
  const id = state.selected;
  if (!id) return;
  if (action === "delete") {
    // a second click within 4 seconds confirms (the workspace goes to data/workspaces/.trash)
    if (!pendingDelete || pendingDelete !== id) {
      pendingDelete = id;
      say(t("ws_delete_confirm"), true);
      setTimeout(() => { if (pendingDelete === id) pendingDelete = null; }, 4000);
      return;
    }
    pendingDelete = null;
    bus.send({ type: "ws_delete", id });
    return;
  }
  bus.send({ type: "ws_" + action, id });
}

export function openProduct(target, how) {
  const j = window.jarvis || {};
  if (how === "url" && j.openExternal) j.openExternal(target);
  else if (how === "copy") { try { navigator.clipboard.writeText(target); say(t("ws_copied")); } catch (e) {} if (j.openExternal) j.openExternal(target); }
  else if (how === "folder" && j.showInFolder) j.showInFolder(target);
  else if (how === "file" && j.openPath) j.openPath(target);
}
