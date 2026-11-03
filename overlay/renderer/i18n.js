/* i18n.js — bilingual UI strings with automatic switching (requirement 2).
 * The signature voice stays British English; on-screen labels follow the active language. */
export const STRINGS = {
  en: {
    tagline: "Just A Rather Very Intelligent System",
    state_idle: "standby", state_listening: "listening", state_thinking: "thinking",
    state_speaking: "speaking",
    chat_title: "Conversation", send: "Send", chat_ph: "Type or speak a command, sir…",
    listen: "hold to speak", mic_user: "You", mic_ai: "JARVIS", wake_btn: "WAKE",
    perm_title: "Permission required", allow: "Allow", deny: "Deny",
    feat: {
      space3d: "3D Space", clone: "Clone", gesture: "Gestures", wallpaper: "Wallpaper",
      music: "Play Music", stopmusic: "Stop Music", sysinfo: "System", search: "Find Files",
      openapp: "Open App", settings: "Settings", devices: "Devices", web: "Web Search",
      volume: "Volume", chat: "Chat", sleep: "Sleep", addobj: "Add Object",
    },
  },
  ko: {
    tagline: "그저 아주 똑똑한 시스템",
    state_idle: "대기", state_listening: "듣는 중", state_thinking: "생각 중", state_speaking: "말하는 중",
    chat_title: "대화", send: "전송", chat_ph: "명령을 입력하거나 말씀하세요, sir…",
    listen: "길게 눌러 말하기", mic_user: "사용자", mic_ai: "자비스", wake_btn: "깨우기",
    perm_title: "권한 필요", allow: "허용", deny: "거부",
    feat: {
      space3d: "3D 공간", clone: "복제", gesture: "손동작", wallpaper: "배경화면",
      music: "음악 재생", stopmusic: "음악 정지", sysinfo: "시스템", search: "파일 검색",
      openapp: "앱 열기", settings: "설정", devices: "장치", web: "웹 검색",
      volume: "볼륨", chat: "채팅", sleep: "대기 모드", addobj: "오브젝트 추가",
    },
  },
};

let current = "en";
export function getLang() { return current; }
export function setLang(lang) { current = (lang === "ko") ? "ko" : "en"; applyI18n(document);
  const badge = document.getElementById("lang-badge"); if (badge) badge.textContent = current.toUpperCase(); }
export function t(key) {
  const parts = key.split("."); let v = STRINGS[current];
  for (const p of parts) v = v && v[p];
  return v != null ? v : key;
}
export function applyI18n(root) {
  root.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.getAttribute("data-i18n")); });
  root.querySelectorAll("[data-i18n-ph]").forEach(el => { el.placeholder = t(el.getAttribute("data-i18n-ph")); });
  root.querySelectorAll("[data-i18n-title]").forEach(el => { el.title = t(el.getAttribute("data-i18n-title")); });
}
