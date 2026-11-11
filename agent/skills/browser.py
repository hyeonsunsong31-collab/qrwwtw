"""Browser / web skills (requirement 20). Uses the stdlib webbrowser (cross-platform)."""
from __future__ import annotations

import urllib.parse
import webbrowser

from .base import ok, fail


def _normalize(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def open_url(url: str, **_) -> dict:
    u = _normalize(url)
    if not u:
        return fail("Which address, sir?")
    try:
        webbrowser.open(u, new=2)
        return ok(f"Opening {u}, sir.", {"url": u})
    except Exception as e:
        return fail(f"I couldn't open {u}, sir.", str(e))


def web_search(query: str, engine: str = "google", **_) -> dict:
    q = (query or "").strip()
    if not q:
        return fail("What should I search for, sir?")
    base = {"google": "https://www.google.com/search?q=",
            "bing": "https://www.bing.com/search?q=",
            "duckduckgo": "https://duckduckgo.com/?q="}.get(engine, "https://www.google.com/search?q=")
    u = base + urllib.parse.quote(q)
    try:
        webbrowser.open(u, new=2)
        return ok(f"Searching the web for '{q}', sir.", {"url": u, "query": q})
    except Exception as e:
        return fail("I couldn't open the browser, sir.", str(e))


def _fetch_text(u: str) -> str:
    from playwright.sync_api import sync_playwright  # type: ignore
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            page = b.new_page()
            page.goto(u, timeout=30000)
            return page.inner_text("body")[:8000]
        finally:
            b.close()


def read_page(url: str, **_) -> dict:
    """Fetch readable text from a page with Playwright. The browser engine (~150 MB) is
    downloaded the first time this is used, which keeps the main install shorter."""
    u = _normalize(url)
    if not u:
        return fail("Which page should I read, sir?")
    try:
        import playwright  # type: ignore  # noqa: F401
    except Exception:
        return fail("Reading page contents needs Playwright, sir.", "playwright missing")
    try:
        text = _fetch_text(u)
    except Exception as e:
        if "Executable doesn't exist" not in str(e) and "playwright install" not in str(e):
            return fail(f"I couldn't read {u}, sir.", str(e))
        import subprocess
        import sys
        try:  # first use: fetch the browser engine, then retry once
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           capture_output=True, timeout=900)
            text = _fetch_text(u)
        except Exception as e2:
            return fail(f"I couldn't read {u}, sir.", str(e2))
    return ok(f"I've read {u}, sir.", {"url": u, "text": text})
