"""Does the selection actually catch bugs? Mutation testing against the full suite.

For a sample of functions: plant one small bug (flip a comparison, swap and/or,
drop a `not`, flip a boolean constant), run the FULL test suite to learn which
tests catch it, then check whether ImpactGraph's selection would have caught it.
A random selection of the same size is the baseline.

    python -m impactgraph.evaluate --repo ../click --mutants 25
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import subprocess
import sys
import time
from pathlib import Path

from .db import connect
from .impact import decide

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


def run_pytest(repo: Path, test_ids: list[str] | None, timeout: int = 900) -> tuple[set[str], float]:
    args = [sys.executable, "-m", "pytest", "-q", "-rf", "--tb=no", "-p", "no:cacheprovider",
            "-o", "console_output_style=classic"]
    if test_ids is not None:
        args += test_ids
    t0 = time.time()
    try:
        out = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return {"<timeout>"}, timeout
    failed = {l[7:].split(" - ")[0].strip() for l in out.splitlines() if l.startswith("FAILED ")}
    return failed, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--src-dir", default="src")
    ap.add_argument("--mutants", type=int, default=25)
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=Path("data/evaluation.json"))
    a = ap.parse_args()
    rnd = random.Random(a.seed)

    g = connect()
    all_tests = [r[0] for r in g.query("MATCH (t:Test) RETURN t.id").result_set]
    fns = g.query("MATCH (f:Function) WHERE f.tests > 0 RETURN f.id, f.module, f.start, f.end").result_set
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
            killers, full_t = run_pytest(a.repo, None)
            if not killers:
                print(f"survived full suite: {fid} line {ln} ({desc})")
                results.append({"function": fid, "line": ln, "mutation": desc, "killed": False})
                continue
            d = decide([fid], budget=a.budget)
            chosen = [p.test for p in d.selected]
            _, sel_t = run_pytest(a.repo, chosen)
            random_pick = set(rnd.sample(all_tests, len(chosen)))
            r = {"function": fid, "line": ln, "mutation": desc, "killed": True,
                 "killers": len(killers), "selected": len(chosen), "coverage_set": d.coverage_set_size,
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
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
