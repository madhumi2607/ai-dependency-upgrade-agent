"""Migration completeness: did a successful run *migrate* the code, or only make the tests pass?

Pydantic v2 still runs most v1-style code (`.dict()`, `@validator`, `class Config`, ...) and only
warns (PydanticDeprecatedSince20); SQLAlchemy 2.0 warns for some legacy calls (SADeprecationWarning).
Tests pass either way, so the success rate can't see it.

For each successful run in a results file, this replays the run's final diff on the task's pinned
commit, runs the exact test command it was graded with in the same sandbox, and counts the
deprecated *call sites in the project's own source* (not tests, not site-packages) that the suite
hit. 0 means no old API was exercised. It can only see code the tests execute.

    uv run python -m benchmark.completeness benchmark/results/<experiment>.json [--workers 2]

Writes benchmark/results/completeness-<experiment>.json (resumable: done runs are skipped).
"""

import argparse
import json
import re
import subprocess
import tempfile
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from agent.checks import TEST_PATH
from agent.trace import read_diff
from mcp_servers.git_tools.tools import apply_patch
from benchmark.runner import RESULTS_DIR, RUNS_DIR, keep_awake
from benchmark.tasks import Task, load_tasks, prepare_checkout
from mcp_servers.sandbox.runner import EXPECTED_EXIT_CODES, SandboxConfig, build_docker_command, upgrade_packages
from mcp_servers.sandbox.report import build_report

# "  /work/pkg/models.py:12: PydanticDeprecatedSince20: Pydantic V1 style `@validator` ..."
WARNING_LINE = re.compile(r"^\s+(?P<path>\S+?):(?P<line>\d+): (?P<category>[A-Za-z_][\w.]*): (?P<message>.*)$")


def is_deprecation(category: str) -> bool:
    return "Deprecat" in category or category.startswith(("MovedIn", "RemovedIn"))


def from_library(category: str, message: str, library: str) -> bool:
    """Is this warning about the library being migrated (vs. some other dependency)?"""
    if library == "pydantic":
        return category.startswith("Pydantic") or "pydantic" in message.lower()
    if library == "sqlalchemy":
        return category.startswith(("SA", "MovedIn", "RemovedIn")) or "sqlalchemy" in message.lower()
    return library.lower() in message.lower()


def parse_deprecations(pytest_log: str, library: str) -> dict[str, list[dict[str, str]]]:
    """Deprecation warnings from pytest's warnings summary, by unique source location.

    Returns {"library": [...], "other": [...], "library_internal": [...]}. "library" and "other" are
    call sites in the repo's own (non-test) source. "library_internal" are deprecations of the migrated
    library that it reports at a line inside itself (e.g. pydantic's class-based `Config` warning), so
    they can't be attributed to a repo line; they're kept, deduplicated by message, rather than dropped.
    """
    section = pytest_log.split("warnings summary", 1)[1] if "warnings summary" in pytest_log else ""
    section = re.split(r"^-- Docs:|^={5,}", section, maxsplit=1, flags=re.M)[0] if section else ""
    found: dict[str, dict[str, dict[str, str]]] = {"library": {}, "other": {}, "library_internal": {}}
    for line in section.splitlines():
        m = WARNING_LINE.match(line)
        if not m or not is_deprecation(m["category"]):
            continue
        path = m["path"].removeprefix("/work/")
        if "site-packages" in path and f"/{library.lower()}/" in path.lower():
            found["library_internal"][m["message"][:200]] = {"location": path.split("site-packages/")[-1] + f":{m['line']}",
                                                             "category": m["category"], "message": m["message"][:200]}
            continue
        if path.startswith("/") or "site-packages" in path or path.startswith("<") or TEST_PATH.search(path):
            continue  # outside the repo, or in its tests (which the agent may not change)
        kind = "library" if from_library(m["category"], m["message"], library) else "other"
        found[kind][f"{path}:{m['line']}"] = {"location": f"{path}:{m['line']}", "category": m["category"],
                                               "message": m["message"][:200]}
    return {kind: list(sites.values()) for kind, sites in found.items()}


