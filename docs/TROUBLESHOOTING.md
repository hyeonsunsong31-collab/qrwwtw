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

## 그 밖의 명령
```
START_JARVIS.bat                    시작(또는 실행 중이면 위치 표시) + 확인
START_JARVIS.bat --restart          전부 멈추고 새로 시작
START_JARVIS.bat --status           상태만 확인
START_JARVIS.bat --normal-graphics  소프트웨어 렌더링 해제
STOP_JARVIS.bat                     지금 종료(로그온 자동 시작은 유지)
UNINSTALL_JARVIS.bat                자동 시작 제거 + 종료
```

## 이전 버전(1.2 이하)에서 올리기
새 ZIP을 **기존 폴더 위에 덮어써서** 압축을 풀고 `START_JARVIS.bat`을 누르면 됩니다. START가 예전 JARVIS를 멈추고(다른 폴더에서
돌고 있어도) 새 버전을 시작합니다. 새 폴더에 풀었다면 그 폴더에서 `INSTALL_JARVIS.bat`을 한 번 실행하세요(자동 시작이 새 폴더를
가리키게 됩니다. 이미 설치된 Python·Ollama·모델은 다시 받지 않습니다).
