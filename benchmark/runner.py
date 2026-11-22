"""Run benchmark experiments: every task x model x docs setting, one agent run each.

    uv run --env-file .env python -m benchmark.runner --name haiku-docs \\
        --models claude-haiku-5-5 --docs both [--tasks geojson-pydantic,pydantic-cli]

Each run is a separate `python -m agent.loop --task ...` process (the same code path as a
manual run; a crash can't leak into the next run). The model is chosen through the
environment, which takes precedence over .env. Results go to
benchmark/results/<name>-<timestamp>.json, one row per run, linking to runs/<run-id>/.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark.tasks import Task, load_tasks
from dashboard.runs import read_checks, summarize

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).parent / "results"
RUNS_DIR = PROJECT_ROOT / "runs"


def keep_awake(on: bool) -> None:
    """Stop Windows from idle-sleeping while an experiment runs (no-op elsewhere).

    Sleep pauses the Docker VM mid test run and drops the network, so the agent's MCP call times
    out or its LLM connection breaks, and the run is lost as an infrastructure failure (it cost
    several LangGraph runs). This is the API video players use, and it only lasts while this process
    lives; lid-close and shutdown still work.

    DISPLAY_REQUIRED matters on laptops with Modern Standby (S0ix): there, the screen turning off
    *is* the trigger for standby, and SYSTEM_REQUIRED alone doesn't prevent it."""
    if sys.platform != "win32":
        return
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
    flags = ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | flags)


