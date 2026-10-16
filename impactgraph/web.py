"""Local web UI: pick a commit, see which tests ImpactGraph runs and the graph path behind each.

    python -m impactgraph.web --repo ../click        # http://localhost:8000

Read-only: every query goes through ro_query; the server binds to 127.0.0.1 only.
The rule-based ranker runs live (free); agent decisions are shown from docs/agent-runs/*.json,
saved with `python -m impactgraph.agent --commit <sha> --json`, so the page needs no API key.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import memory
from . import queries as Q
from .db import connect
from .impact import changed_functions, decide

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
AGENT_RUNS = ROOT / "docs" / "agent-runs"
EVAL_FILE = ROOT / "docs" / "evaluation-click.json"
SHA = re.compile(r"^[0-9a-f]{4,40}$")

RECENT_COMMITS = """
MATCH (c:Commit)-[:MODIFIED]->(f:Function)
RETURN c.sha, c.subject, c.date, count(f) AS functions
ORDER BY c.date DESC LIMIT 300
"""


class BadRequest(Exception):
    pass


def _path_chips(repo: Path, test: str, fid: str) -> list[dict]:
    """The graph path for one choice as a list of chips: node, edge, node, ...
    Coverage/call path first (EXPLAIN_PATH); otherwise the memory path (EXPLAIN_MEMORY)."""
    g = connect(repo)
    rows = g.ro_query(Q.EXPLAIN_PATH, {"test": test, "function": fid}).result_set
    if rows:
        names = min(rows, key=lambda r: r[1])[0]
        chips = [{"node": test, "kind": "test"}, {"edge": "COVERS"}]
        for i, n in enumerate(names):
            if i:
                chips.append({"edge": "CALLS"})
            chips.append({"node": n, "kind": "function"})
        return chips
    rows = g.ro_query(Q.EXPLAIN_MEMORY, {"test": test, "function": fid}).result_set
    if rows:
        commit, _, source = rows[0]
        return [{"node": test, "kind": "test"}, {"edge": "FAILED_IN"},
                {"node": f"Run {str(commit)[:10]} ({source})", "kind": "run"}, {"edge": "CHANGED"},
                {"node": fid.split("::")[1], "kind": "function"}]
    return []


@lru_cache(maxsize=64)
def api_decide(repo: Path, sha: str, budget: int, src_dir: str) -> dict:
    t0 = time.time()
    changed = changed_functions(repo, sha, src_dir)
    d = decide(repo, changed, budget=budget)
    picks = [{"test": p.test, "score": round(p.score, 3), "functions": p.functions,
              "reasons": p.reasons, "path": _path_chips(repo, p.test, p.functions[0])}
             for p in d.selected]
    known = {c["id"] for c in d.changed}
    return {"commit": sha, "changed": d.changed, "not_in_graph": [c for c in changed if c not in known],
            "selected": picks, "total_tests": d.total_tests, "coverage_set_size": d.coverage_set_size,
            "uncovered": d.uncovered, "cochanged_files": d.cochanged_files, "blast_radius": d.blast_radius,
            "memory_hits": d.memory_hits, "ms": round((time.time() - t0) * 1000)}


def api_commits(repo: Path) -> list[dict]:
    saved = {p.stem for p in AGENT_RUNS.glob("*.json")}
    rows = connect(repo).ro_query(RECENT_COMMITS).result_set
    return [{"sha": sha, "subject": subject, "date": date, "functions": n,
             "agent_run": sha in saved} for sha, subject, date, n in rows]


def api_agent_run(name: str) -> dict:
    if not SHA.match(name):
        raise BadRequest("not a commit sha")
    for p in AGENT_RUNS.glob("*.json"):  # only files that exist in the folder: no path traversal
        if p.stem == name or p.stem.startswith(name):
            return json.loads(p.read_text())
    raise FileNotFoundError(name)


def api_eval() -> dict:
    d = json.loads(EVAL_FILE.read_text())
    m = d.get("memory", {})
    return {"summary": d["summary"],
            "memory": {k: m[k]["summary"] for k in ("heldout_seed11", "dev_seed7") if k in m}}


def api_memory(repo: Path) -> list[dict]:
    return [{"id": r["id"], "source": r["source"], "commit": r["commit"], "changed": len(r["changed"]),
             "selected": len(r["selected"]), "failed": len(r["failed"])} for r in memory.export(connect(repo))]


def make_handler(repo: Path, src_dir: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/commits":
                    return self._json(200, api_commits(repo))
                if url.path == "/api/decide":
                    sha = q.get("commit", [""])[0]
                    if not SHA.match(sha):
                        raise BadRequest("commit must be a hex sha")
                    try:
                        budget = max(1, min(int(q.get("budget", ["60"])[0]), 200))
                    except ValueError:
                        raise BadRequest("budget must be a number")
                    return self._json(200, api_decide(repo, sha, budget, src_dir))
                if url.path == "/api/agent-runs":
                    return self._json(200, sorted(p.stem for p in AGENT_RUNS.glob("*.json")))
                if url.path.startswith("/api/agent-runs/"):
                    return self._json(200, api_agent_run(url.path.rsplit("/", 1)[1]))
                if url.path == "/api/eval":
                    return self._json(200, api_eval())
                if url.path == "/api/memory":
                    return self._json(200, api_memory(repo))
                return self._json(404, {"error": "not found"})
            except BadRequest as e:
                return self._json(400, {"error": str(e)})
            except FileNotFoundError:
                return self._json(404, {"error": "not found"})
            except Exception as e:  # keep the server up; the page shows the message
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--src-dir", default="src")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.repo.resolve(), a.src_dir))
    print(f"ImpactGraph UI on http://localhost:{a.port}  (repo {a.repo.resolve().name}, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
