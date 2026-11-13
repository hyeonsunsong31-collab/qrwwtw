# 요구사항 → 구현 매핑 (v1.3.0)

사용자가 제시한 21개 요구사항 + 추가 2개를 어디서/어떻게 구현했는지, 그리고 **어떻게 검증했는지**를 기록합니다.

**상태 표식**
- ✅ **실행 검증** — 빌드 호스트에서 실제 코드를 돌려 테스트로 확인.
- 🌐 **실제 앱 검증** — 실제 Electron 앱(main + preload + 렌더러)을 가상 디스플레이에서 실제 서버에 연결해 확인(v1.3부터;
  이전에는 헤드리스 Chromium). Linux에서의 실행이므로 Windows 고유 동작(투명 합성, WorkerW 등)은 ⚙️로 따로 표시.
- ⚙️ **Windows 확인 필요** — 표준 구현이지만 실제 마이크·스피커·카메라·Windows 셸 동작은 설치한 PC에서 확인.
- 📝 **규모 명목** — 설계값은 문서화, 실제 구동은 하드웨어에 맞춰 축소(2000억 노드).

| # | 요구사항 | 구현 위치 | 방식 / 메모 | 상태 |
|---|----------|-----------|-------------|------|
| 1 | 시냅스 기능을 추가한 새 퍼셉트론 | `core/synaptic_perceptron.py` | 입력·가중치·편향·가중합·활성화 **+ 상태를 갖는 시냅스**(단기 가소성 촉진/억압, 방출확률, 신경전달물질 gain). 같은 입력에도 출력이 이력에 따라 달라짐. | ✅ |
| 1 | 소뇌/대뇌피질/시상 + 부수 구성의 3D 그물망 | `core/brain_mesh.py` | 3D 좌표 + 희소 연결(근접 바이어스 + 모듈간 투사 룰북). 핵심 3 + Brainstem/Hippocampus/BasalGanglia. 3D 공간에서 실시간 점구름으로 시각화. | ✅ 🌐 |
| 1 | 약 2000억 개 연결 | `core/scale.py` | `nominal_neurons=2e11`(설계값), `active_neurons`는 RAM 기반 실제 구동. 구조는 규모 불변. | 📝 |
| 1 | 출력을 제어하는 능력 | `core/control_layer.py` | 메시 리드아웃 → 제어신호 6종 → LLM 파라미터·스타일·후처리 모듈레이션. 피드백으로 시냅스 가소성 학습, 디스크에 영구 저장. | ✅ |
| 1+ | 궁극적 목적·내적 도구의 틀(동기 코어, v1.2) | `core/drive.py`, `docs/DRIVE.md` | 항상성(energy·rapport) + 내적 상태 라벨·협력 지향 드라이브 → 제어신호 조정. 임계점 정지는 의도적으로 제외, 어조 신호만 변경, 주의는 절대 낮추지 않음. | ✅ 🌐 |
| 1++ | 영원한 목적(충성) + 자기 교육으로 쌓는 자기 모델(자기 코어, v1.3) | `core/self_model.py`, `agent/skills/selfcore.py`, `docs/SELF_MODEL.md` | 사용자의 `GoalOrientedNode`를 도입: 고정 지침 "Maximize utility and service to the user." + 요청마다 연결·처리·통합·오류·복구로 학습하는 노드 그래프 + 1인칭 자기 서술. 가르치기·보기·잊기·끄기 가능. 데이터 전용, 지침 불변. 기능적 자기 모델(의식 주장 아님). | ✅ 🌐 |
| 2 | 부팅(로그온) 시 자동 실행 | `boot/register_autostart.ps1`, `boot/jarvis_service.py` | 실제 로그온 사용자에게 예약작업 등록, 실패 시 재시작, 시간 제한 없음. 시작하면 배너로 알림. | ✅(슈퍼바이저) ⚙️(예약 작업) |
| 2/4 | 더블클릭으로 켜면 **반드시 보이거나 이유를 말함** (v1.3) | `START_JARVIS.bat` → `boot/launcher.py`, `boot/overlay_runtime.py`, `docs/TROUBLESHOOTING.md` | 설치 점검·런타임 자가 설치 → 남은 프로세스 정리 → 시작 → 서비스·HUD 창·두뇌·연결 4단계 확인 → 실패 시 원인·할 일·`diagnostics.txt`. | ✅ 🌐 ⚙️(.bat) |
| 2 | 모든 기록 저장 | `recorder/recorder.py`, `agent/orchestrator.py` | `runs/` `commands/` `results/files/`(생성 파일: 파일 쓰기·스크린샷·3D 프로젝트·.glb) `text/conversation` `text/explanations/{errors,answers,process,eta}` `text/options` `text/suggestions` `checkpoints/` `audit/`. 중단된 작업은 "interrupted"로 기록. | ✅ |
| 2 | 입력 언어에 따른 입출력 자동 전환 | `agent/nlu.py`, `agent/replies_ko.py`, `overlay/renderer/i18n.js` | 한글 감지 → 답변·스킬 응답·UI·음성 모두 한국어. | ✅ 🌐 |
| 3 | 텍스트·음성 입출력 | `overlay/renderer/chat.js`, `voice/stt.py`, `voice/tts.py` | 채팅 + Whisper + Piper/Windows 음성. 스트리밍 답변은 말풍선이 실시간으로 채워짐. | 🌐 ⚙️ |
| 4 | 백그라운드 상시 활성 | `boot/jarvis_service.py`, `boot/common.py` | 단일 인스턴스(뮤텍스), 남은 프로세스 정리, 크래시·무응답 시 재시작(강제 종료 후 3초 내 복구 확인), 크래시 루프 감지, 출력 로그, 런타임 자가 복구, Ollama 자동 기동, 주간 유지보수, 트레이. | ✅ ⚙️(Windows API) |
| 5 | 3D 오브젝트 드래그(손동작/마우스) | `space3d/scene.js`, `space3d/gestures.js` | 좌클릭 드래그 + MediaPipe 핀치 드래그. | 🌐 ⚙️(카메라) |
| 5 | 확대/축소(스크롤 / 양손 거리) | `space3d/scene.js` | 오브젝트 위 스크롤=스케일, 빈 곳=카메라, 양손 벌리기=스케일. | 🌐 ⚙️(카메라) |
| 5 | 복제(클론) | `space3d/scene.js` `clone()` | 음성·텍스트·버튼 모두 동일 경로. | ✅ 🌐 |
| 5 | 손동작 감지 버튼/음성 off | `scene.js` `toggleGesture()`, `buttons.js`, `Ctrl+Alt+G`, "turn off hand gestures" | 꺼지면 웹캠 스트림도 정지. | 🌐 ⚙️ |
| 6 | 홀로그램(정밀·고해상도 Jarvis풍) | `overlay/renderer/hologram.css`, `index.html` | 시안 네온·글래스·코너 브래킷·스캔라인·리액터. | 🌐 |
| 6 | 채팅박스 홀로그램 | `chat.js`, `#chat-panel` | | 🌐 |
| 6 | AI·사용자 양쪽 마이크 음소거 | `buttons.js`, `voice/engine.py` `set_listening` | 사용자=시스템 마이크, JARVIS=마이크 스트림을 실제로 닫음. | ✅ ⚙️ |
| 6 | 3D 호출 버튼 / 전 기능 버튼 | `buttons.js` `FEATURES`(24종), `#feature-dock` | 3D 공간이 열리면 표시. | 🌐 |
| 6 | 추가 디자인 구성 | 상태 링·앰비언스·권한 홀로그램·설정 패널 | | 🌐 |
| 7 | 음성 웨이크 "wake up Jarvis" | `voice/wakeword.py`, `voice/engine.py` | Vosk 문법 모드로 정확한 구문(기본) / openWakeWord(onnx). | ✅(로직) ⚙️(마이크) |
| 8 | 웨이크 반응 "Jarvis at your service sir" | `agent/orchestrator.py` `handle_wake`, `voice/tts.py` | 말한 직후 바로 명령을 듣기 시작. | ✅ 🌐 ⚙️(스피커) |
| 9 | 비상 웨이크 버튼 영구 표시 | `#reactor`, `overlay/main.js` | 항상-위 오버레이(작업 영역 안, 작업 표시줄 바로 위) + `Ctrl+Alt+J` + 트레이. 누르면 응답 후 바로 듣기. 시작 시 배너와 함께 강조. | 🌐 ⚙️ |
| 10 | 웨이크 시 어디서든 버튼 오버레이 | `overlay/main.js` | `screen-saver` 레벨 항상-위, 모든 워크스페이스, 클릭 통과. | ⚙️ |
| 11 | 자연스러운 대화 | `agent/prompts/*`, `agent/llm_ollama.py`, `agent/orchestrator.py` | 영국 집사 페르소나 + 로컬 LLM + 스트리밍(첫 문장부터 발화) + 제어신호. | ✅(스트리밍) ⚙️(LLM 품질) |
| 12 | 높은 입력 인지 정확도 | `voice/stt.py`, `voice/engine.py` | GPU면 large-v3-turbo, CPU면 small, "Jarvis" 핫워드, VAD 종료 감지. | ⚙️ |
| 13 | 음성 중단 + 새 입력 | `voice/engine.py`, `voice/wakeword.py` `heard_keyword`, `agent/server.py` `interrupt` | 키워드 바지인(에코 무시) / VAD / 끄기. 새 명령은 진행 중 답변 취소. | ✅(상태기계) ⚙️(오디오) |
| 14 | 기능 명령 유연성 | `agent/nlu.py`, LLM `<action>` 태그 | 한/영 자유 표현 + LLM 도구 호출 폴백. | ✅ |
| 15 | 모든 기능 음성·텍스트 양쪽 | `agent/server.py` 단일 커맨드 버스 | 음성·채팅·버튼이 같은 명령 경로(버튼도 같은 명령을 보냄). | ✅ 🌐 |
| 16 | 어떤 화면에서든 음성·텍스트 | `overlay/main.js`, `voice/engine.py` | 전역 오버레이·단축키·상시 웨이크. | ⚙️ |
| 17 | 3D 공간 음성/텍스트로 열기 | `agent/skills/__init__.py` `open_space3d`, `app.js` | | ✅ 🌐 |
| 18 | 3D 공간이 바탕화면 배경 대체 | `overlay/main.js` 배경화면 창, `overlay/native/workerw.ps1`, `renderer/wallpaper.html` | 별도 창을 아이콘 뒤 WorkerW 계층에 부착(24H2 포함), 실패 시 바탕화면 바로 위·모든 앱 아래로. HUD는 위에 유지. | 🌐(상태 전달) ⚙️(Windows 셸) |
| 19 | 웹 음악 음성/텍스트 재생 | `agent/skills/music.py` | yt-dlp(venv 파이썬으로 실행) → ffplay/mpv, 실패 시 웹 플레이어. 주 1회 yt-dlp 갱신. | ⚙️ |
| 20 | PC 전면 접근 에이전트 | `agent/skills/*` + `agent/permissions.py` | 파일·앱(시작 메뉴/UWP)·창·키보드·스크린샷·설정·브라우저·볼륨·마이크·장치. 권한 게이트+감사. | ✅(게이트) ⚙️(Windows API) |
| 21 | 영국 신사 음성 | `voice/tts.py`, `config/jarvis.config.json` | Piper `en_GB-alan-medium` → Windows "George"(WinRT) → SAPI5. | ⚙️ |
| + | 기본 언어 영어 | `config/jarvis.config.json` | | ✅ |
| + | 더블클릭 영구작동 파일 | `INSTALL_JARVIS.bat` + `scripts/setup_windows.ps1` | 설치·화면 표시부·모델·음성·자동시작·자체 점검·실행·**표시 확인**. 재실행 시 이어하기·복구. | ✅(의존성 해결·런타임 다운로드·스크립트 정적 점검) ⚙️(실행) |

