"""The ImpactGraph agent: Claude investigates a change through the graph tools and
submits a test selection. It never writes Cypher, it only picks tools.

    python -m impactgraph.agent --repo ../click --commit <sha>
    python -m impactgraph.agent --repo ../click --commit <sha> --json

Guarantees enforced in code, not in the prompt:
  * a selected test must have been returned by a graph tool;
  * every kept test carries a path computed by the graph (EXPLAIN_PATH, or EXPLAIN_MEMORY for a test
    that failed when this function changed before), never written by the model;
  * reasons that name code the graph never returned are replaced by a sentence built from rows;
  * each changed function ends in an explicit outcome (path / memory / untested / not in graph).

Needs ANTHROPIC_API_KEY (read from .env).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import TypedDict

import anthropic
from anthropic import beta_tool
from dotenv import load_dotenv

from .tools import Toolbox

MODEL = "claude-opus-5-5"
# $ per million tokens (input, output, cache read); used only to print what a run cost.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.01)}

SYSTEM = """You are ImpactGraph, a test-selection agent. For a code change you decide which tests \
to run, using a FalkorDB graph of the repository: functions, the tests that execute them \
(per-test coverage), the static call graph, git history (which test files changed together \
with which functions), and memory of past test runs (which tests failed when a function changed).

You can only see the graph through the tools. Work like a careful reviewer:
1. get_change: what changed, how many tests cover each function, how often it changed before.
2. baseline_selection: the rule-based ranking. Treat it as a starting point, not the answer.
3. recall: tests that failed before when these functions changed. A remembered failure, especially \
a past miss, is strong evidence; keep such tests unless they are clearly unrelated.
4. Investigate where the baseline may be weak: direct_tests for heavily changed or risky functions, \
uncovered_callers for functions no test runs, cochanged_test_files for history, blast_radius and \
load_bearing (PageRank) for how far and how central the change is, explain_path when a link is not obvious.
5. Call submit_decision exactly once.

