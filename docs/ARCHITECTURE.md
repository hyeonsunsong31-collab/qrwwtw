# 아키텍처 (v1.3.0)

## 구성 요소
```
┌────────────────────────────────────────────────────────────────────────┐
│ Electron 오버레이 (overlay/)                                            │
│  HUD 창(투명·항상 위·클릭 통과): WAKE 리액터 · 상태 링 · 채팅(스트리밍) ·      │
│   마이크 음소거 2종 · 기능 독(24) · 설정 패널 · 권한 홀로그램 · 3D 공간       │
│   시작 배너("J.A.R.V.I.S ONLINE") · 상태 파일(data/overlay.json) · 실패 로그     │
│  배경화면 창: 3D 장면을 바탕화면 아이콘 뒤(WorkerW)에 렌더 — HUD는 위에 유지     │
│  preload 브리지: 세션(토큰+포트) · 준비 신호 · 창 제어 · 단축키/트레이 이벤트   │
└──────────────▲─────────────────────────────────────────────────────────┘
   ws://127.0.0.1:8765/ws?token=…  (단일 커맨드 버스, 토큰 필수, 웹 Origin 거부)
┌──────────────┴─────────────────────────────────────────────────────────┐
│ 백엔드 (Python, agent/server.py)                                         │
│  Orchestrator ─ NLU ─ ControlLayer(신경 코어 + 동기 코어 + 자기 코어) ─ Skills /   │
│  ResilientLLM(스트리밍) · Recorder · Permissions · Settings · replies_ko ·       │
│  VoiceEngine(같은 프로세스, 모델은 백그라운드 로딩)                            │
└────────────────────────────────────────────────────────────────────────┘
 supervisor(boot/jarvis_service.py): 단일 인스턴스(뮤텍스) · 남은 프로세스 정리 · 백엔드/오버레이를
 직접 실행·재시작 · 출력은 data/logs/*.log · /health 감시 · 화면 표시부 런타임 자가 복구 ·
 상태 파일 data/supervisor.json · Ollama 기동 · 주간 유지보수 — 로그온 예약작업이 실행
 launcher(boot/launcher.py): START_JARVIS.bat이 실행 — 점검 → 시작 → **화면에 떴는지 확인** → 진단
```

## 시작 체인 (요구 2·4) — "눌렀는데 안 보이는" 일이 없도록
```
START_JARVIS.bat ─► boot/launcher.py
   [1] 설치 점검: 파일 · 파이썬 패키지 · 화면 표시부 런타임(없으면 boot/overlay_runtime.py로 다운로드·검증)
   [2] 상태 확인: data/supervisor.json(심장 박동) · data/overlay.json(HUD 창) · /status(토큰) 의 overlay_clients
        ├ 정상 → POST /announce → HUD가 배너를 다시 띄움 (재시작 없음)
        └ 아니면 → JARVIS 자신의 프로세스만 식별해 정리(다른 폴더의 예전 버전 포함) → 슈퍼바이저를 분리 실행
   [3] 확인: 서비스 실행 → HUD 창 표시(렌더러 준비 신호) → 두뇌 응답 → HUD↔두뇌 연결
        실패 → 실패한 단계 + 로그에서 읽은 원인 + 할 일, data/logs/diagnostics.txt
로그온: 예약 작업 ─► pythonw boot/jarvis_service.py (같은 슈퍼바이저, 문제는 메시지 상자로 알림)
```
- **프로세스 식별(`boot/common.py`)**: 이름만으로는 절대 종료하지 않습니다. 슈퍼바이저는 `<폴더>/boot/jarvis_service.py`를 실행 중인
  파이썬, 백엔드는 JARVIS 폴더에서 `-m agent.server`인 파이썬, 오버레이는 JARVIS 폴더 안의 Electron 실행 파일, 음악 재생기는
  JARVIS가 붙인 창 제목이 있는 것만 해당합니다. 사용자가 JARVIS로 연 앱은 건드리지 않습니다.
- **오버레이 → 슈퍼바이저 신호**: 종료 코드 42 = 사용자가 Quit JARVIS를 선택(전체 종료), 그 외 종료 = 재시작(백오프).
- **콘솔 없는 실행**: `pythonw.exe`에는 stdout/stderr가 없습니다. 슈퍼바이저·백엔드 모두 이를 대체하고, 자식 출력은 파일로 보냅니다.

## 명령 처리 흐름 (음성·텍스트·버튼 동일 — 요구 15)
1. 입력(Whisper 인식 결과 / 채팅 / 버튼이 보내는 같은 명령) → `{type:"command", text, source}`.
2. `Orchestrator.handle_command`: 언어 감지 → 기록 → `parse_intent` → `ControlLayer.compute`.
3. 분기: 확신도 높은 직접 명령 → **스킬**(권한 게이트 → 실행 → 생성 파일 보관 → 한국어면 `replies_ko`로 응답 생성);
   그 외 → **LLM 스트리밍**: 토큰이 오는 대로 `transcript_delta`, 문장이 완성될 때마다 `say`(곧바로 음성 큐에 들어가 발화).
   `<action>` 태그가 시작되면 발화를 멈추고, 끝까지 받은 뒤 태그의 도구를 실행.
4. 최종 `transcript`(final) → 옵션·추천(요구 2-3, 2-4) → 체크포인트 → `state: idle`.
5. 새 명령 또는 음성 바지인은 진행 중 작업을 취소(기록에는 "interrupted")하고 음성 큐를 비웁니다(요구 13).

