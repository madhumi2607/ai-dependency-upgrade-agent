"""The migration agent as a LangGraph StateGraph: the same behaviour as agent/loop.py, with the
control flow expressed as nodes and edges instead of a while loop. Optionally a second agent, a
reviewer, checks the migration once the tests pass (review mode).

    baseline --(passing / can't run)--------------------------------------> END
       |
       v
    agent --(budget hit / LLM error)--> final --> END
     ^  |
     |  +--(no tool call)--> nudge --+
     |  |                            |
     |  +--(tool calls)--> tools --(finish verified)--> END            (single agent)
     |                       |       |                  \\
     +-----------------------+-------+                   review_start  (review mode)
     |                                                       |
     +-- deliver <--(findings)-- reviewer_tools <--> reviewer --(approved / budget)--> END

What changes vs. the hand-written loop: only orchestration. The LLM client (agent/llm.py), the
MCP ToolBox, the prompts, the budgets and the trace events are the same objects, so a benchmark
difference between the two can only come from the control flow, not from a different model
client, message format or prompt caching. With review=None the graph is exactly that single agent
(tests/test_loop.py runs every loop test against it and checks the event streams are identical).

Review mode: when the migrator's finish passes verification, a reviewer with a fresh context and
read-only tools (plus find_deprecations) looks for old API the tests can't catch. It approves, or
its findings go back to the migrator, which must pass verification again; at most max_rounds.

What LangGraph adds: state with reducers (the message list is append-only *by construction*:
nodes can only return new messages, never edit old ones), explicit routing functions you can
unit-test and draw, and hooks for checkpointing / human-in-the-loop interrupts (not used yet).
"""

import asyncio
import json
import operator
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent import prompts
from agent.llm import LLM, ToolCall, assistant_message, tool_result_message
from agent.loop import (
    BUDGET_WARNING_STEPS,
    FINISH_TOOL,
    EventHandler,
    LoopConfig,
    RunResult,
    TaskSpec,
    ToolBox,
    _emit_llm_call,
    _passed,
    _run_tool_call,
)
from agent_langgraph import review_prompts

# Baseline statuses that mean the tests can't run at all (see agent/loop.py for why we stop).
CANNOT_RUN = ("sandbox_error", "timeout", "install_failed", "upgrade_failed")
# Tools only the reviewer may use, and the reviewer's whole (read-only) toolset.
REVIEWER_ONLY = {"find_deprecations"}
REVIEWER_TOOLS = {"list_files", "search_code", "read_file", "search_docs", "find_deprecations"}
MAX_DIFF_CHARS = 12_000  # the migrator's diff as shown to the reviewer


@dataclass
class ReviewConfig:
    max_rounds: int = 2  # reviews per run; after the last one the run ends whatever it finds
    max_steps_per_round: int = 12  # reviewer LLM calls per review
    # Migrator steps granted to act on each set of findings: the review comes after the migrator's
    # own work, often with few of its steps left (a first trial had 7), and findings are new work.
    fix_steps: int = 15
    repo_root: Path | None = None  # where to take the migrator's diff from


class AgentState(TypedDict, total=False):
    # operator.add as the reducer: a node's {"messages": [...]} is appended, never replaces.
    messages: Annotated[list[dict[str, Any]], operator.add]
    steps: int  # migrator LLM calls
    step_limit: int  # the migrator's budget: config.max_steps, raised by fix_steps after each review
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    pending_calls: list[ToolCall]  # tool calls from the latest model turn, for the tools node
    success: bool
    stop_reason: str
    error: str | None
    summary: str | None
    initial_report: dict[str, Any]
    final_report: dict[str, Any]
    # review mode only
    verified_once: bool  # the migrator passed a full verification at least once
    review_round: int
    review_messages: list[dict[str, Any]]  # the reviewer's own conversation (fresh each round)
    review_round_steps: int
    review_steps: int  # reviewer LLM calls, all rounds
    review_pending: list[ToolCall]
    verdict: dict[str, Any] | None


