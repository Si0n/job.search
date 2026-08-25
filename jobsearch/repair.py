from __future__ import annotations

import gzip
import json
from pathlib import Path

from jobsearch import store


def baseline(conn, source_id: int, runs: int = 7) -> dict:
    """What this source normally produces, so a proposal has something to beat."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT AVG(fetched) AS avg_fetched, MAX(fetched) AS max_fetched, COUNT(*) AS runs "
            "FROM (SELECT fetched FROM runs WHERE kind='harvest' AND source_id=%s "
            "      AND error IS NULL AND fetched > 0 ORDER BY started_at DESC LIMIT %s) t",
            (source_id, runs),
        )
        row = cur.fetchone() or {}
    return {
        "avg_fetched": float(row.get("avg_fetched") or 0),
        "max_fetched": int(row.get("max_fetched") or 0),
        "runs_considered": int(row.get("runs") or 0),
    }


def context(conn, source_name: str) -> dict:
    source = store.source_by_name(conn, source_name)
    latest = store.last_fetch(conn, source["id"])
    if latest is None:
        raise RuntimeError(f"no cached fetch for '{source_name}' — run a harvest first")

    path = Path(latest["path"])
    if not path.exists():
        # raw_fetches rows outlive their files: prune_cache deletes files older
        # than 7 days but never the row (job_sources.raw_fetch_id FKs to it with
        # no ON DELETE). A row whose path no longer resolves means the cache
        # aged out, not that anything is corrupt — this must read as that,
        # not as a bare FileNotFoundError traceback.
        raise RuntimeError(
            f"cached fetch for '{source_name}' at {path} no longer exists on disk — "
            "the cache aged out (prune-cache removes files after 7 days but keeps the "
            "raw_fetches row). Run a harvest for this source to get a fresh cache "
            "before repairing."
        )

    body = gzip.decompress(path.read_bytes())
    return {
        "command": "repair-context",
        "source": source_name,
        "status": source["status"],
        "consecutive_empty": source["consecutive_empty"],
        "current_selectors": json.loads(source["selectors"]) if source["selectors"] else None,
        "cached_fetch": {
            "path": latest["path"],
            "http_status": latest["http_status"],
            "fetched_at": latest["fetched_at"],
            "bytes": len(body),
        },
        "baseline": baseline(conn, source["id"]),
    }
