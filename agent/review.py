"""Human review: look at a finished run, then approve (branch + commit + PR text) or reject.

    uv run python -m agent.review runs/<run-id>                 # interactive
    uv run python -m agent.review runs/<run-id> --approve       # or --reject "reason"

Writes runs/<id>/review.json (the decision) and, on approval, runs/<id>/pr.md (the PR
description) plus a local branch migrate/<library>-v<new>/<run-id> with one commit.
Nothing is pushed: the target repos belong to other people.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dashboard.runs import read_checks, summarize
from mcp_servers.git_tools.tools import GitError, commit_diff_on_branch, diff_stat


def load_run(run_dir: Path) -> dict[str, Any]:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    start = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    diff_path = run_dir / "diff.patch"
    return {
        "dir": run_dir, "meta": meta, "task": start["task"], "summary": summarize(run_dir),
        "checks": read_checks(run_dir) or [], "diff": diff_path.read_text(encoding="utf-8") if diff_path.is_file() else "",
    }


def branch_name(run: dict[str, Any]) -> str:
    task = run["task"]
    return f"migrate/{task['library']}-v{task['new_version']}/{run['meta']['run_id']}"


def _tests(t: dict[str, Any] | None) -> str:
    if not t:
        return "not run"
    return t["status"].replace("_", " ") if t.get("total") is None else f"{t['passed']}/{t['total']} passed"


def pr_description(run: dict[str, Any]) -> str:
    """The PR text, built only from recorded data so it can't overstate what happened."""
    s, task, meta = run["summary"], run["task"], run["meta"]
    stat = diff_stat(run["diff"])
    findings = "\n".join(f"- **{f['severity']}** ({f['check']}): {f['message']}" for f in run["checks"]) or "- none"
    cost = f"${s['cost_usd']:.2f}" if s["cost_usd"] is not None else "n/a"
    return f"""# Migrate to {task['library']} {task['new_version']}

{s['summary'] or '(the agent gave no summary)'}

## Verification
- Tests with {task['library']} {task['new_version']}: **{_tests(s['tests_before'])}** before, **{_tests(s['tests_after'])}** after
- Test files were not modified (the agent's tools refuse test edits; checked again on the final diff)

## Review checks
{findings}

## Change size
{stat['files']} files changed, +{stat['added']} / -{stat['removed']} lines

---
Made by migration-agent run `{meta['run_id']}` ({meta.get('model')}, {s['steps']} steps, est. {cost}).
Reviewed and approved by a human before this branch was created.
"""


def record(run: dict[str, Any], decision: dict[str, Any]) -> None:
    decision = {"decided_at": datetime.now(timezone.utc).isoformat(), "reviewer": os.environ.get("USERNAME") or os.environ.get("USER"), **decision}
    (run["dir"] / "review.json").write_text(json.dumps(decision, indent=1), encoding="utf-8")


def approve(run: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """Create the branch + commit and write pr.md. Refuses failed runs unless force."""
    s = run["summary"]
    if (run["dir"] / "review.json").is_file():
        raise SystemExit(f"{run['meta']['run_id']} was already reviewed (see review.json).")
    if s["status"] != "succeeded" and not force:
        raise SystemExit(f"This run did not succeed ({s['stop_reason']}); pass --force to approve it anyway.")
    task, meta = run["task"], run["meta"]
    title = f"Migrate to {task['library']} {task['new_version']}"
    message = f"{title}\n\n{s['summary'] or ''}\n\nmigration-agent run {meta['run_id']}, approved by a human reviewer."
    branch = branch_name(run)
    try:
        sha = commit_diff_on_branch(Path(meta["repo"]), meta["repo_commit"], branch, run["diff"], message)
    except GitError as exc:
        raise SystemExit(f"Could not create the branch: {exc}") from exc
    (run["dir"] / "pr.md").write_text(pr_description(run), encoding="utf-8")
    decision = {"decision": "approved", "branch": branch, "commit": sha, "forced": force}
    record(run, decision)
    return decision


def reject(run: dict[str, Any], note: str) -> dict[str, Any]:
    if (run["dir"] / "review.json").is_file():
        raise SystemExit(f"{run['meta']['run_id']} was already reviewed (see review.json).")
    decision = {"decision": "rejected", "note": note}
    record(run, decision)
    return decision


def show(run: dict[str, Any]) -> None:
    s = run["summary"]
    stat = diff_stat(run["diff"])
    print(f"Run {s['run_id']}  ·  {s['repo']} @ {s['repo_commit']}  ·  {s['model']}")
    print(f"Status: {s['status']} ({s['stop_reason']})  ·  tests {_tests(s['tests_before'])} -> {_tests(s['tests_after'])}")
    print(f"Changes: {stat['files']} files, +{stat['added']} / -{stat['removed']}")
    print(f"\nAgent's summary:\n{s['summary'] or '(none)'}\n")
    print("Review checks:" if run["checks"] else "Review checks: no findings")
    for f in run["checks"]:
        print(f"  [{f['severity']}] {f['check']}: {f['message']}")
    print(f"\nFull diff: {run['dir'] / 'diff.patch'} (also on the dashboard's Diff tab)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review a finished run: approve (local branch + PR text) or reject.")
    parser.add_argument("run_dir", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", metavar="NOTE")
    parser.add_argument("--force", action="store_true", help="allow approving a run that did not succeed")
    args = parser.parse_args()

    run = load_run(args.run_dir)
    show(run)
    if args.reject is not None:
        reject(run, args.reject)
        print("\nRecorded: rejected.")
        return
    if not args.approve:
        answer = input("\nApprove and create a local branch with these changes? [y/N] ").strip().lower()
        if answer != "y":
            reject(run, input("Why not? (optional) ").strip())
            print("Recorded: rejected.")
            return
    decision = approve(run, force=args.force)
    print(f"\nApproved. Branch {decision['branch']} ({decision['commit'][:7]}) in {run['meta']['repo']}")
    print(f"PR description: {run['dir'] / 'pr.md'}")


if __name__ == "__main__":
    sys.exit(main())
