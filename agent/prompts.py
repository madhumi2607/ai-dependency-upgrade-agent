"""Prompts for the migration agent. Kept in one place so experiments can vary them."""

import json
from typing import Any

from agent.libraries import profile

SYSTEM_PROMPT = """\
You are an autonomous software engineer migrating a Python repository from \
{library} {old_version} to {library} {new_version}.

You can only act through tools:
- list_files, search_code, read_file, edit_file: explore and edit the repository.
- run_tests: run the test suite in a sandbox with {library} {new_version} installed. \
Pass test ids to run a subset; omit them to run everything. Failures are grouped by root cause.
{docs_line}- finish: call this when every test passes. It triggers a full verification run; \
if anything still fails you will get the results and must keep working.

Rules:
- Fix the code under test, never the tests. Test files cannot be edited, and tests must not be skipped.
- Do a real migration to the {library} {new_version} API. Do not route around it with \
compatibility shims that keep the old API alive ({no_shortcuts}).
- Update the dependency pin in the project metadata (pyproject.toml, setup.py/setup.cfg or \
requirements files) so it allows {library} {new_version} and excludes versions the migrated code \
no longer supports. Also check the pins of companion packages that must move with it (for example \
web frameworks or plugins built on {library}): their minimum must support {library} {new_version}.
- Work one root cause at a time, starting with the largest failure group. Read the code at the \
reported location before editing it. Prefer small, targeted edits.
- After related edits, re-run the affected tests to check them before moving on.
"""

NUDGE = "Continue by calling a tool. If every test passes, call finish with a summary of your changes."

# Appended to a full-suite run_tests result that passed: a model that keeps polishing after the
# tests pass can run out of steps before calling finish (seen with Haiku 5.5 on pydantic-cli).
ALL_PASS_HINT = (
    "All tests pass. If the migration is complete, call finish now with a summary; "
    "optional tidy-ups (docs, comments) can be listed in the summary for review instead."
)


def budget_warning(steps_left: int) -> str:
    return (
        f"Budget notice: you have {steps_left} steps left before this run stops. "
        "If the tests pass, call finish now; otherwise make the most important fix and run the tests."
    )


DOCS_LINE = (
    "- search_docs: search the official {library} migration guide. Look up the replacement for "
    "an old API before changing it, using exact names from the error or the code.\n"
)


def system_prompt(library: str, old_version: str, new_version: str, has_docs: bool = False) -> str:
    """has_docs: mention search_docs only when the tool is actually available (it's optional,
    so runs with and without it can be compared)."""
    docs_line = DOCS_LINE.format(library=library) if has_docs else ""
    return SYSTEM_PROMPT.format(library=library, old_version=old_version, new_version=new_version, docs_line=docs_line,
                                no_shortcuts=profile(library).no_shortcuts)


def initial_message(library: str, new_version: str, report: dict[str, Any]) -> str:
    """First user turn: the baseline test run, so the model starts from real failures."""
    return (
        f"The repository's test suite was run with {library} {new_version} installed. Result:\n"
        f"{json.dumps(report, indent=1)}\n\n"
        "Start migrating. Begin with the largest failure group."
    )


def verification_failed(report: dict[str, Any]) -> str:
    return (
        "finish was rejected: the full test suite does not pass yet. Keep working. Result:\n"
        f"{json.dumps(report, indent=1)}"
    )
