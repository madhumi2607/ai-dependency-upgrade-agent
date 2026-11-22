"""Git operations for turning an approved run into a branch with one commit.

Deliberately NOT given to the agent: if the model could commit or open PRs itself, the human
approval step would only be a request in the prompt. These are called by agent/review.py, which
a person runs. The branch is built in a temporary worktree, so the task's checkout stays at its
pinned commit (benchmark runs depend on that).
"""

import subprocess
import tempfile
from pathlib import Path


class GitError(Exception):
    """A git command failed; the message says which and why."""


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def apply_patch(repo: Path, diff: str, patch_file: Path) -> None:
    """Apply a unified diff to a checkout. Written as raw bytes: Windows text mode would add a CR
    to every line. Diffs recorded before traces became byte-exact lost the CR of CRLF content
    lines (agent.trace.read_diff), so they only apply to CRLF files with --ignore-whitespace."""
    patch_file.write_bytes(diff.encode("utf-8"))
    try:
        git(repo, "apply", "--whitespace=nowarn", str(patch_file))
    except GitError:
        git(repo, "apply", "--whitespace=nowarn", "--ignore-whitespace", str(patch_file))


def branch_exists(repo: Path, branch: str) -> bool:
    return bool(git(repo, "branch", "--list", branch).strip())


def commit_diff_on_branch(repo: Path, base_commit: str, branch: str, diff: str, message: str) -> str:
    """Create `branch` at `base_commit` with one commit applying `diff`; return the commit sha.

    Uses a temporary worktree, so `repo`'s own checkout (and HEAD) is never touched.
    """
    if branch_exists(repo, branch):
        raise GitError(f"Branch {branch!r} already exists in {repo}.")
    if not diff.strip():
        raise GitError("The run made no changes: nothing to commit.")
    with tempfile.TemporaryDirectory(prefix="review-") as tmp:
        worktree = Path(tmp) / "wt"
        git(repo, "worktree", "add", "--quiet", "-b", branch, str(worktree), base_commit)
        try:
            apply_patch(worktree, diff, Path(tmp) / "run.patch")
            git(worktree, "add", "--all")
            git(worktree, "commit", "--quiet", "-m", message)
            return git(worktree, "rev-parse", "HEAD").strip()
        except GitError:
            # Leave no half-made branch behind.
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            git(repo, "branch", "-D", branch, check=False)
            raise
        finally:
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            git(repo, "worktree", "prune", check=False)


def diff_stat(diff: str) -> dict[str, int]:
    """Files changed and lines added/removed in a unified diff."""
    lines = diff.splitlines()
    return {
        "files": sum(1 for l in lines if l.startswith("+++ b/")),
        "added": sum(1 for l in lines if l.startswith("+") and not l.startswith("+++")),
        "removed": sum(1 for l in lines if l.startswith("-") and not l.startswith("---")),
    }
