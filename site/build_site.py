"""Build the public project site (GitHub Pages): a landing page plus a read-only dashboard.

    site/_build/
        index.html            landing page (site/index.html)
        demo.mp4, *.png       the narrated demo and screenshots (docs/demo/)
        dashboard/            the dashboard, built in static mode (VITE_STATIC=1)
            data/runs.json        run summaries, newest first
            data/runs/<id>.json   one run each: timeline, diff, review checks

Only the runs behind the committed benchmark results are published, scrubbed of the local
checkout path and email addresses exactly like benchmark/traces (benchmark/export_traces.py).
Nothing here runs an agent: the site is static and needs no API key.

    uv run python site/build_site.py
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark.export_traces import scrub  # noqa: E402
from dashboard.runs import run_detail, summarize  # noqa: E402

SITE = Path(__file__).parent
OUT = SITE / "_build"
RUNS = ROOT / "runs"
WEB = ROOT / "dashboard" / "web"


def published_run_ids() -> list[str]:
    """Run ids referenced by the benchmark results files committed to git."""
    files = subprocess.run(["git", "ls-files", "benchmark/results/*.json"], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.split()
    ids: set[str] = set()
    for name in files:
        data = json.loads((ROOT / name).read_text(encoding="utf-8"))
        if isinstance(data, dict) and "rows" in data:
            ids.update(r["run_id"] for r in data["rows"] if r.get("run_id"))
    return sorted(i for i in ids if (RUNS / i).is_dir())


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(json.dumps(data, separators=(",", ":"), default=str)), encoding="utf-8")


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)

    # 1. The dashboard, built in static mode with relative asset paths.
    npm = "npm.cmd" if os.name == "nt" else "npm"
    subprocess.run([npm, "run", "build", "--", "--base", "./", "--outDir", str(OUT / "dashboard"), "--emptyOutDir"],
                   cwd=WEB, env={**os.environ, "VITE_STATIC": "1"}, check=True, capture_output=True)

    # 2. Its data: the published runs.
    ids = published_run_ids()
    summaries = [summarize(RUNS / i) for i in ids]
    summaries.sort(key=lambda s: s["started_at"] or "", reverse=True)
    write_json(OUT / "dashboard" / "data" / "runs.json", summaries)
    for run_id in ids:
        write_json(OUT / "dashboard" / "data" / "runs" / f"{run_id}.json", run_detail(RUNS / run_id))

    # 3. The landing page and its media.
    shutil.copy(SITE / "index.html", OUT / "index.html")
    for name in ("demo.mp4", "multi-agent-review-1.png", "multi-agent-reviewer-steps.png"):
        shutil.copy(ROOT / "docs" / "demo" / name, OUT / name)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")  # serve files as-is

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{len(ids)} runs published; site {size / 1e6:.1f} MB at {OUT}")


if __name__ == "__main__":
    main()
