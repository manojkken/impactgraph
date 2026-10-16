"""Memory: store what happened when tests ran, so the next decision can use it.

A Run node records which functions a change touched, which tests we selected and
which tests failed. The ranker (impact.decide) and the agent's `recall` tool read
it back: "when this function changed before, these tests failed".

This module is the only writer besides load.py; the agent's tools stay read-only.

    pytest --junitxml=report.xml ...          # in CI, or locally
    python -m impactgraph.memory record --repo ../click --commit <sha> --junit report.xml
    python -m impactgraph.memory show --repo ../click
"""
from __future__ import annotations

import argparse
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from . import queries as Q
from .db import connect
from .impact import changed_functions


def record(g, commit: str | None, changed: list[str], failed: list[str],
           selected: list[str] | None = None, source: str = "ci", run_id: str | None = None,
           date: int | None = None) -> dict:
    """Store one run. Returns how many ids were linked, so unknown ids are visible."""
    rid = run_id or uuid.uuid4().hex[:12]
    selected = selected or []
    g.query(Q.RECORD_RUN, {"id": rid, "commit": commit or "working-tree", "source": source,
                           "date": date or int(time.time()), "n_selected": len(selected),
                           "n_failed": len(set(failed))})
    linked = {}
    for name, q, ids in [("changed", Q.LINK_CHANGED, changed), ("selected", Q.LINK_SELECTED, selected),
                         ("failed", Q.LINK_FAILED, failed)]:
        n = g.query(q, {"id": rid, "ids": sorted(set(ids))}).result_set if ids else [[0]]
        linked[name] = (n[0][0] if n else 0, len(set(ids)))
    return {"run": rid, "linked": linked}


def export(g) -> list[dict]:
    try:
        rows = g.ro_query(Q.EXPORT_MEMORY).result_set
    except Exception:  # the graph does not exist yet
        return []
    keys = ["id", "commit", "date", "source", "changed", "selected", "failed"]
    return [dict(zip(keys, r)) for r in rows]


def restore(g, runs: list[dict]) -> dict:
    """Recreate exported runs after a rebuild. Ids that no longer exist are dropped and counted;
    a run whose changed functions are all gone can never be recalled, so it is dropped too."""
    dropped, kept = 0, 0
    for r in runs:
        if not r["changed"]:
            continue
        kept += 1
        res = record(g, r["commit"], r["changed"], r["failed"], r["selected"], r["source"], r["id"], r["date"])
        dropped += sum(want - got for got, want in res["linked"].values())
    return {"runs": kept, "dropped_runs": len(runs) - kept, "dropped_links": dropped}


def failed_from_junit(path: Path) -> list[str]:
    """Failing test ids (pytest node ids) from a pytest --junitxml report."""
    out = []
    for case in ET.parse(path).iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        if not case.get("classname"):  # collection error: a whole module broke, no test id to blame
            continue
        # classname "tests.test_basic" + name "test_x[a]" -> "tests/test_basic.py::test_x[a]"
        parts = case.get("classname", "").split(".")
        mod = [p for p in parts if not p[:1].isupper()]
        cls = [p for p in parts if p[:1].isupper()]
        out.append("/".join(mod) + ".py::" + "::".join(cls + [case.get("name", "")]))
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="store a test run")
    rec.add_argument("--repo", type=Path, required=True)
    rec.add_argument("--commit")
    rec.add_argument("--src-dir", default="src")
    rec.add_argument("--junit", type=Path, required=True, help="pytest --junitxml report")
    rec.add_argument("--selected", type=Path, help="file with the selected test ids, one per line")
    show = sub.add_parser("show", help="list stored runs")
    show.add_argument("--repo", type=Path, required=True)
    a = ap.parse_args()

    g = connect(a.repo)
    if a.cmd == "record":
        changed = changed_functions(a.repo, a.commit, a.src_dir)
        failed = failed_from_junit(a.junit)
        selected = a.selected.read_text().split() if a.selected else None
        print(record(g, a.commit, changed, failed, selected))
    else:
        for r in export(g):
            print(f"{r['id']}  {r['source']:5s} commit={r['commit'][:10]}  changed={len(r['changed'])} "
                  f"selected={len(r['selected'])} failed={len(r['failed'])}")


if __name__ == "__main__":
    main()
