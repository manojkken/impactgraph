"""FalkorDB connection. Uses a FalkorDB server when FALKORDB_HOST is set
(Docker, self-hosted or FalkorDB Cloud); otherwise an embedded FalkorDB Lite
instance stored in ./data/impactgraph.db so the project runs with no setup.

Each analysed repository gets its own graph ("impactgraph:<repo dir>"), so one
FalkorDB holds several repos and a query can never mix them up."""
from __future__ import annotations

import os
from pathlib import Path


def graph_name(repo: Path) -> str:
    return os.environ.get("IMPACTGRAPH_GRAPH") or f"impactgraph:{repo.resolve().name}"


def connect(repo: Path):
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
    return db.select_graph(graph_name(repo))
