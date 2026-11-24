# 문제 해결 — "더블클릭했는데 JARVIS가 안 보여요"

v1.3부터 `START_JARVIS.bat`은 **시작을 요청만 하지 않고, 실제로 화면에 떴는지 확인**합니다. 창에 단계가 표시됩니다.

```
  [1/3] Checking the installation
   [ok]  program files and Python packages are in place
   [ok]  the on-screen display (Electron, three.js, MediaPipe) is installed
  [2/3] Looking for a running JARVIS
   [ok]  nothing was running
  [3/3] Starting JARVIS
   [ok]  background service is running (process 1234)
   [ok]  the HUD window is on screen (1920x1040 at 0,0)
   [ok]  the brain is online (version 1.3.0, 598,360 active neurons)
   [ok]  the HUD is connected to the brain

  JARVIS is online.
```

성공하면 화면 가운데에 **"J.A.R.V.I.S ONLINE"** 배너가 몇 초간 뜨고, 왼쪽 위에 HUD, 오른쪽 아래(작업 표시줄 바로 위)에
둥근 **WAKE 버튼**이 남습니다. 이미 실행 중일 때 START를 다시 누르면 재시작하지 않고 배너만 다시 띄워 위치를 알려 줍니다.

## 실패했을 때
창에 **What went wrong / What to do**가 나오고, 전체 보고서가 `data\logs\diagnostics.txt`에 저장되어 메모장으로 열립니다.
도움이 필요하면 그 파일 내용을 그대로 보내 주세요(버전·경로·프로세스·각 로그의 마지막 줄이 들어 있습니다).

| 창에 나온 말 | 뜻 | 할 일 |
|---|---|---|
| `JARVIS is not installed in this folder yet` | 이 폴더에 설치된 적이 없음 | 아무 키나 누르면 설치 프로그램이 실행됩니다 |
| `This is not the complete JARVIS folder` | ZIP 안에서 바로 실행했거나 파일이 빠짐 | ZIP을 **모두 압축 풀기** 한 뒤 그 폴더에서 `INSTALL_JARVIS.bat` |
| `JARVIS's Python packages are not installed` | 설치가 중간에 실패 | 아무 키나 눌러 설치 프로그램 실행(끝난 단계는 건너뜀) |
| `the on-screen display is not installed yet - downloading it now` | Electron 런타임이 없어서 지금 받는 중(약 120MB, 한 번만) | 기다리면 됩니다 |
| `The on-screen display (Electron) ... could not be downloaded` | github.com / registry.npmjs.org에 접속 불가 | 인터넷·방화벽·프록시 확인 후 START 다시 |
| `An older JARVIS is still running and could not be stopped` | 관리자 권한으로 시작된 이전 JARVIS가 남아 있음 | Windows에서 로그아웃 후 다시 로그인(또는 재부팅) → START |
| `Port 8765 ... is taken by another program` / `cannot open port 8765` | 다른 프로그램이 포트를 사용 중(이름과 PID 표시) | 그 프로그램을 닫거나 재부팅 → START |
| `the Python package 'X' is missing` | 패키지 누락 | `INSTALL_JARVIS.bat` |
| `a system library is missing` (DLL load failed) | Visual C++ 런타임 없음 | "Microsoft Visual C++ Redistributable (x64)" 설치 후 `INSTALL_JARVIS.bat` |
| `not allowed to write into this folder` | 보호된 폴더(Program Files, 제어된 폴더 액세스 등) | `C:\JARVIS` 같은 일반 폴더로 옮겨 다시 설치 |
| `A settings file is damaged` | `config\*.json`이 깨짐 | 원본 ZIP의 파일로 되돌리기 |
| `The on-screen display did not start` / `keeps closing` / `never finished loading` | 오버레이 프로세스 문제(대개 그래픽 드라이버) | **`START_JARVIS_SAFE_GRAPHICS.bat`** (소프트웨어 렌더링) |
| `The HUD is on screen and the brain is running, but they did not connect` | 보안 프로그램이 127.0.0.1 연결을 막는 경우 | START 한 번 더, 반복되면 보고서 전송 |

