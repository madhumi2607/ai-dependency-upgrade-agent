"""Tests for benchmark task loading and checkout preparation."""

import subprocess
from pathlib import Path

import pytest

from benchmark import tasks
from benchmark.tasks import Task, load_tasks, prepare_checkout


def test_tasks_yaml_is_valid() -> None:
    loaded = load_tasks()
    assert {"geojson-pydantic", "fastapi-restful", "pydantic-cli"} <= set(loaded)
    for task in loaded.values():
        assert len(task.commit) == 40 and task.install and task.test and task.upgrade
        assert task.repo.startswith("https://github.com/")
    assert "pydantic-argparse" not in loaded  # rejected tasks are not runnable


def test_sandbox_env() -> None:
    task = load_tasks()["pydantic-cli"]
    env = task.sandbox_env()
    assert env["SANDBOX_TEST_CMD"] == "pytest pydantic_cli/tests"
    assert env["TARGET_REPO_ROOT"].endswith("pydantic-cli")


@pytest.fixture
def local_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Task:
    """A task whose 'remote' is a local git repo, cloned into a temp workspace."""
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "lib.py").write_text("x = 1\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(origin)], check=True)
    subprocess.run([*git, "-C", str(origin), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(origin), "commit", "-qm", "v1"], check=True)
    commit = subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(tasks, "WORKSPACE", tmp_path / "workspace")
    return Task(id="demo", repo=str(origin), commit=commit, library="pydantic", old_version="1", new_version="2",
                install="pip install -e .", upgrade="pydantic>=2", test="pytest")


def test_prepare_checkout_clones_at_commit(local_task: Task) -> None:
    prepare_checkout(local_task)
    assert (local_task.checkout / "lib.py").read_text(encoding="utf-8") == "x = 1\n"
    prepare_checkout(local_task)  # a clean existing checkout is fine


def test_prepare_checkout_refuses_leftover_edits(local_task: Task) -> None:
    prepare_checkout(local_task)
    (local_task.checkout / "lib.py").write_text("x = 2\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="uncommitted changes"):
        prepare_checkout(local_task)


def test_checkout_keeps_the_repos_own_line_endings(local_task: Task) -> None:
    """With Windows' core.autocrlf=true, files came out with CRLF and scripts broke in Linux."""
    origin = Path(local_task.repo)
    (origin / "run.py").write_bytes(b"#!/usr/bin/env python\nprint('hi')\n")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(origin)]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "script"], check=True)
    local_task.commit = subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    prepare_checkout(local_task)
    assert (local_task.checkout / "run.py").read_bytes() == b"#!/usr/bin/env python\nprint('hi')\n"


def test_old_crlf_clone_is_converted(local_task: Task) -> None:
    """A clone made the old way (inheriting Windows' autocrlf=true) is converted in place."""
    local_task.checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=true", local_task.repo, str(local_task.checkout)], check=True)
    assert (local_task.checkout / "lib.py").read_bytes() == b"x = 1\r\n"  # what the old clones looked like

    prepare_checkout(local_task)
    assert (local_task.checkout / "lib.py").read_bytes() == b"x = 1\n"
