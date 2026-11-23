# Demo video script (about 3 minutes)

A narrated version is already made: [demo/demo.mp4](demo/demo.mp4). [demo/record_demo.py](demo/record_demo.py)
records a real Haiku run on agate-sql, watched live in the dashboard by a headless browser, with
captions (`demo-silent.mp4`) and scene timings (`scenes.json`); [demo/narrate.py](demo/narrate.py)
mixes in an ElevenLabs voiceover (`voice/`, script in `narration.json`) at those timings. This script
is for recording your own narrated version, including the terminal parts.

Record at 1080p with a large terminal font (18pt+). Use a quick task so the live part fits:
**agate-sql** (SQLAlchemy, ~13 steps, ~3 minutes, ~$0.01) or **pyonwater** (Pydantic, ~10 steps).
Do one full rehearsal first so Docker images and the pip cache are warm; the recorded run is then fast.

Before recording:
```bash
docker version                                   # Docker Desktop running
uv run python -m mcp_servers.docs_search.build   # index built (no-op if it exists)
cd dashboard/web && npm run build && cd ../..
uv run uvicorn dashboard.api:app --port 8000     # leave running in a second terminal
```

---

## 1. The problem (0:00–0:20)

**Screen:** the README title and results line on GitHub.

**Say:** "Upgrading a library across a breaking release, like Pydantic v1 to v2 or SQLAlchemy 1.4
to 2.0, means reading a long migration guide and fixing every call site it touches. I built an
agent that does that on real open-source projects, and verifies the result with the project's own
tests."

## 2. Start a run (0:20–0:45)

**Screen:** terminal.
```bash
uv run --env-file .env python -m agent.loop --task agate-sql
```
**Say:** "This is agate-sql, a real project pinned to SQLAlchemy 1.4. The agent upgrades it to 2.0
in a Docker sandbox and runs the tests. Nine of eighteen fail. Those failures, grouped by root
cause, are the agent's starting point."

## 3. Watch it work (0:45–1:40)

**Screen:** dashboard at `http://localhost:8000`, open the live run, Timeline tab. Scroll as steps arrive.

**Say, as the matching steps appear:**
- On a `search_docs` call: "It searches the official SQLAlchemy migration guide. The search is
  hybrid, keyword plus embeddings, because the agent searches with exact error messages."
- On an `edit_file` call: "Edits are exact text replacements, and it can only touch files in the
  repo. It can't edit or skip tests; the tool itself refuses."
- On a `run_tests` result: "Test results come back as a short structured summary, not raw logs."
- On `finish`: "Calling finish doesn't end the run. The loop runs the full suite itself and only
  accepts it if everything passes."

## 4. The result and the checks (1:40–2:15)

**Screen:** run page, Overview tab (cost and token charts), then the Diff tab.

**Say:** "All eighteen tests pass. About thirteen steps and around a cent, because the conversation
is append-only and most of the input is served from the prompt cache. Then automatic review checks
run on the diff: they catch things tests can't, like a version pin that still allows the old
release, or a compatibility shim instead of a real migration."

## 5. Human review (2:15–2:35)

**Screen:** terminal.
```bash
uv run python -m agent.review runs/<run-id>
```
Show the summary, findings and diff, then answer the approval prompt.

**Say:** "Nothing becomes a branch until a person approves it. The model never has git access; only
this review command, run by a human, creates the branch and writes the PR description."

## 6. The numbers (2:35–3:00)

**Screen:** dashboard runs list with the Agent filter (All / Hand-written / LangGraph), then the
README results tables.

**Say:** "Across eighteen projects and two libraries, the agent completed fifty-two of fifty-three
migrations, at two to three cents a run. Passing tests isn't proof, so I also replayed every run to
check for leftover deprecated API: ninety-two percent were fully migrated, and the README shows the
ones that weren't. I also rebuilt it in LangGraph and benchmarked both: identical results, twice the
code. The tasks, results and full traces are in the repo, so anyone can check the numbers."

---

**Keep it honest on camera:** if the live run takes a different number of steps or costs a little
more, say the real number. The dashboard shows it anyway.