def build_graph(llm: LLM, toolbox: ToolBox, task: TaskSpec, config: LoopConfig, emit: Any, system: str,
                review: ReviewConfig | None = None):
    """Compile the graph. Dependencies that aren't data (LLM, MCP connections, the event sink)
    are closed over rather than put in the state: state is what a checkpoint would save."""
    tools = migrator_tools(toolbox)
    review_tools = [s for s in toolbox.specs if s.name in REVIEWER_TOOLS] + [review_prompts.SUBMIT_REVIEW]
    review_system = review_prompts.SYSTEM.format(library=task.library, old=task.old_version, new=task.new_version)

    def user_message(state: AgentState, content: str) -> dict[str, Any]:
        emit("user_message", step=state.get("steps", 0), content=content)
        return {"role": "user", "content": content}

    def reviewer_emit(event_type: str, **data: Any) -> None:
        emit(event_type, agent="reviewer", **data)

    async def test_run(purpose: str) -> dict[str, Any]:
        report = await toolbox.run_tests()
        emit("test_run", purpose=purpose, report=report)
        return report

    # ---------- nodes: each returns only the state keys it changes ----------

    async def baseline(state: AgentState) -> dict[str, Any]:
        report = await test_run("baseline")
        if report.get("status") == "passed":
            return {"initial_report": report, "final_report": report, "success": True, "stop_reason": "already_passing"}
        if report.get("status") in CANNOT_RUN:
            error = str(report.get("detail") or report.get("status"))[:1000]
            return {"initial_report": report, "final_report": report, "stop_reason": "sandbox_error", "error": error}
        first = user_message(state, prompts.initial_message(task.library, task.new_version, report))
        return {"initial_report": report, "messages": [first]}

    async def agent(state: AgentState) -> dict[str, Any]:
        steps = state.get("steps", 0)
        if steps >= state.get("step_limit", config.max_steps):
            return {"stop_reason": "max_steps"}
        billable = state.get("input_tokens", 0) - state.get("cache_read_tokens", 0) + state.get("output_tokens", 0)
        if billable >= config.max_billable_tokens:
            return {"stop_reason": "token_budget"}
        started = time.monotonic()
        try:
            # The LLM client is synchronous; a thread keeps the MCP connections serviced.
            response = await asyncio.to_thread(llm.chat, system, state["messages"], tools)
        except Exception as exc:  # adapters already retried
            error = f"{type(exc).__name__}: {exc}"[:1000]
            emit("llm_error", step=steps + 1, error=error)
            return {"stop_reason": "llm_error", "error": error}
        steps += 1
        _emit_llm_call(emit, steps, response, time.monotonic() - started)
        return {
            "steps": steps,
            "input_tokens": state.get("input_tokens", 0) + response.input_tokens,
            "cache_read_tokens": state.get("cache_read_tokens", 0) + response.cache_read_tokens,
            "output_tokens": state.get("output_tokens", 0) + response.output_tokens,
            "messages": [assistant_message(response)],
            "pending_calls": response.tool_calls,
        }

    async def nudge(state: AgentState) -> dict[str, Any]:
        return {"messages": [user_message(state, prompts.NUDGE)]}

    async def run_tools(state: AgentState) -> dict[str, Any]:
        steps, new_messages = state["steps"], []

        def tool_result(call: ToolCall, content: str, started: float) -> None:
            new_messages.append(tool_result_message(call, content))
            emit("tool_call", step=steps, id=call.id, name=call.name, arguments=call.arguments, result=content,
                 is_error=content.startswith("ERROR:"), seconds=round(time.monotonic() - started, 2))

        for call in state["pending_calls"]:
            started = time.monotonic()
            if call.name == FINISH_TOOL.name and call.error is None:
                report = await test_run("verification")
                if report.get("status") == "passed":
                    tool_result(call, "Verified: the full test suite passes.", started)
                    done = {"messages": new_messages, "pending_calls": [], "success": True, "stop_reason": "finished",
                            "summary": call.arguments.get("summary"), "final_report": report}
                    return {**done, "verified_once": True} if review else done
                tool_result(call, prompts.verification_failed(report), started)
            else:
                content = await _run_tool_call(toolbox, call, config.max_tool_result_chars)
                if call.name == "run_tests" and not call.arguments.get("tests") and _passed(content):
                    content = f"{content}\n\n{prompts.ALL_PASS_HINT}"
                tool_result(call, content, started)

        # Same rule as the loop: warn once, as a new message, when a few steps are left.
        if state.get("step_limit", config.max_steps) - steps == BUDGET_WARNING_STEPS:
            new_messages.append(user_message(state, prompts.budget_warning(BUDGET_WARNING_STEPS)))
        return {"messages": new_messages, "pending_calls": []}

    async def final(state: AgentState) -> dict[str, Any]:
        # Budget exhausted or LLM failed: one last full run so the metrics show where the repo ended up.
        report = await test_run("final")
        if state.get("verified_once") and report.get("status") == "passed":
            # Review mode: the migrator was verified, then ran out of budget fixing review findings
            # without breaking anything. The migration still passes; the findings show in the trace.
            return {"final_report": report, "success": True}
        return {"final_report": report}

    # ---------- the reviewer (review mode) ----------

    async def review_start(state: AgentState) -> dict[str, Any]:
        round_ = state.get("review_round", 0) + 1
        content = review_prompts.first_message(state.get("summary"), _diff(review.repo_root))
        reviewer_emit("user_message", step=0, content=content, round=round_)
        return {"review_round": round_, "review_messages": [{"role": "user", "content": content}],
                "review_round_steps": 0, "review_pending": [], "verdict": None}

    async def reviewer(state: AgentState) -> dict[str, Any]:
        round_steps = state.get("review_round_steps", 0)
        if round_steps >= review.max_steps_per_round:
            # Out of review budget without a verdict: nothing concrete to send back, so the run ends.
            verdict = {"approve": True, "findings": [], "note": "review step budget exhausted"}
            reviewer_emit("review", round=state["review_round"], **verdict)
            return {"verdict": verdict}
        started = time.monotonic()
        try:
            response = await asyncio.to_thread(llm.chat, review_system, state["review_messages"], review_tools)
        except Exception as exc:  # a failed review must not fail a verified migration
            verdict = {"approve": True, "findings": [], "note": f"reviewer error: {type(exc).__name__}"}
            reviewer_emit("review", round=state["review_round"], **verdict)
            return {"verdict": verdict}
        round_steps += 1
        review_steps = state.get("review_steps", 0) + 1
        _emit_llm_call(reviewer_emit, review_steps, response, time.monotonic() - started)
        messages = [*state["review_messages"], assistant_message(response)]
        if not response.tool_calls:
            messages.append({"role": "user", "content": "Use your tools, then call submit_review."})
        return {
            "review_round_steps": round_steps, "review_steps": review_steps,
            "input_tokens": state.get("input_tokens", 0) + response.input_tokens,
            "cache_read_tokens": state.get("cache_read_tokens", 0) + response.cache_read_tokens,
            "output_tokens": state.get("output_tokens", 0) + response.output_tokens,
            "review_messages": messages, "review_pending": response.tool_calls,
        }

    async def reviewer_tools(state: AgentState) -> dict[str, Any]:
        messages, verdict = list(state["review_messages"]), None
        for call in state["review_pending"]:
            started = time.monotonic()
            if call.name == review_prompts.SUBMIT_REVIEW.name and call.error is None:
                findings = _findings(call.arguments.get("findings"))
                verdict = {"approve": bool(call.arguments.get("approve")) or not findings, "findings": findings}
                content = "Review recorded."
            elif call.name not in REVIEWER_TOOLS and call.name != review_prompts.SUBMIT_REVIEW.name:
                content = f"ERROR: {call.name} is not available to the reviewer (read-only tools only)."
            else:
                content = await _run_tool_call(toolbox, call, config.max_tool_result_chars)
            messages.append(tool_result_message(call, content))
            reviewer_emit("tool_call", step=state["review_steps"], id=call.id, name=call.name, arguments=call.arguments,
                          result=content, is_error=content.startswith("ERROR:"), seconds=round(time.monotonic() - started, 2))
        if verdict:
            reviewer_emit("review", round=state["review_round"], **verdict)
        elif review.max_steps_per_round - state["review_round_steps"] == 2:
            messages.append({"role": "user", "content": review_prompts.BUDGET_NOTE})
        return {"review_messages": messages, "review_pending": [], "verdict": verdict}

    async def deliver(state: AgentState) -> dict[str, Any]:
        """Send the findings to the migrator, which goes back to work and must pass verification again."""
        content = review_prompts.findings_message(task.library, state["verdict"]["findings"])
        return {"messages": [user_message(state, content)], "success": False, "stop_reason": "", "verdict": None,
                "step_limit": state.get("steps", 0) + review.fix_steps}

    # ---------- edges ----------

    def after_baseline(state: AgentState) -> str:
        return END if state.get("stop_reason") else "agent"

    def after_agent(state: AgentState) -> str:
        if state.get("stop_reason"):
            return "final"
        return "tools" if state.get("pending_calls") else "nudge"

    def after_tools(state: AgentState) -> str:
        if state.get("stop_reason") != "finished":
            return "agent"
        if review and state.get("review_round", 0) < review.max_rounds:
            return "review_start"
        return END

    def after_reviewer(state: AgentState) -> str:
        if state.get("verdict"):
            return END  # out of budget or failed: ends the review
        return "reviewer_tools" if state.get("review_pending") else "reviewer"

    def after_reviewer_tools(state: AgentState) -> str:
        verdict = state.get("verdict")
        if verdict is None:
            return "reviewer"
        return END if verdict["approve"] else "deliver"

    graph = StateGraph(AgentState)
    graph.add_node("baseline", baseline)
    graph.add_node("agent", agent)
    graph.add_node("nudge", nudge)
    graph.add_node("tools", run_tools)
    graph.add_node("final", final)
    graph.add_edge(START, "baseline")
    graph.add_conditional_edges("baseline", after_baseline, ["agent", END])
    graph.add_conditional_edges("agent", after_agent, ["tools", "nudge", "final"])
    graph.add_edge("nudge", "agent")
    graph.add_edge("final", END)
    if review is None:
        graph.add_conditional_edges("tools", after_tools, ["agent", END])
    else:
        graph.add_node("review_start", review_start)
        graph.add_node("reviewer", reviewer)
        graph.add_node("reviewer_tools", reviewer_tools)
        graph.add_node("deliver", deliver)
        graph.add_conditional_edges("tools", after_tools, ["agent", "review_start", END])
        graph.add_edge("review_start", "reviewer")
        graph.add_conditional_edges("reviewer", after_reviewer, ["reviewer_tools", "reviewer", END])
        graph.add_conditional_edges("reviewer_tools", after_reviewer_tools, ["reviewer", "deliver", END])
        graph.add_edge("deliver", "agent")
    return graph.compile()


