"""The Cypher queries ImpactGraph depends on. Kept in one place so they are easy
to read, explain and list in the README."""

# 1. What did the change touch, and how exposed is each function?
CHANGED_FUNCTIONS = """
UNWIND $changed AS fid
MATCH (f:Function {id: fid})
OPTIONAL MATCH (c:Commit)-[:MODIFIED]->(f)
RETURN f.id AS id, f.qualname AS qualname, f.module AS module,
       f.tests AS tests, count(c) AS churn
"""

# 2. Direct evidence: tests that executed a changed function, with how focused
#    each test is (a test that runs 5 functions says more than one that runs 300).
DIRECT_TESTS = """
UNWIND $changed AS fid
MATCH (t:Test)-[c:COVERS]->(f:Function {id: fid})
RETURN t.id AS test, t.file AS file, t.breadth AS breadth,
       f.id AS function, f.name AS fname, f.size AS fsize, f.tests AS ftests, c.lines AS lines
"""

# 3. History: test files that were edited in the same commits as the changed
#    functions. Co-change is how a team silently records "these belong together".
COCHANGED_TEST_FILES = """
UNWIND $changed AS fid
MATCH (f:Function {id: fid})<-[:MODIFIED]-(c:Commit)-[:TOUCHED]->(tf:TestFile)
RETURN tf.path AS file, count(DISTINCT c) AS commits, collect(DISTINCT f.qualname) AS via
ORDER BY commits DESC
"""

# 4. Blind spots: changed functions no test executes. Walk the static call graph
#    upwards (up to 3 hops) to find covered callers, and the tests behind them.
UNCOVERED_FALLBACK = """
UNWIND $changed AS fid
MATCH (f:Function {id: fid}) WHERE f.tests = 0
MATCH p = (caller:Function)-[:CALLS*1..3]->(f)
WHERE caller.tests > 0
WITH f, caller, min(length(p)) AS hops
MATCH (t:Test)-[:COVERS]->(caller)
RETURN f.id AS function, t.id AS test, t.file AS file, t.breadth AS breadth,
       caller.qualname AS via, hops
"""

# 5. Explain one decision: the call path from what a test runs to what changed.
#    Either the test covers the function itself (0 hops), or it covers a caller
#    from which the function is reachable. FalkorDB only allows shortestPath in
#    WITH/RETURN; the caller picks the row with the fewest hops.
EXPLAIN_PATH = """
MATCH (t:Test {id: $test})-[:COVERS]->(f:Function {id: $function})
RETURN [f.qualname] AS path, 0 AS hops
UNION
MATCH (t:Test {id: $test})-[:COVERS]->(entry:Function), (f:Function {id: $function})
WHERE entry <> f
WITH shortestPath((entry)-[:CALLS*1..4]->(f)) AS p
WHERE p IS NOT NULL
RETURN [n IN nodes(p) | n.qualname] AS path, length(p) AS hops
"""

# 6. Blast radius summary: everything a change can reach upwards in the call graph.
BLAST_RADIUS = """
UNWIND $changed AS fid
MATCH (f:Function {id: fid})<-[:CALLS*1..3]-(up:Function)
RETURN count(DISTINCT up) AS callers, collect(DISTINCT up.module) AS modules
"""

# 7. Load-bearing code: FalkorDB's PageRank over the call graph. Score flows from
#    callers to callees, so functions that much of the code ends up calling rank
#    high; a change there deserves extra caution. Rank 1 = most load-bearing.
LOAD_BEARING = """
CALL algo.pageRank('Function', 'CALLS') YIELD node, score
WITH collect({id: node.id, score: score}) AS all
UNWIND $changed AS fid
WITH fid, all, [x IN all WHERE x.id = fid][0] AS me
WHERE me IS NOT NULL
RETURN fid, me.score AS score, size([x IN all WHERE x.score > me.score]) + 1 AS rank, size(all) AS total
"""

# --- Memory: what happened when tests ran (track 2) -------------------------
# (:Run)-[:CHANGED]->(:Function), (:Run)-[:SELECTED]->(:Test), (:Test)-[:FAILED_IN]->(:Run).
# "Missed" is not stored: a test that FAILED_IN a run that had a selection, without being SELECTED.
# Writes run only from memory.py (and load.py when restoring); agent tools stay read-only.

# 8. Record one run. The three link queries match by id, so ids the graph does not know are skipped.
RECORD_RUN = """
CREATE (r:Run {id: $id, commit: $commit, date: $date, source: $source, selected: $n_selected,
               failed: $n_failed})
"""
LINK_CHANGED = """
MATCH (r:Run {id: $id}) UNWIND $ids AS x MATCH (f:Function {id: x}) CREATE (r)-[:CHANGED]->(f)
RETURN count(*)
"""
LINK_SELECTED = """
MATCH (r:Run {id: $id}) UNWIND $ids AS x MATCH (t:Test {id: x}) CREATE (r)-[:SELECTED]->(t)
RETURN count(*)
"""
LINK_FAILED = """
MATCH (r:Run {id: $id}) UNWIND $ids AS x MATCH (t:Test {id: x}) CREATE (t)-[:FAILED_IN]->(r)
RETURN count(*)
"""

# 9. Recall: tests that failed in past runs that changed the same function,
#    how often, and how often we failed to select them (a past miss).
#    weight: a failure in a run where few tests failed is specific evidence; one test out of
#    700 failing says little about it (the same idea as discounting broad tests).
RECALL = """
UNWIND $changed AS fid
MATCH (t:Test)-[:FAILED_IN]->(r:Run)-[:CHANGED]->(f:Function {id: fid})
OPTIONAL MATCH (r)-[s:SELECTED]->(t)
WITH f, t, r, CASE WHEN s IS NULL AND r.selected > 0 THEN 1 ELSE 0 END AS miss,
     1.0 / (log(2 + coalesce(r.failed, 1) / 10.0) / log(2)) AS w
RETURN f.id AS function, t.id AS test, t.file AS file, t.breadth AS breadth,
       count(DISTINCT r) AS failures, sum(miss) AS missed, max(r.date) AS last_date,
       collect(r.commit)[0] AS commit, sum(w) AS weight, sum(miss * w) AS missed_weight
"""

# 10. Explain a memory-based choice: test -FAILED_IN-> Run -CHANGED-> function.
EXPLAIN_MEMORY = """
MATCH (t:Test {id: $test})-[:FAILED_IN]->(r:Run)-[:CHANGED]->(f:Function {id: $function})
RETURN r.commit AS commit, r.date AS date, r.source AS source
ORDER BY r.date DESC LIMIT 1
"""

# 11. Everything in memory, by id, so a graph rebuild (load.py) can restore it.
EXPORT_MEMORY = """
MATCH (r:Run)
OPTIONAL MATCH (r)-[:CHANGED]->(f:Function)
WITH r, collect(f.id) AS changed
OPTIONAL MATCH (r)-[:SELECTED]->(s:Test)
WITH r, changed, collect(s.id) AS selected
OPTIONAL MATCH (t:Test)-[:FAILED_IN]->(r)
RETURN r.id, r.commit, r.date, r.source, changed, selected, collect(t.id) AS failed
"""
