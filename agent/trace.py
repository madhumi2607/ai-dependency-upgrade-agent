"""Record a run to disk so it can be inspected, compared and replayed.

Layout of one run:
    runs/<run-id>/
        meta.json      who/what/when: target repo and commit, model, settings
        events.jsonl   one JSON object per event, in order (see agent/loop.py run_agent)
        result.json    the RunResult, written when the run ends
        diff.patch     the agent's changes to the target repo (git diff), byte-for-byte

Events hold only what is new (one model turn, one tool result), not the whole conversation,
so the files grow linearly with the run; the conversation is rebuilt by replaying them.
"""

import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


class RunTrace:
    """An event handler for run_agent that appends each event to events.jsonl."""

    def __init__(self, path: Path, repo: Path) -> None:
        self.path = path
        self.repo = repo
        self._events: IO[str] = (path / "events.jsonl").open("a", encoding="utf-8")

    @classmethod
    def create(cls, runs_dir: str | Path, repo: str | Path, meta: dict[str, Any]) -> "RunTrace":
        """Make a new run directory (named by start time and repo) and write meta.json."""
        repo = Path(repo).resolve()
        started = datetime.now(timezone.utc)
        run_id = f"{started:%Y%m%d-%H%M%S}-{repo.name}"
        path = Path(runs_dir) / run_id
        path.mkdir(parents=True, exist_ok=False)
        meta = {"run_id": run_id, "started_at": started.isoformat(), "repo": str(repo),
                "repo_commit": _git(repo, "rev-parse", "HEAD").strip() or None, **meta}
        (path / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return cls(path, repo)

    def __call__(self, event: dict[str, Any]) -> None:
        self._events.write(json.dumps(event, default=str) + "\n")
        self._events.flush()  # so a live dashboard (or a crash) sees every event written so far

    def finish(self, result: Any) -> None:
        """Write result.json and the agent's diff, then close the events file."""
        data = asdict(result)
        data["finished_at"] = datetime.now(timezone.utc).isoformat()
        (self.path / "result.json").write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        # Byte-for-byte: a text-mode round trip would turn the CRLF lines of CRLF files into LF
        # on Windows, and the patch would no longer apply to them (see read_diff for older runs).
        diff = subprocess.run(["git", "-C", str(self.repo), "diff"], capture_output=True)
        (self.path / "diff.patch").write_bytes(diff.stdout if diff.returncode == 0 else b"")
        self._events.close()


def read_diff(run_dir: Path) -> str:
    """A run's diff.patch as text, with line endings exactly as git produced them.

    Runs recorded before the byte-exact fix stored the diff through text mode on Windows: every
    line ended in CRLF, including git's own header lines (which git always ends with LF). That
    format is recognised by its CRLF header line and translated back. Their CRLF *content* lines
    had already lost the CR, so applying them to CRLF files needs --ignore-whitespace.
    """
    path = Path(run_dir) / "diff.patch"
    if not path.is_file():
        return ""
    text = path.read_bytes().decode("utf-8", errors="replace")
    first_line = text.split("\n", 1)[0]
    if first_line.startswith("diff --git") and first_line.endswith("\r"):
        text = text.replace("\r\n", "\n")
    return text


def _git(repo: Path, *args: str) -> str:
    """Output of a git command in the target repo; empty if it isn't a git checkout."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    return proc.stdout if proc.returncode == 0 else ""
