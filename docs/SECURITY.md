# 보안 · 동의 모델

JARVIS는 요구사항 20("PC의 모든 기능과 정보에 전면 접근")을 구현하지만, **사용자 본인의 PC에서,
본인의 동의 하에, 화면에 보이게 동작하는 개인 비서**라는 원칙을 지킵니다. 숨기거나, 탐지를 회피하거나,
다른 사람의 기기를 대상으로 하는 기능은 넣지 않았습니다. (오히려 항상 화면에 떠 있는 가시적 비서입니다.)

## 권한 계층 — `config/permissions.json`
각 동작 범주는 `allow`(바로 실행) / `ask`(먼저 확인) / `deny`(거부) 중 하나입니다.

| 범주 | 기본값 | 설명 |
|------|--------|------|
| read_files / read_system_info / read_devices | allow | 읽기 |
| launch_apps / browser_open / browser_navigate / media_control / play_music | allow | 저위험 실행 |
| write_files / control_apps / change_system_settings / control_devices / browser_form_fill / read_accounts | ask | 실행 전 홀로그램 창으로 확인 |
| delete_files / run_shell / network_send | ask | 되돌리기 어렵거나 외부전송 — 반드시 확인 |
| run_shell_elevated | **deny** | 관리자 셸은 기본 거부 |

추가 규칙:
- `always_ask_keywords`(password, credential, bank, format, registry delete 등)가 명령에 들어가면
  `allow` 범주라도 **강제로 확인**합니다.
- `never_allowed`: 타 기기 대상 동작, 자기 은폐/탐지회피, 감사 로그 비활성화, 동의 없는 파일/자격증명 외부전송.

구현: `agent/permissions.py`의 `PermissionManager.check()`가 결정하고, 확인이 필요하면 오버레이가
홀로그램 확인 창(허용/거부, `overlay/renderer/app.js` `permissionToast`)을 띄우며, 서버가 응답을 기다립니다
(90초 내 무응답 시 자동 거부). 위험도는 `control_layer`의 주의(caution) 신호에도 반영되어 답변이 더 신중해집니다.

## 감사 로그
모든 권한 판단(allow/ask/granted/denied/deny)이 `data/audit/audit.log`에 남습니다. 작업·명령·결과·대화도
`data/` 아래에 전부 기록됩니다(요구 2). 사용자는 언제든 열람할 수 있습니다.

## 데이터와 프라이버시
- 두뇌(LLM)는 **로컬 Ollama**로 완전 오프라인입니다. 대화·파일 내용이 외부로 전송되지 않습니다.
- 예외: 사용자가 명시적으로 요청한 **웹 음악 재생·웹 검색·페이지 읽기**는 해당 사이트로 나갑니다(일반 브라우징과 동일).
- 자격증명·토큰은 수집·저장하지 않습니다. `config/secrets.json`과 `.env`는 `.gitignore`로 커밋 차단됩니다.

## 범위 조정과 제거
- 범위를 넓히거나 좁히려면 `config/permissions.json`을 편집하세요(예: 신뢰하면 `write_files`를 `allow`로).
- `UNINSTALL_JARVIS.bat`이 로그온 자동시작을 해제하고 실행 중 프로세스를 종료합니다. 폴더·기록은 지우지 않습니다.
