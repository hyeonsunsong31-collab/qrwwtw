"""
Developer workspaces by voice or text (v1.5.0) -- the same as the HUD's Workspaces panel.

  "build me a snake game" / "make a website for my bakery" / "뱀 게임 만들어 줘"
                                       -> dev_request: the selected workspace (a new one if none)
  "add a high-score table to the game" -> dev_request: stacked on the workspace's product
  "convert logo.png to ico" / "turn this ini file into an exe"  -> dev_convert
  "new workspace called Portfolio" / "switch to workspace Portfolio"  -> ws_new / ws_switch
  "how is the build going" / "workspace status"                -> ws_status
  "share it publicly" / "share it on my network" / "stop sharing" -> ws_share (asks first)
  "pause the build" / "resume" / "stop coding" / "undo the last change" -> ws_pause / ...

The server registers its DevService with set_service().
"""
from __future__ import annotations

import os
import re

from .base import ok, fail

_SVC = None


def set_service(svc) -> None:
    global _SVC
    _SVC = svc


def _svc():
    if _SVC is None or not _SVC.enabled():
        raise RuntimeError("the developer is turned off in Settings")
    return _SVC


def _ko(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


def dev_request(text: str = "", request: str = "", **_) -> dict:
    text = (text or request or "").strip()
    if not text:
        return fail("What should I build or change, sir?")
    try:
        svc = _svc()
        from dev.files import refused
        why = refused(text)
        if why:
            return fail(why, "refused")
        r = svc.request(text)
    except Exception as e:
        return fail(f"I couldn't start that, sir: {e}", str(e))
    ws, pos = r["workspace"], r["position"]
    when_en = "I'm starting now" if pos == 0 else f"it's number {pos + 1} in the queue"
    when_ko = "지금 시작합니다" if pos == 0 else f"대기열 {pos + 1}번째입니다"
    return ok(f"Very good, sir. I'll work on it in the workspace '{ws['name']}' until every check passes; {when_en}. "
              f"Say 'how is the build going' any time.",
              {"speak_ko": f"알겠습니다, sir. '{ws['name']}' 워크스페이스에서 모든 확인을 통과할 때까지 작업하겠습니다. {when_ko}. "
                           f"'작업 상황'이라고 물어보시면 알려 드립니다.", "workspace": ws["id"], "request": r["request"]["id"],
               "ui": "workspaces"})


def dev_convert(src: str = "", fmt: str = "", **_) -> dict:
    fmt = (fmt or "").strip().lstrip(".").lower()
    if not fmt:
        return fail("Into which format, sir?")
    try:
        svc = _svc()
        path = _find_source(svc, src)
        text = f"convert {os.path.basename(path) if path else src} to {fmt}"
        r = svc.request(text, [path] if path else [], kind="convert", meta={"target": fmt})
    except FileNotFoundError as e:
        return fail(f"I can't find {src or 'the file'}, sir. Attach it in the Workspaces panel or say its full path.",
                    str(e))
    except Exception as e:
        return fail(f"I couldn't start the conversion, sir: {e}", str(e))
    return ok(f"Converting it to .{fmt}, sir; I'll tell you when the file is ready.",
              {"speak_ko": f".{fmt} 형식으로 변환하겠습니다, sir. 파일이 준비되면 알려 드리겠습니다.",
               "workspace": r["workspace"]["id"], "ui": "workspaces"})


def _find_source(svc, src: str) -> str:
    """'this file' = the newest file you attached or JARVIS made; otherwise a path, or a name in your folders."""
    s = (src or "").strip().strip('"').strip("'")
    ext = s[3:] if s.lower().startswith("it:") else ""
    if not s or ext or re.fullmatch(r"(it|this|that|the file|this file|that file|이거|이 파일|그 파일)(\s+file)?", s, re.I):
        # the newest file (of that type) you sent or JARVIS made: a request's product comes after its
        # attachments; the selected workspace first, then any
        wid = svc.store.selected()
        order = sorted(svc.store.all_requests(), key=lambda x: (x[0] == wid, x[1].get("created") or 0), reverse=True)
        for w, r in order:
            prod = (r.get("product") or {}).get("path") or ""
            cands = ([prod] if prod and os.path.isfile(prod) else []) + \
                [os.path.join(svc.store.dir(w), a["path"]) for a in reversed(r.get("attachments") or [])]
            for c in cands:
                if not ext or c.lower().endswith("." + ext):
                    return c
        raise FileNotFoundError("no attached file" + (f" ending in .{ext}" if ext else ""))
    from .files import user_path, search_files
    p = user_path(s)
    if p.exists():
        return str(p)
    hits = (search_files(os.path.basename(s), limit=1).get("data") or {}).get("files") or []
    if hits:
        return hits[0]
    raise FileNotFoundError(s)


def ws_new(name: str = "", **_) -> dict:
    try:
        svc = _svc()
        ws = svc.store.create((name or "").strip())
        svc._changed()
    except Exception as e:
        return fail(f"I couldn't create the workspace, sir: {e}", str(e))
    return ok(f"A new workspace, '{ws['name']}', is ready, sir. Tell me what to build in it.",
              {"speak_ko": f"새 워크스페이스 '{ws['name']}'을(를) 만들었습니다, sir. 무엇을 만들지 말씀해 주세요.",
               "workspace": ws["id"], "ui": "workspaces"})


def ws_switch(name: str = "", **_) -> dict:
    try:
        svc = _svc()
        ws = svc.store.find(name)
        if ws is None:
            return fail(f"I don't have a workspace called {name}, sir.", "not found")
        svc.store.select(ws["id"])
        svc._changed()
    except Exception as e:
        return fail(f"I couldn't switch workspaces, sir: {e}", str(e))
    return ok(f"Switched to the workspace '{ws['name']}', sir.",
              {"speak_ko": f"'{ws['name']}' 워크스페이스로 전환했습니다, sir.", "workspace": ws["id"], "ui": "workspaces"})


def ws_status(**_) -> dict:
    try:
        svc = _svc()
        return ok(svc.status_text("en"), {"speak_ko": svc.status_text("ko"), "ui": "workspaces"})
    except Exception as e:
        return fail(f"I couldn't read the workspaces, sir: {e}", str(e))


def ws_share(mode: str = "public", **_) -> dict:
    try:
        links = _svc().share(mode)
    except PermissionError as e:
        return fail(f"I'll hold off, sir: {e}.", str(e))
    except Exception as e:
        return fail(f"I couldn't share it, sir: {e}", str(e))
    if mode == "stop":
        return ok("Sharing stopped, sir; only this PC can open it now.",
                  {"speak_ko": "공유를 중지했습니다, sir. 이제 이 PC에서만 열 수 있습니다.", "links": links})
    link = links.get("public") if mode != "lan" else links.get("lan")
    return ok(f"Here's the link, sir: {link}", {"speak_ko": f"링크입니다, sir: {link}", "links": links,
                                                "show": link})


def _control(action: str):
    def fn(**_) -> dict:
        try:
            svc = _svc()
            wid = svc.store.selected()
            if action == "undo":
                r = svc.undo(wid)
                return ok("Done, sir: the last request is undone.",
                          {"speak_ko": "마지막 요청을 되돌렸습니다, sir.", "ui": "workspaces"})
            r = {"pause": svc.scheduler.pause, "resume": svc.scheduler.resume, "stop": svc.scheduler.stop_request}[action](wid or "")
            if r is None:
                return fail("There's nothing to " + action + " in this workspace, sir.", "nothing")
        except Exception as e:
            return fail(f"I couldn't {action} it, sir: {e}", str(e))
        en = {"pause": "Paused, sir. Say 'resume' when you want me to carry on.", "resume": "Carrying on, sir.",
              "stop": "Stopped, sir."}[action]
        ko = {"pause": "일시정지했습니다, sir. '계속해'라고 하시면 이어서 하겠습니다.", "resume": "계속하겠습니다, sir.",
              "stop": "중지했습니다, sir."}[action]
        return ok(en, {"speak_ko": ko, "ui": "workspaces"})
    return fn


ws_pause = _control("pause")
ws_resume = _control("resume")
ws_stop = _control("stop")
ws_undo = _control("undo")
