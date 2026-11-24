"""Record the multi-agent segment of the demo: a walkthrough of a recorded reviewer run in the
dashboard. It replays a saved trace (no API calls), so it costs nothing and shows exactly what the
agents did. Writes docs/demo/multi-segment.mp4 and multi-scenes.json (scene start times).

    uv run --with playwright==1.48.0 --with imageio-ffmpeg python docs/demo/record_multi.py
"""

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
sys.path.insert(0, str(OUT))
from record_demo import SIZE, caption  # noqa: E402  (same captions as the main recording)

RUN_ID = "20261123-110126-pydantic-cli"  # multi-agent (langgraph-review) run, Claude Haiku
BASE = "http://127.0.0.1:8767"
SCENES: list[dict] = []
_T0 = 0.0


def mark(scene: str) -> None:
    SCENES.append({"scene": scene, "at": round(time.monotonic() - _T0, 2)})


def show(page: Page, locator, offset: int = -140) -> None:
    """Scroll an element into view, a little below the top, smoothly enough to follow."""
    # To the top of the view, then back by -offset px: "if needed" would park an element that's
    # below the screen at the bottom edge, and the offset would push it out of view.
    locator.evaluate("el => el.scrollIntoView({block: 'start'})")
    page.mouse.wheel(0, offset)
    page.wait_for_timeout(500)


def tool_events(page: Page, name: str):
    """The reviewer's calls to one tool, matched on the tool-name label itself (a findings text
    that merely mentions the tool must not match)."""
    return page.locator(".event-reviewer").filter(has=page.locator(".tool-name", has_text=re.compile(f"^{name}$")))


def main() -> None:
    global _T0
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "dashboard.api:app", "--port", "8767"], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/api/runs", timeout=5)
                break
            except Exception:
                time.sleep(1)
        raw = OUT / "raw-multi"
        shutil.rmtree(raw, ignore_errors=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport=SIZE, record_video_dir=str(raw), record_video_size=SIZE,
                                          color_scheme="dark")
            page = context.new_page()
            _T0 = time.monotonic()

            page.goto(f"{BASE}/runs/{RUN_ID}")
            page.get_by_role("button", name="Timeline").first.wait_for()
            mark("multi_intro")
            caption(page, "Multi-agent mode: a recorded run on pydantic-cli, the task where the single agent "
                          "always left old code behind")
            page.wait_for_timeout(6000)

            page.get_by_role("button", name="Timeline").first.click()
            page.wait_for_timeout(800)
            verification = page.locator(".test-card", has_text="Verification").first
            show(page, verification, -260)
            mark("multi_verified")
            caption(page, "The migrator passes all 35 tests. A reviewer agent then starts, with a fresh context "
                          "and read-only tools")
            page.wait_for_timeout(7000)

            find = tool_events(page, "find_deprecations").first
            find.locator("summary").first.click()
            page.wait_for_timeout(400)
            show(page, find.locator("pre").last, -40)  # the result: the deprecated call sites
            mark("multi_find")
            caption(page, "find_deprecations: 10 deprecated call sites still in examples/, invisible to the tests")
            page.wait_for_timeout(7000)

            show(page, page.locator(".review-card").first, -120)
            mark("multi_findings")
            caption(page, "Review 1: nine findings, each with file:line and the fix, go back to the migrator")
            page.wait_for_timeout(8000)

            show(page, page.locator(".test-card", has_text="Verification").nth(1), -200)
            mark("multi_fixed")
            caption(page, "The migrator fixes them and must pass verification again: 35/35")
            page.wait_for_timeout(5000)

            second = tool_events(page, "find_deprecations").nth(1)
            second.locator("summary").first.click()
            page.wait_for_timeout(400)
            show(page, second.locator("pre").last, -300)  # its result: no call sites left
            mark("multi_zero")
            caption(page, "The reviewer checks again: 0 deprecated call sites. Single agent on this task: "
                          "10 to 29 left, every run")
            page.wait_for_timeout(8000)
            mark("multi_done")

            video = page.video.path()
            context.close()
            browser.close()
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(video), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-r", "25", "-crf", "26", "-preset", "slow", str(OUT / "multi-segment.mp4")],
                       check=True)
        shutil.rmtree(raw, ignore_errors=True)
        (OUT / "multi-scenes.json").write_text(json.dumps({"run_id": RUN_ID, "scenes": SCENES}, indent=1), encoding="utf-8")
        print(json.dumps(SCENES))
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
