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

## Current state (Thu Oct 15 morning)
Working end to end on pallets/click:
- `impactgraph/extract_code.py` — `ast` → modules, functions/methods, approximate CALLS edges (with `resolution`).
- `impactgraph/extract_coverage.py` — per-test coverage (`pytest --cov=src/click --cov-context=test`) → Test-COVERS->Function.
- `impactgraph/extract_git.py` — commits → MODIFIED functions (parsed at that revision) and TOUCHED test files.
- `impactgraph/load.py` — builds the graph (≈20 s). Precomputes `Test.breadth`, `Function.tests`, `Function.size`.
- `impactgraph/queries.py` — the 6 Cypher queries (changed functions, direct tests, co-changed test files,
  uncovered fallback via call graph, explain path, blast radius).
- `impactgraph/impact.py` — changed functions from a commit or working tree → ranked, explained selection.
- `impactgraph/evaluate.py` — mutation testing vs full suite, random same-size baseline.
- `impactgraph/db.py` — FalkorDB server if `FALKORDB_HOST` set, else embedded FalkorDB Lite at `data/impactgraph.db`.

Graph on click: 17 modules, 545 functions, 2,210 tests, 360 commits, ~157k edges.

**Evaluation (docs/evaluation-click.json):** 40 planted bugs, 31 caught by full suite;
ImpactGraph caught **29/31 (94%)** running avg **36 of 2,210 tests** (0.5 s vs 6.6 s). Random same size: 6/31.
The 2 misses = the only killing test ranked too low → motivation for the memory track.

## How to run
```bash
pip install falkordb falkordblite pytest pytest-cov coverage
git clone https://github.com/pallets/click ../click
pip install -e ../click
(cd ../click && COVERAGE_CORE=ctrace python -m pytest -q --cov=src/click --cov-context=test --cov-report=)
# COVERAGE_CORE=ctrace: on Python 3.14 coverage defaults to sysmon, which cannot record per-test contexts
python -m impactgraph.load --repo ../click --package click
python -m impactgraph.impact --repo ../click --commit <sha>
python -m impactgraph.evaluate --repo ../click --mutants 40
```

## Next steps, in order
1. Agent layer: LLM reads a PR/diff, calls the queries as tools, writes the decision with graph paths.
2. Feedback loop: store test runs/failures/misses in the graph (Run, FAILED_IN edges); re-rank from them.
3. Small web UI: PR → selected tests → graph path per test. Deploy (or document local setup).
4. README (setup, graph model diagram, Cypher queries, results, AI-use disclosure), LICENSE, demo video.

## Conventions
- Python 3.11+, small modules, plain functions. Keep Cypher in `queries.py`.
- Commits authored as Manoj Kengudelu; disclose AI assistance in commit trailers/README.
