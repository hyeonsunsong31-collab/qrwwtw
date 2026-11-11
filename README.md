# JARVIS — 로컬 에이전틱 AI (Windows)

영화 *아이언맨*의 자비스처럼 동작하는 **PC용 풀스택 로컬 에이전틱 AI**입니다. 홀로그램 오버레이 UI,
음성·텍스트 입출력, 영국 신사 음성, 카메라 손동작 3D 모델링 공간, 전체 기록, PC 제어, 웹 음악 재생을
갖추고, **더블클릭 한 번으로 설치되어 로그온할 때마다 자동으로 켜집니다.** 두뇌는 **오프라인 로컬 LLM(Ollama)**,
기본 언어는 **영어**(한국어로 말하면 자동으로 한국어 전환)입니다.

---

## ⚠️ 먼저 읽어주세요 — 솔직한 안내

- **"퍼셉트론 약 2000억 개"는 어떤 PC에서도 실제로 메모리에 올리거나 학습할 수 없습니다.** (데이터센터용 LLM도
  수백억 *파라미터* 규모이고 슈퍼컴퓨터로 학습합니다.) 그래서 요구하신 **새 시냅스-퍼셉트론 + 소뇌/대뇌피질/시상
  3D 그물망 구조를 "진짜 실행되는 규모 조절형 코드"로 충실히 구현**하고, 명목 설계값 `nominal_neurons = 2e11`은
  문서·설정에 두며 실제로는 PC 사양에 맞는 크기(기본 수만~수백만, 희소 연결)로 돌립니다. 이 신경망은 요구서대로
  **LLM 출력을 제어(모듈레이션)하는 계층**이고, 실제 자연어 대화 능력은 Ollama LLM이 담당합니다. 3D 공간에서
  "show the neural network"라고 하면 살아 있는 신경망을 홀로그램으로 볼 수 있습니다.
- **대화 품질은 PC 성능에 달려 있습니다.** 설치 프로그램이 GPU 메모리/RAM을 보고 모델 크기를 고릅니다.
- **PC 전면 제어는 본인 PC에서 본인 동의 하에 작동하는 "가시적" 비서**로 구현했습니다. 권한 계층(허용/확인/거부),
  감사 로그, 그리고 JARVIS 자신의 프로세스만 접속할 수 있는 토큰 인증이 있습니다. → `docs/SECURITY.md`
- 이 코드는 Linux에서 작성·검증되었고 **실행 대상은 Windows 10/11**입니다. 실제 마이크·스피커·카메라·Electron 창
  배치는 설치하신 PC에서 확인하시면 됩니다(검증 범위는 아래 "검증" 참고).

---

## 🚀 설치 (Windows) — 더블클릭

1. 이 폴더 전체를 **사용자 폴더 아래**에 둡니다(예: `C:\JARVIS`). `Program Files`는 피해 주세요.
   GitHub에서 ZIP으로 받았다면 압축을 풀기 전에 ZIP 파일 → 속성 → **차단 해제**를 해 두면 편합니다.
2. **`INSTALL_JARVIS.bat` 더블클릭** (관리자 권한을 요청합니다).
   - Python 3.11 · Node.js · Ollama · FFmpeg 설치(winget, 없으면 공식 설치 파일로 자동 대체) → 패키지 설치 →
     PC에 맞는 언어 모델 다운로드 → 영국 음성·"wake up Jarvis" 인식 모델·Whisper·손 추적 모델 → Windows 영어(영국)/
     한국어 음성 팩 → **로그온 자동시작 등록** → 자체 점검 → **즉시 실행**.
   - 첫 설치는 **15~40분** 걸릴 수 있습니다(모델 다운로드). 중간에 실패해도 **다시 실행하면 끝난 단계는 건너뛰고**
     이어서 진행합니다. 전체 기록: `data\install.log`
3. 이후 PC를 켜고 로그온하면 **자동으로 백그라운드에서 상주**합니다. 우하단 **WAKE 버튼은 항상 표시**됩니다.

- 지금 바로 켜기: `START_JARVIS.bat` · 자동시작 제거: `UNINSTALL_JARVIS.bat` (폴더·기록은 유지)

### 권장 사양
Windows 10(1809+)/11, 마이크·스피커(헤드셋이면 더 좋음)·웹캠(손동작), **RAM 16GB+ (32GB 권장)**,
가능하면 NVIDIA GPU(로컬 LLM·Whisper 가속), 디스크 여유 15GB+.

---

## 🗣 사용법

- **깨우기:** "**wake up Jarvis**" (정확한 구문 인식) / 항상 떠 있는 **WAKE 버튼** / `Ctrl+Alt+J`
  → "**Jarvis at your service sir**" 응답 후 **바로 명령을 듣습니다.**
- **말로 명령 (마이크 버튼):** 채팅창의 🎙 버튼 = 누르고 말하기(Whisper 음성 인식).
- **명령은 음성·텍스트·버튼 모두 같은 길로 처리**됩니다. 예:
  - "open the 3D project space" / "3D 공간 열어줘"
  - "add a cube" · "clone the object" · "delete the object" · "save the project" · "export the model"
  - "show the neural network" / "신경망 보여줘" · "use it as my wallpaper" / "배경화면으로 사용해"
  - "play some lofi jazz" / "로파이 재즈 틀어줘" · "stop the music"
  - "open spotify" / "메모장 열어줘" (시작 메뉴·스토어 앱을 이름으로 찾아 실행) · "switch to chrome" · "minimize everything"
  - "take a screenshot" · "type hello world" · "press ctrl+s" · "what's my system status?" · "search for invoice files"
  - "jarvis settings" / "자비스 설정" → 홀로그램 설정 패널
