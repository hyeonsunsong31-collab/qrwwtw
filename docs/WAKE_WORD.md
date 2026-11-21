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
  모델 위치: `models/vosk-small-en` (설치 프로그램이 내려받고, 없으면 JARVIS가 체크섬을 확인하며 스스로 내려받음).

### 2단계 확인 (v1.4.2) — 억양이 있어도 깨어나도록
Vosk 소형 모델은 원어민의 "wake up Jarvis"는 놓치지 않지만, 억양이 있으면 "[unk] jarvis"나 "wake up [unk]"만 듣는 일이 많고,
문법 모드라서 "service"·"nervous" 같은 평범한 단어가 "jarvis"로 들리기도 합니다. 그래서:

1. **즉시 웨이크** — Vosk가 정확히 `wake up jarvis` 또는 `jarvis wake up`만 들었을 때(부분 결과는 0.4초 동안 그대로여야 함:
   "wake up early"가 잠깐 "wake up jarvis"로 보였다가 바뀌는 경우를 거름).
2. **Whisper 확인** — "wake", "up", "jarvis" 중 하나라도 들렸거나, 조용한 순간에 0.5~2.2초짜리 짧은 문구를 말했다면(사람이 비서를
   부르는 방식) Whisper가 **마지막 3초**를 다시 듣습니다. Whisper는 억양("Vaku up, Jarvis", "Weckup Jarvis", "Back up Jarvis")과 한국어
   ("**자비스, 일어나**")를 알아듣습니다. 판정은 `wakeword.wake_verdict()`:
   - 이름 + "wake up"(철자가 달라도 소리가 같으면: W/V-K-P) / "wake" / "get up" / "일어나" → **웨이크**
   - 이름만 → HUD 힌트("wake up Jarvis라고 말씀하세요"), **10초 안에 이름을 두 번** 부르면 웨이크
   - 둘 다 아님 → 무시(대화 속 유사 단어)
3. Whisper가 아직 준비되지 않은 시작 직후 몇 초 동안은 예전처럼 Vosk 결과만으로 판단합니다.

Whisper 확인은 한 번에 하나, 짧은 문구 확인은 2.5초에 한 번으로 제한되어 CPU를 계속 쓰지 않습니다.
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

## 마이크 (v1.4.2) — `voice/mic.py`
- 설정의 **Microphone**(기본 `default`) → Windows 기본 입력 → 같은 장치의 MME·DirectSound·WASAPI → 다른 마이크 순으로 열고,
  16kHz 모노가 안 되면 장치 고유 형식(예: 48kHz 스테레오)으로 열어 실시간 변환합니다. 스테레오 믹스 같은 루프백 장치는 쓰지 않습니다.
- 소리를 전혀 안 보내는 장치는 다음 마이크로 넘어가고, 3초 동안 디지털 무음이면 Windows 개인 정보 설정·음소거를 읽어 원인을 알립니다.
- 명령 녹음: 최근 8프레임 중 4프레임이 소음보다 6dB 이상 큰 말이면 시작, JARVIS가 말을 마친 직후 0.25초는 잔향으로 보고 무시,
  시작 전 0.3초 보존, 0.6초 침묵(설정 가능)이면 끝, 0.2초 미만은 잡음.

## 문제 해결
- 먼저 HUD에 `check the microphone`(또는 `마이크 확인해 줘`)을 입력하거나 `TEST_MICROPHONE.bat`을 실행하세요 — 무엇이 문제인지
  알려 줍니다. 표는 `docs/TROUBLESHOOTING.md`의 "음성 웨이크·음성 명령이 안 될 때".
- 이름만 알아듣는다는 힌트가 자주 뜨면: "wake up Jarvis"를 또박또박, 또는 "Jarvis"를 두 번 → 그래도 불편하면 설정에서
  **Wake on 'Jarvis' alone**(lenient) 켜기 → WAKE 버튼.
- 혼자 말이 끊길 때: 바지인을 `keyword`로(기본) 두거나 헤드셋 사용.
- 로그: `data/logs/backend.log`(음성 엔진: 마이크·웨이크·문제 줄), `data/install.log`(설치).
