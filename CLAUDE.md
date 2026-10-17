# ImpactGraph — context for Claude Code

## What this is
Entry for **Graph Hacks** (WeMakeDevs × FalkorDB online hackathon).
Window: Oct 14 20:31 → **Oct 18 20:29 Berlin time** (Oct 15 00:01 – Oct 18 23:59 IST). Submissions close at the end.
Rules: https://www.wemakedevs.org/hackathons/falkordb/rules

ImpactGraph is an AI agent that decides **which tests to run for a code change** using a FalkorDB graph
of code, tests and git history, and shows the graph path behind every choice ("decisions you can trace").

- Primary track: **Agents That Act on Connected Data**
- Secondary track (planned): **Agent Memory and Coordination** — feed test results/misses back into the graph so ranking learns.

## Hackathon rules that constrain the work
- FalkorDB must be central: the product must not work without it.
- Only public, synthetic or own data. Target repo is public **pallets/click** — never company/private data.
- AI coding assistants allowed but must be **disclosed** in the README.
- Owner must be able to explain the graph model, queries, architecture and decisions.
- Submission needs: public repo, README (setup + graph data model), the Cypher queries, demo video,
  live deployment or full local setup steps, track(s) named.

## Current state (Thu Oct 15 evening)
Working end to end on pallets/click:
- `impactgraph/extract_code.py` — `ast` → modules, functions/methods, approximate CALLS edges (with `resolution`).
- `impactgraph/extract_coverage.py` — per-test coverage (`pytest --cov=src/click --cov-context=test`) → Test-COVERS->Function.
- `impactgraph/extract_git.py` — commits → MODIFIED functions (parsed at that revision) and TOUCHED test files.
- `impactgraph/load.py` — builds the graph (≈20 s). Precomputes `Test.breadth`, `Function.tests`, `Function.size`.
- `impactgraph/queries.py` — the Cypher queries: 7 read (changed functions, direct tests, co-changed test files,
  uncovered fallback via call graph, explain path, blast radius, load-bearing via `algo.pageRank`) + memory
  (RECORD_RUN/LINK_*, RECALL, EXPLAIN_MEMORY, EXPORT_MEMORY).
- `impactgraph/memory.py` — memory (track 2): `(:Run)-[:CHANGED]->(:Function)`, `(:Run)-[:SELECTED]->(:Test)`,
  `(:Test)-[:FAILED_IN]->(:Run)`; missed = failed and not selected. Fed from pytest `--junitxml`.
  Only writer besides load.py (which exports memory before a rebuild and re-links it by id).
- `impactgraph/impact.py` — changed functions from a commit or working tree → ranked, explained selection.
  `use_memory`: tests that failed when the function changed before get boosted, weighted by how few tests failed.
- `impactgraph/evaluate.py` — mutation testing vs full suite, random same-size baseline, ablation
  (coverage / +history / full at budgets 5,10,20,60), blind-spot count; raises on pytest exit codes other than 0/1.
  `--memory`: bug A → memory, score a different bug B with/without, in a copy graph `impactgraph:click:eval`.
  Runs pytest with PYTHONPATH=<repo>/src (the .pth problem below keeps coming back).
- `impactgraph/db.py` — FalkorDB server if `FALKORDB_HOST` set, else embedded FalkorDB Lite at `data/impactgraph.db`.
  One graph per repo: `impactgraph:<repo dir>` (e.g. `impactgraph:click`).
- `impactgraph/tools.py` — `Toolbox`: 9 read-only (incl. `recall`) (`ro_query`) graph tools for the agent; tracks returned test ids + a call trace.
- `impactgraph/web.py` + `impactgraph/static/index.html` — local UI (stdlib http.server, 127.0.0.1, read-only):
  commit list → live rule-based selection with a graph path per test; saved agent runs from `docs/agent-runs/<sha>.json`
  (f67c2bb, 22177f8, 9f0bcdf; $0.59 total) with outcomes, risks and the tool/Cypher trace; eval headline.
