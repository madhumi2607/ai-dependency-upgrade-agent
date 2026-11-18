"""Tests for human review: approve -> local branch + commit + PR text; reject; guards."""

import json
import subprocess
from pathlib import Path

import pytest

from agent.review import approve, branch_name, load_run, pr_description, reject
from mcp_servers.git_tools.tools import GitError, commit_diff_on_branch, git

GIT = ["-c", "user.email=t@t", "-c", "user.name=t"]


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """A target repo at a 'v1' commit, like a workspace checkout."""
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "models.py").write_text("from pydantic import validator\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", *GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", *GIT, "-C", str(repo), "commit", "-qm", "v1"], check=True)
    return repo, git(repo, "rev-parse", "HEAD").strip()


def make_diff(repo: Path) -> str:
    (repo / "models.py").write_text("from pydantic import field_validator\n", encoding="utf-8")
    diff = git(repo, "diff")
    git(repo, "checkout", "--quiet", ".")  # like the runner resetting the checkout afterwards
    return diff


def make_run(tmp_path: Path, repo: Path, commit: str, diff: str, success: bool = True) -> Path:
    run = tmp_path / "runs" / "20261118-100000-target"
    run.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"run_id": run.name, "started_at": "2026-11-18T10:00:00+00:00",
                                               "repo": str(repo), "repo_commit": commit, "model": "claude-haiku-5-5"}),
                                   encoding="utf-8")
    events = [
        {"type": "start", "task": {"library": "pydantic", "old_version": "1", "new_version": "2"}},
        {"type": "test_run", "purpose": "baseline", "report": {"status": "failed", "counts": {"total": 3, "passed": 0}}},
        {"type": "llm_call", "step": 1, "input_tokens": 1000, "cache_read_tokens": 0, "output_tokens": 50},
        {"type": "test_run", "purpose": "verification", "report": {"status": "passed", "counts": {"total": 3, "passed": 3}}},
    ]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (run / "result.json").write_text(json.dumps({"success": success, "stop_reason": "finished" if success else "max_steps",
                                                 "summary": "validator -> field_validator.",
                                                 "finished_at": "2026-11-18T10:05:00+00:00"}), encoding="utf-8")
    (run / "diff.patch").write_text(diff, encoding="utf-8")
    (run / "checks.json").write_text("[]", encoding="utf-8")
    return run


def test_approve_creates_branch_commit_and_pr_text(tmp_path: Path, repo: tuple[Path, str]) -> None:
    target, base = repo
    run = load_run(make_run(tmp_path, target, base, make_diff(target)))
    decision = approve(run)

    assert decision["branch"] == "migrate/pydantic-v2/20261118-100000-target"
    shown = git(target, "show", f"{decision['branch']}:models.py")
    assert shown == "from pydantic import field_validator\n"
    assert git(target, "rev-parse", "HEAD").strip() == base  # the checkout itself never moved
    assert not git(target, "status", "--porcelain").strip()

    review = json.loads((run["dir"] / "review.json").read_text(encoding="utf-8"))
    assert review["decision"] == "approved" and review["commit"] == decision["commit"]
    pr = (run["dir"] / "pr.md").read_text(encoding="utf-8")
    assert "0/3 passed** before, **3/3 passed** after" in pr and "validator -> field_validator." in pr


def test_failed_runs_need_force_and_reviews_are_final(tmp_path: Path, repo: tuple[Path, str]) -> None:
    target, base = repo
    run = load_run(make_run(tmp_path, target, base, make_diff(target), success=False))
    with pytest.raises(SystemExit, match="--force"):
        approve(run)
    reject(run, "still failing")
    assert json.loads((run["dir"] / "review.json").read_text(encoding="utf-8"))["note"] == "still failing"
    with pytest.raises(SystemExit, match="already reviewed"):
        approve(run, force=True)
    assert not git(target, "branch", "--list", branch_name(run)).strip()  # rejection created nothing


def test_a_diff_that_no_longer_applies_leaves_nothing_behind(tmp_path: Path, repo: tuple[Path, str]) -> None:
    target, base = repo
    bad_diff = "diff --git a/missing.py b/missing.py\n--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(GitError):
        commit_diff_on_branch(target, base, "migrate/test", bad_diff, "msg")
    assert not git(target, "branch", "--list", "migrate/test").strip()
    assert git(target, "worktree", "list").count("\n") == 1  # temporary worktree cleaned up


def test_pr_description_lists_review_findings(tmp_path: Path, repo: tuple[Path, str]) -> None:
    target, base = repo
    run_dir = make_run(tmp_path, target, base, make_diff(target))
    (run_dir / "checks.json").write_text(json.dumps([{"severity": "warning", "check": "pins", "message": "fastapi allows <0.100",
                                                      "file": None}]), encoding="utf-8")
    assert "- **warning** (pins): fastapi allows <0.100" in pr_description(load_run(run_dir))