## 화면에는 떴다고 하는데 안 보일 때
- `Ctrl+Alt+J`를 누르거나, 시계 옆 트레이의 JARVIS 아이콘 → **Show the HUD**.
- HUD가 검게 나오거나 깜박이거나 전혀 안 보이면 `START_JARVIS_SAFE_GRAPHICS.bat`을 실행하세요. 이 선택은 기억됩니다.
  되돌리려면 트레이 아이콘 → **Software rendering** 체크 해제.
- 전체 화면 "독점" 모드 게임 위에는 어떤 오버레이도 표시되지 않습니다(테두리 없는 창 모드에서는 보입니다).

## 로그 위치 (`data\logs\`)
| 파일 | 내용 |
|---|---|
| `diagnostics.txt` | 마지막 실패 시의 전체 보고서 |
| `supervisor.log` | 백그라운드 서비스: 시작·재시작·문제(PROBLEM 줄) |
| `backend.log` | 두뇌(파이썬 백엔드)의 출력과 오류 |
| `overlay-main.log` | 화면 표시부(Electron): 창 표시, 로드 실패, 렌더러 오류 |
| `overlay.log` | Electron 자체의 출력 |
| `..\install.log` | 설치 기록 |
| `..\supervisor.json`, `..\overlay.json` | 현재 상태(START가 확인하는 파일) |
| `..\runtime.json` | 두뇌가 실제로 쓰는 포트, 프로세스, 시작 단계와 단계별 시간 |
| `mic_check.txt` | `TEST_MICROPHONE.bat`의 마지막 마이크 점검 보고서 |
| `llm_check.txt` · `ollama.log` | `TEST_LANGUAGE_ENGINE.bat`의 보고서 · JARVIS가 시작한 Ollama의 출력 |

## 그 밖의 명령
```
START_JARVIS.bat                    시작(또는 실행 중이면 위치 표시) + 확인
START_JARVIS.bat --restart          전부 멈추고 새로 시작
START_JARVIS.bat --status           상태만 확인
START_JARVIS.bat --normal-graphics  소프트웨어 렌더링 해제
STOP_JARVIS.bat                     지금 종료(로그온 자동 시작은 유지)
TEST_MICROPHONE.bat                 안내형 마이크 점검(목록·Windows 설정·녹음·웨이크 인식)
TEST_LANGUAGE_ENGINE.bat            안내형 언어 엔진 점검(프록시·포트·Ollama 시작·모델 다운로드·실제 질문)
UNINSTALL_JARVIS.bat                자동 시작 제거 + 종료
```

## 개발 워크스페이스 (v1.5.0)
자세한 사용법과 한계는 [DEV_WORKSPACES.md](DEV_WORKSPACES.md).

| 증상 | 할 일 |
|---|---|
| 첫 요청에서 "downloading the coding model …%"가 오래 걸림 | 처음 한 번 PC에 맞는 qwen2.5-coder(약 2~20GB)를 받습니다. 진행률은 패널 아래에 보입니다. 디스크 여유를 확인하세요. 설정 > `dev.model`에 더 작은 모델(예: `qwen2.5-coder:3b`)을 적으면 빨리 끝납니다. |
| 아주 느리거나 같은 단계를 오래 반복 | GPU 없이 작은 모델이면 단계마다 수십 초~몇 분 걸립니다. "how is the build going"으로 통과한 기준 수를 보세요. 요청을 더 구체적으로(버튼 이름·숫자) 나누면 빨라집니다. ❚❚ 일시정지, ■ 중지, 설정 `dev.max_hours`로 시간 상한. |
| 원하던 것과 다르게 완성됨 | 같은 슬롯에 고칠 점을 요청으로 추가하세요(쌓기). ↶ "undo the last change"로 마지막 요청 전으로 되돌릴 수 있습니다. 패널의 "What JARVIS did"에 기준과 검사 결과가 있습니다. |
| 휴대폰에서 와이파이 링크가 안 열림 | 같은 네트워크인지, Windows 방화벽이 처음에 물었을 때 허용했는지 확인하세요. 공개 링크는 cloudflared 다운로드를 허락해야 합니다. |
| 변환이 "… needs …"로 실패 | 필요한 도구 설치를 묻는 질문에 "yes"라고 답하면 설치 후 다시 시도합니다. .7z/.rar는 7-Zip, 오디오·비디오는 FFmpeg, 문서는 Pandoc/LibreOffice. |
| 만든 .exe를 실행하면 Windows가 경고 | 방금 만든 서명 없는 프로그램이라 SmartScreen/Defender가 경고할 수 있습니다. 직접 만든 것이 맞으면 "추가 정보 → 실행". |
| "I can't find it" (변환) | 파일을 📎로 붙이거나 전체 경로를 말하세요. "this file / it"은 가장 최근에 보냈거나 만든 파일입니다. |
| 거절됨 ("refused") | 복제 방지·라이선스·안티치트 제거, 크랙, 악성 코드 요청은 하지 않습니다. |

