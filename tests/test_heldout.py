"""Tests for overlaying upstream's post-migration tests (benchmark/heldout.py)."""

import subprocess
from pathlib import Path

from benchmark.heldout import overlay_upstream_tests


def _commit(repo: Path, message: str) -> str:
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", message], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def test_overlay_applies_the_ports_test_changes_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "test").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "lib.py").write_text("v = 1\n")
    (repo / "test" / "test_old.py").write_text("old\n")
    (repo / "test" / "sample_data.py").write_text("data\n")
    (repo / "test" / "test_kept.py").write_text("before\n")
    pin = _commit(repo, "pin")
    # Upstream's port: changes the library, deletes, renames, modifies and adds test files.
    (repo / "lib.py").write_text("v = 2  # upstream's own source change\n")
    (repo / "test" / "test_old.py").unlink()
    (repo / "test" / "sample_data.py").rename(repo / "test" / "conftest.py")
    (repo / "test" / "test_kept.py").write_text("after\n")
    (repo / "test" / "test_new.py").write_text("new\n")
    port = _commit(repo, "port")

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", pin], check=True)
    (repo / "lib.py").write_text("v = 'agent'\n")  # the agent's migration
    touched = overlay_upstream_tests(repo, port)

    assert (repo / "lib.py").read_text() == "v = 'agent'\n"  # source stays the agent's
    assert not (repo / "test" / "test_old.py").exists() and not (repo / "test" / "sample_data.py").exists()
    assert (repo / "test" / "conftest.py").read_text() == "data\n"
    assert (repo / "test" / "test_kept.py").read_text() == "after\n"
    assert (repo / "test" / "test_new.py").read_text() == "new\n"
    assert "lib.py" not in touched
