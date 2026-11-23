"""Record the demo video automatically: a real agent run, watched live in the dashboard.

Starts the dashboard and one agent run (agate-sql, SQLAlchemy 1.4 -> 2.0, about $0.01), drives a
headless Chromium through the dashboard with captions, and writes docs/demo/demo-silent.mp4
(captions only) plus scenes.json; docs/demo/narrate.py then adds the voiceover as demo.mp4. Playwright and ffmpeg are only needed here, so they're pulled into a throwaway
environment instead of the project's dependencies:

    uv run --env-file .env --with playwright==1.48.0 --with imageio-ffmpeg python docs/demo/record_demo.py
    (first time only: uv run --with playwright==1.48.0 python -m playwright install chromium)
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmark.runner import reset_checkout  # noqa: E402
from benchmark.tasks import load_tasks  # noqa: E402

MODEL = "claude-haiku-5-5"
SPEED = 1.4  # installs and test runs are real waiting time; stated on the title card
OUT = Path(__file__).parent
BASE = "http://127.0.0.1:8765"
TASK = "agate-sql"
SIZE = {"width": 1280, "height": 720}

CAPTION_CSS = """
#demo-caption { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%); z-index: 99999;
  max-width: 1040px; padding: 12px 22px; border-radius: 10px; background: rgba(12,12,12,.88);
  color: #f3f2ec; font: 500 20px/1.4 system-ui, sans-serif; text-align: center;
  box-shadow: 0 6px 24px rgba(0,0,0,.45); border: 1px solid rgba(255,255,255,.14); }
"""

CARD = """<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
  background:#141413; color:#f3f2ec; font-family:system-ui,sans-serif; }}
