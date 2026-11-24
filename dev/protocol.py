"""
dev/protocol.py -- how the coding model acts on the project.

Two ways, chosen automatically (and switched when one keeps failing):

  tools  native tool calls (Ollama `tools`): write_file, edit_file, read_file, list_files,
         delete_file, run, set_config, set_checks, note, done. Best with 7B and larger models.
  text   plain directives, easiest for small models -- whole files in fenced blocks:

            FILE: src/app.js
            ```js
            ...the complete file...
            ```
            EDIT: index.html
            <<<<<<< SEARCH
            <old lines>
            =======
            <new lines>
            >>>>>>> REPLACE
            RUN: npm install
            DELETE: old.js
            READ: package.json
            CONFIG: {"kind": "web", "run": "static"}
            CHECKS: [{"criterion": 1, "type": "web", "steps": [...]}]
            NOTE: the counter lives in app.js
            DONE

parse_text() is forgiving: "File: `x`", "### x", a fence whose info string is the path
("```python main.py", "```html:index.html") all work.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

_S = {"type": "string"}


def _tool(name: str, desc: str, props: Dict[str, dict], required: List[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _tool("write_file", "Create or replace a file with its COMPLETE content (never abbreviated).",
          {"path": {**_S, "description": "path inside the project, e.g. src/main.py"},
           "content": {**_S, "description": "the whole file"}}, ["path", "content"]),
    _tool("edit_file", "Replace one exact piece of an existing file.",
          {"path": _S, "search": {**_S, "description": "exact existing text"},
           "replace": {**_S, "description": "what it becomes"}}, ["path", "search", "replace"]),
    _tool("read_file", "Read a file of the project.", {"path": _S}, ["path"]),
    _tool("list_files", "List the project's files.", {"path": {**_S, "description": "folder, default ."}}, []),
    _tool("delete_file", "Delete a file or folder of the project.", {"path": _S}, ["path"]),
    _tool("run", "Run a shell command in the project folder (install packages, build, run tests, try a "
                 "command-line program). Not for servers or windowed programs: JARVIS starts those itself.",
          {"command": _S, "timeout": {"type": "integer", "description": "seconds, default 120"}}, ["command"]),
    _tool("set_config", "Write jarvis.json: how to build, test and run the project.",
          {"kind": {**_S, "enum": ["web", "game-web", "cli", "gui", "game-desktop", "api", "library", "file", "gba", "other"]},
           "build": {"type": "array", "items": _S}, "test": {"type": "array", "items": _S},
           "run": {**_S, "description": "command that starts it, or 'static' for a plain web page"},
           "web_root": {**_S, "description": "folder that holds index.html (web)"},
           "product": {**_S, "description": "the deliverable (file or folder)"}}, ["kind"]),
    _tool("set_checks", "Set the checks that prove the acceptance criteria (replaces the previous list).",
          {"checks": {"type": "array", "items": {"type": "object"},
                      "description": 'e.g. [{"criterion": 1, "type": "web", "steps": [{"click": "#add"}, '
                                     '{"expect_text": "1"}]}]'}}, ["checks"]),
    _tool("note", "Remember one short line for the next steps.", {"text": _S}, ["text"]),
    _tool("install_tool", "Install a missing compiler or runtime for the whole PC (the user is asked first).",
          {"tool": {**_S, "description": "node, git, dotnet, java, rust, go, gcc, cmake, arm-none-eabi-gcc, ffmpeg, "
                                         "pandoc, 7zip, libreoffice, mgba, ilspycmd, pyinstaller"}}, ["tool"]),
    _tool("done", "Everything is written: ask JARVIS to check the whole program now.",
          {"summary": {**_S, "description": "what you built or changed"}}, []),
]

CHECK_FORMATS = (
    'Checks prove one acceptance criterion each (by its number):\n'
    '  {"criterion": 1, "type": "cli", "args": "--celsius 100", "stdin": "", "expect": ["212"]}\n'
    '  {"criterion": 2, "type": "web", "steps": [{"click": "#add"}, {"expect_text": "1"}]}\n'
    '     web steps: click <selector>, fill [<selector>, <text>], press <key>, wait <ms>, expect_text <text>,\n'
    '     expect_visible <selector>, expect_count [<selector>, n], expect_changed true (canvas/page changed)\n'
    '  {"criterion": 3, "type": "http", "method": "GET", "path": "/api/items", "expect": ["id"]}\n'
    '  {"criterion": 4, "type": "file", "path": "dist/report.pdf", "format": "pdf"}\n'
    '  {"criterion": 5, "type": "run", "command": "python -m pytest -q", "expect": ["passed"]}')

TEXT_FORMAT = (
    "Reply ONLY with directives, one after another. Prefer FILE (the whole file); use EDIT only for a small change in a "
    "big file. When a page needs new elements, write the HTML and its script in the same reply.\n"
    "FILE: <path>\n```<language>\n<the complete file>\n```\n"
    "EDIT: <path>\n<<<<<<< SEARCH\n<exact existing lines>\n=======\n<new lines>\n>>>>>>> REPLACE\n"
    "RUN: <one shell command>\n"
    "DELETE: <path>\n"
    "READ: <path>          (its content is shown to you next step)\n"
    'CONFIG: {"kind": "...", "build": [...], "test": [...], "run": "...", "product": "..."}\n'
    "CHECKS: [<checks as JSON>]\n"
    "NOTE: <one line to remember>\n"
    "INSTALL: <tool>       (node, git, dotnet, java, rust, go, gcc, cmake, arm-none-eabi-gcc, ffmpeg, pandoc, 7zip; asks the user)\n"
    "DONE                  (when you believe everything works -- JARVIS then checks it)")


# ------------------------------------------------------------------ text protocol parser
_DIRECTIVE = re.compile(r"^\s*(?:[#*>\-\s]*)\b(FILE|EDIT|RUN|DELETE|READ|CONFIG|CHECKS|NOTE|DONE|VERIFY|INSTALL)\b\s*[:=]?\s*(.*)$",
                        re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([^\n`]*)$")
_CLOSE = re.compile(r"^\s*(```+|~~~+)\s*$")
# a protocol line written exactly as the format says (upper case, at the start of the line): inside a
# code block or an EDIT it means the model forgot to close the block -- the block ends there instead
# of swallowing the rest of the reply into the file (measured: a 3B model wrote "<<<<<<< HEAD" with
# no ">>>>>>> REPLACE", and app.js became "DONE / EDIT: index.html / ...")
_STRICT = re.compile(r"^(FILE|EDIT|RUN|DELETE|READ|CONFIG|CHECKS|NOTE|INSTALL)\s*:\s*\S|^(DONE|VERIFY)(\s|$)")
_MARKER = re.compile(r"^\s*(<{7}|={7}|>{7})")


def _is_text_doc(path: str) -> bool:
    return (path or "").lower().endswith((".md", ".markdown", ".txt", ".rst"))
_PATHLIKE = re.compile(r"^[\w./\\@+-]+\.[A-Za-z0-9]{1,8}$|^[\w./\\@+-]*(Makefile|Dockerfile|Procfile|LICENSE|README)$")


def _clean_path(p: str) -> str:
    p = p.strip().strip("`*'\"").strip()
    p = re.sub(r"^\(?(path|file)\s*[:=]\s*", "", p, flags=re.I)
    p = p.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p.strip()


def _json_after(lines: List[str], i: int, first: str):
    """A JSON value starting on line i (after a directive), possibly fenced or multi-line."""
    text = first.strip()
    j = i + 1
    if not text or text.startswith("```"):
        if text.startswith("```"):
            text = ""
        buf = []
        while j < len(lines):
            ln = lines[j]
            if _FENCE.match(ln) and buf:
                j += 1
                break
            if _FENCE.match(ln):
                j += 1
                continue
            if _DIRECTIVE.match(ln) and buf and not ln.strip().startswith(("{", "[", '"')):
                break
            buf.append(ln)
            j += 1
        text = "\n".join(buf)
    else:
        depth = text.count("{") + text.count("[") - text.count("}") - text.count("]")
        while depth > 0 and j < len(lines):
            text += "\n" + lines[j]
            depth += lines[j].count("{") + lines[j].count("[") - lines[j].count("}") - lines[j].count("]")
            j += 1
    try:
        return json.loads(text), j
    except ValueError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            try:
                return json.loads(m.group(1)), j
            except ValueError:
                pass
    return None, j


def parse_text(reply: str) -> List[dict]:
    """Directives -> actions: write / edit / run / delete / read / config / checks / note / done."""
    lines = (reply or "").replace("\r\n", "\n").split("\n")
    actions: List[dict] = []
    i = 0
    pending_path: Optional[str] = None
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            info = fence.group(2).strip()
            path = pending_path
            # ```python main.py   ```html:index.html   ```index.html
            m = re.match(r"^[\w+#-]*[\s:]+([^\s]+)$", info) or (re.match(r"^([^\s]+)$", info) if _PATHLIKE.match(info) else None)
            if m and _PATHLIKE.match(_clean_path(m.group(1))):
                path = _clean_path(m.group(1))
            body: List[str] = []
            i += 1
            doc = _is_text_doc(path or "")
            while i < len(lines) and not _CLOSE.match(lines[i]) and (doc or not _STRICT.match(lines[i])):
                body.append(lines[i])
                i += 1
            if i < len(lines) and _CLOSE.match(lines[i]):
                i += 1                              # (a protocol line that ended the block is read next)
            if path:
                actions.append({"type": "write", "path": path, "content": "\n".join(body) + "\n"})
            pending_path = None
            continue
        d = _DIRECTIVE.match(line)
        if d:
            kind, rest = d.group(1).upper(), d.group(2).strip()
            if kind == "FILE":
                pending_path = _clean_path(rest) or None
            elif kind == "EDIT":
                path = _clean_path(rest)
                j = i + 1
                while j < len(lines) and not lines[j].lstrip().startswith("<<<<<<<"):
                    if _DIRECTIVE.match(lines[j]) and not lines[j].lstrip().startswith(("```", "<<<")):
                        break
                    j += 1
                doc = _is_text_doc(path)
                while j < len(lines) and lines[j].lstrip().startswith("<<<<<<<"):
                    search, replace, k, mode, closed = [], [], j + 1, "s", False
                    while k < len(lines):
                        s = lines[k].strip()
                        if s.startswith("=======") and mode == "s":
                            mode = "r"
                        elif s.startswith(">>>>>>>"):
                            closed = True
                            break
                        elif s.startswith("<<<<<<<") or (not doc and (_STRICT.match(lines[k]) or _CLOSE.match(lines[k]))):
                            break                   # ">>>>>>> REPLACE" forgotten: the block ends here
                        elif mode == "s":
                            search.append(lines[k])
                        else:
                            replace.append(lines[k])
                        k += 1
                    if mode == "r" and search:
                        actions.append({"type": "edit", "path": path, "search": "\n".join(search),
                                        "replace": "\n".join(replace)})
                    j = k + 1 if closed else k
                    while j < len(lines) and lines[j].strip() in ("", "```"):
                        j += 1
                i = j
                continue
            elif kind == "RUN":
                cmd = rest.strip("`").strip()
                if cmd:
                    actions.append({"type": "run", "command": cmd})
            elif kind == "DELETE":
                if rest:
                    actions.append({"type": "delete", "path": _clean_path(rest)})
            elif kind == "READ":
                if rest:
                    actions.append({"type": "read", "path": _clean_path(rest)})
            elif kind in ("CONFIG", "CHECKS"):
                value, j = _json_after(lines, i, rest)
                if value is not None:
                    actions.append({"type": "config" if kind == "CONFIG" else "checks", "value": value})
                i = j
                continue
            elif kind == "NOTE":
                if rest:
                    actions.append({"type": "note", "text": rest[:300]})
            elif kind == "INSTALL":
                if rest:
                    actions.append({"type": "install", "tool": rest.strip("`").strip()})
            elif kind == "DONE":
                actions.append({"type": "done", "summary": rest[:400]})
            elif kind == "VERIFY":
                actions.append({"type": "verify"})
            i += 1
            continue
        stripped = line.strip()
        if stripped.startswith("$ ") and len(stripped) > 2:
            actions.append({"type": "run", "command": stripped[2:].strip()})
        elif re.match(r"^(###|\*\*)\s*`?[\w./\\-]+\.[A-Za-z0-9]{1,8}`?\s*(\*\*)?\s*:?$", stripped):
            pending_path = _clean_path(re.sub(r"^(###|\*\*)|\*\*|:$", "", stripped))
        i += 1
    return actions


# ------------------------------------------------------------------ tool calls -> actions
def from_tool_calls(calls: List[dict]) -> List[dict]:
    out: List[dict] = []
    for c in calls or []:
        name = (c.get("name") or "").lower()
        a = c.get("arguments") or {}
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except ValueError:
                a = {"content": a}
        if name in ("write_file", "create_file", "write"):
            if a.get("path"):
                out.append({"type": "write", "path": _clean_path(str(a["path"])), "content": str(a.get("content", ""))})
        elif name in ("edit_file", "edit", "replace_in_file"):
            out.append({"type": "edit", "path": _clean_path(str(a.get("path", ""))), "search": str(a.get("search", "")),
                        "replace": str(a.get("replace", ""))})
        elif name in ("read_file", "read", "open_file"):
            out.append({"type": "read", "path": _clean_path(str(a.get("path", "")))})
        elif name in ("list_files", "ls", "list"):
            out.append({"type": "list", "path": str(a.get("path") or ".")})
        elif name in ("delete_file", "delete", "remove_file"):
            out.append({"type": "delete", "path": _clean_path(str(a.get("path", "")))})
        elif name in ("run", "run_command", "shell", "execute", "bash", "terminal"):
            if a.get("command"):
                out.append({"type": "run", "command": str(a["command"]), "timeout": a.get("timeout")})
        elif name in ("set_config", "config", "write_config"):
            out.append({"type": "config", "value": a})
        elif name in ("set_checks", "checks", "add_checks"):
            out.append({"type": "checks", "value": a.get("checks", a)})
        elif name == "note":
            out.append({"type": "note", "text": str(a.get("text", ""))[:300]})
        elif name in ("install_tool", "install"):
            out.append({"type": "install", "tool": str(a.get("tool") or a.get("name") or "")})
        elif name in ("done", "finish", "complete", "submit"):
            out.append({"type": "done", "summary": str(a.get("summary", ""))[:400]})
        else:
            out.append({"type": "unknown", "name": name})
    return out