파일 위치: `data\workspaces\<슬롯>\` 아래 `input\`(보낸 원본), `project\`(작업 중인 코드, Git 기록), `output\`(결과), `logs\`(요청별 기록).

## "하겠습니다"라고 하는데 실제로 아무 일도 일어나지 않을 때 (v1.4.4)
- **허락 질문에 답하세요.** 앱 닫기·입력·파일 쓰기·셸 명령·전원처럼 허락이 필요한 동작은 이제 "Shall I …, sir?"라고 말로 묻고
  채팅과 알림에도 보여 줍니다. 호출어 없이 "yes / 네" 또는 "no / 아니요"라고 답하거나 입력하거나 버튼을 누르세요. 90초 안에 답이
  없으면 하지 않습니다. 매번 묻지 않게 하려면 설정 > 권한에서 해당 범주를 `allow`로 바꿉니다(위험한 셸 명령은 항상 확인).
- **언어 모델이 도구를 호출하는지**: 기록(`data\runs\`)의 단계에 `LLM tool call 'open_app' {...}`처럼 남습니다. 모델이 도구 호출을
  지원하지 않으면 `<action>` 줄 방식으로 자동 전환되고, 말로만 설명한 동작은 한 번 더 확인해 실행합니다("the reply described an action
  without doing it" 단계). 작은 모델일수록 실수가 잦으니, 메모리가 넉넉하면 설정의 언어 모델을 7B 이상으로 올려 보세요.
- **입력·단축키가 엉뚱한 곳에 들어갈 때**: JARVIS 채팅창에 입력한 명령은 JARVIS 창을 건너뛰고 바로 아래 창에 적용됩니다. 원하는 창을
  한 번 클릭해 앞으로 가져온 뒤 말로 명령하면 가장 확실합니다.
- **3D 공간이 닫히지 않을 때**: "close the 3D space" / "3D 공간 닫아 줘", 열려 있는 동안은 "close it" / "exit" / "닫아 줘", 또는 **Esc**,
  기능 독의 "3D 닫기" 버튼.

## 언어 엔진에 연결되지 않을 때 (v1.4.3)
HUD 상단에 "language engine offline / not installed / problem"이 보이거나 답변이 짧은 대체 응답뿐이라면, v1.4.3부터 JARVIS가 스스로
고치려고 하고 이유를 말합니다. 대체 응답 자체가 이유를 담고 있습니다("My apologies, sir: …"). 빠른 확인 방법:

- HUD에 **`check the language engine`** (또는 **`언어 엔진 확인해 줘`**) — 꺼져 있으면 시작까지 시도한 뒤, 주소·버전·모델·속도와
  문제·해결 방법을 알려 줍니다. `restart the language engine`으로 다시 시작할 수도 있습니다.
- **`TEST_LANGUAGE_ENGINE.bat`** — 프록시·OLLAMA_HOST·포트를 점검하고, Ollama를 시작하고, 모델이 없으면 받고, 실제로 한 번 물어봅니다.
  보고서: `data\logs\llm_check.txt`.

| 알림 / START 창 | 뜻 | 할 일 |
|---|---|---|
| `My language engine (Ollama) isn't installed` | Ollama가 없음 | 알림의 **설치하기** 버튼이나 `install the language engine`(관리자 권한 불필요, 약 1GB), 또는 `INSTALL_JARVIS.bat` |
| `... installed but won't start (...)` | Ollama가 실행되지 않음(괄호 안에 Ollama의 마지막 오류) | JARVIS가 계속 다시 시도합니다. PC를 다시 시작하면 대개 해결. 자세한 내용: `data\logs\ollama.log` |
| `port 11434 is reserved by Windows / in use by ...; the language engine runs on port 11435` | 기본 포트를 쓸 수 없어 다른 포트로 옮김(정상 동작) | 할 일 없음. 포트가 다시 비면 기본 포트로 돌아갑니다 |
| `A system proxy is set on this PC; JARVIS now connects ... past it` | VPN·보안 프로그램·회사망의 프록시(예전엔 이것 때문에 연결 실패) | 할 일 없음 |
| `Downloading the language model ...: 42%` | 모델을 받는 중(그동안 설치된 다른 모델로 대화하거나 대체 응답) | 기다리면 됩니다. 끝나면 "언어 엔진이 준비되었습니다" |
| `I couldn't download the language model ...` / `There isn't enough disk space ...` | 다운로드 실패 / 디스크 부족 | 인터넷 연결·디스크 공간 확인. 몇 분 뒤 자동 재시도 |
| `... needs more memory than is free; switched to qwen2.5:3b` | 메모리 부족으로 더 작은 모델로 바꿈(설정에 저장) | 다른 프로그램을 닫거나 그대로 사용. 설정 > Language model에서 다시 바꿀 수 있음 |
| `I can't reach the language engine at http://...` | `llm.host`를 다른 컴퓨터로 지정했는데 응답 없음 | 그 컴퓨터 확인, 또는 `config\jarvis.config.json`의 `llm.host`를 `http://127.0.0.1:11434`로 |

