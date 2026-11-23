"""Run the LangGraph version of the agent. Same flags and run folders as agent.loop:
    uv run --env-file .env python -m agent_langgraph.run --task geojson-pydantic

Setup mirrors agent/loop.py's _main (kept separate so the hand-written loop stays untouched);
the only differences are which run_agent is called and "impl" in meta.json.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from agent.checks import check_run
from agent.llm import get_llm
from agent.loop import PROJECT_ROOT, LoopConfig, TaskSpec, ToolBox, _stdio_server, docs_index_ready
from agent.trace import RunTrace
from agent_langgraph.graph import ReviewConfig, run_agent
from benchmark.tasks import load_tasks, prepare_checkout


async def _main(args: argparse.Namespace) -> RunTrace:
    if args.task:
        benchmark_task = load_tasks()[args.task]
        prepare_checkout(benchmark_task)
        os.environ.update(benchmark_task.sandbox_env())  # before the servers start: they read it
        task = TaskSpec(benchmark_task.library, benchmark_task.old_version, benchmark_task.new_version)
    else:
        task = TaskSpec(args.library, args.old_version, args.new_version)
    if args.review:
        os.environ["SANDBOX_REVIEW_TOOLS"] = "1"  # adds find_deprecations; must precede starting the servers
    servers = [_stdio_server("mcp_servers.repo_tools.server"), _stdio_server("mcp_servers.sandbox.server")]
    use_docs = not args.no_docs and docs_index_ready()
    if use_docs:
        os.environ["DOCS_LIBRARY"] = task.library
        servers.append(_stdio_server("mcp_servers.docs_search.server"))
    llm = get_llm()
    trace = RunTrace.create(args.runs_dir, repo=os.environ["TARGET_REPO_ROOT"], meta={
        "task_id": args.task,
        "impl": "langgraph-review" if args.review else "langgraph",
        "docs_search": use_docs,
        "provider": os.environ.get("LLM_PROVIDER", "claude"),
        "model": llm.model,
        "effort": getattr(llm, "effort", None),
        "sandbox": {k: os.environ.get(k) for k in ("SANDBOX_INSTALL_CMD", "SANDBOX_TEST_DEPS", "SANDBOX_TEST_CMD", "SANDBOX_UPGRADE", "SANDBOX_IMAGE")},
    })
    print(f"Recording run to {trace.path}", file=sys.stderr)  # benchmark/runner.py looks for this line
    async with ToolBox(servers) as toolbox:
        review = ReviewConfig(repo_root=Path(os.environ["TARGET_REPO_ROOT"])) if args.review else None
        result = await run_agent(llm, toolbox, task, LoopConfig(max_steps=args.max_steps), on_event=trace, review=review)
    trace.finish(result)
    try:
        findings = check_run(trace.path)
        print(f"Review checks: {len(findings)} finding(s)", file=sys.stderr)
    except Exception as exc:  # the checks must never lose a finished run
        print(f"Review checks failed: {exc}", file=sys.stderr)
    print(json.dumps({k: v for k, v in asdict(result).items() if k not in ("initial_report", "final_report")}, indent=1))
    return trace


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LangGraph migration agent.")
    parser.add_argument("--task", help="task id from benchmark/tasks.yaml")
    parser.add_argument("--library", default="pydantic")
    parser.add_argument("--old-version", default="1")
    parser.add_argument("--new-version", default="2")
    parser.add_argument("--max-steps", type=int, default=LoopConfig.max_steps)
    parser.add_argument("--no-docs", action="store_true")
    parser.add_argument("--review", action="store_true", help="add the reviewer agent (multi-agent mode)")
    parser.add_argument("--runs-dir", default=str(PROJECT_ROOT / "runs"))
    trace = asyncio.run(_main(parser.parse_args()))
    print(f"Trace: {trace.path}")


if __name__ == "__main__":
    main()