def _findings(raw: Any) -> list[str]:
    """submit_review's findings as a list of strings. Models sometimes send the list JSON-encoded
    as one string; iterating that string once turned one finding into 529 one-character ones."""
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = [raw]  # a plain sentence: one finding
        raw = decoded if isinstance(decoded, list) else [str(decoded)]
    return [str(f).strip() for f in raw or [] if str(f).strip()]


def migrator_tools(toolbox: ToolBox) -> list:
    """The migrator's tools: everything except the reviewer-only ones, plus finish."""
    return [*(s for s in toolbox.specs if s.name not in REVIEWER_ONLY), FINISH_TOOL]


def _diff(repo_root: Path | None) -> str:
    if repo_root is None:
        return "(diff not available)"
    proc = subprocess.run(["git", "-C", str(repo_root), "diff"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    diff = proc.stdout if proc.returncode == 0 else ""
    return diff[:MAX_DIFF_CHARS] + (f"\n... [truncated {len(diff) - MAX_DIFF_CHARS} chars]" if len(diff) > MAX_DIFF_CHARS else "")


async def run_agent(
    llm: LLM, toolbox: ToolBox, task: TaskSpec, config: LoopConfig | None = None, on_event: EventHandler | None = None,
    review: ReviewConfig | None = None,
) -> RunResult:
    """Drop-in replacement for agent.loop.run_agent: same arguments, same events, same RunResult.
    review: run the reviewer agent after a verified finish (steps then counts both agents' calls)."""
    config = config or LoopConfig()

    def emit(event_type: str, **data: Any) -> None:
        if on_event:
            on_event({"type": event_type, "ts": time.time(), **data})

    system = prompts.system_prompt(task.library, task.old_version, task.new_version, has_docs=toolbox.has("search_docs"))
    tools = migrator_tools(toolbox)
    emit("start", task=asdict(task), config=asdict(config), system=system, tools=[asdict(t) for t in tools])

    app = build_graph(llm, toolbox, task, config, emit, system, review)
    # LangGraph stops a graph after recursion_limit node executions ("supersteps"). Each model
    # step is at most two (agent + tools or nudge), plus baseline and final: our own budgets
    # must always trip first, so this limit is never the reason a run ends.
    limit = 2 * config.max_steps + 10
    if review:
        limit += review.max_rounds * (2 * review.max_steps_per_round + 2 * review.fix_steps + 6)
    state = await app.ainvoke({"messages": []}, {"recursion_limit": limit})
    return RunResult(
        success=state.get("success", False), stop_reason=state.get("stop_reason", ""), error=state.get("error"),
        steps=state.get("steps", 0) + state.get("review_steps", 0), input_tokens=state.get("input_tokens", 0),
        cache_read_tokens=state.get("cache_read_tokens", 0), output_tokens=state.get("output_tokens", 0),
        summary=state.get("summary"), initial_report=state.get("initial_report", {}),
        final_report=state.get("final_report", {}),
    )