## 셸 명령 (v1.4.3)
- JARVIS는 PowerShell 명령을 실행할 수 있습니다: `run the command Get-Process`, `powershell: ipconfig /all`, `셸에서 ipconfig 실행해 줘`.
  관리자 권한은 `run as administrator: …` / `관리자 권한으로 … 실행해 줘`(Windows UAC 창에서 직접 승인).
- 기본값은 명령마다 확인(`ask`)입니다. 확인 없이 실행하게 하려면 **`allow shell commands`**(또는 `셸 실행 허용해 줘`)라고 하고 확인
  창에서 허용하거나, 설정 패널 > 권한 > *Run shell commands*를 `allow`로. 관리자 명령은 *Run shell commands as administrator*
  (기본: 거부). 되돌리기: `always ask before shell commands` / `block shell access`.
- 포맷·재귀 삭제·레지스트리 삭제 같은 위험한 명령은 `allow`여도 항상 확인합니다. 모든 명령과 출력은 기록됩니다 → `docs/SECURITY.md`.

## 음성 웨이크·음성 명령이 안 될 때 (v1.4.2)
v1.4.2부터 JARVIS는 **왜 안 들리는지 스스로 말합니다**: START 창의 `Microphone` / `Wake phrase` 줄, HUD 위쪽의 🎙 배지
(빨간색 = 문제, 누르면 점검), 화면 알림(**설정 열기** 버튼), 그리고 음성 안내. 가장 빠른 확인 방법은 다음 둘입니다.

- HUD 채팅창에 **`check the microphone`** (또는 **`마이크 확인해 줘`**)라고 입력 — 열린 마이크, 소리 크기, Windows 개인 정보 설정,
  음소거, 웨이크 인식기·Whisper 상태, 마지막으로 들은 말과 해결 방법을 알려 줍니다.
- **`TEST_MICROPHONE.bat`** 더블클릭 — 마이크 목록과 Windows 설정을 보여 주고, "wake up Jarvis"라고 말하는 동안 녹음해 레벨과 인식
  결과로 무엇을 고칠지 알려 줍니다(JARVIS가 실행 중이어도 됨). 보고서: `data\logs\mic_check.txt`.

