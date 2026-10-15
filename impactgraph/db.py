"""FalkorDB connection. Uses a FalkorDB server when FALKORDB_HOST is set
(Docker, self-hosted or FalkorDB Cloud); otherwise an embedded FalkorDB Lite
instance stored in ./data/impactgraph.db so the project runs with no setup."""
from __future__ import annotations

import os
from pathlib import Path

GRAPH = os.environ.get("IMPACTGRAPH_GRAPH", "impactgraph")


def connect():
    host = os.environ.get("FALKORDB_HOST")
    if host:
        from falkordb import FalkorDB
        db = FalkorDB(host=host, port=int(os.environ.get("FALKORDB_PORT", 6379)),
                      username=os.environ.get("FALKORDB_USERNAME"),
                      password=os.environ.get("FALKORDB_PASSWORD"))
    else:
        from redislite.falkordb_client import FalkorDB
        path = Path(os.environ.get("IMPACTGRAPH_DB", "data/impactgraph.db"))
        path.parent.mkdir(parents=True, exist_ok=True)
        db = FalkorDB(str(path))
    return db.select_graph(GRAPH)
