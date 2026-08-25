from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

from jobsearch.config import Settings


def connect(settings: Settings) -> pymysql.Connection:
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
    )


def _applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'schema_migrations'"
        )
        if cur.fetchone()["n"] == 0:
            return set()
        cur.execute("SELECT filename FROM schema_migrations")
        return {row["filename"] for row in cur.fetchall()}


def migrate(conn, migrations_dir: str = "migrations") -> list[str]:
    """Apply every unapplied .sql file in filename order. Each file is one transaction."""
    done = _applied(conn)
    applied: list[str] = []

    for path in sorted(Path(migrations_dir).glob("*.sql")):
        if path.name in done:
            continue
        statements = [s.strip() for s in path.read_text(encoding="utf-8").split(";") if s.strip()]
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            cur.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (%s, %s)",
                (path.name, datetime.now()),
            )
        conn.commit()
        applied.append(path.name)

    return applied
