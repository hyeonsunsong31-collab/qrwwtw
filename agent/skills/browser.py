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


def read_page(url: str, **_) -> dict:
    """Fetch readable text from a page (optional; needs Playwright). Degrades politely."""
    u = _normalize(url)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return fail("Reading page contents needs the Playwright browser, sir "
                    "(the installer sets it up).", "playwright missing")
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page()
            page.goto(u, timeout=30000)
            text = page.inner_text("body")[:8000]
            b.close()
        return ok(f"I've read {u}, sir.", {"url": u, "text": text})
    except Exception as e:
        return fail(f"I couldn't read {u}, sir.", str(e))
