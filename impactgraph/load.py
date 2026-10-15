"""Build the ImpactGraph in FalkorDB from a target repository.

    python -m impactgraph.load --repo ../click --package click

Graph model
  (:Module {path})
  (:Function {id, name, qualname, module, start, end})
  (:TestFile {path})
  (:Test {id, name, file})
  (:Commit {sha, date, subject})

  (:Module)-[:DEFINES]->(:Function)
  (:Module)-[:IMPORTS]->(:Module)
  (:Function)-[:CALLS {resolution}]->(:Function)
  (:TestFile)-[:CONTAINS]->(:Test)
  (:Test)-[:COVERS {lines}]->(:Function)
  (:Commit)-[:MODIFIED]->(:Function)
  (:Commit)-[:TOUCHED]->(:TestFile)
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import extract_code, extract_coverage, extract_git
from .db import connect

BATCH = 2000


def _batched(g, query: str, rows: list[dict]):
    for i in range(0, len(rows), BATCH):
        g.query(query, {"rows": rows[i:i + BATCH]})


def build(repo: Path, package: str, src_dir: str = "src", test_dir: str = "tests",
          coverage_file: Path | None = None, max_commits: int = 400):
    t0 = time.time()
    src_root = repo / src_dir
    modules, funcs, calls = extract_code.extract(src_root, package)
    print(f"code: {len(modules)} modules, {len(funcs)} functions, {len(calls)} call edges")

    covers = extract_coverage.extract(coverage_file or repo / ".coverage", src_root, funcs)
    print(f"coverage: {len(covers)} tests, {sum(map(len, covers.values()))} COVERS edges")

    commits = extract_git.extract(repo, src_dir + "/", test_dir + "/", max_commits)
    print(f"history: {len(commits)} commits")

    g = connect(repo)
    try:
        g.delete()
    except Exception:
        pass
    g = connect(repo)
    for label, prop in [("Module", "path"), ("Function", "id"), ("TestFile", "path"),
                        ("Test", "id"), ("Commit", "sha")]:
        g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")

    _batched(g, "UNWIND $rows AS r CREATE (:Module {path: r.path})",
             [{"path": m} for m in modules])
    _batched(g, """UNWIND $rows AS r MATCH (m:Module {path: r.module})
                   CREATE (m)-[:DEFINES]->(:Function {id: r.id, name: r.name, qualname: r.qualname,
                          module: r.module, start: r.start, end: r.end})""",
             [{"id": f.id, "name": f.name, "qualname": f.qualname, "module": f.module,
               "start": f.start, "end": f.end} for f in funcs.values()])
    _batched(g, """UNWIND $rows AS r MATCH (a:Module {path: r.a}), (b:Module {path: r.b})
                   CREATE (a)-[:IMPORTS]->(b)""",
             [{"a": m.path, "b": i} for m in modules.values() for i in m.imports])
    _batched(g, """UNWIND $rows AS r MATCH (a:Function {id: r.a}), (b:Function {id: r.b})
                   CREATE (a)-[:CALLS {resolution: r.res}]->(b)""",
             [{"a": a, "b": b, "res": res} for a, b, res in calls])

    test_files = sorted({extract_coverage.split_test_id(t)[0] for t in covers}
                        | {p for c in commits for p in c.test_files})
    _batched(g, "UNWIND $rows AS r CREATE (:TestFile {path: r.path})",
             [{"path": p} for p in test_files])
    _batched(g, """UNWIND $rows AS r MATCH (tf:TestFile {path: r.file})
                   CREATE (tf)-[:CONTAINS]->(:Test {id: r.id, name: r.name, file: r.file})""",
             [{"id": t, "file": extract_coverage.split_test_id(t)[0],
               "name": extract_coverage.split_test_id(t)[1]} for t in covers])
    _batched(g, """UNWIND $rows AS r MATCH (t:Test {id: r.t}), (f:Function {id: r.f})
                   CREATE (t)-[:COVERS {lines: r.n}]->(f)""",
             [{"t": t, "f": f, "n": n} for t, fs in covers.items() for f, n in fs.items()])

    # Precomputed graph degrees used for ranking: how broad a test is, and how
    # many tests reach a function.
    g.query("MATCH (t:Test) OPTIONAL MATCH (t)-[:COVERS]->(f) WITH t, count(f) AS n SET t.breadth = n")
    g.query("MATCH (f:Function) OPTIONAL MATCH (t:Test)-[:COVERS]->(f) WITH f, count(t) AS n SET f.tests = n")
    g.query("MATCH (f:Function) SET f.size = f.end - f.start + 1")

    _batched(g, "UNWIND $rows AS r CREATE (:Commit {sha: r.sha, date: r.date, subject: r.subject})",
             [{"sha": c.sha, "date": c.date, "subject": c.subject} for c in commits])
    _batched(g, """UNWIND $rows AS r MATCH (c:Commit {sha: r.sha}), (f:Function {id: r.f})
                   CREATE (c)-[:MODIFIED]->(f)""",
             [{"sha": c.sha, "f": f} for c in commits for f in c.functions if f in funcs])
    _batched(g, """UNWIND $rows AS r MATCH (c:Commit {sha: r.sha}), (tf:TestFile {path: r.p})
                   CREATE (c)-[:TOUCHED]->(tf)""",
             [{"sha": c.sha, "p": p} for c in commits for p in c.test_files])

    stats = g.query("MATCH (n) RETURN labels(n)[0] AS l, count(n) ORDER BY l").result_set
    rels = g.query("MATCH ()-[r]->() RETURN type(r), count(r) ORDER BY type(r)").result_set
    print("nodes:", dict(stats))
    print("edges:", dict(rels))
    print(f"built in {time.time() - t0:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--package", required=True)
    ap.add_argument("--src-dir", default="src")
    ap.add_argument("--test-dir", default="tests")
    ap.add_argument("--coverage-file", type=Path)
    ap.add_argument("--max-commits", type=int, default=400)
    a = ap.parse_args()
    build(a.repo, a.package, a.src_dir, a.test_dir, a.coverage_file, a.max_commits)


if __name__ == "__main__":
    main()
