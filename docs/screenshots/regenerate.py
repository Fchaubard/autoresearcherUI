"""Capture screenshots of every dashboard tab for the README.

Targets the live public Cloudflare URL of the running pod, walks through
the SPA's URL routes, takes a full-page PNG of each, and lands them under
docs/screenshots/. Skips authkeys (has SSH info in it).
"""
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ARUI_BASE",
                      "https://distance-vegetables-laid-allied.trycloudflare.com")
OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

# (path, output filename, optional inline JS to nudge UI before snap)
SHOTS = [
    ("/dashboard",        "dashboard.png",        None),
    ("/analysis",         "analysis.png",         None),
    ("/lessons",          "lessons.png",          None),
    ("/write-paper",      "write-paper.png",      None),
    ("/system-stats",     "system-stats.png",     None),
]


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        for path, fn, prep in SHOTS:
            url = BASE.rstrip("/") + path
            print(f"  → {url}", flush=True)
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                print(f"    (load slow: {e}; continuing)")
            time.sleep(2.5)  # let SSE + chart render settle
            if prep:
                try:
                    page.evaluate(prep)
                    time.sleep(0.8)
                except Exception as e:
                    print(f"    prep failed: {e}")
            out = OUT / fn
            page.screenshot(path=str(out), full_page=True)
            print(f"    saved {out}  ({out.stat().st_size//1024}KB)")
        browser.close()


if __name__ == "__main__":
    main()