- **3D 공간:** 집게 손가락(핀치) 또는 좌클릭 드래그로 이동, 오브젝트 위 **스크롤**·**양손 벌리기**로 확대/축소,
  더블클릭으로 추가. 손동작 감지는 버튼·`Ctrl+Alt+G`·음성("turn off hand gestures")으로 끕니다.
  **배경화면 모드**에서는 3D 장면이 바탕화면 아이콘 **뒤**에서 천천히 회전하고, HUD는 그대로 위에 남습니다.
- **말 끊기:** JARVIS가 말하는 중에 "**stop**" / "**wait**" / "**hold on**" / "**Jarvis**"라고 하면 즉시 멈추고 새 명령을
  듣습니다(스피커 소리에 스스로 끊기지 않는 방식). 헤드셋이면 설정에서 "Interrupt mode = vad"로 아무 말에나 끊을 수
  있습니다. 새 명령을 입력해도 진행 중인 답변은 즉시 멈춥니다.
- **음소거:** "YOU"(내 마이크 음소거) / "JARVIS"(JARVIS의 마이크를 **완전히 닫음**).
- **언어 자동 전환:** 한국어로 말하면 화면·답변이 한국어로, 영어로 말하면 영어로(기본 영어, 영국 음성).
  한국어 음성은 Windows 한국어 음성으로 읽습니다(공식 Piper 한국어 음성이 아직 없음).

---

## 🧩 폴더 구조

```
core/      새 시냅스-퍼셉트론 + 뇌 기반 3D 신경망 + 출력 제어 계층 + 동기 코어(drive.py) (+ demo.py)
agent/     토큰 인증 명령 버스 서버 · 오케스트레이터(스트리밍) · NLU · 권한 · 설정 · 한국어 응답 · 스킬
           skills/ 파일 · 앱(시작 메뉴/UWP 검색) · 창/키보드/스크린샷 · 시스템 · 브라우저 · 미디어 · 음악 · 장치
voice/     웨이크(Vosk 정확 구문 / openWakeWord) · Whisper STT · 영국 TTS(Piper/Windows 음성) · 바지인 · 엔진
recorder/  모든 기록(작업/명령/생성 파일/대화/설명/옵션/추천/체크포인트/감사)
overlay/   Electron 홀로그램 HUD · Three.js 3D 공간 · MediaPipe 손동작 · 배경화면 창 · 설정 패널 · native/workerw.ps1
boot/      자동시작 등록 · 자가복구 슈퍼바이저
scripts/   setup_windows.ps1(설치) · healthcheck.py · mock_run.py · set_config.py
tests/     단위 · 음성 엔진 · 실서버 E2E 테스트
docs/      ARCHITECTURE · REQUIREMENTS_MAP · SECURITY · WAKE_WORD
INSTALL_JARVIS.bat · START_JARVIS.bat · UNINSTALL_JARVIS.bat
```

요구사항 21개 + 추가 2개의 구현 위치·상태: **[docs/REQUIREMENTS_MAP.md](docs/REQUIREMENTS_MAP.md)** · 변경 내역: **[CHANGELOG.md](CHANGELOG.md)**

---

## 🔧 검증 (빌드 호스트에서 실행한 것)

```bash
python scripts/healthcheck.py          # 설정·모듈·코어 경로·선택 라이브러리 점검
python -m core.demo                    # 신경 코어 실제 구동
python tests/test_core.py              # 코어·NLU·기록 단위 테스트
python tests/test_phase2.py            # 의도·스트리밍 분할·설정 검증·한국어 응답·보안 도우미
python tests/test_voice_engine.py      # 음성 상태기계(가짜 오디오 장치): 웨이크→응답→듣기, 바지인, 음소거
python tests/test_server_e2e.py        # 실제 서버 + 토큰 WebSocket (fastapi/uvicorn/httpx/websockets 필요)
python scripts/mock_run.py             # Ollama·오디오 없이 명령 버스 전체 경로
```
추가로 실제 오버레이 페이지를 헤드리스 Chromium에서 실제 서버에 연결해 12개 시나리오(WAKE, 3D 열기·추가·복제·저장·
.glb 내보내기, 신경망 보기, 스트리밍 대화, 설정 변경, 권한 대화상자, 한국어 전환, 배경화면 모드)를 통과시켰고,
Windows용 Python 의존성 84개가 **컴파일러 없이 설치되는 휠**로만 해결되는 것을 `uv pip compile --python-platform windows`로
확인했습니다. 실제 오디오 장치·Electron 창·WorkerW 배치는 Windows에서 확인이 필요합니다.

## 🔒 보안·동의
명령 버스는 127.0.0.1에만 열리고, 매 실행마다 새로 만드는 토큰(`data/session.token`)이 있어야 접속됩니다.
웹페이지는 접속할 수 없습니다. 범위는 `config/permissions.json` 또는 설정 패널에서 조정하고, 모든 동작은
`data/audit/audit.log`에 남습니다. 자세한 내용: **[docs/SECURITY.md](docs/SECURITY.md)**
