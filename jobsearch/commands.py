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
