"""Decide which tests to run for a change, and say why.

    python -m impactgraph.impact --repo ../click --commit <sha>
    python -m impactgraph.impact --repo ../click            # uncommitted changes
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import queries as Q
from .db import connect
from .extract_git import changed_lines, functions_for_lines


@dataclass
class Pick:
    test: str
    score: float
    reasons: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)


@dataclass
class Decision:
    changed: list[dict]
    selected: list[Pick]
    coverage_set_size: int
    total_tests: int
    uncovered: list[str]
    cochanged_files: list[dict]
    blast_radius: dict


def changed_functions(repo: Path, commit: str | None, src_dir: str = "src") -> list[str]:
    prefix = src_dir + "/"
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                                    text=True, check=True).stdout
    if commit:
        diff = git("show", "-U0", "--format=", commit)
        read = lambda p: git("show", f"{commit}:{p}")
    else:
        diff = git("diff", "-U0", "HEAD")
        read = lambda p: (repo / p).read_text(encoding="utf-8")
    out: set[str] = set()
    for path, lines in changed_lines(diff, prefix).items():
        try:
            out |= functions_for_lines(read(path), path[len(prefix):], lines)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return sorted(out)


def decide(repo: Path, changed: list[str], per_function: int = 5, budget: int = 60) -> Decision:
    g = connect(repo)
    rows = lambda q, **p: g.ro_query(q, p).result_set

    total = rows("MATCH (t:Test) RETURN count(t)")[0][0]
    info = [dict(zip(["id", "qualname", "module", "tests", "churn"], r))
            for r in rows(Q.CHANGED_FUNCTIONS, changed=changed)]
    known = [i["id"] for i in info]

    cochanged = [dict(zip(["file", "commits", "via"], r))
                 for r in rows(Q.COCHANGED_TEST_FILES, changed=known)]
    co = {c["file"]: c["commits"] for c in cochanged}

    picks: dict[str, Pick] = {}
    per_fn: dict[str, list[tuple[float, str]]] = defaultdict(list)
    coverage_set = set()

    def add(test, file, fid, s, reason):
        p = picks.setdefault(test, Pick(test, 0.0))
        p.score += s
        if fid not in p.functions:
            p.functions.append(fid)
        if reason not in p.reasons:
            p.reasons.append(reason)
        per_fn[fid].append((s, test))

    for test, file, breadth, fid, fname, fsize, ftests, lines in rows(Q.DIRECT_TESTS, changed=known):
        coverage_set.add(test)
        focus = 1 / math.log2(2 + breadth)
        idf = math.log(1 + total / max(ftests, 1))
        depth = min(lines / max(fsize, 1), 1.0)
        s = focus * idf * (0.5 + depth)
        short = fid.split("::")[1]
        reason = f"runs {short} ({lines} lines; test touches {breadth} functions)"
        if fname.lower().strip("_") in test.lower():
            s *= 2
            reason += "; named after it"
        if file in co:
            s *= 1 + 0.5 * min(co[file], 4)
            reason += f"; its file changed with this code in {co[file]} commits"
        add(test, file, fid, s, reason)

    uncovered = [i["id"] for i in info if i["tests"] == 0]
    seen_fb = set()
    for fid, test, file, breadth, via, hops in rows(Q.UNCOVERED_FALLBACK, changed=uncovered):
        if (fid, test) in seen_fb:
            continue
        seen_fb.add((fid, test))
        s = 0.3 / (hops * math.log2(2 + breadth))
        add(test, file, fid, s, f"no test runs {fid.split('::')[1]}; reaches it via {via} ({hops} call hops)")

    # Guarantee: every changed function keeps its strongest tests, then fill by score.
    chosen: dict[str, Pick] = {}
    for fid, lst in per_fn.items():
        for _, test in sorted(lst, reverse=True)[:per_function]:
            chosen[test] = picks[test]
    for p in sorted(picks.values(), key=lambda p: -p.score):
        if len(chosen) >= budget:
            break
        chosen.setdefault(p.test, p)

    br = rows(Q.BLAST_RADIUS, changed=known)
    blast = {"callers": br[0][0], "modules": sorted(set(br[0][1]))} if br else {}
    return Decision(info, sorted(chosen.values(), key=lambda p: -p.score), len(coverage_set),
                    total, uncovered, cochanged[:8], blast)


def explain(repo: Path, test: str, function: str) -> list[str]:
    r = connect(repo).ro_query(Q.EXPLAIN_PATH, {"test": test, "function": function}).result_set
    return min(r, key=lambda row: row[1])[0] if r else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--commit")
    ap.add_argument("--src-dir", default="src")
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    changed = changed_functions(a.repo, a.commit, a.src_dir)
    d = decide(a.repo, changed, budget=a.budget)
    if a.json:
        print(json.dumps(asdict(d), indent=2))
        return
    print(f"Changed functions ({len(d.changed)}):")
    for c in d.changed:
        print(f"  {c['qualname']:40s} {c['module']:20s} tests={c['tests']:<5} churn={c['churn']}")
    if d.blast_radius:
        print(f"Blast radius: {d.blast_radius['callers']} callers in {', '.join(d.blast_radius['modules'])}")
    print(f"\nSelected {len(d.selected)} of {d.total_tests} tests "
          f"(plain coverage would run {d.coverage_set_size}):")
    for p in d.selected[:15]:
        print(f"  {p.score:6.2f}  {p.test}")
        print(f"          why: {p.reasons[0]}")
    if d.uncovered:
        print("\nNo direct test coverage:", ", ".join(u.split('::')[1] for u in d.uncovered))


if __name__ == "__main__":
    main()
