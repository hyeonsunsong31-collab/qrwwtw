/* buttons.js — reactor wake, dual mic-mutes, and the full feature dock (requirements 6, 9).
 * Every button maps to a command on the SAME bus, so each capability is reachable by
 * click, voice, or text identically. Feature labels follow the active UI language. */
import { bus } from "./bus.js";
import { t } from "./i18n.js";

// id, icon, i18n label key, and how the button acts.
const FEATURES = [
  { id: "space3d",  ic: "🧊", key: "feat.space3d",  cmd: "open the 3D project space" },
  { id: "addobj",   ic: "➕", key: "feat.addobj",   ui: "add_object" },
  { id: "clone",    ic: "⧉", key: "feat.clone",    cmd: "clone the object" },
  { id: "gesture",  ic: "✋", key: "feat.gesture",  ui: "toggle_gesture" },
  { id: "wallpaper",ic: "🖼", key: "feat.wallpaper",ui: "wallpaper_toggle" },
  { id: "music",    ic: "🎵", key: "feat.music",    prefill: "play " },
  { id: "stopmusic",ic: "⏹", key: "feat.stopmusic",cmd: "stop the music" },
  { id: "sysinfo",  ic: "📊", key: "feat.sysinfo",  cmd: "what is my system status?" },
  { id: "search",   ic: "🔎", key: "feat.search",   prefill: "find " , suffix: " files" },
  { id: "openapp",  ic: "🚀", key: "feat.openapp",  prefill: "open " },
  { id: "settings", ic: "⚙️", key: "feat.settings", cmd: "open settings" },
  { id: "devices",  ic: "🔌", key: "feat.devices",  cmd: "list my devices" },
  { id: "web",      ic: "🌐", key: "feat.web",      prefill: "search for " },
  { id: "volume",   ic: "🔊", key: "feat.volume",   prefill: "set volume to " },
  { id: "chat",     ic: "💬", key: "feat.chat",     act: "toggle_chat" },
  { id: "sleep",    ic: "🌙", key: "feat.sleep",    cmd: "go to sleep" },
];

export function initButtons(handlers) {
  // handlers: { onUi(action), toggleChat(), focusChat(prefill), wake() }
  const reactor = document.getElementById("reactor");
  reactor.addEventListener("click", () => { handlers.wake(); });

  const muteUser = document.getElementById("mute-user");
  const muteAi = document.getElementById("mute-ai");
  let userMuted = false, aiMuted = false;
  muteUser.addEventListener("click", () => {
    userMuted = !userMuted;
    muteUser.classList.toggle("muted", userMuted);
    bus.command(userMuted ? "mute the microphone" : "unmute the microphone", "text");
  });
  muteAi.addEventListener("click", () => {
    aiMuted = !aiMuted;
    muteAi.classList.toggle("muted", aiMuted);
    bus.ui("ai_listen", { enabled: !aiMuted });   // backend stops/starts wake listening
  });

  const dock = document.getElementById("feature-dock");
  renderDock(dock, handlers);
  return {
    toggleDock: () => dock.classList.toggle("hidden"),
    showDock: () => dock.classList.remove("hidden"),
    hideDock: () => dock.classList.add("hidden"),
    rerenderLabels: () => renderDock(dock, handlers),
    reactor,
  };
}

function renderDock(dock, handlers) {
  dock.innerHTML = "";
  FEATURES.forEach((f) => {
    const b = document.createElement("button");
    b.className = "feat";
    b.innerHTML = `<span class="fic">${f.ic}</span><span>${t(f.key)}</span>`;
    b.addEventListener("click", () => {
      if (f.cmd) { bus.interrupt(); bus.command(f.cmd, "text"); }
      else if (f.ui) handlers.onUi(f.ui);
      else if (f.act === "toggle_chat") handlers.toggleChat();
      else if (f.prefill) handlers.focusChat(f.prefill, f.suffix || "");
    });
    dock.appendChild(b);
  });
}
