from __future__ import annotations

from jobsearch import db
from jobsearch.config import load_settings


def init_db(args) -> dict:
    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        applied = db.migrate(conn)
    finally:
        conn.close()
    return {"command": "init-db", "applied": applied, "count": len(applied)}


def harvest(args) -> dict:
    from jobsearch import harvest as harvest_mod

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return harvest_mod.run(conn, settings, getattr(args, "sources", None), args.dry_run)
    finally:
        conn.close()


def filter_jobs(args) -> dict:
    from jobsearch import filters
    from jobsearch.profile import load_profile

    settings = load_settings(args.env)
    profile = load_profile(args.profile)
    conn = db.connect(settings)
    try:
        return filters.apply(conn, profile)
    finally:
        conn.close()


def sweep(args) -> dict:
    from jobsearch import sweep as sweep_mod

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return sweep_mod.run(conn)
    finally:
        conn.close()


def run_start(args) -> dict:
    from jobsearch import store

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return {"command": "run-start", "run_id": store.start_run(conn, args.kind)}
    finally:
        conn.close()


def run_finish(args) -> dict:
    from jobsearch import store

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        store.finish_run(conn, args.id)
        return {"command": "run-finish", "run_id": args.id}
    finally:
        conn.close()


def queue(args) -> dict:
    from jobsearch import scoring
    from jobsearch.profile import load_profile

    settings = load_settings(args.env)
    profile = load_profile(args.profile)
    conn = db.connect(settings)
    try:
        if args.unscored:
            jobs = scoring.unscored(conn, profile, args.limit)
            mode = "unscored"
        else:
            jobs = scoring.coarse_passed(conn, profile, args.min, args.limit)
            mode = "coarse-passed"
    finally:
        conn.close()
    return {"command": "queue", "mode": mode, "profile_hash": profile.hash,
            "count": len(jobs), "jobs": jobs,
            "weights": profile.weights, "profile": profile.data}


def score(args) -> dict:
    import json as json_mod

    from jobsearch import scoring
    from jobsearch.profile import load_profile

    settings = load_settings(args.env)
    profile = load_profile(args.profile)
    payload = json_mod.loads(args.json)
    conn = db.connect(settings)
    try:
        return scoring.record(conn, args.id, args.run_id, args.pass_no, payload, profile)
    finally:
        conn.close()


def list_jobs(args) -> dict:
    from jobsearch import store

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        jobs = store.list_jobs(
            conn, status=args.status, min_score=args.min_score,
            since=args.since, source=args.source, limit=args.limit,
        )
    finally:
        conn.close()
    return {"command": "list", "count": len(jobs), "jobs": jobs}


def set_status(args) -> dict:
    from jobsearch import store

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        result = store.set_status(conn, args.id, args.status, args.note)
    finally:
        conn.close()
    return {"command": "status", **result}


def sources(args) -> dict:
    from jobsearch import store

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        rows = store.list_sources(conn, degraded=getattr(args, "degraded", False))
    finally:
        conn.close()
    return {"command": "sources", "sources": rows}


def prune_cache(args) -> dict:
    from jobsearch import harvest as harvest_mod

    return harvest_mod.prune_cache()


def ingest(args) -> dict:
    import json as json_mod
    import sys

    from jobsearch import ingest as ingest_mod

    settings = load_settings(args.env)
    payload = json_mod.load(sys.stdin)
    conn = db.connect(settings)
    try:
        return ingest_mod.run(conn, settings, args.source, payload)
    finally:
        conn.close()


def repair_context(args) -> dict:
    from jobsearch import repair

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return repair.context(conn, args.source)
    finally:
        conn.close()


def serve(args) -> dict:
    from jobsearch import server

    server.run(load_settings(args.env), port=args.port,
               open_browser=getattr(args, "open_browser", False))
    return {"command": "serve", "status": "stopped"}
