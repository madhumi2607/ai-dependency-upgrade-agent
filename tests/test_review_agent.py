"""Tests for review mode: a reviewer agent checks the migration after the tests pass (agent_langgraph)."""

import asyncio
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from agent.llm import LLMResponse, Message, ToolCall, ToolSpec
from agent.loop import LoopConfig, RunResult, ToolBox
from agent_langgraph.graph import ReviewConfig, run_agent
from mcp_servers.repo_tools.server import build_server as build_repo_tools
from tests.test_loop import FAILING, PASSING, TASK, call

FINDING = "pkg/models.py:1 still imports ModelField; use FieldInfo"


class TwoAgentLLM:
    """Scripted responses in order, recording which tools each call was offered and what it saw."""

    model = "scripted"

    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], list[Message]]] = []

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append(([t.name for t in tools], [dict(m) for m in messages]))
        return self.responses.pop(0) if self.responses else call("list_files", {})


def review(approve: bool, findings: list[str] | None = None) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=[ToolCall("id_review", "submit_review", {"approve": approve, "findings": findings or []}, None)],
                       input_tokens=50, output_tokens=5)


def sandbox_with_review_tool(reports: list[dict[str, Any]]) -> MCPServer:
    server = MCPServer("sandbox")
    runs: list[int] = []

    @server.tool()
    def run_tests(tests: list[str] | None = None) -> dict[str, Any]:
        """Run tests."""
        runs.append(1)
        return reports[min(len(runs) - 1, len(reports) - 1)]

    @server.tool()
    def find_deprecations() -> dict[str, Any]:
        """Deprecated call sites."""
        return {"status": "passed", "deprecated_call_sites": [{"location": "pkg/models.py:1", "message": "ModelField"}]}

    return server


def run(llm: TwoAgentLLM, repo: Path, reports: list[dict[str, Any]], max_rounds: int = 2) -> tuple[RunResult, list[dict]]:
    events: list[dict[str, Any]] = []

    async def go() -> RunResult:
        async with ToolBox([build_repo_tools(repo), sandbox_with_review_tool(reports)]) as toolbox:
            return await run_agent(llm, toolbox, TASK, LoopConfig(max_steps=10), on_event=events.append,
                                   review=ReviewConfig(max_rounds=max_rounds, max_steps_per_round=4, repo_root=repo))

    return asyncio.run(go()), events


def make_repo(tmp_path: Path) -> Path:
    """A git repo, so the reviewer can be shown the migrator's diff."""
    import subprocess
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "models.py").write_text("from pydantic.fields import ModelField\nx = 1\n", encoding="utf-8")
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    return tmp_path


def test_findings_go_back_to_the_migrator_and_a_second_review_approves(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    llm = TwoAgentLLM(
        call("edit_file", {"path": "pkg/models.py", "old_text": "x = 1", "new_text": "x = 2"}),   # migrator
        call("finish", {"summary": "done"}),                                                     # verified -> review 1
        call("find_deprecations", {}),                                                           # reviewer
        review(False, [FINDING]),                                                                # -> back to migrator
        call("edit_file", {"path": "pkg/models.py", "old_text": "ModelField", "new_text": "FieldInfo"}),
        call("finish", {"summary": "done, review fixed"}),                                       # verified -> review 2
        review(True),                                                                            # approved -> end
    )
    result, events = run(llm, repo, [FAILING, PASSING])
    assert result.success and result.stop_reason == "finished" and result.summary == "done, review fixed"
    assert "FieldInfo" in (repo / "pkg" / "models.py").read_text()
    reviews = [e for e in events if e["type"] == "review"]
    assert [(r["round"], r["approve"]) for r in reviews] == [(1, False), (2, True)]
    assert result.steps == 7  # 4 migrator calls + 3 reviewer calls

    migrator_tools, reviewer_tools = llm.calls[0][0], llm.calls[2][0]
    assert "find_deprecations" not in migrator_tools and "finish" in migrator_tools
    assert set(reviewer_tools) == {"list_files", "search_code", "read_file", "find_deprecations", "submit_review"}
    # The reviewer starts fresh: one user message (summary + diff), none of the migrator's turns.
    first_review_view = llm.calls[2][1]
    assert len(first_review_view) == 1 and "x = 2" in first_review_view[0]["content"]
    # The migrator is told exactly what to fix.
    assert FINDING in str(llm.calls[4][1][-1]["content"])
    assert all(e.get("agent") == "reviewer" for e in events if e["type"] == "review")


def test_the_reviewer_cannot_edit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    llm = TwoAgentLLM(
        call("finish", {"summary": "done"}),
        call("edit_file", {"path": "pkg/models.py", "old_text": "x = 1", "new_text": "x = 3"}),  # reviewer tries
        review(True),
    )
    result, events = run(llm, repo, [FAILING, PASSING])
    assert result.success and (repo / "pkg" / "models.py").read_text().endswith("x = 1\n")
    refused = [e for e in events if e["type"] == "tool_call" and e.get("agent") == "reviewer" and e["name"] == "edit_file"]
    assert refused and refused[0]["is_error"] and "not available to the reviewer" in refused[0]["result"]


def test_review_rounds_are_capped(tmp_path: Path) -> None:
    """After the last allowed review, the run ends even if the reviewer still has findings."""
    repo = make_repo(tmp_path)
    llm = TwoAgentLLM(call("finish", {"summary": "done"}), review(False, [FINDING]),
                      call("finish", {"summary": "again"}), review(False, [FINDING]))
    result, events = run(llm, repo, [FAILING, PASSING], max_rounds=1)
    assert result.success and [e["round"] for e in events if e["type"] == "review"] == [1]


def test_findings_come_with_a_fresh_step_allowance(tmp_path: Path) -> None:
    """A review late in the migrator's budget must still leave it room to act on the findings."""
    repo = make_repo(tmp_path)
    burn = [call("list_files", {}) for _ in range(8)]  # the migrator uses 9 of its 10 steps before finishing
    llm = TwoAgentLLM(*burn, call("finish", {"summary": "done"}), review(False, [FINDING]),
                      call("edit_file", {"path": "pkg/models.py", "old_text": "ModelField", "new_text": "FieldInfo"}),
                      call("list_files", {}), call("finish", {"summary": "fixed"}), review(True))
    result, _ = run(llm, repo, [FAILING, PASSING])
    assert result.success and result.stop_reason == "finished" and result.summary == "fixed"
    assert "FieldInfo" in (repo / "pkg" / "models.py").read_text()


def test_findings_sent_as_a_json_string_are_decoded() -> None:
    """A real run's reviewer sent its list JSON-encoded; it was split into 529 single characters."""
    from agent_langgraph.graph import _findings

    assert _findings('["a.py:1 use model_dump", "b.py:2 use pattern="]') == ["a.py:1 use model_dump", "b.py:2 use pattern="]
    assert _findings("a.py:1 still uses .dict()") == ["a.py:1 still uses .dict()"]
    assert _findings(["x", " ", ""]) == ["x"] and _findings(None) == []
