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
