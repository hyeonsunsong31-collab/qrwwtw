# 웨이크 워드 · 말 끊기 가이드

## 깨우는 방법
| 방법 | 동작 |
|------|------|
| "**wake up Jarvis**" | 음성 웨이크(아래 엔진). "Jarvis at your service sir" 응답 후 바로 명령을 듣습니다. |
| WAKE 버튼 (우하단, 항상 표시) | 위와 동일. 마이크가 없거나 시끄러울 때의 비상 수단(요구 9). |
| `Ctrl+Alt+J` / 트레이 아이콘 클릭 | 위와 동일. |
| 채팅창 🎙 버튼 | 응답 없이 바로 한 번 듣기(누르고 말하기). |

## 웨이크 엔진 — `voice.wakeword.engine`
- **`vosk` (기본):** Vosk 소형 영어 모델을 **문법 모드**로 사용합니다. 인식기가 `wake up jarvis`, `jarvis`, `stop`, `wait`,
  `hold on` 외의 단어는 출력하지 못하게 제한되어 있어, 별도 학습 없이 **정확한 구문**을 안정적으로 잡습니다. CPU 부담이 작습니다.
  모델 위치: `models/vosk-small-en` (설치 프로그램이 내려받음).
- **`openwakeword`:** 신경망 기반 감지기(onnxruntime). 기본으로 포함된 "hey jarvis" 모델을 쓰고, 직접 학습한 모델이
  `models/wake_up_jarvis.onnx`에 있으면 그것을 씁니다. 감도: `voice.wakeword.sensitivity`(0.3~0.95).
- **`voice.wakeword.lenient`:** 켜면 "Jarvis"만 말해도 깨어납니다(오작동은 늘 수 있음).

설정은 홀로그램 **설정 패널**("jarvis settings")에서 바꿀 수 있습니다. 엔진 변경은 JARVIS 재시작 후 적용됩니다.

### (선택) "wake up jarvis" 전용 openWakeWord 모델 만들기
openWakeWord는 합성 음성으로 사용자 정의 웨이크 워드를 학습하는 공식 노트북을 제공합니다(openWakeWord 저장소의
"automatic model training" 노트북). 구문에 `wake up jarvis`를 넣어 학습한 뒤 나온 `.onnx` 파일을
`models/wake_up_jarvis.onnx`로 복사하고, 설정에서 엔진을 `openwakeword`로 바꾸면 됩니다.

## 말 끊기(바지인) — `voice.barge_in.mode`
- **`keyword` (기본):** JARVIS가 말하는 중에 "stop" / "wait" / "hold on" / "Jarvis"라고 하면 즉시 멈추고 새 명령을 듣습니다.
  JARVIS가 **지금 말하고 있는 문장에 들어 있는 단어는 무시**하므로, 스피커로 나온 자기 목소리 때문에 스스로 끊기지 않습니다.
- **`vad`:** 0.4초 이상 사람 목소리가 들리면 끊습니다. 스피커 소리가 마이크로 들어가지 않는 **헤드셋** 사용 시 권장.
- **`off`:** 음성으로는 끊지 않습니다(새 명령 입력이나 🎙 버튼은 여전히 즉시 끊음).

## 문제 해결
- 잘 안 깨어날 때: 마이크 기본 장치 확인 → 설정에서 감도 조정(openwakeword) 또는 `lenient` 켜기 → WAKE 버튼 사용.
- 혼자 말이 끊길 때: 바지인을 `keyword`로(기본) 두거나 헤드셋 사용.
- 로그: `data/service.log`(서비스), `data/install.log`(설치).
