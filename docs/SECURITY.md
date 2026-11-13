# 보안 · 동의 모델

JARVIS는 요구사항 20("PC의 모든 기능과 정보에 전면 접근")을 구현하지만, **사용자 본인의 PC에서, 본인의 동의 하에,
화면에 보이게 동작하는 개인 비서**라는 원칙을 지킵니다. 숨기거나, 탐지를 회피하거나, 다른 사람의 기기를 대상으로 하는
기능은 없습니다.

## 1. 명령 버스 접근 제어 (v1.1)
- 서버(`agent/server.py`)는 **127.0.0.1에만** 바인딩합니다(설정에 다른 주소가 있어도 거부하고 루프백 사용).
- 시작할 때마다 **새 무작위 토큰**을 `data/session.token`(현재 사용자 전용)에 씁니다. 모든 WebSocket 접속과
  `/status` `/neural/points` `/history` `/announce` HTTP 요청은 이 토큰이 있어야 합니다. 공개 엔드포인트는 `{"ok": true}`만
  돌려주는 `/health`(슈퍼바이저 감시용) 하나입니다. `/announce`는 HUD에 배너를 띄우는 것 외에 아무것도 하지 않습니다.
- 브라우저에서 열린 웹페이지는 접근할 수 없습니다. 웹페이지는 토큰 파일을 읽을 수 없고, http(s) Origin을 단 WebSocket
  핸드셰이크는 토큰과 무관하게 거부됩니다. 거부된 시도는 감사 로그에 `ws_connect / rejected`로 남습니다.
- Electron 오버레이는 preload 브리지를 통해 토큰을 읽고, 백엔드가 재시작되면 재접속할 때 새 토큰을 다시 읽습니다.

## 2. 권한 계층 — `config/permissions.json` (설정 패널에서도 변경 가능)
| 범주 | 기본값 | 설명 |
|------|--------|------|
| read_files · read_system_info · read_devices | allow | 읽기 |
| launch_apps · browser_open · browser_navigate · media_control · play_music | allow | 저위험 실행 |
| jarvis_data | allow | JARVIS 자체 폴더에 저장(3D 프로젝트·모델 내보내기) |
| window_control | allow | 창 전환·최소화·최대화·배경화면 모드(되돌릴 수 있음) |
| screen_capture | allow | 스크린샷(로컬 저장) |
| write_files · control_apps · change_system_settings · control_devices · browser_form_fill · read_accounts | ask | 실행 전 홀로그램 확인 |
| keyboard_input | ask | 포커스된 앱에 입력·단축키(무엇이든 할 수 있으므로 확인) |
| delete_files · run_shell · network_send | ask | 되돌리기 어렵거나 외부 전송 |
| run_shell_elevated | **deny** | 관리자 셸 — 설정 패널에서도 **잠김** |

추가 규칙: `always_ask_keywords`(password, credential, bank, format 등)가 명령에 있으면 `allow` 범주도 강제로 확인합니다.
확인은 홀로그램 대화상자(허용/거부)로 하고, 90초 내 응답이 없으면 거부합니다. 위험도는 신경 제어 계층의 주의(caution)
신호에도 반영되어 답변이 더 신중해집니다. 설정 패널의 변경은 키·값을 서버가 검증한 뒤 원자적으로 저장하며, LLM이 권한을
바꾸는 도구는 없습니다.

## 3. 감사 로그 · 기록
모든 권한 판단(allow/ask/granted/denied/deny), 거부된 접속, 설정 변경이 `data/audit/audit.log`에 남습니다. 작업·명령·생성
파일·대화·설명·옵션·추천·체크포인트도 `data/` 아래에 전부 기록됩니다(요구 2).

## 4. 마이크 · 카메라
- "JARVIS" 음소거 버튼은 마이크 스트림을 **완전히 닫습니다**(Windows 개인정보 표시도 꺼짐).
- 웹캠은 손동작 감지를 켠 동안에만 열리고, 끄면 즉시 정지합니다.

## 5. 데이터와 프라이버시
- 두뇌(LLM)·음성 인식·음성 합성은 모두 **로컬·오프라인**입니다. 대화·파일 내용이 외부로 전송되지 않습니다.
- 예외: 사용자가 요청한 **웹 음악·웹 검색·페이지 읽기**는 해당 사이트로 나갑니다(일반 브라우징과 동일). 설치 중에는
  모델·음성·런타임 다운로드를 위해 공식 배포처(python.org, registry.npmjs.org, github.com(Electron), ollama.com, huggingface.co,
  alphacephei.com, gyan.dev, storage.googleapis.com, aka.ms(Visual C++ 런타임), Windows Update)에 접속합니다.
- 자격증명은 수집·저장하지 않습니다. `config/secrets.json`, `.env`, `data/`는 git에서 제외됩니다.

- **자기 코어가 기억하는 것**(`data/brain/self_model.json`): 요청의 *종류*별 횟수·결과와, 사용자가 "기억해"라고 한 문장뿐입니다.
  요청의 원문은 여기에 저장되지 않습니다. "what have you learned"로 보고, "forget what you have learned"로 지우고, 설정에서 끌 수
  있습니다. 지침("Maximize utility and service to the user.")은 코드 상수라 배운 내용이나 파일로 바뀌지 않고, 이 모듈에는 권한·스킬로
  가는 경로가 없습니다 → `docs/SELF_MODEL.md`.
- 화면 표시부 런타임은 `registry.npmjs.org`와 `github.com/electron/electron/releases`에서만 받고, `package-lock.json`의 SHA-512와
  Electron 체크섬(SHA-256)에 맞지 않으면 설치하지 않습니다. 압축 파일 안의 경로 이탈(`..`)도 거부합니다.

## 6. 시작·종료가 건드리는 프로세스
`START_JARVIS.bat`·`STOP_JARVIS.bat`·설치 프로그램은 **JARVIS 자신의 프로세스만** 종료합니다(`boot/common.py`
`classify_process`): `<JARVIS 폴더>/boot/jarvis_service.py`를 실행 중인 파이썬, JARVIS 폴더에서 `-m agent.server`로 실행된 파이썬,
JARVIS 폴더 안의 Electron 실행 파일, JARVIS가 표식을 붙여 실행한 음악 재생기. 이름만 같은 다른 프로그램, 다른 Electron 앱,
사용자가 JARVIS로 연 앱은 대상이 아닙니다(테스트로 확인). 로그·상태 파일은 모두 `data\` 안에 있으며 토큰은 로그에서 가립니다.

## 7. 범위 조정과 제거
- 설정 패널 또는 `config/permissions.json`으로 범위를 넓히거나 좁힙니다.
- `STOP_JARVIS.bat` 또는 트레이 → **Quit JARVIS**로 언제든 끕니다. `UNINSTALL_JARVIS.bat`은 자동시작을 해제하고 JARVIS
  프로세스만 종료합니다. 폴더·기록은 지우지 않습니다.