- `impactgraph/agent.py` — Claude (Opus 5.5, Tool Runner) picks tools, submits a decision; code enforces: tests must come
  from a tool, path computed by EXPLAIN_PATH (or EXPLAIN_MEMORY), unknown names in reasons replaced, explicit outcome
  per function (answered_with_path / answered_from_memory / untested_graph_does_not_know / tests_exist_none_selected / not_in_graph).

Graph on click: 17 modules, 545 functions, 2,209 tests, 360 commits, ~157k edges.

**Evaluation (docs/evaluation-click.json):** 40 planted bugs, 1 hung the suite (excluded), 31 caught by full suite;
ImpactGraph caught **30/31 (97%)** running avg **35.5 of 2,209 tests** (0.4 s vs 5.9 s). Random same size: 12/31.
Ablation (caught of 31 @ budget 5/10/20/60): coverage 25/26/28/30, +history 25/27/28/30, full same, random 5/7/7/12.
→ per-test coverage is the backbone; history +1 at budget 10; call graph only matters for untested code
(83 untested functions, 9 reachable via callers), which mutants cannot measure.
The 1 miss: `Option._parse_decls` line 3485 — 214 killers, but the top 60 cover a different branch
→ input-dependent: the changed line runs in 1,453/1,454 covering tests, 6 picks take the killers' branch; line/branch coverage would NOT fix it (checked Oct 16).

**Memory evaluation** (`memory` in the same file; 40 pairs, caught bug B @ budget 5/10/20/60):
held-out seed 11: without 28/29/33/37, with 27/29/33/37 (neutral). Dev seed 7 (weighting derived here):
without 26/27/33/38, with 30/30/34/38. Unweighted v1 on seed 7 lost 3 pairs (bug A with hundreds of killers).
Limits: 13/40 bug A's caught nothing, ~12 pairs had zero killer overlap; `_parse_decls` not helped
(bug A had 2 killers, overlap 0). Mutation pairs are a pessimistic proxy for real recurring regressions.

## How to run
```bash
pip install falkordb falkordblite pytest pytest-cov coverage anthropic python-dotenv
git clone https://github.com/pallets/click ../click
pip install -e ../click
(cd ../click && COVERAGE_CORE=ctrace python -m pytest -q --cov=src/click --cov-context=test --cov-report=)
# COVERAGE_CORE=ctrace: on Python 3.14 coverage defaults to sysmon, which cannot record per-test contexts
# Python 3.14 on macOS skips .pth files with the 'hidden' flag -> `import click` fails silently.
# If it does: chflags nohidden .venv/lib/python3.14/site-packages/*.pth
python -m impactgraph.load --repo ../click --package click
python -m impactgraph.impact --repo ../click --commit <sha>
python -m impactgraph.evaluate --repo ../click --mutants 40
python -m impactgraph.evaluate --repo ../click --memory --mutants 40 --focus "click/core.py::Option._parse_decls:3485"
python -m impactgraph.memory record --repo ../click --commit <sha> --junit report.xml   # pytest --junitxml
python -m impactgraph.memory show --repo ../click
python -m impactgraph.web --repo ../click          # http://localhost:8000
python -m pytest tests -q                          # 24 tests, tiny graph in temp FalkorDB Lite
python -m impactgraph.agent --repo ../click --commit <full sha> --json > docs/agent-runs/<full sha>.json   # show in UI
cp .env.example .env   # add ANTHROPIC_API_KEY
python -m impactgraph.agent --repo ../click --commit <sha>   # --json, --model, --effort, --budget
```

## Next steps, in order
1. ~~Agent layer~~ (done: agent.py). LLM reads a PR/diff, calls the queries as tools, writes the decision with graph paths.
2. ~~Feedback loop~~ (done: memory.py). Store test runs/failures/misses in the graph (Run, FAILED_IN edges); re-rank from them.
3. ~~Small web UI~~ (done: web.py, local). PR → selected tests → graph path per test. Deploy (or document local setup).
4. ~~README, LICENSE, requirements.txt~~ (done; fresh-install verified with FalkorDB Lite). Demo video: script in tasks/demo-script.md — Manoj records it.

## Conventions
- Python 3.11+, small modules, plain functions. Keep Cypher in `queries.py`.
- Commits authored as Manoj Kengudelu; disclose AI assistance in commit trailers/README.