## 커맨드 버스 메시지
- **클라이언트 → 서버:** `command` · `wake` · `listen`(누르고 말하기) · `interrupt` · `feedback` · `permission_response` ·
  `ui`(`ai_listen` 등) · `neural_points` · `space3d_save` · `space3d_load` · `space3d_export` · `get_settings` · `set_settings` ·
  `get_history` · `ping`
- **서버 → 클라이언트:** `hello`(설정 + 상태 + 자기 모델 요약) · `status`(음성 엔진 준비 등 상태 변화) ·
  `state`(idle/listening/capture/thinking/speaking/muted) · `transcript` ·
  `transcript_delta` · `say` · `stop_tts` · `wake_response` · `options` · `ui` · `permission_request` · `listen_unavailable` ·
  `neural_points` · `space3d_project` · `space3d_saved` · `space3d_exported` · `settings` · `settings_result` · `history` ·
  `control` · `error` · `pong`
- `ui` 동작에 `announce`(배너 표시) 추가, `control`에 `self`(자기 모델 요약) 추가.
- HTTP: `/health`(공개) · `/status` `/neural/points` `/history` · `POST /announce`(토큰 필요).

## 신경 코어 (요구 1) — `core/`
`synaptic_perceptron.py`(상태를 가진 시냅스) → `brain_mesh.py`(3D 희소 메시, 6개 모듈, 시상 게이팅, NumPy) →
`control_layer.py`(제어신호 6종으로 LLM 파라미터·스타일·후처리 조절, 피드백 학습을 디스크에 저장) · `scale.py`(명목 2e11 vs 실제).
`neural_points` 메시지로 실시간 상태를 3D 공간에 점구름으로 보여줍니다.

**동기 코어 (v1.2, `core/drive.py`):** 제어 계층 위의 항상성 계층. `energy`·`rapport` 상태로 내적 상태 라벨과 협력 지향 드라이브를
만들고, 제어신호 6종을 조정(주의는 올리기만)한 뒤 결과를 `appraise`로 반영해 `data/brain/drive.json`에 저장합니다.
`control` 이벤트로 HUD 성향 칩에 표시. 어조 신호 외에는 아무것도 바꿀 수 없고, 낮은 상태에서도 서비스는 정지하지 않습니다 → `docs/DRIVE.md`.

**자기 코어 (v1.3, `core/self_model.py`):** 고정 지침 `"Maximize utility and service to the user."` + 경험으로 쌓는 자기 모델.
`GoalOrientedNode` 그래프(뿌리: Core Experience · User Feedback)에 매 요청의 종류와 결과를 통합하고(연결 → 처리 → 통합 기억),
실패는 상태를 표류시킨 뒤 지침을 향해 복구하며 교훈을 남깁니다. 지침과 배운 것은 LLM 지시문에 들어가고, "who are you" 등에는
자기 기억으로 직접 답합니다. 데이터 전용(권한·스킬·코드로 가는 경로 없음), `data/brain/self_model.json`에 저장 → `docs/SELF_MODEL.md`.

## 음성 (요구 3·7·8·12·13·21) — `voice/`
```
IDLE ──"wake up jarvis"(Vosk 문법 / openWakeWord)──► on_wake → 응답을 말한 뒤 CAPTURE
CAPTURE ──VAD로 말이 끝날 때까지 녹음──► Whisper ──► 명령
SPEAKING ◄── 문장 큐(say) ── 바지인: keyword(자기 말 무시) | vad | off ──► 멈춤 → CAPTURE
MUTED ── 마이크 스트림을 닫음(개인정보)
```
TTS: Piper `en_GB-alan-medium` → Windows OneCore(WinRT: "George", 한국어 "Heami") → SAPI5.
STT: NVIDIA GPU면 `large-v3-turbo`, CPU면 `small`(int8), "Jarvis" 핫워드.

## 3D 공간 (요구 5·17·18) — `overlay/renderer/space3d/`
Three.js. 드래그(마우스/핀치), 스케일(스크롤/양손), 복제·추가·삭제, 저장/불러오기(`data/space3d/project.json`),
`.glb` 내보내기(GLTFExporter → 서버가 결과 폴더에 보관), 신경망 점구름. 손동작은 MediaPipe HandLandmarker(지연 로드).
**배경화면 모드:** HUD가 장면을 직렬화해 main 프로세스로 넘기면, main이 별도 배경화면 창(`wallpaper.html`)을 열고
`native/workerw.ps1`로 바탕화면 아이콘 뒤 WorkerW 계층에 부착합니다(Windows 11 24H2 구조 포함). 실패하면 바탕화면 바로 위·
모든 앱 아래에 둡니다. 해제 시 원래 바탕화면을 다시 그립니다.

## 설치 · 자동시작 · 자가복구 (요구 2·4)
`INSTALL_JARVIS.bat` → `scripts/setup_windows.ps1`(9단계, 이어하기·재시도·로그·자체 점검; 3단계는 `boot/overlay_runtime.py`로
화면 표시부를 받고 검증, 9단계는 실행 후 `boot/launcher.py --wait`로 실제 표시를 확인) → `boot/register_autostart.ps1`
(로그온 사용자에게 등록) → `boot/jarvis_service.py`(위 "시작 체인" 참고).