| 알림 / START 창 | 뜻 | 할 일 |
|---|---|---|
| `Windows is blocking desktop apps from using the microphone` | Windows 개인 정보 설정에서 데스크톱 앱의 마이크가 꺼져 있음(소리 대신 무음이 들어옴) | 설정 > 개인 정보 및 보안 > 마이크 → **마이크 액세스**와 **데스크톱 앱이 마이크에 액세스하도록 허용** 켜기. 알림의 **설정 열기** 버튼이나 `open microphone privacy settings`로 바로 열 수 있음. 켜는 즉시 다시 듣습니다 |
| `Your microphone is muted in Windows` | 기본 녹음 장치가 음소거됨(HUD의 **You** 버튼도 음소거 표시) | `unmute the microphone` 입력, **You** 버튼, 또는 설정 > 시스템 > 소리 > 입력 |
| `Your microphone sends nothing but silence` | 장치가 꺼져 있거나 다른(빈) 장치가 기본 입력으로 선택됨 | 설정 > 시스템 > 소리 > 입력에서 올바른 마이크·입력 볼륨 확인, 또는 JARVIS 설정 > **Microphone**에서 직접 선택 |
| `I can't find a microphone` | Windows에 녹음 장치가 없음 | 마이크 연결 / 장치 관리자에서 사용 설정. 꽂으면 JARVIS가 스스로 찾습니다 |
| `I can't open the microphone` | 다른 프로그램이 독점 중이거나 장치 오류 | 그 프로그램을 닫거나 JARVIS 설정 > Microphone에서 다른 장치 선택 |
| `My wake-word listener isn't available` | Vosk 웨이크 모델이 없음 | JARVIS가 스스로 내려받습니다(인터넷 필요). 그동안 WAKE 버튼·Ctrl+Alt+J |
| `Speech recognition is loading (downloading its model)` | 첫 실행에서 Whisper 모델을 받는 중 | 기다리면 됩니다. 그동안 말한 명령은 준비되면 처리 |
| `My speech recognition isn't working` | Whisper를 불러올 수 없음(이유 표시: 인터넷, Visual C++ 런타임, 메모리) | 표시된 대로 조치 후 `INSTALL_JARVIS.bat` |
| HUD 알림 `I heard "…Jarvis"` | 이름만 알아들음(억양·발음) | "**wake up Jarvis**" 전체를 말하거나 "**Jarvis**"를 두 번. 이름만으로 깨우려면 설정 > **Wake on 'Jarvis' alone** |
| `I didn't hear anything` / `I didn't catch that` | 말이 너무 작거나 짧음 | 마이크 가까이에서, 조금 크게. 🎙 배지의 레벨 막대(JARVIS 버튼 아래)가 말할 때 움직이는지 확인 |

깨어나긴 하는데 말을 끝까지 듣기 전에 끊으면 설정 > **Pause that ends a spoken command**를 늘리세요(기본 600ms).
자세한 동작 원리는 `docs/WAKE_WORD.md`.

## 두뇌가 늦게 뜨거나 "still waiting for the brain"이 계속될 때 (v1.4.1)

v1.4.1부터 START 창은 기다리는 동안 두뇌가 **지금 무엇을 하는지** 보여 줍니다
(`still waiting for the brain (building the neural mesh)` 같은 식). 보통은 몇 초 안에
`the brain is online (…, started in 2 s)`가 나옵니다.

- `port 8765 is in use by … ; the command bus uses port 8766 instead` → 다른 프로그램(예: Anki의 AnkiConnect)이
  8765를 쓰고 있어서 두뇌가 **스스로 다른 포트로 옮겼다**는 뜻입니다. 할 일은 없습니다.
- `the brain stopped (code …) and is starting again - last message: …` → 두뇌가 시작 중에 꺼졌습니다. 같은 일이 3번
  반복되면 START가 원인과 할 일을 알려 줍니다(대부분 `INSTALL_JARVIS.bat` 재실행으로 해결).
- 두뇌가 어디까지 왔는지는 `data\runtime.json`(포트, 프로세스, 단계, 단계별 시간)과 `data\logs\backend.log`의
  `start-up:` 줄에 있습니다. 문제를 알려 주실 때 `data\logs\diagnostics.txt`와 함께 보내 주세요.

## 답이 늦게 나올 때 (v1.4)

v1.4에서 응답 지연의 원인을 하나씩 찾아 고쳤습니다. 그래도 느리다면 아래 순서로 확인하세요.

