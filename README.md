# Migration Agent

An autonomous AI agent that upgrades Python codebases across breaking library releases. Given a
repository written for **Pydantic v1**, it upgrades the dependency, runs the project's own test
suite, reads the migration guide, fixes what broke, and verifies the result with the full suite.
A human then reviews the diff and approves a branch.

**Result:** on a benchmark of 8 real open-source projects, Claude Haiku 5.5 completed **22 of 23
migrations** (3 attempts per project), at an average of **$0.03 per run** and **$0.79 for the whole
24-run experiment**. No run edited or skipped a test, and automatic review checks found no
shortcuts in any of them.

| | |
|---|---|
| Agent loop | hand-written Python, no agent framework |
| Tools | [MCP](https://modelcontextprotocol.io) servers: repo tools, Docker test sandbox, docs search, git |
| Models | Claude (default), Gemini, Groq behind one small interface |
| Verification | the project's own tests in Docker, plus automatic review checks on the final diff |
| Observability | every model and tool call recorded; React dashboard with per-run charts |

---

## How it works

```mermaid
flowchart LR
    A[Target repo<br/>pinned to Pydantic v1] --> B[Baseline test run<br/>with v2 installed]
    B --> C{Agent loop}
    C -->|search / read / edit| R[repo_tools MCP]
    C -->|run tests| S[sandbox MCP<br/>Docker]
    C -->|look up v2 API| D[docs_search MCP<br/>hybrid RAG]
    C -->|finish| V[Full verification run]
    V -->|still failing| C
    V -->|all pass| K[Review checks<br/>on the diff]
    K --> H[Human review]
    H -->|approve| G[Local branch + commit<br/>+ PR description]
```

1. **Baseline.** The repo is installed in a fresh Docker container, Pydantic is upgraded to v2,
   and the tests run. The failures, grouped by root cause, become the agent's first message.
2. **Loop.** Each step: send the conversation to the model, run the tool it picks, append the
   result. The agent can only act through tools:
   - `repo_tools`: list, search, read and edit files. Edits are exact-text replacements, and a
     failed edit returns the closest matching lines so the model can copy them.
   - `sandbox`: install and run the tests in Docker, and return a **structured summary**: failures
     grouped by root cause, with the error type, message and the deepest `file:line` *inside the repo*.
   - `docs_search`: search the official Pydantic migration guide.
3. **Finish is a request, not a declaration.** When the model calls `finish`, the loop runs the
   full suite itself and only stops if it passes; otherwise the model gets the failures back.
4. **Review checks** run on the final diff and flag what tests can't catch: pins that still allow
   the old version, removed dependencies, compatibility shims, test edits.
5. **Human review.** `python -m agent.review runs/<id>` shows the outcome, the agent's summary, the
   findings and the diff, then asks for approval. On approval it creates a local branch with one
   commit and writes a PR description built only from recorded data.

### Guardrails, and why they're in code rather than the prompt

- **Tests can't be edited or skipped.** The edit tool refuses test files and refuses to add
  `skip`/`xfail` markers; `run_tests` refuses pytest flags such as `-k` or `--deselect`.
- **Only files inside the target repo** can be read or edited.
- **Step and token budgets** stop a run; the budget counts *billable* tokens, since cached input
  costs about 1/20 of normal input.
- **The model never gets git tools.** If it could commit or open a PR, "a human approves first"
  would only be a request in the prompt. Only the review command, run by a person, creates a branch.

---

## Results

### Benchmark: 8 real projects, Claude Haiku 5.5, docs search on, 3 runs each

Each task is a public repo pinned to a commit that depends on Pydantic v1. A task is only
accepted after a sandbox check: its own tests pass on v1 and genuinely fail on v2.

| Project | Difficulty | What breaks under v2 | Succeeded | Median steps | Avg cost | Tests after |
|---|---|---|---|---|---|---|
| [geojson-pydantic](https://github.com/developmentseed/geojson-pydantic) | easy | removed `ErrorWrapper`, `GenericModel` | 3/3 | 15 | $0.016 | 134/134 |
| [pyonwater](https://github.com/kdeyev/pyonwater) | easy | `Optional` fields become required (behaviour change) | 3/3 | 13 | $0.008 | 25/25 |
| [FastApi-RESTful](https://github.com/yuval9313/FastApi-RESTful) | medium | `is_classvar`, `BaseSettings` moved, `orm_mode` | 3/3 | 13 | $0.010 | 69/69 |
| [pydantic-sqlite](https://github.com/Phil997/pydantic-sqlite) | medium | `ModelField` and field introspection | 3/3 | 17 | $0.021 | 30/30 |
| [dbt-cloud-cli](https://github.com/data-mie/dbt-cloud-cli) | medium | v1 validator signatures, field internals | 3/3 | 29 | $0.041 | 31/31 |
| [pydantic-cli](https://github.com/mpkocher/pydantic-cli) | hard | core built on v1 field internals | 3/3 | 32 | $0.052 | 35/35 |
| [gto](https://github.com/iterative/gto) | hard | `BaseSettings` with custom sources, `parse_obj_as` | 3/3 | 28 | $0.038 | 141/141 |
| [copier](https://github.com/copier-org/copier) | hard | v1 error/validator internals, pydantic dataclasses | 1/2 | 39.5 | $0.085 | 691/696* |

\* 5 skipped, as on v1. copier's third run was cut short twice by infrastructure and isn't counted.
The failed run reached 690/696 before the 40-step limit. The successful run solved the hardest part
without touching tests: upstream's own port *changed* a test that expects a `TypeError`, while the
agent kept v1 behaviour by making `Worker` a plain dataclass and validating its fields with a separate
Pydantic model in `__post_init__`.

### Model comparison (same tasks, earlier runs)

| Model | geojson-pydantic | FastApi-RESTful | pydantic-cli |
|---|---|---|---|
| Claude Opus 5.5 | 11 steps, $0.28 | 8 steps, $0.15 | 34 steps, $1.14 |
| Claude Haiku 5.5 | 15 steps, $0.016 | 13 steps, $0.010 | 32 steps, $0.052 |

On these tasks Haiku succeeds about as reliably as Opus at **20–50× lower cost**.

### Does docs search help? (Haiku, 3 runs per setting)

| Project | Success (off → on) | Median steps (off → on) |
|---|---|---|
| geojson-pydantic | 3/3 → 3/3 | 19 → 15 |
| FastApi-RESTful | 3/3 → 3/3 | 16 → 11 |
| pydantic-cli | 3/3 → 3/3 | 27 → 29 |

Docs search doesn't change *whether* Haiku succeeds on these tasks. It tends to shorten the easier
ones and shows no benefit on the hard one. With 3 runs per setting this is a tendency, not proof.

### Retrieval: why the RAG is mostly keyword search

The docs index was evaluated on 16 queries phrased the way the agent actually searches, mostly
exact identifiers from error messages (`orm_mode`, `ModelField`, `__fields__`). Right section in the top 3:

| Strategy | Hits (of 16) |
|---|---|
| Semantic only (embeddings, the "classic RAG") | 8 |
| Keyword only (BM25) | 15 |
| Hybrid, equal weights | 15 |
| **Hybrid, keyword ×2 (shipped)** | **16** |

---

## Lessons from building it

Most of the effort went into making the environment trustworthy, and most of the agent's odd
behaviour turned out to be caused by the harness, not the model.

- **A harness conflict produced a "passing but wrong" migration.** An era pin in the install
  command made the agent's correct dependency bump fail to install, so the agent hand-rolled
  `BaseSettings` to get the tests green, and said so in its summary. After separating the repo's
  own dependencies from the harness's test dependencies, the same model did the proper
  `pydantic-settings` migration in 8 steps instead of 28.
- **Passing tests aren't enough.** That run led to the review checks. A prompt rule about companion
  packages then took "pin still allows an incompatible version" from 2/6 runs to 0/18.
- **Line endings mattered.** With Windows' `core.autocrlf=true` every checked-out repo had CRLF
  endings, so scripts run by tests failed in the Linux sandbox (`python\r`). Checkouts are now
  byte-identical to upstream.
- **Failure reports must survive odd crashes.** When pytest dies before writing JUnit XML (an
  import error in `conftest.py`), the summary falls back to parsing the traceback; one broken test
  module no longer hides the rest (`--continue-on-collection-errors`).
- **Benchmark tasks need real verification.** None of the six candidates found by static code
  review passed its v1 baseline on the first try: test plugins, CLI libraries and git bindings had
  all moved on. Every task is pinned and verified in the sandbox before it's accepted.
- **Cost is a caching problem.** Each step resends the whole conversation; keeping the history
  append-only lets Claude's prompt cache serve 75–95% of input at a fraction of the price.

---

## Running it

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker, and an API key for Claude,
Gemini or Groq.

```bash
uv sync
cp .env.example .env                      # add ANTHROPIC_API_KEY (or another provider's key)
docker build -t migration-agent-sandbox:py3.11 sandbox
uv run python -m mcp_servers.docs_search.build    # one-time: download and index the migration guide

# Run the agent on one benchmark task (clones it into workspace/)
uv run --env-file .env python -m agent.loop --task geojson-pydantic

# Review the result: approve -> local branch + PR description
uv run python -m agent.review runs/<run-id>

# Run an experiment: tasks x models x docs on/off, N repeats -> benchmark/results/*.json
uv run --env-file .env python -m benchmark.runner --name my-exp --models claude-haiku-5-5 --docs both --repeats 3

# Dashboard (http://localhost:8000)
cd dashboard/web && npm install && npm run build && cd ../..
uv run uvicorn dashboard.api:app --port 8000
```

Tests: `uv run pytest` (about 130 tests; the Docker end-to-end test is opt-in with `-m docker`).

## Project layout

```
agent/            loop.py (the agent loop), llm.py (Claude/Gemini/Groq), prompts.py,
                  trace.py (run recording), checks.py (review checks), review.py (human review)
mcp_servers/      repo_tools/, sandbox/, docs_search/, git_tools/
sandbox/          Dockerfile + entrypoint for running a target repo's tests
benchmark/        tasks.yaml (verified tasks), runner.py (experiments), results/ (committed results)
dashboard/        FastAPI backend + React frontend over runs/
tests/            tests for this project's own code
```

## Limitations and next steps

- **Small benchmark.** 8 projects, so every number above has wide error bars. The goal is 30–50.
- **One library so far.** SQLAlchemy 1.4 → 2.0 is the planned second migration.
- **No memory module yet.** Across ~60 runs no agent repeated a failed fix, so it was deferred
  until a task needs it.
- **A LangGraph version** of the loop is planned, to compare against the hand-written one.
