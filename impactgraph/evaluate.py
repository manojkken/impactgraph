"""Does the selection actually catch bugs? Mutation testing against the full suite.

For a sample of functions: plant one small bug (flip a comparison, swap and/or,
drop a `not`, flip a boolean constant), run the FULL test suite to learn which
tests catch it, then check whether ImpactGraph's selection would have caught it.
A random selection of the same size is the baseline.

Memory (--memory): for each function plant two different bugs, A and B. A's failing
tests are recorded as a Run (memory); B is then scored with and without memory. B is
never in memory, so this measures whether past failures generalise to a new bug in
the same function. Runs go to a copy of the graph, never into the real memory.

Ablation: the same killed mutants are scored for several rankers (coverage edges
only, + co-change history, full) at several test budgets. Catching a mutant only
needs the set of tests that failed in the full run, so variants cost no extra runs.

    python -m impactgraph.evaluate --repo ../click --mutants 25
    python -m impactgraph.evaluate --repo ../click --memory --mutants 40 \
        --focus "click/core.py::Option._parse_decls:3485"
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from . import memory
from . import queries as Q
from .db import connect, graph_name
from .impact import decide

# memory is scored separately (--memory); the ablation measures the static graph only
VARIANTS = {
    "coverage": dict(use_history=False, use_calls=False, use_names=False, use_memory=False),
    "coverage+history": dict(use_calls=False, use_names=False, use_memory=False),
    "full": dict(use_memory=False),
}

FLIP = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.GtE: "<", ast.Gt: "<=",
        ast.LtE: ">", ast.Is: "is not", ast.IsNot: "is", ast.In: "not in", ast.NotIn: "in"}
SYM = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.GtE: ">=", ast.Gt: ">",
       ast.LtE: "<=", ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in"}


def mutations(source: str, start: int, end: int):
    """Yield (line_no, old_line, new_line, description) single-line mutations inside [start, end]."""
    lines = source.splitlines()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", None)
        if ln is None or not (start <= ln <= end) or node.lineno != getattr(node, "end_lineno", ln):
            continue
        line = lines[ln - 1]
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in FLIP:
            op = type(node.ops[0])
            left_end = node.left.end_col_offset
            right_start = node.comparators[0].col_offset
            seg = line[left_end:right_start]
            if seg.strip() == SYM[op]:
                new = line[:left_end] + f" {FLIP[op]} " + line[right_start:]
                yield ln, line, new, f"{SYM[op]} -> {FLIP[op]}"
        elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
            a, b = node.values
            seg = line[a.end_col_offset:b.col_offset]
            want = " and " if isinstance(node.op, ast.And) else " or "
            if seg == want:
                swap = " or " if want == " and " else " and "
                yield ln, line, line[:a.end_col_offset] + swap + line[b.col_offset:], f"{want.strip()} -> {swap.strip()}"
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            if line[node.col_offset:node.col_offset + 4] == "not ":
                yield ln, line, line[:node.col_offset] + line[node.col_offset + 4:], "drop not"
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            if line[node.col_offset:node.end_col_offset] == str(node.value):
                new = line[:node.col_offset] + str(not node.value) + line[node.end_col_offset:]
                yield ln, line, new, f"{node.value} -> {not node.value}"


def run_pytest(repo: Path, test_ids: list[str] | None, timeout: int = 120,
               src_dir: str = "src") -> tuple[set[str], float]:
    args = [sys.executable, "-m", "pytest", "-q", "-rf", "--tb=no", "-p", "no:cacheprovider",
            "-o", "console_output_style=classic"]
    if test_ids is not None:
        args += test_ids
    t0 = time.time()
    try:
        # import the package straight from the checkout: an editable install depends on a .pth file,
        # which Python 3.14 skips when macOS (iCloud-synced folders) flags it hidden
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            filter(None, [str((repo / src_dir).resolve()), os.environ.get("PYTHONPATH")]))}
        proc = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"<timeout>"}, timeout
    # 0 = passed, 1 = some tests failed; anything else (collection/import error,
    # usage error) means nothing ran, which must not be scored as "mutant survived"
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"pytest exited {proc.returncode}:\n{(proc.stdout + proc.stderr)[-2000:]}")
    out = proc.stdout
    failed = {l[7:].split(" - ")[0].strip() for l in out.splitlines() if l.startswith("FAILED ")}
    return failed, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--src-dir", default="src")
    ap.add_argument("--mutants", type=int, default=25)
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--budgets", default="5,10,20,60", help="ablation budgets")
    ap.add_argument("--out", type=Path, default=Path("data/evaluation.json"))
    ap.add_argument("--memory", action="store_true", help="score memory: bug A -> memory, score bug B")
    ap.add_argument("--focus", action="append", default=[],
                    help="function_id:line to always include as bug B (memory mode)")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    budgets = [int(b) for b in a.budgets.split(",")]
    if a.memory:
        return memory_eval(a, rnd, budgets)

    g = connect(a.repo)
    all_tests = [r[0] for r in g.ro_query("MATCH (t:Test) RETURN t.id").result_set]
    fns = g.ro_query("MATCH (f:Function) WHERE f.tests > 0 RETURN f.id, f.module, f.start, f.end").result_set
    rnd.shuffle(fns)

    results = []
    for fid, module, start, end in fns:
        if len(results) >= a.mutants:
            break
        path = a.repo / a.src_dir / module
        original = path.read_text(encoding="utf-8")
        cands = list(mutations(original, start, end))
        if not cands:
            continue
        ln, old, new, desc = rnd.choice(cands)
        lines = original.splitlines(keepends=True)
        lines[ln - 1] = new + ("\n" if lines[ln - 1].endswith("\n") else "")
        try:
            path.write_text("".join(lines), encoding="utf-8")
            killers, full_t = run_pytest(a.repo, None, src_dir=a.src_dir)
            if "<timeout>" in killers:  # the bug made the suite hang: no killing tests to score
                print(f"hung full suite: {fid} line {ln} ({desc})", flush=True)
                results.append({"function": fid, "line": ln, "mutation": desc, "killed": False, "hung": True})
                continue
            if not killers:
                print(f"survived full suite: {fid} line {ln} ({desc})")
                results.append({"function": fid, "line": ln, "mutation": desc, "killed": False})
                continue
            d = decide(a.repo, [fid], budget=a.budget, use_memory=False)
            chosen = [p.test for p in d.selected]
            _, sel_t = run_pytest(a.repo, chosen, src_dir=a.src_dir)
            random_pick = set(rnd.sample(all_tests, len(chosen)))
            ablation = {}
            for b in budgets:
                for name, flags in VARIANTS.items():
                    picked = {p.test for p in decide(a.repo, [fid], budget=b, **flags).selected}
                    ablation[f"{name}@{b}"] = bool(killers & picked)
                ablation[f"random@{b}"] = bool(killers & set(rnd.sample(all_tests, b)))
            r = {"function": fid, "line": ln, "mutation": desc, "killed": True,
                 "killers": len(killers), "killer_tests": sorted(killers), "ablation": ablation,
                 "selected": len(chosen), "coverage_set": d.coverage_set_size,
                 "caught": bool(killers & set(chosen)), "random_caught": bool(killers & random_pick),
                 "full_seconds": round(full_t, 1), "selected_seconds": round(sel_t, 1)}
            results.append(r)
            print(f"{'CAUGHT' if r['caught'] else 'MISSED'} {fid} ({desc}) "
                  f"killers={r['killers']} selected={r['selected']} random={'y' if r['random_caught'] else 'n'}")
        finally:
            path.write_text(original, encoding="utf-8")

    killed = [r for r in results if r["killed"]]
    summary = {
        "mutants": len(results), "killed_by_full_suite": len(killed),
        "caught_by_impactgraph": sum(r["caught"] for r in killed),
        "caught_by_random_same_size": sum(r["random_caught"] for r in killed),
        "avg_selected": round(sum(r["selected"] for r in killed) / max(len(killed), 1), 1),
        "total_tests": len(all_tests),
        "avg_full_seconds": round(sum(r["full_seconds"] for r in killed) / max(len(killed), 1), 1),
        "avg_selected_seconds": round(sum(r["selected_seconds"] for r in killed) / max(len(killed), 1), 1),
    }
    # ablation: caught per ranker per budget, over the same killed mutants
    summary["ablation"] = {k: sum(r["ablation"][k] for r in killed)
                           for k in (killed[0]["ablation"] if killed else {})}
    # the call graph's job is code with no coverage, which mutation testing cannot reach
    untested = [r[0] for r in g.ro_query("MATCH (f:Function) WHERE f.tests = 0 RETURN f.id").result_set]
    reach = {r[0] for r in g.ro_query(Q.UNCOVERED_FALLBACK, {"changed": untested}).result_set}
    summary["blind_spots"] = {"untested_functions": len(untested),
                              "reachable_via_call_graph": len(reach)}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))


def full_run(a, path: Path, original: str, ln: int, new: str) -> set[str] | None:
    """Failing tests of the full suite with one line replaced; None if the bug hangs the suite
    or breaks test collection (then no test id can be blamed)."""
    lines = original.splitlines(keepends=True)
    lines[ln - 1] = new + ("\n" if lines[ln - 1].endswith("\n") else "")
    try:
        path.write_text("".join(lines), encoding="utf-8")
        killers, _ = run_pytest(a.repo, None, src_dir=a.src_dir)
        return None if "<timeout>" in killers else killers
    except RuntimeError:
        return None
    finally:
        path.write_text(original, encoding="utf-8")


def memory_eval(a, rnd: random.Random, budgets: list[int]):
    # the environment must work before a broken run can be blamed on a bug
    if run_pytest(a.repo, None, src_dir=a.src_dir)[0]:
        raise RuntimeError("the unmodified test suite already fails")
    real = connect(a.repo)
    eval_name = graph_name(a.repo) + ":eval"
    os.environ["IMPACTGRAPH_GRAPH"] = eval_name  # decide() and connect() now use the copy
    try:
        connect(a.repo).delete()  # leftover copy from an earlier evaluation
    except Exception:
        pass
    real.copy(eval_name)
    g = connect(a.repo)
    g.query("MATCH (r:Run) DETACH DELETE r")  # start from empty memory

    fns = g.ro_query("MATCH (f:Function) WHERE f.tests > 0 RETURN f.id, f.module, f.start, f.end").result_set
    rnd.shuffle(fns)
    focus = dict(f.rsplit(":", 1) for f in a.focus)
    fns.sort(key=lambda r: r[0] not in focus)

    results, skipped = [], 0
    for fid, module, start, end in fns:
        if len(results) >= a.mutants:
            break
        path = a.repo / a.src_dir / module
        original = path.read_text(encoding="utf-8")
        cands = list(mutations(original, start, end))
        b_cands = [c for c in cands if str(c[0]) == focus[fid]] if fid in focus else cands
        if not b_cands:
            continue
        bug_b = rnd.choice(b_cands)
        a_cands = [c for c in cands if c[0] != bug_b[0]]  # A on another line: a different bug
        if not a_cands:
            continue
        killers_b = full_run(a, path, original, bug_b[0], bug_b[2])
        if not killers_b:
            skipped += 1
            continue
        rnd.shuffle(a_cands)
        for bug_a in a_cands[:3]:  # some bugs break test collection; try another one
            killers_a = full_run(a, path, original, bug_a[0], bug_a[2])
            if killers_a is not None:
                break
        if killers_a is None:
            skipped += 1
            continue
        # remember bug A the way CI would: what we selected, what failed
        sel_a = [p.test for p in decide(a.repo, [fid], budget=a.budget, use_memory=False).selected]
        memory.record(g, f"bugA:{fid}:{bug_a[0]}", [fid], sorted(killers_a), sel_a, source="eval")
        r = {"function": fid, "bug_a": f"line {bug_a[0]} ({bug_a[3]})", "bug_b": f"line {bug_b[0]} ({bug_b[3]})",
             "killers_a": len(killers_a), "killers_b": len(killers_b),
             "overlap": len(killers_a & killers_b), "without": {}, "with": {}}
        for b in budgets:
            for key, use in [("without", False), ("with", True)]:
                picked = {p.test for p in decide(a.repo, [fid], budget=b, use_memory=use).selected}
                r[key][b] = bool(killers_b & picked)
        results.append(r)
        print(f"{fid} B={r['bug_b']} killersA={r['killers_a']} killersB={r['killers_b']} "
              f"without={sum(r['without'].values())} with={sum(r['with'].values())}", flush=True)

    summary = {"pairs": len(results), "skipped": skipped,
               "caught_without_memory": {b: sum(r["without"][b] for r in results) for b in budgets},
               "caught_with_memory": {b: sum(r["with"][b] for r in results) for b in budgets},
               "a_caught_nothing": sum(r["killers_a"] == 0 for r in results),
               "focus": [r for r in results if r["function"] in focus]}
    out = json.loads(a.out.read_text()) if a.out.exists() else {}
    out["memory"] = {"summary": summary, "results": results}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
