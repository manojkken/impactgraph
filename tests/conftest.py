"""A tiny hand-built graph in an embedded FalkorDB Lite database, so the tests need
no Docker, no click checkout and no API key.

    pkg/m.py::Parser.parse   covered by test_focus (narrow) and test_broad (wide)
        └─CALLS─► pkg/m.py::Parser._helper   untested: reachable only through the call graph
    pkg/m.py::unrelated      covered by test_other
    commit c1 MODIFIED parse and TOUCHED tests/test_m.py
"""
from __future__ import annotations

import pytest

from impactgraph.db import connect

FOCUS = "tests/test_m.py::test_parse_basic"
BROAD = "tests/test_other.py::test_everything"
OTHER = "tests/test_other.py::test_unrelated"
PARSE = "pkg/m.py::Parser.parse"
HELPER = "pkg/m.py::Parser._helper"
UNRELATED = "pkg/m.py::unrelated"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A fresh graph per test. Returns the 'repo' path the code passes to connect()."""
    monkeypatch.delenv("FALKORDB_HOST", raising=False)
    monkeypatch.setenv("IMPACTGRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.setenv("IMPACTGRAPH_GRAPH", "impactgraph:test")
    g = connect(tmp_path)
    g.query("""
    CREATE (m:Module {path: 'pkg/m.py'}),
           (parse:Function {id: $parse, name: 'parse', qualname: 'Parser.parse', module: 'pkg/m.py',
                            start: 10, end: 19, size: 10, tests: 2}),
           (helper:Function {id: $helper, name: '_helper', qualname: 'Parser._helper', module: 'pkg/m.py',
                             start: 21, end: 25, size: 5, tests: 0}),
           (unrel:Function {id: $unrel, name: 'unrelated', qualname: 'unrelated', module: 'pkg/m.py',
                            start: 30, end: 33, size: 4, tests: 1}),
           (m)-[:DEFINES]->(parse), (m)-[:DEFINES]->(helper), (m)-[:DEFINES]->(unrel),
           (parse)-[:CALLS {resolution: 'self'}]->(helper),
           (tf1:TestFile {path: 'tests/test_m.py'}), (tf2:TestFile {path: 'tests/test_other.py'}),
           (focus:Test {id: $focus, name: 'test_parse_basic', file: 'tests/test_m.py', breadth: 1}),
           (broad:Test {id: $broad, name: 'test_everything', file: 'tests/test_other.py', breadth: 40}),
           (other:Test {id: $other, name: 'test_unrelated', file: 'tests/test_other.py', breadth: 2}),
           (tf1)-[:CONTAINS]->(focus), (tf2)-[:CONTAINS]->(broad), (tf2)-[:CONTAINS]->(other),
           (focus)-[:COVERS {lines: 9}]->(parse), (broad)-[:COVERS {lines: 3}]->(parse),
           (other)-[:COVERS {lines: 4}]->(unrel),
           (c:Commit {sha: 'c1', date: '2026-10-01', subject: 'change parse'}),
           (c)-[:MODIFIED]->(parse), (c)-[:TOUCHED]->(tf1)
    """, {"parse": PARSE, "helper": HELPER, "unrel": UNRELATED, "focus": FOCUS, "broad": BROAD, "other": OTHER})
    return tmp_path
