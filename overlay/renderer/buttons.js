/* buttons.js -- reactor wake, dual mic-mutes, and the full feature dock (requirements 6, 9).
 * Buttons send the SAME commands a voice or typed request would, so every capability is
 * reachable by click, voice or text identically and is recorded the same way. */
import { bus } from "./bus.js";
import { t } from "./i18n.js";

// id, icon, label key, and the action: cmd (send a command), ui (local toggle),
// prefill (put a command starter in the chat box), act (overlay-only action)
const FEATURES = [
  { id: "space3d",   ic: "🧊", key: "feat.space3d",   cmd: "open the 3D project space" },
  { id: "addobj",    ic: "➕", key: "feat.addobj",    cmd: "add an object" },
  { id: "clone",     ic: "⧉",  key: "feat.clone",     cmd: "clone the object" },
  { id: "delete",    ic: "✖",  key: "feat.delete",    cmd: "delete the object" },
  { id: "save",      ic: "💾", key: "feat.save",      cmd: "save the project" },
  { id: "export",    ic: "📦", key: "feat.export",    cmd: "export the model" },
  { id: "neural",    ic: "🧠", key: "feat.neural",    cmd: "show the neural network" },
  { id: "gesture",   ic: "✋", key: "feat.gesture",   ui: "toggle_gesture" },
  { id: "wallpaper", ic: "🖼",  key: "feat.wallpaper", ui: "wallpaper_toggle" },
  { id: "music",     ic: "🎵", key: "feat.music",     prefill: "play " },
  { id: "stopmusic", ic: "⏹",  key: "feat.stopmusic", cmd: "stop the music" },
  { id: "sysinfo",   ic: "📊", key: "feat.sysinfo",   cmd: "what is my system status?" },
  { id: "search",    ic: "🔎", key: "feat.search",    prefill: "find ", suffix: " files" },
  { id: "openapp",   ic: "🚀", key: "feat.openapp",   prefill: "open " },
  { id: "screenshot",ic: "📸", key: "feat.screenshot",cmd: "take a screenshot" },
  { id: "web",       ic: "🌐", key: "feat.web",       prefill: "search for " },
  { id: "volume",    ic: "🔊", key: "feat.volume",    prefill: "set volume to " },
  { id: "devices",   ic: "🔌", key: "feat.devices",   cmd: "list my devices" },
  { id: "winsettings", ic: "⚙️", key: "feat.settings", cmd: "open settings" },
  { id: "jsettings", ic: "🛠", key: "feat.jsettings", act: "open_settings" },
  { id: "chat",      ic: "💬", key: "feat.chat",      act: "toggle_chat" },
  { id: "aboutme",   ic: "🪞", key: "feat.aboutme",   cmd: "who are you" },
  { id: "remember",  ic: "🧷", key: "feat.remember",  prefill: "remember that " },
  { id: "sleep",     ic: "🌙", key: "feat.sleep",     cmd: "go to sleep" },
];

export function initButtons(handlers) {
  // handlers: { onUi(action), toggleChat(), focusChat(prefill, suffix), openSettings(), wake() }
  const reactor = document.getElementById("reactor");
  reactor.addEventListener("click", () => handlers.wake());

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
    bus.ui("ai_listen", { enabled: !aiMuted });     // the backend closes / reopens its microphone
  });

  const dock = document.getElementById("feature-dock");
  renderDock(dock, handlers);
  return {
    toggleDock: () => dock.classList.toggle("hidden"),
    showDock: () => dock.classList.remove("hidden"),
    hideDock: () => dock.classList.add("hidden"),
    rerenderLabels: () => renderDock(dock, handlers),
    setAiMuted: (on) => { aiMuted = !!on; muteAi.classList.toggle("muted", aiMuted); },
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
      if (f.cmd) bus.command(f.cmd, "text");
      else if (f.ui) handlers.onUi(f.ui);
      else if (f.act === "toggle_chat") handlers.toggleChat();
      else if (f.act === "open_settings") handlers.openSettings();
      else if (f.prefill) handlers.focusChat(f.prefill, f.suffix || "");
    });
    dock.appendChild(b);
  });
}
