/* settings.js -- holographic Settings panel (requirement 2-(3): options for continuous
 * adjustment). Renders whatever the backend's validated schema offers (language, voice,
 * wake word, interrupt mode, 3D, model, and every permission category) and applies each
 * change immediately; the backend validates, saves atomically and hot-applies it. */
import { bus } from "./bus.js";
import { t } from "./i18n.js";

let panel, body, statusEl, makeInteractive;

export function initSettings(opts = {}) {
  makeInteractive = opts.makeInteractive || (() => {});
  panel = document.getElementById("settings-panel");
  body = document.getElementById("settings-body");
  statusEl = document.getElementById("settings-status");
  document.getElementById("settings-close").addEventListener("click", close);
  bus.on("settings", render);
  bus.on("settings_result", showResult);
  return { open, close, toggle: () => (panel.classList.contains("hidden") ? open() : close()) };
}

function open() { panel.classList.remove("hidden"); statusEl.textContent = ""; bus.send({ type: "get_settings" }); }
function close() { panel.classList.add("hidden"); }

function render(msg) {
  body.innerHTML = "";
  const general = [], perms = [];
  Object.entries(msg.schema || {}).forEach(([k, spec]) => (spec.group === "permissions" ? perms : general).push([k, spec]));
  section(t("settings_general"), general, msg.values || {});
  section(t("settings_permissions"), perms, msg.values || {});
}

function section(title, items, values) {
  if (!items.length) return;
  const h = document.createElement("h4");
  h.textContent = title;
  body.appendChild(h);
  items.forEach(([key, spec]) => body.appendChild(row(key, spec, values[key])));
}

function send(key, value) { bus.send({ type: "set_settings", changes: { [key]: value } }); }

function row(key, spec, value) {
  const r = document.createElement("label");
  r.className = "set-row" + (spec.locked ? " locked" : "");
  const name = document.createElement("span");
  name.className = "set-name";
  name.textContent = spec.label || key;
  if (spec.restart) { const b = document.createElement("em"); b.textContent = " ↻"; b.title = t("restart_note"); name.appendChild(b); }
  r.appendChild(name);

  let ctl;
  if (spec.type === "bool") {
    ctl = document.createElement("input"); ctl.type = "checkbox"; ctl.checked = !!value;
    ctl.addEventListener("change", () => send(key, ctl.checked));
  } else if (spec.type === "choice") {
    ctl = document.createElement("select");
    spec.choices.forEach((c) => { const o = document.createElement("option"); o.value = c; o.textContent = c;
      if (c === value) o.selected = true; ctl.appendChild(o); });
    ctl.addEventListener("change", () => send(key, ctl.value));
  } else if (spec.type === "float") {
    const wrap = document.createElement("span"); wrap.className = "set-range";
    ctl = document.createElement("input"); ctl.type = "range";
    ctl.min = spec.min; ctl.max = spec.max; ctl.step = spec.step || 0.05; ctl.value = value;
    const out = document.createElement("output"); out.textContent = Number(value).toFixed(2);
    ctl.addEventListener("input", () => { out.textContent = Number(ctl.value).toFixed(2); });
    ctl.addEventListener("change", () => send(key, Number(ctl.value)));
    wrap.appendChild(ctl); wrap.appendChild(out);
    if (spec.locked) ctl.disabled = true;
    r.appendChild(wrap);
    return r;
  } else {
    ctl = document.createElement("input"); ctl.type = "text"; ctl.value = value || "";
    ctl.addEventListener("change", () => send(key, ctl.value.trim()));
  }
  if (spec.locked) ctl.disabled = true;
  r.appendChild(ctl);
  return r;
}

function showResult(msg) {
  const parts = [];
  const applied = Object.keys(msg.applied || {});
  if (applied.length) parts.push(`${t("saved")}: ${applied.join(", ")}`);
  Object.entries(msg.rejected || {}).forEach(([k, why]) => parts.push(`✖ ${k}: ${why}`));
  if ((msg.restart_required || []).length) parts.push(t("restart_note"));
  statusEl.textContent = parts.join("  ·  ");
}
