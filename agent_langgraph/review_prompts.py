"""Prompts for the reviewer agent (agent_langgraph/graph.py, review mode).

The reviewer only runs once the migrator's full test suite passes. Its job is what the tests can't
see: old API that still *runs* on the new version (deprecated calls, old-style config) and modules
the tests barely touch (examples, rarely used paths). It can't edit; it reports to the migrator.
"""

from agent.llm import ToolSpec

SUBMIT_REVIEW = ToolSpec(
    name="submit_review",
    description="Your verdict. approve=true if no old API is left; otherwise approve=false with one finding per problem.",
    input_schema={
        "type": "object",
        "properties": {
            "approve": {"type": "boolean"},
            "findings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Each: file:line, what is old, and what to replace it with.",
            },
        },
        "required": ["approve"],
    },
)

SYSTEM = """You are reviewing a migration of a Python repository from {library} {old} to {library} {new}, \
done by another agent. Its full test suite already passes on {library} {new}. Your job is to find what \
the tests can't catch: code that still uses the old {library} API. That means deprecated calls that \
still run on {new}, old-style configuration, and old patterns in modules the tests barely touch (such \
as examples or rarely used code paths).

You cannot edit anything. Tools:
- find_deprecations: runs the suite and lists the deprecated {library} call sites it hits. Start here.
- search_code, read_file, list_files: look for old-API patterns the tests don't exercise.
- search_docs: check the official migration guide.
- submit_review: your verdict. approve=true if the migration is complete; otherwise approve=false and \
findings, one per problem, each with file:line, what is old, and what to replace it with.

Report only real remaining uses of the old {library} API in the project's own source, not in tests. \
Don't report style issues, other libraries' deprecations, or changes the migration guide doesn't \
require. Be concrete and brief; you have a small step budget, so submit once you've checked."""


def first_message(summary: str | None, diff: str) -> str:
    return (f"The migrator's summary:\n{summary or '(none)'}\n\n"
            f"Its changes so far (git diff, may be truncated):\n```diff\n{diff}\n```\n\nReview the migration now.")


def findings_message(library: str, findings: list[str]) -> str:
    """What the migrator is told when the review finds old API left behind."""
    items = "\n".join(f"- {f}" for f in findings)
    return (f"A reviewer checked your migration. The tests pass, but these places still use the old {library} "
            f"API and must be migrated too:\n{items}\n\nFix them (don't change tests), run the tests, and call "
            "finish again.")


BUDGET_NOTE = "Step budget for this review is almost used up. Call submit_review now."