.box {{ max-width: 980px; padding: 0 40px; }} h1 {{ font-size: 46px; margin: 0 0 18px; }}
p {{ font-size: 23px; line-height: 1.5; color:#c3c2b7; margin: 8px 0; }} b {{ color:#f3f2ec; }}
</style></head><body><div class="box">{body}</div></body></html>"""

TITLE = """<h1>Migration Agent</h1>
<p>An AI agent that upgrades real Python codebases across breaking library releases
(<b>Pydantic v1 → v2</b>, <b>SQLAlchemy 1.4 → 2.0</b>), and proves it with the project's own tests.</p>
<p>What follows is a real, unedited run, recorded automatically (played at 1.4× speed).</p>"""

END = """<h1>Results</h1>
<p><b>18</b> real open-source projects, two libraries</p>
<p><b>52 / 53</b> migrations succeeded &nbsp;·&nbsp; about <b>$0.025</b> per run &nbsp;·&nbsp; no test edited or skipped</p>
<p><b>92%</b> of successful runs leave no deprecated API behind (replayed and measured)</p>
<p>Held-out upstream tests: every failure traced to upstream design changes, none to the migration</p>
<p>Same results from a LangGraph rebuild, at twice the code</p>"""

# What the agent is doing -> what the caption says (keyed by tool name).
TOOL_CAPTIONS = {
    "search_docs": "Searching the official SQLAlchemy 2.0 migration guide (hybrid keyword + embedding search)",
    "read_file": "Reading the code it needs to change",
    "search_code": "Searching the repo for every call site of the old API",
    "list_files": "Exploring the repository",
    "edit_file": "Exact-text edits, only inside the repo. Test files are off-limits: the tool refuses them",
    "run_tests": "Running the tests in a Docker sandbox: results come back as a short structured summary",
    "finish": "Calling finish doesn't end the run: the loop re-runs the full suite and only accepts a pass",
}


def get(path: str) -> object:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


SCENES: list[dict] = []  # when each scene starts, in final-video seconds (for the voiceover)
_T0 = 0.0


def mark(scene: str) -> None:
    SCENES.append({"scene": scene, "at": round((time.monotonic() - _T0) / SPEED, 2)})


def caption(page: Page, text: str) -> None:
    page.evaluate("""([text, css]) => {
        if (!document.getElementById('demo-caption-style')) {
            const s = document.createElement('style'); s.id = 'demo-caption-style'; s.textContent = css;
            document.head.appendChild(s);
        }
        let el = document.getElementById('demo-caption');
        if (!el) { el = document.createElement('div'); el.id = 'demo-caption'; document.body.appendChild(el); }
        el.textContent = text;
    }""", [text, CAPTION_CSS])


def card(page: Page, body: str, seconds: float) -> None:
    page.set_content(CARD.format(body=body))
    page.wait_for_timeout(seconds * 1000)


def wait_for_server() -> None:
    for _ in range(60):
        try:
            get("/api/runs")
            return
        except Exception:
            time.sleep(1)
    raise SystemExit("dashboard did not start")


def main() -> None:
    known = set()
    facts: dict = {"task": TASK, "model": MODEL, "speed": SPEED}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "dashboard.api:app", "--port", "8765"], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    agent = None
    try:
        wait_for_server()
        known = {r["run_id"] for r in get("/api/runs")}
        video_dir = OUT / "raw"
        shutil.rmtree(video_dir, ignore_errors=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport=SIZE, record_video_dir=str(video_dir), record_video_size=SIZE,
                                          color_scheme="dark")
            page = context.new_page()
            global _T0
            _T0 = time.monotonic()  # the video starts with the page

            mark("title")
            card(page, TITLE, 7)

            # The run list, then the agent filter.
            page.goto(BASE + "/")
            page.wait_for_selector("table.runs")
            mark("runs")
            caption(page, "Every agent run is recorded: outcome, tests before → after, review checks, cost")
            page.wait_for_timeout(6000)
            page.get_by_role("button", name="LangGraph").click()
            mark("filter")
            caption(page, "Two implementations of the same agent: a hand-written loop and a LangGraph graph")
            page.wait_for_timeout(3500)
            page.get_by_role("button", name="All").first.click()
            page.wait_for_timeout(800)

            # Start a real run and open it as soon as it appears.
            mark("start")
            caption(page, f"Starting a live run: {TASK}, a real project pinned to SQLAlchemy 1.4, upgraded to 2.0")
            reset_checkout(load_tasks()[TASK])  # a previous recording's edits are kept in its diff.patch
            # The model all the README numbers are for, whatever .env defaults to.
            env = {**os.environ, "LLM_PROVIDER": "claude", "AGENT_CLAUDE_MODEL": MODEL}
            agent = subprocess.Popen([sys.executable, "-m", "agent.loop", "--task", TASK], cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run_id = None
            for _ in range(120):
                new = [r["run_id"] for r in get("/api/runs") if r["run_id"] not in known]
                if new:
                    run_id = new[0]
                    break
                page.wait_for_timeout(1000)
            if run_id is None:
                raise SystemExit("the agent run never appeared")
            page.goto(f"{BASE}/runs/{run_id}")
            page.get_by_role("button", name="Timeline").first.wait_for()
            page.get_by_role("button", name="Timeline").first.click()
            mark("baseline")
            caption(page, "Baseline: the project is installed in Docker with SQLAlchemy 2.0 and its tests run")

            # Follow the run: caption whatever the agent did last, keep the newest events in view.
            last_caption, said_baseline = None, False
            deadline = time.time() + 12 * 60
            while time.time() < deadline:
                run = get(f"/api/runs/{run_id}")
                events = run["events"]
                if run["summary"]["status"] != "running":
                    break
                text = None
                baseline = next((e for e in events if e["type"] == "test_run"), None)
                if baseline and not said_baseline:
                    c = baseline["report"].get("counts") or {}
                    text = (f"{c.get('failed', '?')} of {c.get('total', '?')} tests fail on 2.0. "
                            "Failures are grouped by root cause and handed to the model")
                    said_baseline = True
                    mark("failures")
                    facts["failing"], facts["total"] = c.get("failed"), c.get("total")
                else:
                    calls = [e for e in events if e["type"] == "tool_call"]
                    if calls:
                        text = TOOL_CAPTIONS.get(calls[-1]["name"])
                        if not any(x["scene"] == "working" for x in SCENES):
                            mark("working")
                if text and text != last_caption:
                    caption(page, text)
                    last_caption = text
                page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")
                page.wait_for_timeout(2500)

            # The review checks run right after the trace is closed; wait for them so the run page shows them.
            for _ in range(60):
                run = get(f"/api/runs/{run_id}")
                if run["summary"].get("check_errors") is not None:
                    break
                page.wait_for_timeout(1000)
            s = run["summary"]
            after = s.get("tests_after") or {}
            page.reload()
            page.get_by_role("button", name="Timeline").first.click()
            page.evaluate("window.scrollTo({top: document.body.scrollHeight})")
            mark("verified")
            facts.update(status=s["status"], steps=s["steps"], passed=after.get("passed"), cost=s.get("cost_usd"))
            if s["status"] == "succeeded":
                caption(page, f"Verified: {after.get('passed', '?')}/{after.get('total', '?')} tests pass on "
                              f"SQLAlchemy 2.0, in {s['steps']} steps")
            else:  # say what really happened
                caption(page, f"This run stopped ({s.get('stop_reason')}): {after.get('passed', '?')}/"
                              f"{after.get('total', '?')} tests passing after {s['steps']} steps")
            page.wait_for_timeout(6500)

            page.evaluate("window.scrollTo({top: 0})")
            page.get_by_role("button", name="Overview").click()
            mark("cost")
            cost = s.get("cost_usd")
            caption(page, f"Cost: ${cost:.3f} for the whole run. The history is append-only, so most input is "
                          "served from the prompt cache" if cost is not None else "Cost and tokens per step")
            page.wait_for_timeout(9000)

            page.get_by_role("button", name="Diff").click()
            mark("diff")
            caption(page, "The final diff. Automatic review checks then look for weak version pins, "
                          "compatibility shims and test edits")
            page.wait_for_timeout(3000)
            for _ in range(6):
                page.mouse.wheel(0, 260)
                page.wait_for_timeout(900)
            mark("review")
            caption(page, "Nothing becomes a branch until a human approves it: the model never gets git access")
            page.wait_for_timeout(7000)

            mark("end")
            card(page, END, 15)
            video = page.video.path()
            context.close()
            browser.close()

        mp4 = OUT / "demo-silent.mp4"  # captions only; docs/demo/narrate.py adds the voiceover -> demo.mp4
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(video),
                        "-filter:v", f"setpts=PTS/{SPEED}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-preset", "slow",
                        "-movflags", "+faststart", str(mp4)], check=True)
        shutil.rmtree(OUT / "raw", ignore_errors=True)
        # Scene start times and the run's numbers, for docs/demo/narrate.py to place the voiceover.
        (OUT / "scenes.json").write_text(json.dumps({"run_id": run_id, "facts": facts, "scenes": SCENES}, indent=1),
                                         encoding="utf-8")
        print(f"run {run_id}: {s['status']}, {s['steps']} steps; video: {mp4} ({mp4.stat().st_size / 1e6:.1f} MB)")
    finally:
        if agent is not None and agent.poll() is None:
            agent.wait(timeout=600)  # let the run finish writing its trace and review checks
        server.terminate()


if __name__ == "__main__":
    main()
