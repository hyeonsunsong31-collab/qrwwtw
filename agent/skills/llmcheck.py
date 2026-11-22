"""
Language-engine self-check and repair (v1.4.3).

  "check the language engine" / "why are you offline" / "restart the language engine"
  "언어 엔진 확인해 줘" / "언어 엔진 다시 시작해 줘"      -> check(): starts the engine if it is not
                                                           running, then says exactly what works
                                                           and what does not, with the fix
  "install the language engine" / "언어 엔진 설치해 줘"  -> install(): installs Ollama for this
                                                           Windows user (no administrator rights)

The server registers its ResilientLLM with set_provider().
"""
from __future__ import annotations

import sys
from typing import List, Optional, Tuple

_LLM = None


def set_provider(llm) -> None:
    global _LLM
    _LLM = llm


def summarise(d: dict) -> Tuple[str, str, Optional[str]]:
    """(English, Korean, suggested action) for ResilientLLM.describe()."""
    en: List[str] = []
    ko: List[str] = []
    state = d.get("state")
    eng = d.get("engine") or {}
    p = d.get("problem") or {}
    prog = d.get("progress") or {}
    model = d.get("model") or "?"
    st = d.get("stats") or {}
    if state == "ready":
        ver = f" {eng['version']}" if eng.get("version") else ""
        en.append(f"My language engine is running: Ollama{ver} at {eng.get('url')}, with the model {model}.")
        ko.append(f"언어 엔진이 실행 중입니다: {eng.get('url')}의 Ollama{ver}, 모델 {model}.")
        tps, gpu = st.get("tokens_per_s"), st.get("gpu")
        if tps:
            where = (f", {int(round(100 * gpu))}% on the graphics card" if gpu else ", on the processor")
            amount = "less than one token" if tps < 1 else f"about {tps:.0f} token{'s' if round(tps) != 1 else ''}"
            en.append(f"It writes {amount} a second{where}.")
            ko.append(f"초당 약 {tps:.0f}토큰을 생성합니다" + (f"(그래픽카드 {int(round(100 * gpu))}%)." if gpu else "(프로세서)."))
    elif state == "pulling" and prog:
        en.append(f"I'm downloading the language model {prog.get('model')}: {prog.get('percent', 0):.0f}% done.")
        ko.append(f"언어 모델 {prog.get('model')}을(를) 다운로드하는 중입니다: {prog.get('percent', 0):.0f}% 완료.")
    elif state == "loading":
        en.append(f"The language model {model} is loading into memory.")
        ko.append(f"언어 모델 {model}을(를) 메모리에 불러오는 중입니다.")
    elif state == "starting":
        en.append("My language engine is starting.")
        ko.append("언어 엔진을 시작하는 중입니다.")
    if p:
        en.append(f"{p.get('message')} {p.get('fix')}")
        ko.append(f"{p.get('message_ko')} {p.get('fix_ko')}")
    elif state in ("offline", "not_installed", "error") and d.get("error"):
        en.append(f"The last error was: {d['error']}.")
        ko.append(f"마지막 오류: {d['error']}.")
    note = d.get("note")
    if note:
        en.append(note[0].upper() + note[1:] + ".")
        ko.append(f"참고: {note}.")
    if prog and state != "pulling":
        en.append(f"Downloading {prog.get('model')}: {prog.get('percent', 0):.0f}% done.")
        ko.append(f"{prog.get('model')} 다운로드 중: {prog.get('percent', 0):.0f}% 완료.")
    if eng.get("proxy"):
        en.append("A system proxy is set on this PC; I now reach the local engine directly, past it.")
        ko.append("이 PC에 시스템 프록시가 설정되어 있어, 이제는 프록시를 거치지 않고 로컬 엔진에 직접 연결합니다.")
    if not en:
        en.append(f"My language engine is {state}.")
        ko.append(f"언어 엔진 상태: {state}.")
    return " ".join(en), " ".join(ko), p.get("action")


def check(restart: str = "", **_) -> dict:
    llm = _LLM
    if llm is None:
        return {"ok": False, "speak": "My language engine isn't available in this process, sir.",
                "data": {"speak_ko": "이 프로세스에서는 언어 엔진을 사용할 수 없습니다, sir."}, "error": "no provider"}
    if restart or not llm.cached_online():
        try:
            llm.restart_engine()                # starts Ollama if it is installed but not running
        except Exception as e:
            llm.error = f"{type(e).__name__}: {e}"[:200]
    d = llm.describe()
    en, ko, action = summarise(d)
    return {"ok": True, "speak": en, "data": {"report": d, "speak_ko": ko, "action": action}, "error": None}


def install(**_) -> dict:
    llm = _LLM
    if llm is None:
        return {"ok": False, "speak": "My language engine isn't available in this process, sir.",
                "data": {"speak_ko": "이 프로세스에서는 언어 엔진을 사용할 수 없습니다, sir."}, "error": "no provider"}
    from agent.ollama_service import find_ollama
    if llm.state == "installing":
        return {"ok": True, "speak": "I'm already installing my language engine, sir.",
                "data": {"speak_ko": "이미 언어 엔진을 설치하고 있습니다, sir."}, "error": None}
    if find_ollama():
        ok = llm.restart_engine()
        if ok:
            return {"ok": True, "speak": "Ollama is already installed, sir, and it's running now.",
                    "data": {"speak_ko": "Ollama는 이미 설치되어 있으며 지금 실행 중입니다, sir."}, "error": None}
        en, ko, _ = summarise(llm.describe())
        return {"ok": False, "speak": "Ollama is already installed, sir, but it won't start. " + en,
                "data": {"speak_ko": "Ollama는 이미 설치되어 있지만 시작되지 않습니다, sir. " + ko}, "error": "start failed"}
    if not sys.platform.startswith("win"):
        return {"ok": False, "speak": "I can only install Ollama by myself on Windows, sir.",
                "data": {"speak_ko": "Ollama 자동 설치는 Windows에서만 할 수 있습니다, sir."}, "error": "not windows"}
    llm.install_engine()
    return {"ok": True,
            "speak": "Installing my language engine now, sir. It downloads about a gigabyte and takes a few "
                     "minutes; then I'll fetch a language model. I'll tell you when it's ready.",
            "data": {"speak_ko": "지금 언어 엔진을 설치하겠습니다, sir. 약 1GB를 내려받아 몇 분 걸리며, 이어서 언어 모델을 "
                                 "받습니다. 준비되면 알려 드리겠습니다."}, "error": None}
