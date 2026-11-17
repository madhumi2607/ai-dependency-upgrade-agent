"""Load benchmark tasks (benchmark/tasks.yaml) and prepare a task's checkout for a run."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

TASKS_FILE = Path(__file__).parent / "tasks.yaml"
WORKSPACE = Path(__file__).parent.parent / "workspace"


@dataclass
class Task:
    id: str
    repo: str
    commit: str
    library: str
    old_version: str
    new_version: str
    install: str
    upgrade: str
    test: str
    test_deps: str = ""
    difficulty: str = ""

    @property
    def checkout(self) -> Path:
        return WORKSPACE / self.id

    def sandbox_env(self) -> dict[str, str]:
        """The env vars the MCP servers read (see .env.example)."""
        return {
            "TARGET_REPO_ROOT": str(self.checkout.resolve()),
            "SANDBOX_INSTALL_CMD": self.install,
            "SANDBOX_TEST_DEPS": self.test_deps,
            "SANDBOX_TEST_CMD": self.test,
            "SANDBOX_UPGRADE": self.upgrade,
        }


def load_tasks(path: Path = TASKS_FILE) -> dict[str, Task]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fields = Task.__dataclass_fields__
    return {t["id"]: Task(**{k: str(v) for k, v in t.items() if k in fields}) for t in data["tasks"]}


def prepare_checkout(task: Task) -> None:
    """Clone the task's repo into workspace/<id> if needed, and make sure it is clean at the
    pinned commit. Refuses (rather than resets) a dirty checkout: leftover edits may be the
    previous run's work that someone still wants to look at."""
    if not (task.checkout / ".git").is_dir():
        task.checkout.parent.mkdir(parents=True, exist_ok=True)
        # Byte-identical to upstream: with Windows' core.autocrlf=true every file was checked
        # out with CRLF, so scripts run by tests failed in the Linux sandbox ("python\r").
        _git(None, "clone", "--quiet", "-c", "core.autocrlf=false", task.repo, str(task.checkout))
        _git(task.checkout, "checkout", "--quiet", task.commit)
    if _git(task.checkout, "status", "--porcelain").strip():
        raise SystemExit(f"{task.checkout} has uncommitted changes from an earlier run. "
                         f"Review them, then reset with: git -C {task.checkout} checkout .")
    if _git(task.checkout, "config", "--get", "core.autocrlf", check=False).strip() != "false":
        # An older clone made with CRLF: re-check out every file with the repo's own line endings.
        # Safe because the checkout was just confirmed clean.
        _git(task.checkout, "config", "core.autocrlf", "false")
        _git(task.checkout, "rm", "-r", "--cached", "--quiet", ".")
        _git(task.checkout, "reset", "--hard", "--quiet")
    head = _git(task.checkout, "rev-parse", "HEAD").strip()
    if head != task.commit:
        raise SystemExit(f"{task.checkout} is at {head[:7]}, but task {task.id} pins {task.commit[:7]}. "
                         f"Fix with: git -C {task.checkout} checkout {task.commit}")


def _git(cwd: Path | None, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout
