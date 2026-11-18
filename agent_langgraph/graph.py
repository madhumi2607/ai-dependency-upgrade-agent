"""The migration agent as a LangGraph StateGraph: the same behaviour as agent/loop.py, with the
control flow expressed as nodes and edges instead of a while loop.

    baseline --(passing / can't run)--------------------------------------> END
       |
       v
    agent --(budget hit / LLM error)--> final --> END
     ^  |
     |  +--(no tool call)--> nudge --+
     |  |                            |
     |  +--(tool calls)--> tools --(finish verified)--> END
     |                       |       |
     +-----------------------+-------+

What changes vs. the hand-written loop: only orchestration. The LLM client (agent/llm.py), the
MCP ToolBox, the prompts, the budgets and the trace events are the same objects, so a benchmark
difference between the two can only come from the control flow, not from a different model
client, message format or prompt caching.

What LangGraph adds: state with reducers (the message list is append-only *by construction*:
nodes can only return new messages, never edit old ones), explicit routing functions you can
unit-test and draw, and hooks for checkpointing / human-in-the-loop interrupts (not used yet).
"""

import asyncio
import operator
import time
from dataclasses import asdict
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

# Baseline statuses that mean the tests can't run at all (see agent/loop.py for why we stop).
CANNOT_RUN = ("sandbox_error", "timeout", "install_failed", "upgrade_failed")


class AgentState(TypedDict, total=False):
    # operator.add as the reducer: a node's {"messages": [...]} is appended, never replaces.
    messages: Annotated[list[dict[str, Any]], operator.add]
    steps: int
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


def build_graph(llm: LLM, toolbox: ToolBox, task: TaskSpec, config: LoopConfig, emit: Any, system: str):
    """Compile the graph. Dependencies that aren't data (LLM, MCP connections, the event sink)
    are closed over rather than put in the state: state is what a checkpoint would save."""
    tools = [*toolbox.specs, FINISH_TOOL]

    def user_message(state: AgentState, content: str) -> dict[str, Any]:
        emit("user_message", step=state.get("steps", 0), content=content)
        return {"role": "user", "content": content}

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
        if steps >= config.max_steps:
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
                    return {"messages": new_messages, "pending_calls": [], "success": True, "stop_reason": "finished",
                            "summary": call.arguments.get("summary"), "final_report": report}
                tool_result(call, prompts.verification_failed(report), started)
            else:
                content = await _run_tool_call(toolbox, call, config.max_tool_result_chars)
                if call.name == "run_tests" and not call.arguments.get("tests") and _passed(content):
                    content = f"{content}\n\n{prompts.ALL_PASS_HINT}"
                tool_result(call, content, started)

        # Same rule as the loop: warn once, as a new message, when a few steps are left.
        if config.max_steps - steps == BUDGET_WARNING_STEPS:
            new_messages.append(user_message(state, prompts.budget_warning(BUDGET_WARNING_STEPS)))
        return {"messages": new_messages, "pending_calls": []}

    async def final(state: AgentState) -> dict[str, Any]:
        # Budget exhausted or LLM failed: one last full run so the metrics show where the repo ended up.
        return {"final_report": await test_run("final")}

    # ---------- edges ----------

    def after_baseline(state: AgentState) -> str:
        return END if state.get("stop_reason") else "agent"

    def after_agent(state: AgentState) -> str:
        if state.get("stop_reason"):
            return "final"
        return "tools" if state.get("pending_calls") else "nudge"

    def after_tools(state: AgentState) -> str:
        return END if state.get("stop_reason") == "finished" else "agent"

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
    graph.add_conditional_edges("tools", after_tools, ["agent", END])
    graph.add_edge("final", END)
    return graph.compile()


async def run_agent(
    llm: LLM, toolbox: ToolBox, task: TaskSpec, config: LoopConfig | None = None, on_event: EventHandler | None = None,
) -> RunResult:
    """Drop-in replacement for agent.loop.run_agent: same arguments, same events, same RunResult."""
    config = config or LoopConfig()

    def emit(event_type: str, **data: Any) -> None:
        if on_event:
            on_event({"type": event_type, "ts": time.time(), **data})

    system = prompts.system_prompt(task.library, task.old_version, task.new_version, has_docs=toolbox.has("search_docs"))
    tools = [*toolbox.specs, FINISH_TOOL]
    emit("start", task=asdict(task), config=asdict(config), system=system, tools=[asdict(t) for t in tools])

    app = build_graph(llm, toolbox, task, config, emit, system)
    # LangGraph stops a graph after recursion_limit node executions ("supersteps"). Each model
    # step is at most two (agent + tools or nudge), plus baseline and final: our own budgets
    # must always trip first, so this limit is never the reason a run ends.
    state = await app.ainvoke({"messages": []}, {"recursion_limit": 2 * config.max_steps + 10})
    return RunResult(
        success=state.get("success", False), stop_reason=state.get("stop_reason", ""), error=state.get("error"),
        steps=state.get("steps", 0), input_tokens=state.get("input_tokens", 0),
        cache_read_tokens=state.get("cache_read_tokens", 0), output_tokens=state.get("output_tokens", 0),
        summary=state.get("summary"), initial_report=state.get("initial_report", {}),
        final_report=state.get("final_report", {}),
    )
