# 요구사항 → 구현 매핑

사용자가 제시한 21개 요구사항 + 추가 2개를 어디서/어떻게 구현했는지와 상태를 모두 기록합니다.

**상태 표식**
- ✅ **동작** — Linux 빌드 호스트에서 실제 실행·검증함(코어/로직).
- ⚙️ **표준 구현 · Windows 미검증** — 올바른 표준 구현이나 실제 확인은 사용자 Windows PC에서(오디오/오버레이/OS 제어).
- 📝 **규모 명목** — 설계값은 문서화, 실제 구동은 하드웨어에 맞춰 축소(2000억 노드 관련).

| # | 요구사항 | 구현 위치 | 방식 / 메모 | 상태 |
|---|----------|-----------|-------------|------|
| 1 | 시냅스 기능을 추가한 새 퍼셉트론 | `core/synaptic_perceptron.py` | 입력·가중치·편향·가중합·활성화 **+ 상태를 갖는 시냅스**(단기 가소성 촉진/억압, 방출확률, 신경전달물질 gain). 출력이 최근 이력에 의존 → 표준 퍼셉트론과 본질적으로 다름. 목적은 분류가 아니라 상호작용으로 출력 제어. | ✅ |
| 1 | 소뇌/대뇌피질/시상 + 부수 구성의 3D 그물망 | `core/brain_mesh.py` | 3D 좌표 배치 + 희소 연결(근접 바이어스 + 모듈간 투사 룰북). Cortex/Cerebellum/Thalamus 핵심 + Brainstem/Hippocampus/BasalGanglia 보조. 순수 NumPy(COO+bincount)로 선형 메모리. | ✅ |
| 1 | 약 2000억 개 연결 | `core/scale.py` | `nominal_neurons=2e11`은 설계/문서값, `active_neurons`는 RAM 기반 실제 구동(기본 2만~2백만). 구조는 규모 불변. | 📝 |
| 1 | "출력을 제어하는 능력" | `core/control_layer.py` | 입력→특징→시상/뇌간 주입→메시 수렴→cortex/cerebellum/basal-ganglia 리드아웃으로 **제어신호**(격식·장황도·확신·페르소나·온기·주의) 산출→LLM 파라미터+스타일 지시+후처리 **모듈레이션**. 피드백으로 시냅스 가소성 학습(영구 저장). | ✅ |
| 2 | 부팅 시 자동 실행 | `boot/register_autostart.ps1`, `boot/jarvis_service.py` | 로그온 트리거 예약작업 + 실패 시 재시작. | ⚙️ |
| 2 | 모든 기록 저장 | `recorder/recorder.py` | `data/` 트리: `runs/`(작업), `commands/`(명령), `results/files/`(생성 파일-모든 형태), `text/conversation`(대화), `text/explanations/{errors,answers,process,eta}`(오류/질문/성패/소요시간 설명), `text/options`(옵션), `text/suggestions`(추천), `checkpoints/`(최신 워크플로우), `audit/`. | ✅ |
| 2 | 입력 언어에 따른 입출력 자동 전환 | `agent/nlu.py` `detect_language`, `agent/orchestrator.py`, `overlay/renderer/i18n.js` | 한글 감지 + langdetect. UI·답변 언어 전환(기본 영어). | ✅ |
| 3 | 모든 텍스트·음성 입출력 | `overlay/renderer/chat.js`, `voice/stt.py`, `voice/tts.py` | 채팅창 + STT/TTS. | ✅/⚙️ |
| 4 | 백그라운드 상시 활성 | `boot/jarvis_service.py` | 슈퍼바이저가 백엔드+오버레이 상주·크래시 재시작, 트레이. | ⚙️ |
| 5 | 3D 오브젝트 드래그(손동작/마우스) | `overlay/renderer/space3d/scene.js`, `gestures.js` | 좌클릭 드래그 + MediaPipe 핀치 드래그(드래그 평면 교차). | ⚙️ |
| 5 | 확대/축소(스크롤 / 양손 거리) | `space3d/scene.js` `_onWheel`, `_gestureScale` | 포인터 위 오브젝트 스케일, 빈 공간은 카메라 돌리, 양손 벌리기=스케일. | ⚙️ |
| 5 | 복제(클론) | `space3d/scene.js` `clone()` | 선택 오브젝트 복제·오프셋 배치. | ⚙️ |
| 5 | 손동작 감지 버튼/음성 off | `space3d/scene.js` `toggleGesture()`, `buttons.js`, `Ctrl+Alt+G` | 카메라 on/off. | ⚙️ |
| 6 | 홀로그램(정밀·고해상도 Jarvis풍) | `overlay/renderer/hologram.css`, `index.html` | 시안 네온·글래스모피즘·코너 브래킷·스캔라인·리액터 링. | ⚙️ |
| 6 | 채팅박스 홀로그램 | `overlay/renderer/chat.js`, `#chat-panel` | | ⚙️ |
| 6 | AI·사용자 양쪽 마이크 음소거 | `overlay/renderer/buttons.js` `#mute-user`/`#mute-ai` | 사용자=시스템 마이크(pycaw), AI=웨이크 청취 on/off. | ⚙️ |
| 6 | 3D 호출 버튼 | `buttons.js` 피처독 `space3d` | | ⚙️ |
| 6 | 명시된 모든 기능 버튼(3D 열릴 때 표시) | `buttons.js` `FEATURES`(16종) `#feature-dock` | 3D 공간 열릴 때 전체 버튼 노출. | ⚙️ |
| 6 | 추가 디자인 구성 | `hologram.css` 상태 링·앰비언스·토스트 등 | | ⚙️ |
| 7 | 음성 웨이크 "wake up Jarvis" | `voice/wakeword.py`, `voice/engine.py` | openWakeWord(오프라인, 커스텀 모델 지원). | ⚙️ |
| 8 | 웨이크 반응 "Jarvis at your service sir" | `voice/response.py`, `agent/orchestrator.py` `handle_wake`, `voice/tts.py` | 영국 음성으로 발화. | ✅/⚙️ |
| 9 | 비상 웨이크 버튼 영구 표시 | `overlay/renderer/index.html` `#reactor`, `main.js` | 항상-위 오버레이에 상시 표시. `Ctrl+Alt+J`도 제공. | ⚙️ |
| 10 | 웨이크 시 어디서든 버튼 오버레이 | `overlay/main.js` | 투명·`screen-saver` 레벨 항상-위·모든 워크스페이스·전경앱 위. | ⚙️ |
| 11 | 자연스러운 인간같은 대화 | `agent/prompts/system_en.txt`, `agent/llm_ollama.py`, `control_layer.py` | 영국 집사 페르소나 + 로컬 LLM + 제어신호 톤 조절. | ✅/⚙️ |
| 12 | 높은 입력 인지 정확도 | `voice/stt.py`(faster-whisper+VAD), `voice/engine.py` | 모델 자동 크기, 무음 종료, 텍스트 상시 병행. | ⚙️ |
| 13 | 음성 중단 + 새 입력 | `voice/interrupt.py`, `voice/engine.py`, `agent/server.py` `interrupt`, `chat.js` | VAD 바지인→TTS 정지→새 명령. 새 명령은 진행 중 답변을 취소. | ⚙️ |
| 14 | 기능 명령 유연성 | `agent/nlu.py`, `agent/orchestrator.py` `parse_action_tags` | 자유어·동의어·한/영 패턴 + LLM 액션 태그 폴백. | ✅/⚙️ |
| 15 | 모든 기능 음성·텍스트 양쪽 | `agent/server.py` 단일 WS 커맨드 버스 | STT·채팅·버튼이 동일 경로. | ✅ |
| 16 | 어떤 탭에서든 음성·텍스트 | `overlay/main.js`(전역 오버레이·단축키), `voice/engine.py`(상시 웨이크) | | ⚙️ |
| 17 | 3D 공간 음성/텍스트로 열기 | `agent/skills/__init__.py` `open_space3d`, `overlay/renderer/app.js` | "open 3D space"/버튼 모두. | ✅/⚙️ |
| 18 | 3D 공간이 바탕화면 배경 대체 | `overlay/renderer/space3d/wallpaper.js`, `overlay/main.js` `space3d:wallpaper` | 전체화면 바닥 레이어로 데스크톱 덮음. 아이콘 뒤 삽입은 Windows WorkerW(문서화). | ⚙️ |
| 19 | 웹 음악 음성/텍스트 재생 | `agent/skills/music.py` | yt-dlp로 스트림 해석→mpv/ffplay, 실패 시 웹플레이어. | ⚙️ |
| 20 | PC 전면 접근 에이전트 | `agent/skills/*`(files·apps·system·browser·media·devices) + `agent/permissions.py` | 파일/앱/설정/브라우저/볼륨/마이크/장치. 권한 게이트+감사. | ⚙️ |
| 21 | 영국 신사 음성 | `voice/tts.py`, `config/jarvis.config.json` | Piper `en_GB-alan-medium` 기본, SAPI5 'George' 폴백. | ⚙️ |
| + | 기본 언어 영어 | `config/jarvis.config.json` `language.default=en`, `agent/prompts/system_en.txt` | | ✅ |
| + | 더블클릭 영구작동 파일 | `INSTALL_JARVIS.bat` (+ `scripts/setup_windows.ps1`) | 설치+모델+자동시작+즉시 실행. AI 폴더에 포함. | ✅/⚙️ |

## Linux에서 실제 검증한 것
- `python -m core.demo` — 시냅스 동역학, 2만 뉴런/41만 시냅스 메시 수렴(안정·bounded), 제어신호 분화, 피드백 학습.
- `python scripts/mock_run.py` — 웨이크 응답, 앱/음악/시스템/3D/파일/마이크/한국어 명령 전 경로, 권한 감사, 전 기록 카테고리 생성.
- `python scripts/healthcheck.py` — 설정·16개 모듈 임포트·코어 경로·선택 라이브러리 점검(exit 0).
- `node --check` — 오버레이 JS 11개 파일 구문 전부 통과.
