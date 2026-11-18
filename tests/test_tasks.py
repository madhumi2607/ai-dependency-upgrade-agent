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


def test_task_timeout_reaches_the_sandbox() -> None:
    tasks = load_tasks()
    assert tasks["copier"].sandbox_env()["SANDBOX_TIMEOUT_SECONDS"] == "1500"  # slow suite
    assert tasks["geojson-pydantic"].sandbox_env()["SANDBOX_TIMEOUT_SECONDS"] == "600"  # default


def test_deselected_tests_exist() -> None:
    """pytest silently ignores a --deselect that matches nothing. A typo once left two impossible
    tests in the dataset task, and every run burned its budget on them. Checked against the
    pinned commit of each task that is cloned locally."""
    import re
    import shlex

    checked = 0
    for task in load_tasks().values():
        ids = [a for a in re.findall(r"--deselect\s+(\S+)", task.test)]
        if not ids or not (task.checkout / ".git").is_dir():
            continue
        for test_id in ids:
            path, *names = shlex.split(test_id)[0].split("::")
            source = subprocess.run(["git", "-C", str(task.checkout), "show", f"{task.commit}:{path}"],
                                    capture_output=True, text=True, encoding="utf-8").stdout
            *classes, func = names
            for cls in classes:
                assert re.search(rf"^class {cls}\b", source, re.M), f"{task.id}: no class {cls} in {path}"
            assert re.search(rf"^\s*def {func}\b", source, re.M), f"{task.id}: no test {func} in {path}"
            if classes:  # the function must be inside that class, not a same-named one elsewhere
                body = source.split(f"class {classes[-1]}", 1)[1]
                nxt = re.search(r"^class \w", body, re.M)
                assert f"def {func}" in body[: nxt.start() if nxt else None], f"{task.id}: {func} is not in {classes[-1]}"
            checked += 1
    if not checked:
        pytest.skip("no task with --deselect is cloned locally")
