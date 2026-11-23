"""Held-out tests: grade a run against upstream's own post-migration tests, which the agent never saw.

Where upstream ported a project to the new version itself, the port usually changed some tests
(new expectations for v2 behaviour, or tests rewritten for the new API). For each successful run:
take the pinned commit, apply the run's diff, overlay upstream's versions of exactly the test files
its port changed, and run only those files.

A failure is not automatically an agent error: upstream sometimes chose a *different* behaviour on
purpose, or its new tests call API that upstream renamed. So this reports agreement with upstream,
and each disagreement needs reading. As a control, upstream's own port is run against the same
tests: if it fails them in this sandbox, the task's held-out result is not meaningful.

    uv run python -m benchmark.heldout benchmark/results/<experiment>.json [...]

Writes benchmark/results/heldout.json (one entry per run id, plus "control:<task>").
"""

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from agent.trace import read_diff
from benchmark.runner import RESULTS_DIR, RUNS_DIR, keep_awake
from benchmark.tasks import Task, load_tasks
from mcp_servers.git_tools.tools import apply_patch
from mcp_servers.sandbox.report import build_report
from mcp_servers.sandbox.runner import EXPECTED_EXIT_CODES, SandboxConfig, build_docker_command, upgrade_packages

# task id -> (upstream's port commit, the held-out test command). Every port listed here has the
# task's pinned commit as its direct parent, so its test changes are about the migration only.
# Commands run exactly the test files the port changed, with the task's own pytest options.
# Not listed: pyonwater, agate-sql, rdflib-sqlalchemy (their ports changed no tests: nothing is held
# out) and gto (its port is 57 unrelated commits after the pin).
HELDOUT: dict[str, tuple[str, str]] = {
    "geojson-pydantic": ("f704dfabf4ebc48d9581ade75d2989c64b2d7dc6", "pytest tests/test_features.py tests/test_geometries.py"),
    "fastapi-restful": ("22e61d23e4b08f8d5920d3448e1e71f5af469958", "pytest tests/test_api_model.py"),
    "pydantic-cli": ("bdb7e324664a68bb9d813c8cf21c69c049979723",
                     "pytest pydantic_cli/tests/test_examples_simple_boolean_and_config.py "
                     "pydantic_cli/tests/test_examples_simple_schema.py pydantic_cli/tests/test_examples_simple_with_boolean.py "
                     "pydantic_cli/tests/test_examples_simple_with_boolean_custom.py pydantic_cli/tests/test_examples_with_json_config.py"),
    "pydantic-sqlite": ("ed16083f1d19009758669292a52929e0c5b783d3",
                        "pytest tests/test_fields_basic.py tests/test_fields_extends.py tests/test_nested.py"),
    "dbt-cloud-cli": ("d9ea51b891825d96088ae2439ade134dcce46a80", "DBT_CLOUD_ACCOUNT_ID=123456 pytest tests/test_command.py"),
    "copier": ("a42a3a702cf68c5697b26a575e0b26306cb8ebae", 'pytest -m "not impure" -n 2 tests/test_config.py'),
    "dataset": ("04358d12425310ec2fdffcd52199803785b06d30", "pytest test"),  # upstream restructured the whole suite
    "sqladmin": ("af1a293d462bb2f728fbcedc703d4a63641f4856", "pytest tests/test_ajax.py"),
    "confz": ("ef46286ba", "pytest tests/loaders/test_env_loader.py -o asyncio_mode=auto"),
    "morecantile": ("0015490a4", "pytest tests/test_models.py"),
}
TEST_DIRS = ("test/", "tests/", "pydantic_cli/tests/")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


def overlay_upstream_tests(repo: Path, port: str) -> list[str]:
    """Replace the test files upstream's port changed with the port's versions (adding and
    deleting files as it did). Returns the paths it touched."""
    touched = []
    for line in _git(repo, "diff", "--name-status", f"{port}^1", port).splitlines():
        status, *paths = line.split("\t")
        if status.startswith("R"):
            old, new = paths
            (repo / old).unlink(missing_ok=True)
            paths = [new]
        for path in paths:
            if not path.startswith(TEST_DIRS):
                continue
            target = repo / path
            if status == "D":
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(subprocess.run(["git", "-C", str(repo), "show", f"{port}:{path}"],
                                                  capture_output=True, check=True).stdout)
            touched.append(path)
    return touched


def run_heldout(task: Task, run_id: str | None) -> dict[str, Any]:
    """run_id=None runs the control: upstream's own port against its own tests."""
    port, command = HELDOUT[task.id]
    with tempfile.TemporaryDirectory(prefix="heldout-") as tmp:
        repo, out = Path(tmp) / "repo", Path(tmp) / "out"
        out.mkdir()
        # Full local clone (see completeness.py: a --shared clone breaks git inside the container).
        subprocess.run(["git", "clone", "--quiet", "-c", "core.autocrlf=false", "--no-checkout",
                        str(task.checkout), str(repo)], check=True, capture_output=True)
        _git(repo, "checkout", "--quiet", port if run_id is None else task.commit)
        if run_id is not None:
            diff = read_diff(RUNS_DIR / run_id)
            if diff.strip():
                apply_patch(repo, diff, Path(tmp) / "run.patch")
            overlay_upstream_tests(repo, port)
        config = SandboxConfig(repo_root=repo, install_cmd=task.install, upgrade=task.upgrade,
                               test_deps=task.test_deps, test_cmd=command, timeout_seconds=int(task.timeout_seconds))
        name = f"migration-heldout-{uuid.uuid4().hex[:8]}"
        try:
            proc = subprocess.run(build_docker_command(config, out, name, None), capture_output=True, text=True,
                                  timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return {"status": "timeout"}
        if not (out / "install.log").exists() or proc.returncode not in EXPECTED_EXIT_CODES:
            return {"status": "sandbox_error", "detail": proc.stderr.strip()[-500:]}
        report = build_report(out, proc.returncode, upgrade_packages(config.upgrade))
    failures = [{"tests": g.get("example_tests"), "count": g.get("count"), "error": f"{g.get('error_type')}: {(g.get('message') or '')[:300]}",
                 "location": g.get("location")} for g in report.get("failure_groups") or []]
    return {"status": report.get("status"), "counts": report.get("counts"), "failures": failures}


def main(results_files: list[str]) -> None:
    tasks = load_tasks()
    out_path = RESULTS_DIR / "heldout.json"
    done: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    jobs: list[tuple[str, str, str | None, str]] = [(f"control:{t}", t, None, "upstream") for t in HELDOUT]
    for f in results_files:
        for row in json.loads(Path(f).read_text(encoding="utf-8"))["rows"]:
            if row.get("success") and row["task"] in HELDOUT:
                jobs.append((row["run_id"], row["task"], row["run_id"], row.get("impl", "loop")))
    todo = [j for j in jobs if done.get(j[0], {}).get("status") not in ("passed", "failed")]
    print(f"{len(jobs)} jobs ({len(HELDOUT)} controls); {len(jobs) - len(todo)} already done; running {len(todo)}.")
    keep_awake(True)
    try:
        for key, task_id, run_id, impl in todo:
            result = {"task": task_id, "impl": impl, **run_heldout(tasks[task_id], run_id)}
            done[key] = result
            out_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
            c = result.get("counts") or {}
            print(f"  {key:34} {result['status']:13} {c.get('passed', '?')}/{c.get('total', '?')}", flush=True)
    finally:
        keep_awake(False)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
