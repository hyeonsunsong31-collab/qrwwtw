# JARVIS — 로컬 에이전틱 AI (Windows)

영화 *아이언맨*의 자비스처럼 동작하는 **PC용 풀스택 로컬 에이전틱 AI**입니다. 홀로그램 오버레이 UI,
음성·텍스트 입출력, 영국 신사 음성, 카메라 손동작 3D 모델링 공간, 전체 기록, PC 제어, 웹 음악 재생을
갖추고, **더블클릭 한 번으로 설치되어 부팅 때마다 자동으로 켜집니다.** 두뇌는 **오프라인 로컬 LLM(Ollama)**,
기본 언어는 **영어**(입력 언어에 따라 자동 전환)입니다.

---

## ⚠️ 먼저 읽어주세요 — 솔직한 안내

- **"퍼셉트론 약 2000억 개"는 어떤 PC에서도 실제로 메모리에 올리거나 학습할 수 없습니다.** (데이터센터용 LLM도
  수백억 *파라미터* 규모이고 슈퍼컴퓨터로 학습합니다.) 그래서 이 프로젝트는 요구하신 **새 시냅스-퍼셉트론 +
  소뇌/대뇌피질/시상 3D 그물망 구조를 "진짜 실행되는 규모 조절형 코드"로 충실히 구현**하고, 명목 설계값
  `nominal_neurons = 2e11`은 문서·설정에만 두며, 실제로는 PC 사양에 맞는 크기(`active_neurons`, 기본 수만~수백만,
  희소 연결)로 돌립니다. 이 신경망은 요구서대로 **LLM의 출력을 제어(모듈레이션)하는 계층**으로 작동하고,
  실제 자연어 대화 능력은 그 계층이 감싸는 Ollama LLM이 담당합니다. → `core/`, `docs/ARCHITECTURE.md`
- **대화 품질은 설치하는 PC의 성능에 달려 있습니다.** 로컬 LLM이므로 GPU/충분한 RAM이 좋을수록 똑똑하고 빠릅니다.
- **PC 전면 제어는 본인 PC에서 본인 동의 하에 작동하는 "가시적" 비서**로 구현했습니다. 권한 계층(허용/확인/거부)과
  감사 로그가 있고, 파괴적·외부전송 동작은 실행 전에 홀로그램 창으로 확인을 받습니다. → `docs/SECURITY.md`
- 이 코드는 Linux 클라우드에서 작성·검증되었고 **실행 대상은 Windows 10/11**입니다. 일부 Windows 전용 동작
  (오버레이, 배경화면 대체, 실제 마이크/카메라)은 설치하신 PC에서 확인하시면 됩니다.

---

## 🚀 설치 (Windows) — 더블클릭

1. 이 폴더 전체를 PC에 둡니다(예: `C:\JARVIS`).
2. **`INSTALL_JARVIS.bat` 더블클릭.** (관리자 권한을 요청합니다.)
   - Python 3.11 · Node.js · Ollama + 의존성 설치, 하드웨어에 맞는 LLM 모델 다운로드, 영국 음성·손 모델 내려받기,
     **로그온 자동시작 등록**, 그리고 **즉시 실행**까지 한 번에 진행합니다. (첫 실행은 10~25분 걸릴 수 있음 — 모델 다운로드)
3. 이후 PC를 켜면 **자동으로 백그라운드에서 상주**합니다. 화면 우하단의 **WAKE 버튼은 항상 표시**됩니다.

- 지금 바로 켜기: `START_JARVIS.bat`
- 자동시작 제거: `UNINSTALL_JARVIS.bat`

### 권장 사양
- Windows 10/11, 마이크·웹캠(손동작용), **RAM 16GB+ (32GB 권장)**, 가능하면 NVIDIA GPU(로컬 LLM/STT 가속).
- RAM이 적으면 작은 모델이 자동 선택됩니다(대화 간결).

---

## 🗣 사용법

- **깨우기:** "**wake up Jarvis**" 라고 말하거나, 항상 떠 있는 **WAKE 버튼**/단축키 `Ctrl+Alt+J`.
  → 응답: "**Jarvis at your service sir**" (영국 음성)
- **명령(음성·텍스트 동일):** 모든 기능은 말로도, 채팅창으로도 됩니다.
  - "open the 3D project space" / "3D 공간 열어줘"
  - "play some lofi jazz" / "로파이 재즈 틀어줘"
  - "open notepad", "what's my system status?", "search for invoice files", "mute the microphone"
  - 3D 공간: **집게 손가락(핀치)** 또는 **좌클릭 드래그**로 이동, 포인터 위 **스크롤**·**양손 벌리기**로 확대/축소,
    "clone the object"로 복제. 손동작 감지는 버튼/`Ctrl+Alt+G`/음성으로 끌 수 있습니다.
- **말 끊기:** JARVIS가 말하는 중에 말을 시작하거나 입력하면 즉시 멈추고 새 명령을 받습니다.
- **언어 자동 전환:** 한국어로 말하면 한국어로, 영어로 말하면 영어로 답합니다(기본 영어).

---

## 🧩 폴더 구조

```
core/     새 시냅스-퍼셉트론 + 뇌 기반 3D 신경망 + 출력 제어 계층 (+ demo.py)
agent/    명령 버스 서버 · 오케스트레이터 · NLU · 권한 · 스킬(파일/앱/시스템/브라우저/미디어/음악/장치)
voice/    웨이크워드 · STT · 영국 TTS · 바지인(말끊기) · 풀듀플렉스 엔진
recorder/ 모든 기록(작업/명령/결과물/대화/설명/옵션/추천/체크포인트/감사)
overlay/  Electron 홀로그램 오버레이 + Three.js 3D 공간 + MediaPipe 손동작
boot/     자동시작 등록(PowerShell) · 백그라운드 슈퍼바이저
scripts/  setup_windows.ps1(설치) · healthcheck.py · mock_run.py
docs/     ARCHITECTURE.md · REQUIREMENTS_MAP.md · SECURITY.md
INSTALL_JARVIS.bat · START_JARVIS.bat · UNINSTALL_JARVIS.bat
```

전체 요구사항 21개 + 추가 2개의 구현 위치·상태는 **[docs/REQUIREMENTS_MAP.md](docs/REQUIREMENTS_MAP.md)** 참고.

---

## 🔧 개발·검증 (Linux/빌드 호스트에서 가능)

```bash
python scripts/healthcheck.py     # 설정·임포트·코어 경로·선택 라이브러리 점검
python -m core.demo               # 신경 코어 실제 구동(시냅스 동역학/메시/제어/학습)
python scripts/mock_run.py        # Ollama·오디오 없이 명령 버스 전체 경로(기록 생성까지)
```
Windows 전용 동작의 확인 절차는 위 "사용법" 및 `docs/REQUIREMENTS_MAP.md`의 상태 표식을 참고하세요.

## 🔒 보안·동의
본인 PC·본인 동의·가시적 동작이 원칙입니다. `config/permissions.json`으로 범위를 조정하고, 모든 동작은
`data/audit/audit.log`에 남습니다. 자세한 내용은 **[docs/SECURITY.md](docs/SECURITY.md)**.
