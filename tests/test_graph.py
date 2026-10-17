"""The guarantees the README promises, checked against the tiny graph in conftest.py."""
from __future__ import annotations

from conftest import BROAD, FOCUS, HELPER, OTHER, PARSE, UNRELATED

from impactgraph import memory
from impactgraph.agent import validate
from impactgraph.db import connect
from impactgraph.impact import decide, explain
from impactgraph.tools import Toolbox


# --- rule-based ranker -------------------------------------------------------

def test_focused_test_ranks_above_broad_one(repo):
    d = decide(repo, [PARSE])
    assert [p.test for p in d.selected] == [FOCUS, BROAD]
    assert d.coverage_set_size == 2 and d.uncovered == []


def test_unrelated_tests_are_not_selected(repo):
    assert OTHER not in {p.test for p in decide(repo, [PARSE]).selected}


def test_cochange_history_appears_in_reason(repo):
    focus = decide(repo, [PARSE]).selected[0]
    assert "changed with this code in 1 commits" in focus.reasons[0]
    no_history = decide(repo, [PARSE], use_history=False).selected[0]
    assert focus.score > no_history.score


def test_untested_function_falls_back_to_callers_tests(repo):
    d = decide(repo, [HELPER])
    assert d.uncovered == [HELPER]
    assert {p.test for p in d.selected} == {FOCUS, BROAD}
    assert "reaches it via Parser.parse (1 call hops)" in d.selected[0].reasons[0]
    assert decide(repo, [HELPER], use_calls=False).selected == []


def test_explain_path_direct_and_through_calls(repo):
    assert explain(repo, FOCUS, PARSE) == ["Parser.parse"]
    assert explain(repo, FOCUS, HELPER) == ["Parser.parse", "Parser._helper"]
    assert explain(repo, OTHER, PARSE) == []


# --- agent guardrail ---------------------------------------------------------

def test_guardrail_rejects_tests_no_tool_returned(repo):
    tb = Toolbox(repo)
    tb.direct_tests([PARSE])
    kept, rejected = validate(tb, [
        {"test": FOCUS, "function": PARSE, "reason": "focused"},
        {"test": "tests/test_m.py::test_made_up", "function": PARSE, "reason": "invented"},
    ])
    assert [k["test"] for k in kept] == [FOCUS]
    assert kept[0]["path"] == f"{FOCUS} -COVERS-> Parser.parse" and kept[0]["hops"] == 0
    assert rejected[0]["why"] == "test id was not returned by any tool"


def test_guardrail_rejects_test_without_graph_path(repo):
    tb = Toolbox(repo)
    tb.direct_tests([UNRELATED])  # returns OTHER, which has no path to PARSE
    kept, rejected = validate(tb, [{"test": OTHER, "function": PARSE, "reason": "?"}])
    assert kept == [] and rejected[0]["why"] == "graph has no path from this test to that function"


def test_tools_never_write(repo):
    tb = Toolbox(repo)
    before = connect(repo).ro_query("MATCH (n) RETURN count(n)").result_set
    for call in (lambda: tb.direct_tests([PARSE]), lambda: tb.uncovered_callers([HELPER]),
                 lambda: tb.cochanged_test_files([PARSE]), lambda: tb.blast_radius([HELPER]),
                 lambda: tb.load_bearing([PARSE, HELPER]), lambda: tb.recall([PARSE])):
        call()
    assert connect(repo).ro_query("MATCH (n) RETURN count(n)").result_set == before
    assert {t["tool"] for t in tb.trace} >= {"direct_tests", "load_bearing", "recall"}


def test_load_bearing_ranks_the_callee_higher(repo):
    lb = Toolbox(repo).load_bearing([PARSE, HELPER])
    assert lb[HELPER]["pagerank_rank"] < lb[PARSE]["pagerank_rank"]  # score flows caller -> callee


# --- memory ------------------------------------------------------------------

def test_memory_record_links_known_ids_and_counts_unknown(repo):
    res = memory.record(connect(repo), "abc", [PARSE, "pkg/gone.py::x"], [BROAD, "tests/x.py::gone"],
                        selected=[FOCUS], source="ci")
    assert res["linked"] == {"changed": (1, 2), "selected": (1, 1), "failed": (1, 2)}


def test_memory_boosts_a_past_miss_and_explains_it(repo):
    without = {p.test: p for p in decide(repo, [PARSE]).selected}
    memory.record(connect(repo), "abc", [PARSE], failed=[BROAD], selected=[FOCUS])
    d = decide(repo, [PARSE])
    broad = next(p for p in d.selected if p.test == BROAD)
    assert d.memory_hits == 1
    assert broad.score > without[BROAD].score
    assert "failed when Parser.parse changed before" in broad.reasons[0]
    assert "we missed it then" in broad.reasons[0]
    assert decide(repo, [PARSE], use_memory=False).selected[1].score == without[BROAD].score

    tb = Toolbox(repo)
    hit = tb.recall([PARSE])[PARSE]["top"][0]
    assert hit["test"] == BROAD and hit["missed_in_runs"] == 1
    assert tb.explain_memory(BROAD, PARSE)["path"] == f"{BROAD} -FAILED_IN-> Run(abc, ci) -CHANGED-> Parser.parse"


def test_memory_boost_is_weighted_by_how_specific_the_failure_was(repo):
    base = {p.test: p.score for p in decide(repo, [PARSE]).selected}
    # selected and failed: no miss, so only the failure weight applies
    memory.record(connect(repo), "narrow", [PARSE], failed=[BROAD], selected=[BROAD])
    narrow = {p.test: p.score for p in decide(repo, [PARSE]).selected}
    assert narrow[BROAD] / base[BROAD] > 1.9  # 1 failing test -> weight ~0.93
    assert narrow[FOCUS] == base[FOCUS]

    connect(repo).query("MATCH (r:Run) DETACH DELETE r")
    many = [BROAD] + [f"tests/test_x.py::t{i}" for i in range(699)]  # 700 failed: weak evidence
    memory.record(connect(repo), "wide", [PARSE], failed=many, selected=[BROAD])
    wide = {p.test: p.score for p in decide(repo, [PARSE]).selected}
    assert 1.0 < wide[BROAD] / base[BROAD] < 1.3


def test_guardrail_accepts_a_memory_only_path(repo):
    memory.record(connect(repo), "abc", [HELPER], failed=[OTHER])  # OTHER has no coverage path to HELPER
    tb = Toolbox(repo)
    tb.recall([HELPER])
    kept, rejected = validate(tb, [{"test": OTHER, "function": HELPER, "reason": "failed before"}])
    assert rejected == []
    assert kept[0]["hops"] is None and "-FAILED_IN->" in kept[0]["path"]


def test_memory_survives_export_and_restore(repo):
    g = connect(repo)
    memory.record(g, "abc", [PARSE], failed=[BROAD], selected=[FOCUS], run_id="r1")
    runs = memory.export(g)
    runs.append({**runs[0], "id": "r2", "changed": [], "failed": [], "selected": []})  # code no longer exists
    g.query("MATCH (r:Run) DETACH DELETE r")
    assert memory.restore(g, runs) == {"runs": 1, "dropped_runs": 1, "dropped_links": 0}
    (r,) = memory.export(g)
    assert (r["id"], r["changed"], r["failed"], r["selected"]) == ("r1", [PARSE], [BROAD], [FOCUS])
