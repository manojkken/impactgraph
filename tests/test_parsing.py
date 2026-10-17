"""Pure functions: diff parsing, junit parsing, bug planting, web input checks. No graph needed."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from impactgraph import web
from impactgraph.evaluate import mutations
from impactgraph.extract_git import changed_lines, functions_for_lines
from impactgraph.memory import failed_from_junit

SOURCE = '''\
class Parser:
    def parse(self, x):
        if x is None and self.strict:
            return False
        return not x


def unrelated(a):
    return a == 1
'''


def test_changed_lines_from_unified_diff():
    diff = """\
diff --git a/src/pkg/m.py b/src/pkg/m.py
--- a/src/pkg/m.py
+++ b/src/pkg/m.py
@@ -3 +3,2 @@
-old
+new
+new2
@@ -9,0 +11 @@
+added
diff --git a/README.md b/README.md
+++ b/README.md
@@ -1 +1 @@
"""
    assert changed_lines(diff, "src/") == {"src/pkg/m.py": {3, 4, 11}}


def test_functions_for_lines_maps_to_qualified_ids():
    assert functions_for_lines(SOURCE, "pkg/m.py", {3, 9}) == {"pkg/m.py::Parser.parse", "pkg/m.py::unrelated"}
    assert functions_for_lines(SOURCE, "pkg/m.py", {7}) == set()  # blank line between functions


def test_mutations_plant_one_small_bug_per_line():
    found = {(ln, desc, new.strip()) for ln, _, new, desc in mutations(SOURCE, 2, 5)}
    assert (3, "is -> is not", "if x is not None and self.strict:") in found
    assert (3, "and -> or", "if x is None or self.strict:") in found
    assert (4, "False -> True", "return True") in found
    assert (5, "drop not", "return x") in found
    assert all(2 <= ln <= 5 for ln, _, _ in found)  # never outside the chosen function


def test_junit_failures_become_pytest_ids(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text("""<testsuites><testsuite>
      <testcase classname="tests.test_basic" name="test_ok"/>
      <testcase classname="tests.test_basic" name="test_bad[a-1]"><failure/></testcase>
      <testcase classname="tests.test_options.TestGroup" name="test_err"><error/></testcase>
      <testcase classname="" name="tests.test_broken"><error message="collection failure"/></testcase>
    </testsuite></testsuites>""")
    assert failed_from_junit(report) == ["tests/test_basic.py::test_bad[a-1]",
                                         "tests/test_options.py::TestGroup::test_err"]


@pytest.fixture()
def server(tmp_path, monkeypatch):
    runs = tmp_path / "agent-runs"
    runs.mkdir()
    (runs / "abc1234.json").write_text(json.dumps({"commit": "abc1234"}))
    monkeypatch.setattr(web, "AGENT_RUNS", runs)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(Path("unused"), "src"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url):
    try:
        with urlopen(url) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("path, code", [
    ("/api/decide?commit=../etc", 400),
    ("/api/decide?commit=abcd&budget=lots", 400),
    ("/api/agent-runs/..%2F..%2Fsecrets", 400),
    ("/api/agent-runs/deadbeef", 404),
    ("/nope", 404),
])
def test_web_rejects_bad_input(server, path, code):
    status, body = _get(server + path)
    assert status == code and "error" in body


def test_web_serves_saved_agent_runs(server):
    assert _get(server + "/api/agent-runs") == (200, ["abc1234"])
    assert _get(server + "/api/agent-runs/abc1234") == (200, {"commit": "abc1234"})