def replay(task: Task, run_id: str) -> dict[str, Any]:
    """Apply runs/<run_id>/diff.patch to the task's commit, run the graded test command, and read
    the deprecations from pytest's warnings summary."""
    diff = read_diff(RUNS_DIR / run_id)
    with tempfile.TemporaryDirectory(prefix="completeness-") as tmp:
        repo, out = Path(tmp) / "repo", Path(tmp) / "out"
        out.mkdir()
        # A full local clone (hard-linked, so still cheap), not --shared: a shared clone points at
        # the source's objects by a Windows path that doesn't exist inside the container, so git is
        # broken there, and builds that read the version from git (copier) fail to install.
        _git(None, "clone", "--quiet", "-c", "core.autocrlf=false", "--no-checkout", str(task.checkout), str(repo))
        _git(repo, "checkout", "--quiet", task.commit)
        if diff.strip():
            apply_patch(repo, diff, Path(tmp) / "run.patch")
        # Exactly the command the run was graded with: pytest already lists DeprecationWarnings in
        # its summary by default. (Forcing them on with -W changed a coilmq test's outcome.)
        config = SandboxConfig(repo_root=repo, install_cmd=task.install, upgrade=task.upgrade, test_deps=task.test_deps,
                               test_cmd=task.test, timeout_seconds=int(task.timeout_seconds))
        measurable = not hides_deprecations(repo)
        name = f"migration-completeness-{uuid.uuid4().hex[:8]}"
        try:
            proc = subprocess.run(build_docker_command(config, out, name, None), capture_output=True, text=True,
                                  timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return {"status": "timeout"}
        if not (out / "install.log").exists() or proc.returncode not in EXPECTED_EXIT_CODES:
            return {"status": "sandbox_error", "detail": proc.stderr.strip()[-500:]}
        report = build_report(out, proc.returncode, upgrade_packages(config.upgrade))
        log = (out / "pytest.log").read_text(encoding="utf-8", errors="replace") if (out / "pytest.log").exists() else ""
    sites = parse_deprecations(log, task.library)
    if not measurable:  # 0 would be misleading: the project's own config hid every deprecation
        return {"status": report.get("status"), "counts": report.get("counts"), "measurable": False}
    return {"status": report.get("status"), "counts": report.get("counts"), "measurable": True,
            "library_deprecations": len(sites["library"]), "library_internal": len(sites["library_internal"]),
            "other_deprecations": len(sites["other"]),
            "sites": sites}


def hides_deprecations(repo: Path) -> bool:
    """True if the project's pytest config ignores every DeprecationWarning (e.g. rdflib-sqlalchemy's
    `ignore::DeprecationWarning`), which makes them invisible to this metric."""
    for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
        path = repo / name
        if path.is_file() and re.search(r"ignore::DeprecationWarning\b", path.read_text(encoding="utf-8", errors="replace")):
            return True
    return False


def _git(cwd: Path | None, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure migration completeness for an experiment's successful runs.")
    parser.add_argument("results", help="a benchmark results file")
    parser.add_argument("--workers", type=int, default=2, help="sandbox runs in parallel (each uses 2 CPUs / 2 GB)")
    args = parser.parse_args()

    source = Path(args.results)
    rows = [r for r in json.loads(source.read_text(encoding="utf-8"))["rows"] if r.get("success") and r.get("run_id")]
    out_path = RESULTS_DIR / f"completeness-{source.stem}.json"
    done: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    tasks = load_tasks()
    for task_id in {r["task"] for r in rows}:
        # Replays clone the pinned commit from the checkout's git history, never its working files,
        # so it only has to exist: an agent run may be editing it at the same time.
        if not (tasks[task_id].checkout / ".git").is_dir():
            prepare_checkout(tasks[task_id])
    todo = [r for r in rows if done.get(r["run_id"], {}).get("status") not in ("passed",)]
    print(f"{len(rows)} successful runs; {len(rows) - len(todo)} already measured; replaying {len(todo)}.")

    lock = threading.Lock()

    def one(row: dict[str, Any]) -> None:
        result = {"task": row["task"], "repeat": row.get("repeat"), "impl": row.get("impl", "loop"), **replay(tasks[row["task"]], row["run_id"])}
        with lock:  # workers share `done` and the output file
            done[row["run_id"]] = result
            out_path.write_text(json.dumps(done, indent=1), encoding="utf-8")  # saved after every replay
        print(f"  {row['task']:18} r{row.get('repeat')} {result['status']:8} library deprecations: "
              f"{result.get('library_deprecations', '?')} (+{result.get('library_internal', '?')} inside the library)  "
              f"other: {result.get('other_deprecations', '?')}", flush=True)

    keep_awake(True)
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(one, todo))
    finally:
        keep_awake(False)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