1. **`INSTALL_JARVIS.bat`을 한 번 더 실행하세요.** 언어 모델은 그래픽카드(VRAM)에 다 들어가야 빠릅니다. 예전 설치
   프로그램은 RAM이 32GB인 PC에 그래픽카드가 없어도 7B 모델을 골랐는데, 그러면 프로세서로 돌아가 1초에 몇 단어밖에 나오지
   않습니다. 새 설치 프로그램은 이 PC에서 빠르게 도는 크기를 고릅니다(NVIDIA 12GB 이상 → 14B, 6GB 이상 → 7B, 그 외 → 3B,
   RAM 8GB 미만 → 1.5B). 이미 받은 모델은 그대로 두고 필요한 것만 받습니다.
2. **시작 직후 1~2분**은 언어 모델을 메모리에 올리는 중일 수 있습니다. HUD 상단에 "language engine warming up"이 보이면
   기다리세요. 한 번 올라가면 계속 메모리에 남아 있어(설정 *Keep the language model loaded = always*) 다시 기다리지 않습니다.
3. `START_JARVIS.bat`이 성공 화면에 **"Replies are slow on this PC …"**라고 알려 주면, 측정한 생성 속도가 낮다는 뜻입니다
   (1번 해결책).
4. 말로 명령할 때 말을 끝내고 응답이 시작되기까지가 길게 느껴지면, 설정의 *Pause that ends a spoken command*를
   줄여 보세요(기본 600ms). 반대로 말하는 도중에 끊긴다면 늘리세요.

| 무엇이 느렸나 | v1.4에서 |
|---|---|
| 명령마다 신경 메시가 먼저 돌았는데, RAM에 맞춰 커져서 16GB PC에서 명령당 **약 4.5초**(32GB면 더 김) | 시작할 때 이 PC의 속도를 재서 **명령당 60ms 안에** 끝나는 크기로 정함. 같은 메시 계산도 2배 빨라짐(결과는 동일) |
| Ollama가 5분 동안 쓰이지 않으면 모델을 내려서, 다음 답이 **모델을 다시 올리는 5~30초**를 기다림 | 요청마다 `keep_alive`로 계속 올려 두고, 시작할 때 미리 올려 두며, Ollama가 재시작되면 다시 올림 |
| 매 턴 시스템 프롬프트(요청 횟수·스타일 지시)와 대화 창이 바뀌어 Ollama가 **대화 전체를 매번 다시 읽음** | 시스템 프롬프트와 대화 기록을 턴마다 그대로 두고, 바뀌는 지시는 새 메시지 끝의 짧은 메모로. 기록은 가끔 한꺼번에 줄임 |
| GPU 없는 PC에 7B 모델 | 설치 프로그램이 PC에 맞는 모델 선택, GPU에서는 flash attention |
| 음성 인식이 짧은 명령에도 넓게 탐색하고, 첫 명령에서 모델을 초기화 | 빠른 디코딩 설정, 시작할 때 미리 준비 |
| 문장 사이마다 다음 문장을 합성하느라 끊김, "Jarvis at your service sir"를 매번 새로 합성 | 다음 문장을 재생 중에 미리 합성, 자주 하는 말은 시작할 때 미리 만들어 둠 |
| 앱 목록(PowerShell)·노래 찾기(새 파이썬 실행)가 처음에 느림 | 시작할 때 백그라운드에서 준비, 노래는 같은 프로세스에서 검색, 오래 걸리는 작업은 "Right away, sir."로 바로 대답 |

## 이전 버전(1.2 이하)에서 올리기
새 ZIP을 **기존 폴더 위에 덮어써서** 압축을 풀고 `START_JARVIS.bat`을 누르면 됩니다. START가 예전 JARVIS를 멈추고(다른 폴더에서
돌고 있어도) 새 버전을 시작합니다. 새 폴더에 풀었다면 그 폴더에서 `INSTALL_JARVIS.bat`을 한 번 실행하세요(자동 시작이 새 폴더를
가리키게 됩니다. 이미 설치된 Python·Ollama·모델은 다시 받지 않습니다).
