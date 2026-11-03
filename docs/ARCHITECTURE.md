# 아키텍처

## 구성 요소
```
┌────────────────────────────────────────────────────────────────────┐
│  Electron 오버레이 (overlay/)                                        │
│   · 홀로그램 HUD: 리액터 WAKE 버튼(상시)·상태 링·채팅·마이크뮤트·피처독 │
│   · 3D 프로젝트 공간 (Three.js) + 손동작 (MediaPipe) + 배경화면 모드     │
│   · 브라우저 TTS/STT 폴백, 권한 확인 창, i18n 자동 전환                 │
└───────────────▲───────────────────────────────────────▲──────────────┘
         ws://127.0.0.1:8765/ws (단일 커맨드 버스)         │ /health /neural/points /history
┌───────────────┴───────────────────────────────────────┴──────────────┐
│  백엔드 (Python, agent/server.py)                                      │
│   Orchestrator → NLU → ControlLayer(신경 코어) → Skills / LLM           │
│                    │                 │              │                   │
│   Recorder(기록)  Permissions(권한)  Ollama LLM   VoiceEngine(음성)     │
└────────────────────────────────────────────────────────────────────┘
          supervisor(boot/jarvis_service.py) 가 백엔드+오버레이 상주·재시작
          register_autostart.ps1 가 로그온 시 supervisor 자동 실행
```

## 명령 처리 흐름 (음성·텍스트 동일 경로 — 요구 15)
1. 입력(STT 또는 채팅/버튼) → `{type:"command", text, source}` 로 커맨드 버스에 전송.
2. `Orchestrator.handle_command`: 언어 감지 → 기록 → `parse_intent` → `ControlLayer.compute`(제어신호/LLM 파라미터).
3. 분기: 확신도 높은 직접 명령 → **스킬**(권한 게이트 통과 후 실행); 그 외 → **LLM 대화**(필요 시 `<action>` 태그로 스킬 호출).
4. `control_layer.postprocess`로 출력 조정 → 기록(대화·성패·소요시간) → `transcript`+`say` 이벤트 송출.
5. 후속 **옵션(3)·추천(4)** 생성·기록·표시 → 워크플로우 **체크포인트** 저장 → 상태 idle.

새 명령이나 사용자 발화(바지인)는 진행 중 답변을 **취소**하고 TTS를 멈춥니다(요구 13).

## 커맨드 버스 메시지 (요약)
- 클라이언트→서버: `command` · `wake` · `interrupt` · `feedback` · `permission_response` · `ui` · `ping`
- 서버→클라이언트: `hello` · `state`(idle/listening/thinking/speaking) · `transcript` · `say` · `wake_response`
  · `options` · `ui`(open_space3d 등) · `permission_request` · `stop_tts` · `control` · `pong`

## 신경 코어 (요구 1) — `core/`
- `synaptic_perceptron.py`: 시냅스 상태(단기 가소성)를 가진 퍼셉트론. `SynapticLayer`는 벡터화 집단.
- `brain_mesh.py`: 3D 좌표 + 희소 연결(근접 바이어스 + 모듈간 투사 룰북). Cortex/Cerebellum/Thalamus 핵심 +
  Brainstem(각성)/Hippocampus(기억)/BasalGanglia(게이트). 각 스텝: 시냅스 전달→뉴런 입력 합산(bincount)→
  시상 게이팅→누설 통합→활성. 순수 NumPy.
- `scale.py`: 명목 2e11 vs 실제 active(하드웨어 기반). 구조는 규모 불변.
- `control_layer.py`: 입력 특징을 시상/뇌간에 주입→메시 수렴→모듈 리드아웃으로 6개 제어신호→LLM 파라미터·스타일·
  후처리에 반영. 피드백 시 3요소 시냅스 가소성 + 바이어스 학습(디스크 영구 저장 → 재부팅 후 유지).

## 음성 (요구 3·7·8·12·13·21) — `voice/`
단일 마이크 스트림 상태기계: IDLE(웨이크 감지)→CAPTURE(무음까지 녹음→STT→명령)→SPEAKING(발화 중 VAD 바지인).
TTS는 Piper 영국 음성(실패 시 SAPI5 'George', 그마저 없으면 브라우저 speechSynthesis 영국 음성).

## 3D 공간 (요구 5·17·18) — `overlay/renderer/space3d/`
Three.js 씬. 좌클릭/핀치 드래그, 스크롤/양손 스케일, 복제. OrbitControls(줌은 수동). 손동작은 MediaPipe HandLandmarker
(지연 로드·토글). 배경화면 모드는 전체화면 바닥 레이어(아이콘 뒤 삽입은 Windows WorkerW로 확장, `wallpaper.js` 주석).

## 자동시작·상주 (요구 2·4) — `boot/`
`register_autostart.ps1`이 로그온 예약작업 등록 → `jarvis_service.py`(슈퍼바이저)가 백엔드+오버레이를 띄우고
크래시 시 백오프 재시작, 트레이 제공. `INSTALL_JARVIS.bat`이 설치·등록·즉시 실행을 한 번에 수행.
