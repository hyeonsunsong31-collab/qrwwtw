"""
dev/service.py -- the developer inside the JARVIS server.

  * workspaces and their stacked requests (HUD messages ws_*, and voice / text through the skills)
  * one job at a time (scheduler) with the coding model chosen for this PC (models.py), downloaded
    the first time with progress in the HUD
  * permission prompts from the worker thread go through the server's own asker, so they look and
    work like every other JARVIS prompt (spoken, answered by voice, text or click)
  * when a job finishes: the deliverable is registered with the recorder, a web product gets its
    link, and JARVIS says so; progress notices every ~10 minutes while it works
  * conversions ("convert X to Y") run at once when a converter exists, otherwise as a coding job
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
import traceback
from typing import Dict, List, Optional

from . import convert, models, toolchains
from .agent import DevAgent, Job
from .history import History
from .scheduler import Scheduler
from .serve import Links
from .tools import Sandbox
from .verify import Verifier
from .workspaces import WorkspaceStore

IS_WINDOWS = sys.platform.startswith("win")
CONVERSIONS = "Conversions"


class DevService:
    def __init__(self, server, data_dir: str, config: dict):
        self.server = server
        self.data_dir = data_dir
        self.cfg = dict((config or {}).get("dev") or {})
        self.store = WorkspaceStore(os.path.join(data_dir, "workspaces"))
        self.links = Links(data_dir, log=lambda m: self._log("links", m))
        self.scheduler = Scheduler(self.store, self._run_job, on_change=self._changed, log=lambda m: self._log("scheduler", m))
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.coder_name: Optional[str] = None
        self.coder_ctx = 8192
        self.coder_why = ""
        self.pull: Optional[dict] = None
        self._lent = False
        self._toolchains = ""
        convert.TOOLS_DIR = os.path.join(data_dir, "tools")

    # ------------------------------------------------------------ life cycle
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        if self.enabled():
            self.scheduler.start()
            threading.Thread(target=self._detect_tools, daemon=True).start()

    def _detect_tools(self):
        try:
            self._toolchains = toolchains.summary()
        except Exception:
            self._toolchains = "python"

    def stop(self) -> None:
        try:
            self.scheduler.stop()
        finally:
            self.links.stop_all()

    def reconfigure(self, config: dict) -> None:
        self.cfg = dict((config or {}).get("dev") or {})

    # ------------------------------------------------------------ thread-safe plumbing
    def _log(self, kind: str, text: str) -> None:
        print(f"[jarvis] developer {kind}: {text}"[:600], flush=True)

    def _run_coro(self, coro, wait: bool = False, timeout: float = 160.0):
        loop = self.loop
        if loop is None or loop.is_closed():
            coro.close()
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        if wait:
            return fut.result(timeout=timeout)
        return fut

    def _emit(self, ev: dict) -> None:
        """From the worker thread: to the HUD; spoken notices through the server."""
        if ev.get("type") == "dev_notice":
            lang = self.server.voice_lang()
            self._run_coro(self.server.notify(ev.get("ko") if lang == "ko" else ev.get("en"), lang))
            return
        self._run_coro(self.server.manager.broadcast(ev))

    def _changed(self) -> None:
        self._run_coro(self.server.manager.broadcast({"type": "ws_state", **self.state()}))

    def ask(self, category: str, detail: str, force: bool = False, risk: Optional[float] = None) -> bool:
        """A permission prompt from the worker thread (blocks it until answered)."""
        from agent.permissions import PermissionRequest
        lang = self.server.voice_lang()
        q = _question(category, detail, lang)
        req = PermissionRequest(action=f"developer: {category}", category=category, detail=detail, risk=risk or 0.0,
                                force_ask=force, keywords=False, question=q, lang=lang)
        try:
            return bool(self._run_coro(self.server.permissions.check(req), wait=True))
        except Exception:
            return False

    # ------------------------------------------------------------ state for the HUD
    def state(self) -> dict:
        return {"workspaces": self.store.list(), "selected": self.store.selected(), "busy": self.scheduler.busy(),
                "coder": self.coder_name, "coder_why": self.coder_why, "pull": self.pull,
                "links": {w["id"]: self.links.links(w["id"]) for w in self.store.list() if self.links.links(w["id"])}}

    def brief(self) -> dict:
        busy = self.scheduler.busy()
        return {"enabled": self.enabled(), "workspaces": len(self.store.list()), "busy": busy, "coder": self.coder_name}

    # ------------------------------------------------------------ the coding model
    def coder(self, job: Optional[Job] = None):
        from agent.llm_ollama import OllamaLLM
        base = self.server.llm.service.base
        probe = OllamaLLM(base, "x")
        installed = probe.local_models()
        want = str(self.cfg.get("model") or "auto-largest")
        ctx = 8192
        if want in ("auto", "auto-largest", ""):
            want, ctx, why = models.pick()
        else:
            ctx = int(self.cfg.get("context_tokens") or 16384)
            why = "chosen in Settings"
        have = {n.lower() for n in installed}
        if want.lower() not in have and want.lower() + ":latest" not in have:
            try:
                self._pull(probe, want, job)
            except Exception as e:
                self._log("model", f"could not download {want}: {e}")
                fallback = models.installed_coder(installed) or self.server.llm.ollama.model
                why = f"{want} could not be downloaded ({e}); using {fallback}"
                want = fallback
        self.coder_name, self.coder_ctx, self.coder_why = want, ctx, why
        llm = OllamaLLM(base, want, keep_alive=str(self.cfg.get("keep_alive") or "15m"), num_ctx=ctx, timeout=1800)
        return llm, want, ctx

    def _pull(self, probe, name: str, job: Optional[Job]):
        self._log("model", f"downloading the coding model {name}")
        self.pull = {"model": name, "percent": 0.0}
        self._changed()
        last = [0.0]

        def progress(p):
            self.pull = {"model": name, "percent": p.get("percent", 0.0)}
            if time.time() - last[0] > 2:
                last[0] = time.time()
                self._emit({"type": "dev_progress", "phase": "downloading",
                            "activity": f"downloading the coding model {name}: {p.get('percent', 0):.0f}%"})
                self._changed()
            if job is not None and job.stopped():
                raise RuntimeError("stopped")
        try:
            probe.pull(name, on_progress=progress)
        finally:
            self.pull = None
            self._changed()

    def _lend_if_tight(self, llm, name: str, ctx: int) -> None:
        """When the chat model and the coding model do not fit side by side, JARVIS answers chat
        with the coding model while a job runs (no swapping models in and out of memory)."""
        mode = str(self.cfg.get("share_model_for_chat") or "auto")
        if mode == "never" or not hasattr(self.server.llm, "lend"):
            return
        if mode == "auto":
            from agent.llm_ollama import MODEL_GB
            mem = models.memory()
            budget = mem.get("vram_free", 0) + mem.get("ram_available", 0) - models.RESERVE_GB
            chat_gb = MODEL_GB.get(self.server.llm.ollama.model, 3.0) * 1.15
            if budget >= models.need_gb(name, ctx) + chat_gb:
                return
        self.server.llm.lend(llm)
        self._lent = True
        self._log("model", f"answering chat with {name} while the job runs (memory is shared)")

    def _unlend(self) -> None:
        if self._lent and hasattr(self.server.llm, "lend"):
            self.server.llm.lend(None)
        self._lent = False

    # ------------------------------------------------------------ jobs
    def _run_job(self, wid: str, req: dict, job: Job) -> dict:
        ws = self.store.get(wid) or {}
        p = self.store.paths(wid)
        rid = req["id"]
        log = lambda kind, text: self.store.log(wid, rid, kind, text)
        sb = Sandbox(p["root"], p["project"], ask=self.ask, log=log, should_stop=job.stopped, data_dir=self.data_dir,
                     mem_limit_mb=float(self.cfg.get("memory_limit_mb") or 0))
        hist = History(p["project"], os.path.join(p["root"], "snapshots"))
        try:
            base = hist.begin(rid)
            self.store.update_request(wid, rid, base=base)
        except Exception as e:
            log("error", f"could not start the project history: {e}")
        ver = Verifier(sb, self.store.request_dir(wid, rid), log=log)
        self._emit({"type": "dev_progress", "workspace": wid, "request": rid, "phase": "starting",
                    "activity": "starting"})
        try:
            if req.get("kind") == "convert":
                quick = self._convert_job(wid, req, log)
                if quick is not None:
                    return quick
            llm, name, ctx = self.coder(job)
            self._lend_if_tight(llm, name, ctx)
            try:
                agent = DevAgent(llm, self.store, wid, rid, sb, ver, job, self._emit, cfg=self.cfg, history=hist,
                                 log=log, toolchains=self._toolchains or "python", num_ctx=ctx, model_name=name,
                                 protocol_mode=str(self.cfg.get("protocol") or "auto"))
                result = agent.run()
            finally:
                self._unlend()
                sb.stop_all()
            if result.get("status") == "done":
                self._deliver(wid, rid, result.get("product") or {})
            else:
                self._say_result(wid, rid, result)
            return result
        except Exception as e:
            log("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1200:]}")
            return {"status": "failed", "summary": f"{type(e).__name__}: {e}"}

    def _convert_job(self, wid: str, req: dict, log) -> Optional[dict]:
        meta = req.get("meta") or {}
        p = self.store.paths(wid)
        src = meta.get("source") or ""
        if not src and req.get("attachments"):
            src = os.path.join(p["root"], req["attachments"][0]["path"])
        if not src or not os.path.exists(src):
            return {"status": "failed", "summary": "there is no file to convert (attach it, or say its full path)"}
        ext = str(meta.get("target") or "").lstrip(".").lower()
        stem = os.path.splitext(os.path.basename(src))[0]
        dest = os.path.join(p["output"], f"{stem}.{ext}")
        log("convert", f"{os.path.basename(src)} -> .{ext}")
        r = convert.convert(src, dest, ask=self.ask, log=lambda m: log("convert", m))
        if r.get("ok"):
            product = {"kind": "file", "title": os.path.basename(dest), "path": r["path"], "note": r.get("message", "")}
            summary = f"Converted {os.path.basename(src)} to {os.path.basename(dest)}: {r.get('message', '')}"
            self.store.update_request(wid, req["id"], status="done", finished=time.time(), product=product,
                                      summary=summary, checklist=[{"id": 1, "text": f"a valid .{ext} file", "status": "pass",
                                                                   "checks": [{"type": "file", "format": ext}]}])
            self.store.update(wid, product=product, status="idle")
            self._deliver(wid, req["id"], product)
            return {"status": "done", "summary": summary, "product": product}
        if not r.get("fallback"):
            self.store.update_request(wid, req["id"], status="failed", summary=r.get("message"), error=r.get("message"))
            self._say_result(wid, req["id"], {"status": "failed", "summary": r.get("message")})
            return {"status": "failed", "summary": r.get("message")}
        # no ready-made converter: the coding loop builds one and checks the output
        rel = os.path.relpath(src, p["root"]).replace("\\", "/")
        text = (f"{req.get('text') or ''}\nConvert the file {rel} (in the workspace folder, one level above the "
                f"project) into a .{ext} file saved as out/{stem}.{ext} inside the project. Write a program that does the "
                f"conversion, run it, and make sure the result really is a valid .{ext} file. "
                f'jarvis.json: kind "file", run = the conversion command, product = "out/{stem}.{ext}".').strip()
        self.store.update_request(wid, req["id"], text=text, kind="build")
        log("convert", r.get("message", ""))
        return None

    def _deliver(self, wid: str, rid: str, product: dict) -> None:
        req = self.store.get_request(wid, rid) or {}
        serve = product.get("serve")
        if serve:
            try:
                product["links"] = self.links.serve(wid, **serve)
            except Exception as e:
                product["serve_error"] = str(e)
        if product.get("path") and os.path.exists(product["path"]):
            try:
                self.server.recorder.register_file(product["path"], note=f"developer workspace product ({wid})")
            except Exception:
                pass
        self.store.update_request(wid, rid, product=product)
        self.store.update(wid, product=product)
        self._emit({"type": "dev_product", "workspace": wid, "request": rid, "product": product,
                    "summary": req.get("summary", "")})
        self._changed()
        title = product.get("title") or "the product"
        lang = self.server.voice_lang()
        if product.get("links"):
            link = product["links"].get("local")
            en = f"{title} is ready and checked, sir. It's open for you at {link}."
            ko = f"{title}이(가) 완성되어 확인까지 마쳤습니다, sir. {link} 에서 열 수 있습니다."
        else:
            name = os.path.basename(product.get("path") or "") or title
            en = f"{title} is ready and checked, sir: {name} is in the workspace's output folder."
            ko = f"{title}이(가) 완성되어 확인까지 마쳤습니다, sir: {name} 파일이 워크스페이스의 output 폴더에 있습니다."
        self._run_coro(self.server.notify(ko if lang == "ko" else en, lang))

    def _say_result(self, wid: str, rid: str, result: dict) -> None:
        status = result.get("status")
        if status == "stopped":
            en, ko = "I've stopped working on it, sir.", "작업을 멈췄습니다, sir."
        else:
            msg = result.get("summary") or "it could not be finished"
            en, ko = f"The request could not be carried out, sir: {msg}", f"요청을 수행하지 못했습니다, sir: {msg}"
        lang = self.server.voice_lang()
        self._changed()
        self._run_coro(self.server.notify(ko if lang == "ko" else en, lang))

    # ------------------------------------------------------------ actions (skills + HUD)
    def _ensure_workspace(self, name: str = "", hint: str = "") -> dict:
        wid = self.store.selected()
        ws = self.store.get(wid) if wid else None
        if ws is None or name:
            ws = self.store.create(name or _name_from(hint))
        return ws

    def request(self, text: str, attachments: Optional[List[str]] = None, workspace: str = "",
                kind: str = "build", meta: Optional[dict] = None, new: bool = False) -> dict:
        quiet = False
        if workspace:
            ws = self.store.find(workspace) or self.store.create(workspace)
        elif new:
            ws = self.store.create(_name_from(text or (attachments or [""])[0]))
        elif kind == "convert":
            # a file conversion has its own slot: it never replaces the product (and the link) of the
            # workspace you are working in, and that workspace stays selected
            ws = self.store.find(CONVERSIONS) or self.store.create(CONVERSIONS, select=False)
            quiet = True
        else:
            ws = self._ensure_workspace(hint=text or (os.path.basename((attachments or [""])[0])))
        if not quiet:
            self.store.select(ws["id"])
        req = self.scheduler.submit(ws["id"], text, attachments or [], kind=kind, meta=meta)
        return {"workspace": self.store.get(ws["id"]), "request": req,
                "position": self._position(ws["id"], req["id"])}

    def _position(self, wid: str, rid: str) -> int:
        queued = sorted(((w, r) for w, r in self.store.all_requests() if r["status"] in ("queued", "working")),
                        key=lambda x: x[1]["created"])
        for i, (w, r) in enumerate(queued):
            if r["id"] == rid:
                return i
        return 0

    def undo(self, wid: Optional[str] = None) -> dict:
        wid = wid or self.store.selected()
        if not wid:
            raise KeyError("no workspace is selected")
        reqs = [r for r in self.store.requests(wid) if r.get("status") == "done"]
        if not reqs:
            raise ValueError("there is no finished request to undo")
        last = reqs[-1]
        p = self.store.paths(wid)
        how = History(p["project"], os.path.join(p["root"], "snapshots")).undo(last.get("base"), last.get("commit"))
        self.store.update_request(wid, last["id"], status="undone")
        prev = next((r.get("product") for r in reversed(reqs[:-1]) if r.get("product")), None)
        self.store.update(wid, product=prev)
        self.links.stop(wid)
        if prev and prev.get("serve"):
            try:
                prev["links"] = self.links.serve(wid, **prev["serve"])
            except Exception:
                pass
        self._changed()
        return {"undone": last, "how": how}

    def share(self, mode: str, wid: Optional[str] = None) -> dict:
        wid = wid or self.store.selected()
        if not wid:
            raise KeyError("no workspace is selected")
        if mode == "stop":
            links = self.links.stop_sharing(wid)
        else:
            if not self.links.running(wid):
                ws = self.store.get(wid) or {}
                serve = (ws.get("product") or {}).get("serve")
                if not serve:
                    raise ValueError("this workspace has no web product to share")
                self.links.serve(wid, **serve)
            if mode == "lan":
                links = self.links.share_lan(wid)
            else:
                if not self.links.cloudflared():
                    if not self.ask("install_software", "Download Cloudflare's cloudflared (free, ~40 MB) to make a "
                                                        "public link", risk=0.5):
                        raise PermissionError("downloading cloudflared was not allowed")
                    self.links.download_cloudflared()
                links = self.links.share_public(wid)
        ws = self.store.get(wid) or {}
        product = dict(ws.get("product") or {}, links=links)
        self.store.update(wid, product=product)
        self._changed()
        return links

    def status_text(self, lang: str = "en") -> str:
        busy = self.scheduler.busy()
        if not busy:
            items = self.store.list()
            if not items:
                return ("There are no developer workspaces yet, sir. Ask me to build something, or say 'new workspace'."
                        if lang != "ko" else "아직 개발 워크스페이스가 없습니다, sir. 무엇을 만들지 말씀하시거나 '새 워크스페이스'라고 해 주세요.")
            queued = sum(w.get("counts", {}).get("queued", 0) for w in items)
            return (f"I'm not building anything right now, sir. {len(items)} workspace(s)" +
                    (f", {queued} request(s) waiting." if queued else ".")) if lang != "ko" else \
                f"지금은 작업 중인 것이 없습니다, sir. 워크스페이스 {len(items)}개" + (f", 대기 중인 요청 {queued}개." if queued else ".")
        req = self.store.get_request(busy["workspace"], busy["request"]) or {}
        ws = self.store.get(busy["workspace"]) or {}
        crit = req.get("checklist") or []
        passed = sum(1 for c in crit if c.get("status") == "pass")
        prog = req.get("progress") or {}
        mins = int((time.time() - (req.get("started") or time.time())) / 60)
        title = (req.get("meta") or {}).get("title") or ws.get("name")
        if lang == "ko":
            return (f"{ws.get('name')} 워크스페이스에서 {title} 작업 중입니다, sir: {mins}분째, 확인 항목 {len(crit)}개 중 "
                    f"{passed}개 통과. 지금: {prog.get('activity') or prog.get('phase') or '진행 중'}." +
                    (" (일시정지됨)" if busy.get("paused") else ""))
        return (f"I'm working on {title} in {ws.get('name')}, sir: {mins} minutes in, {passed} of {len(crit)} checks "
                f"pass. Right now: {prog.get('activity') or prog.get('phase') or 'working'}." +
                (" (paused)" if busy.get("paused") else ""))

    # ------------------------------------------------------------ HUD messages
    async def handle(self, msg: dict, ws=None) -> None:
        t = msg.get("type")
        send = lambda ev: self.server.manager.send(ws, ev)
        try:
            if t == "ws_list":
                await send({"type": "ws_state", **self.state()})
            elif t == "ws_get":
                d = await asyncio.to_thread(self.store.detail, str(msg.get("id") or self.store.selected() or ""))
                await send({"type": "ws_detail", "detail": d, "links": self.links.links(str(msg.get("id") or ""))})
            elif t == "ws_create":
                ws_ = self.store.create(str(msg.get("name") or ""))
                await send({"type": "ws_created", "workspace": ws_})
                self._changed()
            elif t == "ws_select":
                self.store.select(str(msg.get("id")))
                self._changed()
            elif t == "ws_rename":
                self.store.rename(str(msg.get("id")), str(msg.get("name") or ""))
                self._changed()
            elif t == "ws_delete":
                wid = str(msg.get("id"))
                self.scheduler.stop_workspace(wid)
                self.links.stop(wid)
                await asyncio.to_thread(self.store.delete, wid)
                self._changed()
            elif t == "ws_request":
                text = str(msg.get("text") or "").strip()
                atts = [str(a) for a in (msg.get("attachments") or []) if a][:20]
                if not text and not atts:
                    return
                from agent.nlu import parse_intent
                from dev.files import refused
                why = refused(text)
                if why:
                    await send({"type": "ws_error", "message": why})
                    return
                kind, meta = "build", None
                conv = parse_intent(text) if text else None
                if conv is not None and conv.name == "dev_convert":
                    kind, meta = "convert", {"target": conv.args.get("fmt", "").lstrip(".")}
                r = await asyncio.to_thread(self.request, text, atts, str(msg.get("workspace") or ""), kind, meta,
                                            bool(msg.get("new")))
                await send({"type": "ws_queued", **r})
                self._changed()
            elif t in ("ws_pause", "ws_resume", "ws_stop"):
                wid = str(msg.get("id") or self.store.selected() or "")
                fn = {"ws_pause": self.scheduler.pause, "ws_resume": self.scheduler.resume,
                      "ws_stop": self.scheduler.stop_request}[t]
                fn(wid, msg.get("request"))
            elif t == "ws_undo":
                r = await asyncio.to_thread(self.undo, str(msg.get("id") or "") or None)
                await send({"type": "ws_undone", "request": r["undone"]["id"], "how": r["how"]})
            elif t == "ws_share":
                links = await asyncio.to_thread(self.share, str(msg.get("mode") or "public"), str(msg.get("id") or "") or None)
                await send({"type": "ws_links", "id": msg.get("id"), "links": links})
        except Exception as e:
            await send({"type": "ws_error", "message": f"{type(e).__name__}: {e}"})


def _name_from(text: str) -> str:
    t = re.sub(r"[\\/]", " ", text or "").strip()
    t = re.sub(r"^(please\s+)?(build|make|create|develop|code|write|design|generate)\s+(me\s+)?(a|an|the|my)?\s*", "", t,
               flags=re.IGNORECASE)
    words = re.findall(r"[\w가-힣]+", t)[:4]
    name = " ".join(words).strip()[:40]
    return name[:1].upper() + name[1:] if name else ""


def _question(category: str, detail: str, lang: str) -> str:
    d = detail.splitlines()[0][:140] if detail else ""
    if lang == "ko":
        return {"dev_outside": f"워크스페이스 밖의 경로를 사용해도 될까요: {d}, sir?",
                "install_software": f"설치를 진행할까요: {d}, sir?",
                "network_share": f"공유를 시작할까요: {d}, sir?"}.get(category, f"진행할까요: {d}, sir?")
    return {"dev_outside": f"May I use a path outside the workspace -- {d}, sir?",
            "install_software": f"Shall I install it -- {d}, sir?",
            "network_share": f"Shall I share it -- {d}, sir?",
            "dev_workspace": f"This command is always confirmed -- {d}. Shall I run it, sir?"}.get(
        category, f"Shall I go ahead -- {d}, sir?")
