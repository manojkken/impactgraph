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
EXPLAIN_PATH = """
MATCH (t:Test {id: $test})-[:COVERS]->(entry:Function)
MATCH p = shortestPath((entry)-[:CALLS*0..4]->(f:Function {id: $function}))
RETURN [n IN nodes(p) | n.qualname] AS path, length(p) AS hops
ORDER BY hops LIMIT 1
"""

# 6. Blast radius summary: everything a change can reach upwards in the call graph.
BLAST_RADIUS = """
UNWIND $changed AS fid
MATCH (f:Function {id: fid})<-[:CALLS*1..3]-(up:Function)
RETURN count(DISTINCT up) AS callers, collect(DISTINCT up.module) AS modules
"""
