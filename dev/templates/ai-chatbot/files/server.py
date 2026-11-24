"""{{TITLE}} -- a chat page and an API that answers with the local language model (Ollama).
Runs with Python alone (no installs). Free and offline: the model runs on this PC."""
import json
import os
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
MODEL = os.environ.get("CHAT_MODEL", "qwen2.5:3b")
SYSTEM = "You are a friendly, helpful assistant. Answer briefly."
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def answer(messages):
    body = json.dumps({"model": MODEL, "stream": False,
                       "messages": [{"role": "system", "content": SYSTEM}] + messages}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    with NO_PROXY.open(req, timeout=300) as r:
        return json.loads(r.read())["message"]["content"]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(HERE, "public"), **k)

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404)
            return
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            reply = {"reply": answer(data.get("messages") or [])}
            code = 200
        except Exception as e:
            reply, code = {"error": f"the local language model is not reachable: {e}"}, 503
        out = json.dumps(reply).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
