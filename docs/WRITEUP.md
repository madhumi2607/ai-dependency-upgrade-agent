# Building an agent that upgrades real codebases, and what actually made it work

Every Python team has a dependency it's afraid to upgrade. Pydantic v2 and SQLAlchemy 2.0 both
shipped breaking changes that touch hundreds of call sites in a typical project, and both come with
long migration guides that someone has to read, apply and verify. That is a well-shaped job for an
AI agent: the goal is clear (the project's own tests pass on the new version), progress is
measurable, and the reference material exists.

So I built one. Given a repository pinned to the old version, the agent upgrades the dependency,
runs the tests in Docker, reads the official migration guide, fixes what broke, and verifies the
result with the full test suite. A human reviews the diff before anything becomes a branch.

**Result:** on 18 real open-source projects (13 Pydantic, 5 SQLAlchemy), Claude Haiku 5.5 completed
52 of 53 migrations at about $0.025 per run. No run edited or skipped a test. And because passing
tests isn't proof of a migration, I measured that too: 92% of successful runs leave no deprecated
old-version API behind (section 7).

The model was the easy part. Here is what took the work.

## 1. The loop is small. The environment is the product.

The agent loop is about 75 lines of plain Python: send the conversation to the model, run the tool
it picks, append the result, repeat until it finishes or a budget runs out. No framework.

The model can only act through four tool servers, built on the Model Context Protocol (MCP):

- **repo_tools**: list, search, read and edit files. Edits are exact-text replacements; a failed
  edit returns the closest matching lines, so the model can copy them instead of guessing.
- **sandbox**: install the project and run its tests in Docker, then return a *structured
  summary*: failures grouped by root cause, with error type, message and the deepest `file:line`
  inside the repo. Not 3,000 lines of pytest output.
- **docs_search**: search the official migration guides.
- **git_tools**: branch, commit and diff, but only the human review command can use them.

The structured failure summary did more for success rate than any prompt change. A model that
sees "37 tests fail with the same `AttributeError` at `models.py:12`" fixes one line; a model that
sees raw logs reads them for ten steps.

## 2. "Finish" is a request, not a declaration

When the model calls `finish`, the loop runs the full test suite itself and only stops if it
passes. Otherwise the model gets the failures back and keeps going. Agents are optimistic about
their own work; this one rule turns "I believe I'm done" into "the tests say I'm done".

## 3. Guardrails belong in code, not in the prompt

The prompt says "don't edit tests". The edit tool also *refuses* to touch test files, refuses to add
`skip`/`xfail` markers, and the test runner refuses flags like `-k` or `--deselect`. The model never
gets git tools at all: if it could commit, "a human approves first" would only be a polite request.

## 4. Passing tests aren't enough

Early on, a run passed every test by hand-rolling a replacement for `BaseSettings` and pinning
`pydantic>=1.10`, which still allows v1. Green tests, wrong migration. The cause was my harness: an
install command pinned a package so that the agent's *correct* dependency bump failed to install,
and the agent routed around it.

That run produced two changes. The harness now installs the project's own dependencies separately
from its test-tool pins, and every run's final diff goes through automatic review checks: version
pins that still allow the old version, removed dependencies, compatibility shims (`pydantic.v1`
imports), and any change to tests. The same model then did the proper migration in 8 steps
instead of 28.

## 5. RAG over migration guides: mostly keyword search

I measured retrieval before trusting it. The agent searches the way it reads errors, with exact
identifiers like `orm_mode`, `ModelField` or `'Engine' object has no attribute 'execute'`. On the
Pydantic guide, embedding search found the right section in the top 3 for only 8 of 16 such
queries; BM25 keyword search found 15. The shipped version fuses both rankings (reciprocal rank
fusion, keyword weighted ×2) and finds all of them, on both libraries' guides.

The two guides behaved differently: SQLAlchemy's is mostly prose, so embeddings did far better
there (13/15). Only the hybrid was right everywhere, which is the argument for measuring instead of
picking a retrieval method by default.

## 6. Benchmarks lie quietly

The most useful lesson came from my own mistakes. Two examples:

- **An impossible task that looked like a model failure.** All three runs on one SQLAlchemy project
  stopped at exactly 57 of 59 tests. Two tests call a function SQLAlchemy 2.0 removed, *from the
  test code itself*, so I had excluded them with `--deselect`. I had the class name wrong, and pytest
  silently ignores a deselect that matches nothing. Identical scores across repeats were the tell:
  that's a property of the task, not the model. A test now checks every deselect against the pinned
  commit, and the task went 3/3.
- **A check that never looked.** The pin check read `pyproject.toml` and requirements files, but not
  `setup.py`. The Pydantic projects never needed it; a SQLAlchemy project did, and a weak pin went
  unflagged until I read the diffs myself.

Both bugs were *silent*: a check that matches nothing looks exactly like a check that passed. The
fix in both cases was to state what a passing check must look like (57 tests, not 59) and fail when
it doesn't.

Every task is now accepted only after a sandbox run proves its tests pass on the old version and
genuinely fail on the new one, with the upgrade capped (`sqlalchemy>=2.0,<2.1`, since an uncapped
spec silently installed 2.1).

## 7. Did it migrate, or just make the tests pass?

A fair critique of any "N of M succeeded" number. Pydantic v2 still *runs* most v1-style code
(`.dict()`, `@validator`, `class Config`) and only warns, so a half-done migration can pass every test.

So I replayed every successful run's diff on the pinned commit, ran the exact graded test command,
and counted the deprecated old-API call sites the suite still hits in the project's own source. All
105 replays reproduced; 99 were measurable (one project's config hides deprecation warnings); **91
(92%) left nothing behind.** Six of the eight exceptions are every run on one project, pydantic-cli,
in both agents: the core was migrated but v1 `class Config` blocks were left in its example modules.
The tests import those examples, and v2 accepts them with a warning. That's a real, systematic miss,
and the success rate alone would never have shown it.

Building the replay was its own lesson. It found a bug in how I recorded diffs (Windows text mode
silently stripped carriage returns, so patches for CRLF files no longer applied), and my first
version *changed the result it measured*: forcing warnings on with `-W` flipped one project's test.
A measurement has to run exactly what was graded.

The other gap: the agent is graded by tests it can see. For 10 projects, upstream later ported
the code itself and changed some tests along the way, so I swapped those post-port tests into each
run and ran them, with upstream's own port as a control. 36 of 57 runs pass every one. I read every
remaining failure, and each traces to something upstream changed *beyond* the migration: a feature
added in the same PR, a new attribute, a redesigned command line, a different error type. None was
a migration mistake, and the scores were identical across every run of a task, which is what
deliberate upstream choices look like.

## 8. Hand-written loop vs LangGraph

I rebuilt the agent as a LangGraph `StateGraph`, holding the model client, tools, prompts and
budgets constant so only the orchestration differed. A test runs one scripted session through both
and asserts identical event streams. Then I benchmarked both on all 18 projects, 3 runs each:

| | Hand-written | LangGraph |
|---|---|---|
| Succeeded | 52/53 | 53/54 |
| Mean cost per run | $0.025 | $0.024 |
| No deprecated API left | 45/49 | 46/50 |
| Control-flow code | 75 lines | 143 lines |

Same results, as you'd expect: the framework doesn't change what the agent does. It changes the
code: routing becomes small, testable functions, the append-only history is enforced by a reducer,
and checkpointing and human-in-the-loop interrupts come built in. For a single-agent loop this size,
that's twice the code for features I don't use yet. With branching workflows or resumable runs, the
trade would flip.

## 9. Cost is a caching problem

Each step resends the whole conversation. Keeping the history strictly append-only lets the prompt
cache serve 75–95% of input tokens at about 1/20 of the price, which is why a 20-step run costs two
or three cents. The step budget also counts only *billable* tokens; counting cached ones would end
cheap, well-cached runs early.

On these tasks, Haiku succeeded about as reliably as Opus at 20–50× lower cost.

## What I'd do next

- Grow the benchmark from 18 projects toward 30–50; every number above has wide error bars.
- Use LangGraph's checkpointing to resume a run after a crash, now that the graph version exists.
- Harder SQLAlchemy tasks: the current five are mostly Core and SQLite-only; none exercises the
  2.0-style typed ORM.

The code, the benchmark tasks (repo, pinned commit, install and test commands) and every results
file are in the repository, so each experiment can be rerun.
