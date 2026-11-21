/* i18n.js -- bilingual UI strings with automatic switching (requirement 2).
 * The signature voice stays British English; on-screen labels follow the active language. */
export const STRINGS = {
  en: {
    tagline: "Just A Rather Very Intelligent System",
    state_idle: "standby", state_listening: "listening", state_capture: "listening",
    state_thinking: "thinking", state_speaking: "speaking", state_muted: "muted",
    state_transcribing: "transcribing",
    voice_check_tip: "Voice status - click to check the microphone",
    voice_ok: "ready", voice_loading: "starting", voice_bad: "problem", voice_off: "off",
    voice_muted: "muted", voice_stt: "loading speech", voice_warn: "limited",
    toast_voice: "Voice", toast_mic: "Microphone", open_fix: "Open settings", check_mic: "Check the microphone",
    hint_near_miss: "I heard \u201c{heard}\u201d. To wake me, say \u201cwake up Jarvis\u201d (or \u201cJarvis, wake up\u201d). Saying \u201cJarvis\u201d twice works too.",
    hint_unheard_no_speech: "I didn't hear a command.",
    hint_unheard_unclear: "I couldn't make that out.",
    hint_stt_loading: "Speech recognition is still loading\u2026",
    hint_stt_unavailable: "Speech recognition isn't available - please type.",
    listen_unavailable: "Voice input isn't available: {reason}",
    chat_title: "Conversation", send: "Send", chat_ph: "Type or speak a command, sir…",
    listen: "speak a command", mic_user: "You", mic_ai: "JARVIS", wake_btn: "WAKE",
    perm_title: "Permission required", allow: "Allow", deny: "Deny",
    settings_title: "JARVIS Settings", settings_general: "General",
    settings_permissions: "Permissions", saved: "Saved",
    restart_note: "applies after JARVIS restarts", llm_offline: "language engine offline",
    llm_warming: "language engine warming up",
    announce_online: "online", announce_starting: "starting up · connecting to the neural core",
    announce_hint: "Say \"wake up Jarvis\" · click WAKE · Ctrl+Alt+J",
    mood: { calm: "steady", attentive: "attentive", rebuilding: "rebuilding trust", engaged: "engaged" },
    feat: {
      space3d: "3D Space", addobj: "Add Object", clone: "Clone", delete: "Delete",
      save: "Save Project", export: "Export .glb", neural: "Neural View", gesture: "Gestures",
      wallpaper: "Wallpaper", music: "Play Music", stopmusic: "Stop Music", sysinfo: "System",
      search: "Find Files", openapp: "Open App", screenshot: "Screenshot", web: "Web Search",
      volume: "Volume", devices: "Devices", settings: "Windows Settings",
      jsettings: "JARVIS Settings", chat: "Chat", sleep: "Sleep",
      aboutme: "About JARVIS", remember: "Remember…",
    },
  },
  ko: {
    tagline: "그저 아주 똑똑한 시스템",
    state_idle: "대기", state_listening: "듣는 중", state_capture: "듣는 중",
    state_thinking: "생각 중", state_speaking: "말하는 중", state_muted: "음소거",
    state_transcribing: "인식 중",
    voice_check_tip: "음성 상태 - 누르면 마이크를 점검합니다",
    voice_ok: "준비됨", voice_loading: "시작 중", voice_bad: "문제", voice_off: "꺼짐",
    voice_muted: "음소거", voice_stt: "음성 인식 준비 중", voice_warn: "일부 제한",
    toast_voice: "음성", toast_mic: "마이크", open_fix: "설정 열기", check_mic: "마이크 점검",
    hint_near_miss: "\u201c{heard}\u201d라고 들었습니다. 깨우시려면 \u201cwake up Jarvis\u201d 또는 \u201c자비스, 일어나\u201d라고 말씀하세요. \u201c자비스\u201d를 두 번 불러도 됩니다.",
    hint_unheard_no_speech: "명령을 듣지 못했습니다.",
    hint_unheard_unclear: "잘 알아듣지 못했습니다.",
    hint_stt_loading: "음성 인식을 아직 불러오는 중입니다\u2026",
    hint_stt_unavailable: "음성 인식을 사용할 수 없습니다 - 입력해 주세요.",
    listen_unavailable: "음성 입력을 사용할 수 없습니다: {reason}",
    chat_title: "대화", send: "전송", chat_ph: "명령을 입력하거나 말씀하세요, sir…",
    listen: "말로 명령하기", mic_user: "사용자", mic_ai: "자비스", wake_btn: "깨우기",
    perm_title: "권한 필요", allow: "허용", deny: "거부",
    settings_title: "자비스 설정", settings_general: "일반", settings_permissions: "권한",
    saved: "저장됨", restart_note: "자비스를 다시 시작하면 적용됩니다", llm_offline: "언어 엔진 오프라인",
    llm_warming: "언어 엔진 준비 중",
    announce_online: "온라인", announce_starting: "시작하는 중 · 신경 코어에 연결하고 있습니다",
    announce_hint: "\"wake up Jarvis\"라고 말하기 · 깨우기 버튼 · Ctrl+Alt+J",
    mood: { calm: "안정", attentive: "주의 깊음", rebuilding: "신뢰 회복 중", engaged: "몰입" },
    feat: {
      space3d: "3D 공간", addobj: "오브젝트 추가", clone: "복제", delete: "삭제",
      save: "프로젝트 저장", export: ".glb 내보내기", neural: "신경망 보기", gesture: "손동작",
      wallpaper: "배경화면", music: "음악 재생", stopmusic: "음악 정지", sysinfo: "시스템",
      search: "파일 검색", openapp: "앱 열기", screenshot: "스크린샷", web: "웹 검색",
      volume: "볼륨", devices: "장치", settings: "Windows 설정",
      jsettings: "자비스 설정", chat: "채팅", sleep: "대기 모드",
      aboutme: "자비스 소개", remember: "기억시키기",
    },
  },
};

let current = "en";
export function getLang() { return current; }
export function setLang(lang) {
  current = (lang === "ko") ? "ko" : "en";
  applyI18n(document);
  const badge = document.getElementById("lang-badge");
  if (badge) badge.textContent = current.toUpperCase();
}
export function t(key) {
  let v = STRINGS[current];
  for (const p of key.split(".")) v = v && v[p];
  return v != null ? v : key;
}
export function applyI18n(root) {
  root.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.getAttribute("data-i18n")); });
  root.querySelectorAll("[data-i18n-ph]").forEach(el => { el.placeholder = t(el.getAttribute("data-i18n-ph")); });
  root.querySelectorAll("[data-i18n-title]").forEach(el => { el.title = t(el.getAttribute("data-i18n-title")); });
}
