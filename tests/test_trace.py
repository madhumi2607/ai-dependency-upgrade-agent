"""Tests for run tracing: the event stream from run_agent, replay, and RunTrace's files."""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from agent.llm import assistant_message, LLMResponse, ToolCall, tool_result_message
from agent.loop import RunResult, ToolBox, run_agent
from agent.trace import RunTrace
from mcp_servers.repo_tools.server import build_server as build_repo_tools
from tests.test_loop import FAILING, PASSING, TASK, ScriptedLLM, call, fake_sandbox, repo  # noqa: F401  (repo is a fixture)


def run_with_events(llm: ScriptedLLM, repo_path: Path, reports: list[dict[str, Any]], on_event: Any) -> RunResult:
    sandbox, _ = fake_sandbox(reports)

    async def go() -> RunResult:
        async with ToolBox([build_repo_tools(repo_path), sandbox]) as toolbox:
            return await run_agent(llm, toolbox, TASK, on_event=on_event)

    return asyncio.run(go())


def scripted() -> ScriptedLLM:
    return ScriptedLLM(
        LLMResponse(text="Let me look first."),  # no tool call -> nudge
        call("read_file", {"path": "pkg/models.py"}, call_id="r1"),
        call("finish", {"summary": "too early"}, call_id="f1"),  # rejected: tests still fail
        call("finish", {"summary": "done"}, call_id="f2"),
    )


def test_event_stream_order(repo: Path) -> None:
    events: list[dict[str, Any]] = []
    result = run_with_events(scripted(), repo, [FAILING, FAILING, PASSING], events.append)
    assert result.success
    summary = [(e["type"], e.get("purpose") or e.get("name")) for e in events]
    assert summary == [
        ("start", None), ("test_run", "baseline"), ("user_message", None),
        ("llm_call", None), ("user_message", None),  # nudge
        ("llm_call", None), ("tool_call", "read_file"),
        ("llm_call", None), ("test_run", "verification"), ("tool_call", "finish"),
        ("llm_call", None), ("test_run", "verification"), ("tool_call", "finish"),
    ]
    assert all("ts" in e for e in events)
    assert events[0]["system"] and {t["name"] for t in events[0]["tools"]} >= {"read_file", "finish"}


def test_replaying_events_rebuilds_the_exact_history(repo: Path) -> None:
    """The trace must be enough to reconstruct what the model saw on every call."""
    llm = scripted()
    events: list[dict[str, Any]] = []
    run_with_events(llm, repo, [FAILING, FAILING, PASSING], events.append)

    rebuilt: list[dict[str, Any]] = []
    for e in events:
        if e["type"] == "user_message":
            rebuilt.append({"role": "user", "content": e["content"]})
        elif e["type"] == "llm_call":
            calls = [ToolCall(c["id"], c["name"], c["arguments"], c["error"]) for c in e["tool_calls"]]
            rebuilt.append(assistant_message(LLMResponse(text=e["text"], tool_calls=calls, provider_state=e["provider_state"])))
        elif e["type"] == "tool_call":
            rebuilt.append(tool_result_message(ToolCall(e["id"], e["name"], e["arguments"]), e["result"]))

    last_seen = llm.seen[-1]  # history sent on the final model call
    assert rebuilt[: len(last_seen)] == last_seen


def test_run_trace_writes_all_files(repo: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True)

    trace = RunTrace.create(tmp_path / "runs", repo=repo, meta={"model": "scripted"})
    llm = ScriptedLLM(
        call("edit_file", {"path": "pkg/models.py", "old_text": "ModelField", "new_text": "FieldInfo"}),
        call("finish", {"summary": "done"}),
    )
    result = run_with_events(llm, repo, [FAILING, PASSING], trace)
    trace.finish(result)

    meta = json.loads((trace.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["model"] == "scripted" and len(meta["repo_commit"]) == 40 and trace.path.name == meta["run_id"]
    events = [json.loads(line) for line in (trace.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["type"] == "start" and events[-1]["name"] == "finish"
    assert json.loads((trace.path / "result.json").read_text(encoding="utf-8"))["success"] is True
    assert "+from pydantic.fields import FieldInfo" in (trace.path / "diff.patch").read_text(encoding="utf-8")


def _repo_with_crlf_file(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)
    (repo / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (repo / "lf.py").write_bytes(b"c = 3\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    return repo


def test_diff_is_recorded_byte_exact_and_reapplies(tmp_path) -> None:
    """CRLF files must survive record -> read_diff -> apply (they broke on Windows text mode)."""
    import subprocess
    from agent.loop import RunResult
    from agent.trace import RunTrace, read_diff
    from mcp_servers.git_tools.tools import apply_patch

    repo = _repo_with_crlf_file(tmp_path)
    trace = RunTrace.create(tmp_path / "runs", repo, meta={})
    (repo / "crlf.py").write_bytes(b"a = 10\r\nb = 2\r\n")
    (repo / "lf.py").write_bytes(b"c = 30\n")
    trace.finish(RunResult())
    assert b"a = 10\r\n" in (trace.path / "diff.patch").read_bytes()

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--", "."], check=True)
    apply_patch(repo, read_diff(trace.path), tmp_path / "p.patch")
    assert (repo / "crlf.py").read_bytes() == b"a = 10\r\nb = 2\r\n" and (repo / "lf.py").read_bytes() == b"c = 30\n"


def test_old_text_mode_diffs_still_apply(tmp_path) -> None:
    """The pre-fix format: every line CRLF (headers too), and CRLF content lines had lost their CR."""
    from agent.trace import read_diff
    from mcp_servers.git_tools.tools import apply_patch

    repo = _repo_with_crlf_file(tmp_path)
    old = ("diff --git a/crlf.py b/crlf.py\n--- a/crlf.py\n+++ b/crlf.py\n@@ -1,2 +1,2 @@\n-a = 1\n+a = 10\n b = 2\n"
           "diff --git a/lf.py b/lf.py\n--- a/lf.py\n+++ b/lf.py\n@@ -1 +1 @@\n-c = 3\n+c = 30\n").replace("\n", "\r\n")
    run = tmp_path / "old-run"
    run.mkdir()
    (run / "diff.patch").write_bytes(old.encode())
    diff = read_diff(run)
    assert "\r" not in diff  # translated back to what git produced (minus the lost CRs)
    apply_patch(repo, diff, tmp_path / "p.patch")
    assert (repo / "lf.py").read_bytes() == b"c = 30\n" and b"a = 10" in (repo / "crlf.py").read_bytes()
