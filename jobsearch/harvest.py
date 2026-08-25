from __future__ import annotations

import gzip
import json
import time
from datetime import datetime
from pathlib import Path

import httpx

from jobsearch import store
from jobsearch.adapters import registry

RAW_DIR = "var/raw"
DELAY_SECONDS = 2.0
NOT_MODIFIED = 304
PRUNE_AFTER_DAYS = 7


def _persist(raw, source_name: str, run_id: int) -> str:
    directory = Path(RAW_DIR) / source_name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.gz"
    path.write_bytes(gzip.compress(raw.body))
    return str(path)


def _harvest_source(conn, settings, source: dict) -> dict:
    now = datetime.now()
    run_id = store.start_run(conn, "harvest", source["id"])
    summary = {
        "source": source["name"], "run_id": run_id, "status": None,
        "fetched": 0, "new": 0, "dropped": 0, "error": None,
    }

    try:
        adapter = registry.get(source["name"])
        previous = store.last_fetch(conn, source["id"])
        conditional = (
            {"etag": previous["etag"], "last_modified": previous["last_modified"]}
            if previous else None
        )

        query = json.loads(source["query"]) if source["query"] else {}
        selectors = json.loads(source["selectors"]) if source["selectors"] else None
        if not selectors:
            raise RuntimeError(f"source '{source['name']}' has no selectors configured")

        raw = adapter.fetch(query, conditional)
        path = _persist(raw, source["name"], run_id)
        fetch_id = store.record_fetch(conn, source["id"], run_id, raw, path)

        if raw.http_status == NOT_MODIFIED:
            # 304 means the site confirmed nothing changed since our last
            # conditional request. The body is empty by definition, and
            # feeding an empty body to adapter.parse() would hit the "no
            # items" path and get classified `broken` — wrongly degrading a
            # perfectly healthy source. Treat it as an uneventful run.
            #
            # mark_source_ran, NOT mark_source: advancing last_ok_at here would
            # tell sweep every posting was reconfirmed, but a 304 means nothing
            # was parsed at all — no posting's last_seen_at moved. Three 304s
            # would then age (and eventually deactivate) the source's entire
            # inventory despite the listing being byte-identical to last time.
            summary["status"] = "not_modified"
            store.mark_source_ran(conn, source["id"], now)
            store.finish_run(conn, run_id, fetched=0, new=0)
            return summary

        result = adapter.parse(raw, selectors)
        summary["status"] = result.status
        summary["fetched"] = len(result.postings)
        summary["dropped"] = result.diagnostics.get("dropped", 0)
        summary["diagnostics"] = result.diagnostics

        for posting in result.postings:
            _job_id, is_new = store.upsert_posting(
                conn, source, posting, fetch_id, settings.rates, now
            )
            summary["new"] += int(is_new)

        store.mark_source(
            conn, source["id"],
            "ok" if result.status in ("ok", "empty") else "degraded",
            saw_items=bool(result.postings), now=now,
        )
        store.finish_run(conn, run_id, fetched=summary["fetched"], new=summary["new"])

    except httpx.HTTPError as exc:
        # Network trouble is not a markup change. mark_source is deliberately
        # NOT called here — the source's status is left exactly as it was, so
        # a transient timeout can neither trigger a pointless Task 23 repair
        # nor get skipped by the Task 16 activity sweep (which only skips
        # sources already `degraded`).
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        store.finish_run(conn, run_id, error=summary["error"])
    except Exception as exc:  # one bad source must never kill the harvest
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        store.finish_run(conn, run_id, error=summary["error"])

    return summary


def prune_cache(days: int = PRUNE_AFTER_DAYS) -> dict:
    """Delete cached raw fetch files older than `days`. Files only — the
    matching raw_fetches row is left in place.

    That row's value (etag/last_modified for conditional GETs, content_hash
    and http_status for audit) doesn't depend on the archived body still
    being on disk, and job_sources.raw_fetch_id has an FK to this table with
    no ON DELETE clause (RESTRICT) — deleting a row a live posting still
    references would fail outright. A row whose `path` no longer resolves is
    an expected "cache aged out" state, not corruption: any reader of that
    path (e.g. Task 23's repair flow) must treat a missing file as such,
    not assume it exists because the row does.
    """
    cutoff = time.time() - days * 86400
    removed = []
    for path in Path(RAW_DIR).rglob("*.gz"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(str(path))
    return {"command": "prune-cache", "removed": len(removed)}


def run(conn, settings, source_names: list[str] | None = None, dry_run: bool = False) -> dict:
    sources = store.active_sources(conn, ("http-html", "http-json"))
    if source_names:
        sources = [s for s in sources if s["name"] in source_names]

    if dry_run:
        return {
            "command": "harvest", "dry_run": True,
            "would_harvest": [s["name"] for s in sources],
        }

    results = []
    for index, source in enumerate(sources):
        if index:
            time.sleep(DELAY_SECONDS)
        results.append(_harvest_source(conn, settings, source))

    return {
        "command": "harvest",
        "sources": results,
        "totals": {
            "fetched": sum(r["fetched"] for r in results),
            "new": sum(r["new"] for r in results),
            "degraded": [r["source"] for r in results if r["status"] == "broken"],
            "errors": [r["source"] for r in results if r["status"] == "error"],
        },
    }
