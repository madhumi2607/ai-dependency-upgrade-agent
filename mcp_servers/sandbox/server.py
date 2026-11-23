"""sandbox MCP server: run the target repo's tests in Docker and summarise the failures.

How tests are installed and run is fixed at startup from environment variables
(see .env.example); the agent only chooses *which* tests to run.

Run (stdio transport):
    uv run python -m mcp_servers.sandbox.server
"""

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_servers.sandbox import runner
from mcp_servers.sandbox.runner import SandboxConfig


def config_from_env() -> SandboxConfig:
    """Read the task's sandbox settings from environment variables."""
    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise SystemExit(f"Set {name} (see .env.example).")
        return value

    return SandboxConfig(
        repo_root=Path(required("TARGET_REPO_ROOT")),
        install_cmd=required("SANDBOX_INSTALL_CMD"),
        test_cmd=required("SANDBOX_TEST_CMD"),
        upgrade=os.environ.get("SANDBOX_UPGRADE", ""),
        test_deps=os.environ.get("SANDBOX_TEST_DEPS", ""),
        image=os.environ.get("SANDBOX_IMAGE", SandboxConfig.image),
        timeout_seconds=int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", SandboxConfig.timeout_seconds)),
    )


MAX_SITES = 25  # deprecated call sites returned by find_deprecations (keep the reviewer's context small)


def build_server(config: SandboxConfig, review_tools: bool = False) -> MCPServer:
    """Create the server for one task. Separate from main() so tests can inject a config.

    review_tools adds find_deprecations, for the reviewer agent only (SANDBOX_REVIEW_TOOLS=1): the
    single-agent baselines must see exactly the tools they were benchmarked with."""
    server = MCPServer(
        "sandbox",
        instructions="Runs the target repo's test suite in an isolated Docker container with the upgraded dependency.",
    )

    @server.tool()
    def run_tests(tests: list[str] | None = None) -> dict[str, Any]:
        """Install the repo with the upgraded dependency and run its tests in a fresh container.

        tests: optional pytest ids or paths to run (e.g. ["tests/test_x.py::test_y"]); omit to run
        the whole suite. Failures are grouped by root cause, each with error type, message, the
        deepest file:line inside the repo, a count and example test ids. Takes ~30s or more.
        """
        return runner.run_tests(config, tests)

    if review_tools:
        @server.tool()
        def find_deprecations() -> dict[str, Any]:
            """Run the full test suite and list where the code still uses deprecated API of the upgraded
            library: the deprecation warnings the suite triggers, by file:line in the repo's own source
            (test files excluded). Old-style code that still runs on the new version shows up here even
            though every test passes. Also lists deprecations the library reports from inside itself
            (their message names the old API). Takes as long as a full test run.
            """
            report = runner.run_tests(config, None, deprecations=True)
            found = report.get("deprecations") or {}
            sites = found.get("library", [])
            return {
                "status": report.get("status"), "counts": report.get("counts"),
                "deprecated_call_sites": [{"location": s["location"], "message": s["message"]} for s in sites[:MAX_SITES]],
                "more_call_sites": max(len(sites) - MAX_SITES, 0),
                "reported_inside_library": [s["message"] for s in found.get("library_internal", [])][:10],
            }

    return server


def main() -> None:
    build_server(config_from_env(), review_tools=os.environ.get("SANDBOX_REVIEW_TOOLS") == "1").run()


if __name__ == "__main__":
    main()