def reset_checkout(task: Task) -> None:
    """Discard the previous run's edits. Safe: every run's diff is already in runs/<id>/diff.patch."""
    if not (task.checkout / ".git").is_dir():
        return  # agent.loop clones it
    if subprocess.run(["git", "-C", str(task.checkout), "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
        print(f"  resetting {task.checkout.name} (previous edits are kept in runs/<id>/diff.patch)")
        subprocess.run(["git", "-C", str(task.checkout), "checkout", "--quiet", "."], check=True)
        subprocess.run(["git", "-C", str(task.checkout), "clean", "--quiet", "-fd"], check=True)


def failure_category(summary: dict[str, Any]) -> str | None:
    """Automatic part of CLAUDE.md's failure categories. Missed file / misread docs / behavior
    change need a human reading the trace, so those start as needs_review."""
    if summary["status"] == "running":
        return "infrastructure"  # the process died before writing result.json (e.g. Docker stopped)
    if summary["status"] == "succeeded":
        return "passed_with_findings" if (summary["check_errors"] or summary["check_warnings"]) else None
    if summary["stop_reason"] in ("max_steps", "token_budget"):
        return "budget_exhausted"
    if summary["stop_reason"] in ("llm_error", "sandbox_error"):
        return "infrastructure"
    return "needs_review"


def diff_size(run_dir: Path) -> dict[str, int]:
    diff = (run_dir / "diff.patch").read_text(encoding="utf-8") if (run_dir / "diff.patch").is_file() else ""
    lines = diff.splitlines()
    return {
        "files_changed": sum(1 for l in lines if l.startswith("+++ b/")),
        "lines_added": sum(1 for l in lines if l.startswith("+") and not l.startswith("+++")),
        "lines_removed": sum(1 for l in lines if l.startswith("-") and not l.startswith("---")),
    }


def result_row(task: Task, model: str, docs: bool, run_dir: Path) -> dict[str, Any]:
    s = summarize(run_dir)
    checks = read_checks(run_dir) or []
    return {
        "task": task.id, "difficulty": task.difficulty, "model": model, "docs_search": docs,
        "run_id": s["run_id"], "success": s["status"] == "succeeded", "stop_reason": s["stop_reason"],
        "failure_category": failure_category(s),
        "steps": s["steps"], "input_tokens": s["input_tokens"], "cache_read_tokens": s["cache_read_tokens"],
        "output_tokens": s["output_tokens"], "cost_usd": s["cost_usd"], "duration_seconds": s["duration_seconds"],
        "tests_before": s["tests_before"], "tests_after": s["tests_after"],
        **diff_size(run_dir),
        "check_errors": s["check_errors"], "check_warnings": s["check_warnings"],
        "check_findings": [f"[{f['severity']}] {f['check']}: {f['message']}" for f in checks],
    }


# Agent implementations an experiment can run: the hand-written loop or the LangGraph graph.
IMPLS = {"loop": "agent.loop", "langgraph": "agent_langgraph.run"}


def run_one(task: Task, model: str, docs: bool, max_steps: int, impl: str = "loop") -> tuple[Path | None, str | None]:
    """One agent run in a subprocess. Returns (its runs/<id> directory or None if it never
    started, the error text if it never started or didn't finish)."""
    reset_checkout(task)
    cmd = [sys.executable, "-m", IMPLS[impl], "--task", task.id, "--max-steps", str(max_steps)]
    if not docs:
        cmd.append("--no-docs")
    env = {**os.environ, "LLM_PROVIDER": "claude", "AGENT_CLAUDE_MODEL": model}
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    match = re.search(r"Recording run to (.+)", proc.stderr)
    error = proc.stderr.strip()[-1500:] if proc.returncode != 0 else None
    if not match:
        print(f"  run did not start (exit {proc.returncode}):\n{error}")
        return None, error or f"exit {proc.returncode}"
    return Path(match[1].strip()), error


def docker_ready() -> str | None:
    """None if the Docker engine answers, else the reason. Checked before every run: when Docker
    stopped mid-experiment, every later run failed to start one after another."""
    try:
        proc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"docker unavailable: {exc}"
    return None if proc.returncode == 0 else (proc.stderr.strip() or "docker engine not responding")[-300:]


def is_complete(row: dict[str, Any]) -> bool:
    """A row that is a real agent result (kept on --resume); infrastructure failures are rerun."""
    return row.get("run_id") is not None and row.get("failure_category") != "infrastructure"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a benchmark experiment.")
    parser.add_argument("--name", required=True, help="experiment name, used in the results file name")
    parser.add_argument("--models", default="claude-opus-5-5", help="comma-separated Claude model ids")
    parser.add_argument("--docs", choices=["on", "off", "both"], default="on")
    parser.add_argument("--tasks", default="all", help="comma-separated task ids, or 'all'")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--impl", choices=list(IMPLS), default="loop", help="agent implementation to run")
    parser.add_argument("--repeats", type=int, default=1, help="runs per setting (one run per setting is anecdote, not data)")
    parser.add_argument("--resume", help="an earlier results file: keep its real results, rerun only missing "
                                         "or infrastructure-failed runs (its settings are used; other flags ignored)")
    args = parser.parse_args()

    all_tasks = load_tasks()
    if args.resume:
        out = Path(args.resume)
        experiment: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
        config = experiment["config"]
    else:
        started = datetime.now(timezone.utc)
        out = RESULTS_DIR / f"{args.name}-{started:%Y%m%d-%H%M%S}.json"
        config = {"models": args.models.split(","), "docs": args.docs,
                  "tasks": list(all_tasks) if args.tasks == "all" else args.tasks.split(","),
                  "max_steps": args.max_steps, "repeats": args.repeats, "impl": args.impl}
        experiment = {"name": args.name, "started_at": started.isoformat(), "config": config, "rows": []}
    tasks = [all_tasks[t] for t in config["tasks"]]
    docs_settings = {"on": [True], "off": [False], "both": [False, True]}[config["docs"]]
    # Repeats are the outer loop, so each setting's runs are spread over the experiment's time
    # (API latency and load drift over an hour; back-to-back repeats would share that drift).
    plan = [(r, t, m, d) for r in range(1, config["repeats"] + 1) for t in tasks for m in config["models"] for d in docs_settings]

    key = lambda r: (r.get("repeat", 1), r["task"], r["model"], r["docs_search"])  # noqa: E731
    if args.resume:
        # Re-check old rows against today's rules: a row written by an older runner version can
        # carry an outdated category (e.g. a killed run labelled needs_review instead of infrastructure).
        for r in experiment["rows"]:
            run_dir = RUNS_DIR / r["run_id"] if r.get("run_id") else None
            if run_dir is not None and run_dir.is_dir() and r["task"] in all_tasks:
                r.update(result_row(all_tasks[r["task"]], r["model"], r["docs_search"], run_dir))
    done = {key(r): r for r in experiment["rows"] if is_complete(r)}
    if args.resume:
        print(f"Resuming {out.name}: keeping {len(done)} results, running {len(plan) - len(done)}.")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rows: dict[tuple, dict[str, Any]] = dict(done)
    keep_awake(True)
    for i, (repeat, task, model, docs) in enumerate(plan, 1):
        k = (repeat, task.id, model, docs)
        if k in done:
            continue
        print(f"[{i}/{len(plan)}] {task.id} · {model} · docs {'on' if docs else 'off'} · repeat {repeat}", flush=True)
        if problem := docker_ready():
            print(f"Stopping: Docker isn't available ({problem}). Start Docker Desktop, then resume with:\n"
                  f"  uv run --env-file .env python -m benchmark.runner --name x --resume {out}")
            break
        impl = config.get("impl", "loop")  # older results files predate --impl
        run_dir, error = run_one(task, model, docs, config["max_steps"], impl)
        if run_dir is None:
            rows[k] = {"task": task.id, "model": model, "docs_search": docs, "repeat": repeat, "success": False,
                       "failure_category": "infrastructure", "run_id": None, "error": error}
        else:
            row = {**result_row(task, model, docs, run_dir), "repeat": repeat, "impl": impl}
            if error:
                row["error"] = error
            rows[k] = row
            cost = f"${row['cost_usd']:.2f}" if row["cost_usd"] is not None else "?"
            print(f"  -> {'OK' if row['success'] else 'FAIL'} · {row['steps']} steps · {cost} · {row['run_id']}", flush=True)
        experiment["rows"] = [rows[(r, t.id, m, d)] for (r, t, m, d) in plan if (r, t.id, m, d) in rows]
        out.write_text(json.dumps(experiment, indent=1, default=str), encoding="utf-8")  # saved after every run
    keep_awake(False)
    for task in tasks:
        reset_checkout(task)

    final = experiment["rows"]
    real = [r for r in final if is_complete(r)]
    total = sum(r.get("cost_usd") or 0 for r in final)
    print(f"\n{sum(r['success'] for r in real)}/{len(real)} agent runs succeeded"
          f" ({len(plan) - len(real)} missing or infrastructure) · est. ${total:.2f} · {out}\n")
    for g in summarize_rows(final):
        print(f"  {g['task']:18} {g['model']:18} docs {'on ' if g['docs_search'] else 'off'}  "
              f"{g['succeeded']}/{g['runs']} ok · median {g['median_steps']} steps · mean ${g['mean_cost_usd']:.3f}")


def summarize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per (task, model, docs) setting: success count, median steps, mean cost.
    Infrastructure failures are left out: they measure Docker or the network, not the agent."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        if not is_complete(r):
            continue
        groups.setdefault((r["task"], r["model"], r["docs_search"]), []).append(r)
    summary = []
    for (task, model, docs), runs in groups.items():
        steps = sorted(r.get("steps") or 0 for r in runs)
        costs = [r.get("cost_usd") or 0.0 for r in runs]
        summary.append({
            "task": task, "model": model, "docs_search": docs, "runs": len(runs),
            "succeeded": sum(bool(r.get("success")) for r in runs),
            "median_steps": steps[len(steps) // 2] if len(steps) % 2 else (steps[len(steps) // 2 - 1] + steps[len(steps) // 2]) / 2,
            "mean_cost_usd": sum(costs) / len(costs),
        })
    return summary


if __name__ == "__main__":
    main()