Selection rules:
- Every test you select must have come back from a tool in this session. Never invent or edit test ids.
- For each changed function that has tests, keep at least one focused test (low test_breadth, \
high lines_hit, or named after the function).
- Prefer a small set that would catch a regression over a long list. Stay within the test budget.
- If the graph has no evidence for a function (no tests, no covered callers), say so in risks. \
"No results" means the graph does not know, not that the change is safe.
- Reasons are one short sentence grounded in tool results (coverage, call path, co-change history).
"""


class Choice(TypedDict):
    test: str
    function: str
    reason: str


def _known_names(change: dict) -> set[str]:
    names = set()
    for c in change["changed_functions"]:
        names.update({c["qualname"], c["qualname"].split(".")[-1], c["module"]})
    return names


def _unknown_names(text: str, known: set[str]) -> list[str]:
    """Code-looking names (`x`, Foo.bar) in the model's text that no tool returned."""
    found = set(re.findall(r"`([^`]+)`", text))
    found |= set(re.findall(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b", text))
    return sorted(n for n in found if n not in known and not n.startswith(("tests/", "click/"))
                  and not any(n in k for k in known))


def validate(tb: Toolbox, selected: list[Choice]) -> tuple[list[dict], list[dict]]:
    """Keep only tests a tool returned, and attach the path computed by the graph."""
    t0 = time.time()
    kept, rejected = [], []
    for c in selected:
        if c["test"] not in tb.seen_tests:
            rejected.append({**c, "why": "test id was not returned by any tool"})
            continue
        p = tb.explain_path(c["test"], c["function"], record=False)
        m = tb.explain_memory(c["test"], c["function"])
        if not p["found"] and not m["found"]:
            rejected.append({**c, "why": "graph has no path from this test to that function"})
            continue
        kept.append({**c, "path": p["path"] if p["found"] else m["path"], "hops": p.get("hops"),
                     **({"memory": m["path"]} if m["found"] else {})})
    tb._log("guardrail", {"submitted": len(selected), "kept": len(kept)}, t0, len(kept), "EXPLAIN_PATH")
    return kept, rejected


def run(repo: Path, commit: str | None, model: str = MODEL, effort: str = "medium",
        budget: int = 40, verbose: bool = True) -> dict:
    tb = Toolbox(repo, commit)
    state: dict = {"change": None, "decision": None, "rejected": []}
    say = (lambda *a: print(*a, file=sys.stderr)) if verbose else (lambda *a: None)

    @beta_tool
    def get_change() -> str:
        """The change under review: commit subject, diff, and each changed function with its id,
        number of covering tests and churn (how many past commits modified it). Call this first."""
        state["change"] = tb.get_change()
        return json.dumps(state["change"])

    @beta_tool
    def baseline_selection(functions: list[str], budget: int = budget) -> str:
        """Rule-based ranking of tests for the given changed functions (coverage focus, rarity,
        co-change history, call-graph fallback). A starting point for your own selection.

        Args:
            functions: Changed function ids exactly as returned by get_change.
            budget: Maximum number of tests to return.
        """
        return json.dumps(tb.baseline_selection(functions, budget))

    @beta_tool
    def recall(functions: list[str]) -> str:
        """Memory of past test runs: tests that failed in earlier runs that changed the same
        functions, how often (failed_in_runs), and how often they failed without having been
        selected (missed_in_runs, a past mistake). Empty means no recorded runs, not that the
        code is safe.

        Args:
            functions: Changed function ids exactly as returned by get_change.
        """
        return json.dumps(tb.recall(functions))

    @beta_tool
    def direct_tests(functions: list[str], limit: int = 15) -> str:
        """Tests that execute each function, most focused first, with lines hit and test breadth
        (how many functions the test runs; lower is more focused).

        Args:
            functions: Function ids.
            limit: Maximum tests returned per function.
        """
        return json.dumps(tb.direct_tests(functions, limit))

    @beta_tool
    def cochanged_test_files(functions: list[str]) -> str:
        """Test files that were edited in the same commits as these functions, with commit counts.

        Args:
            functions: Function ids.
        """
        return json.dumps(tb.cochanged_test_files(functions))

    @beta_tool
    def uncovered_callers(functions: list[str]) -> str:
        """For functions no test executes: covered callers up to 3 call hops away, and their tests.

        Args:
            functions: Function ids (only those with zero covering tests return results).
        """
        return json.dumps(tb.uncovered_callers(functions))

    @beta_tool
    def blast_radius(functions: list[str]) -> str:
        """How many functions call these (up to 3 hops) and in which modules.

        Args:
            functions: Function ids.
        """
        return json.dumps(tb.blast_radius(functions))

    @beta_tool
    def load_bearing(functions: list[str]) -> str:
        """PageRank (FalkorDB algo.pageRank) over the call graph: how much of the code ends up
        calling each function. top_percent 1 means among the most load-bearing 1%; such changes
        deserve broader testing. CALLS edges are resolved approximately by name, so treat the
        rank as a hint, not a fact.

        Args:
            functions: Function ids.
        """
        return json.dumps(tb.load_bearing(functions))

    @beta_tool
    def explain_path(test: str, function: str) -> str:
        """The graph path from a test to a function: the test covers it directly, or covers a
        caller that reaches it through CALLS edges. found=false means the graph has no path.

        Args:
            test: Test id exactly as returned by a tool.
            function: Function id.
        """
        return json.dumps(tb.explain_path(test, function))

    @beta_tool
    def submit_decision(summary: str, selected: list[Choice], risks: list[str]) -> str:
        """Submit the final selection. Call exactly once, at the end.

        Args:
            summary: Two or three sentences: what changed and how the selection covers it.
            selected: Tests to run; each with the changed function it protects and a one-sentence reason.
            risks: Gaps the graph cannot cover (untested functions, wide blast radius, missing data).
        """
        kept, rejected = validate(tb, selected)
        state["decision"] = {"summary": summary, "selected": kept, "risks": risks}
        state["rejected"] = rejected
        if rejected:
            return json.dumps({"accepted": len(kept), "rejected": rejected,
                               "note": "Rejected tests were dropped. Do not resubmit; finish."})
        return json.dumps({"accepted": len(kept)})

    tools = [get_change, baseline_selection, recall, direct_tests, cochanged_test_files,
             uncovered_callers, blast_radius, load_bearing, explain_path, submit_decision]
    target = f"commit {commit}" if commit else "the uncommitted changes"
    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=model, max_tokens=16000, system=SYSTEM, tools=tools, max_iterations=20,
        thinking={"type": "adaptive"}, output_config={"effort": effort},
        cache_control={"type": "ephemeral"},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        messages=[{"role": "user", "content":
                   f"Decide which tests to run for {target} in the repository '{repo.resolve().name}'. "
                   f"Test budget: at most {budget} tests."}],
    )

    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    stop = None
    for message in runner:
        u = message.usage
        usage["input"] += u.input_tokens
        usage["output"] += u.output_tokens
        usage["cache_read"] += u.cache_read_input_tokens or 0
        usage["cache_write"] += u.cache_creation_input_tokens or 0
        stop = message.stop_reason
        for b in message.content:
            if b.type == "tool_use":
                say(f"  → {b.name}({', '.join(f'{k}={_short(v)}' for k, v in b.input.items())})")
    if stop == "refusal":
        say("model refused; falling back to the rule-based selection")

    return _finish(tb, state, usage, model, budget, stop)


def _short(v) -> str:
    s = json.dumps(v)
    return s if len(s) < 60 else s[:57] + "..."


def _finish(tb: Toolbox, state: dict, usage: dict, model: str, budget: int, stop) -> dict:
    change = state["change"] or tb.get_change()
    fns = [c["id"] for c in change["changed_functions"]]
    decision, source = state["decision"], "agent"
    if decision is None:  # the model never submitted (refusal, iteration cap): be honest about it
        base = tb.baseline_selection(fns, budget)
        decision = {"summary": "The agent did not submit a decision; this is the rule-based selection.",
                    "selected": [], "risks": []}
        for p in base["selected"]:
            path = tb.explain_path(p["test"], p["functions"][0], record=False)
            decision["selected"].append({"test": p["test"], "function": p["functions"][0],
                                         "reason": p["reason"], "path": path.get("path"),
                                         "hops": path.get("hops")})
        source = "baseline"

    # reasons may only name code the graph returned; otherwise use a sentence built from rows
    known = _known_names(change) | {n for s in decision["selected"]
                                        for n in re.split(r" -\w+-> ", s.get("path") or "")}
    replaced = 0
    for s in decision["selected"]:
        if _unknown_names(s["reason"], known):
            fn = s["function"].split("::")[1]
            s["reason"] = (f"failed when {fn} changed before" if s["hops"] is None
                           else f"covers {fn} directly" if s["hops"] == 0
                           else f"reaches {fn} through {s['hops']} call hop(s)")
            s["reason_replaced"] = True
            replaced += 1

    covered = {s["function"] for s in decision["selected"] if s.get("hops") is not None}
    from_memory = {s["function"] for s in decision["selected"] if s.get("memory")}
    outcomes = {}
    for c in change["changed_functions"]:
        if c["id"] in covered:
            outcomes[c["id"]] = "answered_with_path"
        elif c["id"] in from_memory:
            outcomes[c["id"]] = "answered_from_memory"
        elif c["tests"] == 0:
            outcomes[c["id"]] = "untested_graph_does_not_know"
        else:
            outcomes[c["id"]] = "tests_exist_none_selected"
    for f in change["not_in_graph"]:
        outcomes[f] = "not_in_graph"

    pin, pout, pcache = PRICES.get(model, PRICES[MODEL])
    cost = (usage["input"] * pin + usage["cache_write"] * pin * 1.25
            + usage["cache_read"] * pcache + usage["output"] * pout) / 1e6
    return {"commit": change["commit"], "subject": change["subject"], "source": source,
            "model": model, "stop_reason": stop, "total_tests": change["total_tests"],
            **decision, "outcomes": outcomes, "rejected": state["rejected"],
            "reasons_replaced": replaced, "trace": tb.trace,
            "usage": usage, "cost_usd": round(cost, 4)}


def render(d: dict) -> str:
    out = [f"# {d['subject']}", f"commit {d['commit']} · decided by {d['source']} ({d['model']})", "",
           d["summary"], "", f"## Run {len(d['selected'])} of {d['total_tests']} tests"]
    for s in d["selected"]:
        out += [f"- `{s['test']}`", f"  - why: {s['reason']}", f"  - path: {s['path']}"]
        if s.get("memory") and s["memory"] != s["path"]:
            out.append(f"  - memory: {s['memory']}")
    out += ["", "## Changed functions"]
    out += [f"- {f.split('::')[1]}: {o}" for f, o in d["outcomes"].items()]
    if d["risks"]:
        out += ["", "## Risks"] + [f"- {r}" for r in d["risks"]]
    if d["rejected"]:
        out += ["", "## Rejected by guardrail"] + [f"- {r['test']}: {r['why']}" for r in d["rejected"]]
    out += ["", "## How I decided"]
    for i, t in enumerate(d["trace"], 1):
        if t["tool"] == "guardrail":
            out.append(f"{i}. guardrail: kept {t['args']['kept']} of {t['args']['submitted']} submitted tests "
                       f"(each re-checked with EXPLAIN_PATH)")
        else:
            out.append(f"{i}. {t['tool']} → {t['rows']} rows ({t['ms']} ms)")
    u = d["usage"]
    out += ["", f"tokens: {u['input']} in, {u['cache_write']} cache write, {u['cache_read']} cache read, "
                f"{u['output']} out · "
                f"cost ≈ ${d['cost_usd']:.3f}"]
    return "\n".join(out)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--commit")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--budget", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    d = run(a.repo, a.commit, a.model, a.effort, a.budget, verbose=not a.json)
    print(json.dumps(d, indent=2) if a.json else render(d))


if __name__ == "__main__":
    main()
