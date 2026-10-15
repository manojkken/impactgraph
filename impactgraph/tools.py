"""Graph tools the agent calls. Each one runs Cypher from queries.py and returns
small JSON-ready dicts, so the model sees evidence, not raw result sets.

A Toolbox remembers every test id a tool returned (`seen_tests`), which lets the
agent layer reject tests the graph never produced, and keeps a trace of calls.
"""
from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path

from . import queries as Q
from .db import connect
from .impact import changed_functions, decide

MAX_DIFF_CHARS = 20000


class Toolbox:
    def __init__(self, repo: Path, commit: str | None = None, src_dir: str = "src"):
        self.repo, self.commit, self.src_dir = repo, commit, src_dir
        self.g = connect(repo)
        self.seen_tests: set[str] = set()
        self.trace: list[dict] = []

    def _rows(self, q: str, **params):
        return self.g.ro_query(q, params).result_set  # read-only: tools can never write

    def _log(self, tool: str, args: dict, t0: float, rows: int, query: str | None = None):
        self.trace.append({"tool": tool, "args": args, "query": query, "rows": rows,
                           "ms": round((time.time() - t0) * 1000, 1)})

    def _total_tests(self) -> int:
        return self._rows("MATCH (t:Test) RETURN count(t)")[0][0]

    # 1. what changed
    def get_change(self) -> dict:
        t0 = time.time()
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], capture_output=True,
                                        text=True, check=True).stdout
        if self.commit:
            subject = git("log", "-1", "--format=%s", self.commit).strip()
            diff = git("show", "--format=", self.commit, "--", self.src_dir)
        else:
            subject = "(uncommitted changes)"
            diff = git("diff", "HEAD", "--", self.src_dir)
        changed = changed_functions(self.repo, self.commit, self.src_dir)
        info = [dict(zip(["id", "qualname", "module", "tests", "churn"], r))
                for r in self._rows(Q.CHANGED_FUNCTIONS, changed=changed)]
        known = {i["id"] for i in info}
        out = {"commit": self.commit, "subject": subject,
               "changed_functions": info,
               "not_in_graph": [c for c in changed if c not in known],
               "total_tests": self._total_tests(),
               "diff": diff[:MAX_DIFF_CHARS],
               "diff_truncated": len(diff) > MAX_DIFF_CHARS}
        self._log("get_change", {}, t0, len(info), "CHANGED_FUNCTIONS")
        return out

    # 2. tests that execute the changed functions
    def direct_tests(self, functions: list[str], limit: int = 15) -> dict:
        t0 = time.time()
        rows = self._rows(Q.DIRECT_TESTS, changed=functions)
        by_fn: dict[str, list[dict]] = {}
        for test, file, breadth, fid, fname, fsize, ftests, lines in rows:
            by_fn.setdefault(fid, []).append({
                "test": test, "file": file, "lines_hit": lines, "function_size": fsize,
                "test_breadth": breadth, "named_after_function": fname.lower().strip("_") in test.lower(),
                # focused tests that hit more of the function rank first
                "_rank": (1 / math.log2(2 + breadth)) * (0.5 + min(lines / max(fsize, 1), 1.0))})
        out = {}
        for fid, lst in by_fn.items():
            lst.sort(key=lambda d: -d["_rank"])
            top = lst[:limit]
            for d in top:
                d.pop("_rank")
                self.seen_tests.add(d["test"])
            out[fid] = {"covering_tests": len(lst), "top": top}
        for fid in functions:
            out.setdefault(fid, {"covering_tests": 0, "top": []})
        self._log("direct_tests", {"functions": functions, "limit": limit}, t0, len(rows), "DIRECT_TESTS")
        return out

    # 3. test files that historically change together with these functions
    def cochanged_test_files(self, functions: list[str]) -> list[dict]:
        t0 = time.time()
        out = [{"test_file": f, "commits": n, "via": via}
               for f, n, via in self._rows(Q.COCHANGED_TEST_FILES, changed=functions)][:10]
        self._log("cochanged_test_files", {"functions": functions}, t0, len(out), "COCHANGED_TEST_FILES")
        return out

    # 4. untested functions: covered callers up the call graph, and their tests
    def uncovered_callers(self, functions: list[str], limit: int = 10) -> dict:
        t0 = time.time()
        rows = self._rows(Q.UNCOVERED_FALLBACK, changed=functions)
        by_fn: dict[str, list[dict]] = {}
        for fid, test, file, breadth, via, hops in rows:
            by_fn.setdefault(fid, []).append({"test": test, "file": file, "test_breadth": breadth,
                                              "via_caller": via, "call_hops": hops})
        out = {}
        for fid, lst in by_fn.items():
            lst.sort(key=lambda d: (d["call_hops"], d["test_breadth"]))
            top = lst[:limit]
            self.seen_tests.update(d["test"] for d in top)
            out[fid] = {"candidate_tests": len(lst), "top": top}
        self._log("uncovered_callers", {"functions": functions}, t0, len(rows), "UNCOVERED_FALLBACK")
        return out

    # 5. the graph path from a test to a changed function
    def explain_path(self, test: str, function: str, record: bool = True) -> dict:
        t0 = time.time()
        r = self._rows(Q.EXPLAIN_PATH, test=test, function=function)
        if r:
            self.seen_tests.add(test)
            path, hops = min(r, key=lambda row: row[1])
            out = {"test": test, "function": function, "found": True, "hops": hops,
                   "path": f"{test} -COVERS-> " + " -CALLS-> ".join(path)}
        else:
            out = {"test": test, "function": function, "found": False}
        if record:
            self._log("explain_path", {"test": test, "function": function}, t0, len(r), "EXPLAIN_PATH")
        return out

    # 6. how far a change can spread
    def blast_radius(self, functions: list[str]) -> dict:
        t0 = time.time()
        r = self._rows(Q.BLAST_RADIUS, changed=functions)
        out = {"callers": r[0][0], "modules": sorted(set(r[0][1]))} if r else {"callers": 0, "modules": []}
        self._log("blast_radius", {"functions": functions}, t0, len(r), "BLAST_RADIUS")
        return out

    # 7. the rule-based ranking (impact.decide) as a starting point
    def baseline_selection(self, functions: list[str], budget: int = 40) -> dict:
        t0 = time.time()
        d = decide(self.repo, functions, budget=budget)
        picks = [{"test": p.test, "score": round(p.score, 2), "functions": p.functions,
                  "reason": p.reasons[0]} for p in d.selected]
        self.seen_tests.update(p["test"] for p in picks)
        out = {"selected": picks, "coverage_set_size": d.coverage_set_size,
               "total_tests": d.total_tests, "uncovered": d.uncovered}
        self._log("baseline_selection", {"functions": functions, "budget": budget}, t0, len(picks))
        return out
