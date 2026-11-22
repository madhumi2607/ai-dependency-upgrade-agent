"""Publish the traces behind a results file, so every number in it can be checked.

runs/ is gitignored (it holds every local run, including experiments that were thrown away).
This packs only the runs a results file refers to into benchmark/traces/<results-name>.tar.gz,
scrubbed of the local checkout path and of email addresses (maintainers' addresses the agent read
in the target repos' own files).

    uv run python -m benchmark.export_traces benchmark/results/<experiment>.json
To browse them in the dashboard, unpack into runs/:
    tar -xzf benchmark/traces/<experiment>.tar.gz -C runs
"""

import io
import json
import re
import sys
import tarfile
from pathlib import Path

from benchmark.runner import PROJECT_ROOT, RUNS_DIR

TRACES_DIR = Path(__file__).parent / "traces"
FILES = ("meta.json", "events.jsonl", "result.json", "diff.patch", "checks.json")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def scrub(text: str) -> str:
    """Remove the local project path (both slash styles, JSON-escaped too) and email addresses."""
    root = str(PROJECT_ROOT)
    for variant in (root.replace("\\", "\\\\"), root, root.replace("\\", "/")):
        text = text.replace(variant, "<project>")
    return EMAIL.sub("<email>", text)


def export(results_file: Path) -> Path:
    rows = json.loads(results_file.read_text(encoding="utf-8"))["rows"]
    run_ids = sorted({r["run_id"] for r in rows if r.get("run_id")})
    TRACES_DIR.mkdir(exist_ok=True)
    out = TRACES_DIR / f"{results_file.stem}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for run_id in run_ids:
            for name in FILES:
                path = RUNS_DIR / run_id / name
                if not path.is_file():
                    continue
                data = scrub(path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
                info = tarfile.TarInfo(f"{run_id}/{name}")
                info.size, info.mtime = len(data), int(path.stat().st_mtime)
                tar.addfile(info, io.BytesIO(data))
    print(f"{results_file.name}: {len(run_ids)} runs -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        export(Path(arg))