## 빌드 호스트에서 실행한 검증
- `tests/test_core.py` 7/7 · `tests/test_phase2.py` 7/7 · `tests/test_drive.py` 7/7 · `tests/test_voice_engine.py` 5/5 ·
  `tests/test_self_model.py` 9/9 · `tests/test_boot.py` 15/15 · `scripts/mock_run.py` · `scripts/healthcheck.py`.
- `tests/test_server_e2e.py` — 실제 uvicorn 서버: 토큰 없는/잘못된/웹 Origin 접속 거부, 명령→UI 이벤트, 스트리밍(28 델타·3문장),
  한국어 응답, 권한 거부 왕복, 3D 저장·불러오기·.glb 내보내기(경로 조작 무력화), 설정 적용·잠긴 권한 거부, 신경망 점, 기록, 감사 로그.
- **실제 Electron 앱** + 실제 서버 E2E 19개 시나리오(시작 배너·창 상태·세션 전달·WAKE·3D·내보내기·신경망·스트리밍·자기 코어·
  설정·권한·한국어·실제 배경화면 창·Quit 종료 코드), 렌더러 JavaScript 오류 0.
- **START 전체 체인을 실제 프로세스로**: 첫 시작 · 이미 실행 중(재시작 없이 배너) · 백엔드/오버레이 강제 종료 후 자동 복구 ·
  슈퍼바이저 강제 종료 후 고아 정리 · 두 번째 슈퍼바이저 거부 · 동시 실행 · v1.2(다른 폴더)에서 넘겨받기 · 런타임 자동 설치 ·
  오프라인 실패 안내 · 포트 충돌(점유 프로그램 이름 표시) · 설정 파일 손상.
- 화면 표시부 런타임: Linux x64와 **Windows x64** 압축 파일을 같은 코드로 받아 SHA-256 검증·압축 해제, npm 설치본과 차이 0.
- Windows 의존성: `uv pip compile --python-platform x86_64-pc-windows-msvc --python-version 3.11 --only-binary :all:` → 84개 해결
  (소스 전용 패키지는 모두 순수 파이썬으로 확인).
- `.bat`/`.ps1` 전부 ASCII, `.ps1` 괄호·here-string 구조 정적 점검, `git archive`가 CRLF를 주는 것을 실험으로 확인.
