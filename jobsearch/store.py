from __future__ import annotations

from datetime import datetime

from jobsearch import dedupe, normalize, salary as salary_mod
from jobsearch.models import RawFetch, RawPosting


def start_run(conn, kind: str, source_id: int | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (kind, source_id, started_at) VALUES (%s, %s, %s)",
            (kind, source_id, datetime.now()),
        )
        run_id = cur.lastrowid
    conn.commit()
    return run_id


def set_status(conn, job_id: int, status: str, note: str | None = None) -> dict:
    """Triage a job. One row per job — a re-triage overwrites, it doesn't append.

    Shared by the dashboard's /api/status handler and the `status` CLI command.
    """
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applications (job_id, status, note, updated_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status=VALUES(status), note=VALUES(note), "
            "updated_at=VALUES(updated_at)",
            (job_id, status, note, now),
        )
    conn.commit()
    return {"job_id": job_id, "status": status}


def list_jobs(conn, *, status=None, min_score=None, since=None, source=None,
              limit: int = 50) -> list[dict]:
    """Active, unfiltered jobs. Every filter is optional; all AND together.

    Always excludes inactive/filtered jobs — that's the useful default view,
    not "everything ever seen."

    COALESCE, not a bare `sc.score >= %s`: sc.score is NULL for every
    unscored job (LEFT JOIN scores), and `NULL >= n` is never true in SQL.
    A bare comparison would silently drop every unscored job the moment
    --min-score is used — the same bug dashboard.py's fetch_rows guards
    against for the same reason.
    """
    clauses = ["j.inactive_at IS NULL", "j.filtered_at IS NULL"]
    params: list = []

    if min_score is not None:
        clauses.append("COALESCE(sc.score, 0) >= %s")
        params.append(min_score)
    if status is not None:
        clauses.append("a.status = %s")
        params.append(status)
    if since is not None:
        clauses.append("j.first_seen_at >= %s")
        params.append(since)
    if source is not None:
        clauses.append("s.name = %s")
        params.append(source)

    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.title, j.company, j.location, j.arrangement, "
            "       j.salary_min, j.salary_max, j.salary_currency, j.salary_period, "
            "       sc.score, sc.verdict, sc.dimensions, sc.hard_concerns, "
            "       a.status, a.note, "
            "       (SELECT js2.url FROM job_sources js2 WHERE js2.job_id = j.id "
            "         AND js2.inactive_at IS NULL "
            "         ORDER BY (js2.source_id = j.canonical_source_id) DESC, js2.id "
            "         LIMIT 1) AS url, "
            "       GROUP_CONCAT(DISTINCT s.name) AS sources "
            "FROM jobs j "
            "LEFT JOIN scores sc      ON sc.id = j.latest_score_id "
            "LEFT JOIN applications a ON a.job_id = j.id "
            "JOIN job_sources js      ON js.job_id = j.id AND js.inactive_at IS NULL "
            "JOIN sources s           ON s.id = js.source_id "
            f"WHERE {' AND '.join(clauses)} "
            "GROUP BY j.id ORDER BY sc.score DESC, j.first_seen_at DESC LIMIT %s",
            params,
        )
        return list(cur.fetchall())


def finish_run(conn, run_id: int, *, fetched=None, new=None, error=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET finished_at=%s, fetched=%s, new=%s, error=%s WHERE id=%s",
            (datetime.now(), fetched, new, error, run_id),
        )
    conn.commit()


def active_sources(conn, fetch_modes: tuple[str, ...]) -> list[dict]:
    placeholders = ", ".join(["%s"] * len(fetch_modes))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM sources WHERE enabled = TRUE AND fetch_mode IN ({placeholders}) "
            "ORDER BY priority",
            fetch_modes,
        )
        return list(cur.fetchall())


def source_by_name(conn, name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM sources WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"unknown source: {name}")
    return row


def list_sources(conn, *, degraded: bool = False) -> list[dict]:
    sql = "SELECT name, enabled, fetch_mode, status, consecutive_empty, last_ok_at FROM sources"
    if degraded:
        sql += " WHERE status = 'degraded'"
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def last_fetch(conn, source_id: int) -> dict | None:
    """Most recent raw_fetches row for a source, used for conditional GETs.

    The row's `path` may point at a file harvest.prune_cache() has since
    deleted (files older than 7 days are pruned; the row is kept — see that
    function's docstring). A caller that opens `path` must handle it being
    gone; the row's existence is not a guarantee the file still is.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM raw_fetches WHERE source_id=%s ORDER BY fetched_at DESC LIMIT 1",
            (source_id,),
        )
        return cur.fetchone()


def record_fetch(conn, source_id: int, run_id: int, raw: RawFetch, path: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_fetches "
            "(source_id, run_id, path, content_hash, http_status, etag, last_modified, fetched_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (source_id, run_id, path, raw.content_hash, raw.http_status,
             raw.etag, raw.last_modified, raw.fetched_at),
        )
        fetch_id = cur.lastrowid
    conn.commit()
    return fetch_id


def _salary_specificity(row: dict) -> int:
    if row.get("salary_source") != "posting":
        return 0
    has_min, has_max = row.get("salary_min") is not None, row.get("salary_max") is not None
    if has_min and has_max:
        return 3 if row["salary_min"] != row["salary_max"] else 2
    return 1 if (has_min or has_max) else 0


def _find_merge_target(conn, fingerprint: str, source_id: int, now: datetime,
                        description: str) -> int | None:
    """Return a canonical job id this posting may join, or None to create a new one."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT js.job_id, js.source_id, js.last_seen_at, js.description "
            "FROM job_sources js JOIN jobs j ON j.id = js.job_id "
            "WHERE j.fingerprint = %s ORDER BY js.last_seen_at DESC",
            (fingerprint,),
        )
        candidates = list(cur.fetchall())

    for candidate in candidates:
        ok, _reason = dedupe.can_merge(
            existing_source_id=candidate["source_id"],
            candidate_source_id=source_id,
            existing_last_seen=candidate["last_seen_at"],
            candidate_last_seen=now,
            existing_description=candidate["description"],
            candidate_description=description,
        )
        if ok:
            return candidate["job_id"]
    return None


def upsert_posting(conn, source: dict, posting: RawPosting, raw_fetch_id: int | None,
                    rates: dict, now: datetime) -> tuple[int, bool]:
    # Multiple statements run before the final commit (lookup, then either
    # one-to-two updates or an insert pair, then _refresh_canonical's writes).
    # Any failure partway through must roll back rather than leave this call's
    # statements uncommitted in a connection a caller may reuse afterward.
    try:
        description = normalize.description(posting.description)
        desc_hash = normalize.description_hash(description)
        arrangement = normalize.arrangement(posting.arrangement_hint, description)
        employment = normalize.employment(posting.employment_hint, description)
        pay = salary_mod.parse(posting.salary_raw)
        monthly_eur = salary_mod.to_monthly_eur(pay, rates)
        fingerprint = dedupe.fingerprint(
            posting.company, posting.title, posting.location or "", employment, arrangement
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, job_id, description_hash FROM job_sources "
                "WHERE source_id=%s AND external_id=%s",
                (source["id"], posting.external_id),
            )
            existing = cur.fetchone()

        if existing:
            with conn.cursor() as cur:
                if existing["description_hash"] != desc_hash:
                    cur.execute(
                        "UPDATE job_sources SET description=%s, description_hash=%s, "
                        "salary_raw=%s, url=%s, raw_fetch_id=%s, last_seen_at=%s, "
                        "missed_runs=0, inactive_at=NULL WHERE id=%s",
                        (description, desc_hash, posting.salary_raw, posting.url,
                         raw_fetch_id, now, existing["id"]),
                    )
                else:
                    cur.execute(
                        "UPDATE job_sources SET last_seen_at=%s, missed_runs=0, "
                        "inactive_at=NULL WHERE id=%s",
                        (now, existing["id"]),
                    )
            job_id = existing["job_id"]
            is_new = False
        else:
            job_id = _find_merge_target(conn, fingerprint, source["id"], now, description)
            if job_id is None:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO jobs (fingerprint, canonical_source_id, title, company, "
                        "location, arrangement, employment_type, salary_min, salary_max, "
                        "salary_currency, salary_period, salary_type, salary_source, "
                        "salary_monthly_eur, first_seen_at, last_seen_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (fingerprint, source["id"], posting.title, posting.company,
                         posting.location, arrangement, employment, pay.min, pay.max,
                         pay.currency, pay.period, pay.type, pay.source, monthly_eur, now, now),
                    )
                    job_id = cur.lastrowid
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job_sources (job_id, source_id, external_id, url, raw_fetch_id, "
                    "description, description_hash, salary_raw, posted_at, first_seen_at, "
                    "last_seen_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (job_id, source["id"], posting.external_id, posting.url, raw_fetch_id,
                     description, desc_hash, posting.salary_raw, posting.posted_at, now, now),
                )
            is_new = True

        _refresh_canonical(conn, job_id, source, posting, arrangement, employment,
                            pay, monthly_eur, now)
        conn.commit()
        return job_id, is_new
    except Exception:
        conn.rollback()
        raise


def _refresh_canonical(conn, job_id: int, source: dict, posting: RawPosting,
                        arrangement: str, employment: str, pay, monthly_eur, now) -> None:
    """Merge this posting's claims into the canonical row.

    Identity fields follow source priority (lower number wins). Salary ignores
    priority and follows specificity, because a source that publishes a range is
    more useful than a higher-priority source that publishes nothing.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))
        job = cur.fetchone()

        cur.execute("SELECT priority FROM sources WHERE id=%s", (job["canonical_source_id"],))
        owner = cur.fetchone()
        owner_priority = owner["priority"] if owner else 999

        if source["priority"] <= owner_priority:
            cur.execute(
                "UPDATE jobs SET canonical_source_id=%s, title=%s, company=%s, location=%s, "
                "arrangement=%s, employment_type=%s WHERE id=%s",
                (source["id"], posting.title, posting.company, posting.location,
                 arrangement, employment, job_id),
            )

        incoming = {
            "salary_source": pay.source,
            "salary_min": pay.min,
            "salary_max": pay.max,
        }
        if _salary_specificity(incoming) > _salary_specificity(job):
            cur.execute(
                "UPDATE jobs SET salary_min=%s, salary_max=%s, salary_currency=%s, "
                "salary_period=%s, salary_type=%s, salary_source=%s, salary_monthly_eur=%s "
                "WHERE id=%s",
                (pay.min, pay.max, pay.currency, pay.period, pay.type, pay.source,
                 monthly_eur, job_id),
            )

        cur.execute(
            "UPDATE jobs SET last_seen_at = GREATEST(last_seen_at, %s), inactive_at = NULL "
            "WHERE id=%s",
            (now, job_id),
        )


def mark_source(conn, source_id: int, status: str, saw_items: bool, now: datetime) -> None:
    """Record the outcome of a full-inventory read: a harvest that actually parsed
    a listing page. `last_ok_at` is the sweep's contract that every still-active
    posting was reconfirmed — only call this when that's true. A 304 or an
    `ingest` batch is never a full-inventory read; use mark_source_ran for those.
    """
    with conn.cursor() as cur:
        if status == "ok":
            cur.execute(
                "UPDATE sources SET status='ok', consecutive_empty=0, last_ok_at=%s, "
                "last_run_at=%s WHERE id=%s",
                (now, now, source_id),
            )
        else:
            cur.execute(
                "UPDATE sources SET status=%s, consecutive_empty=consecutive_empty+%s, "
                "last_run_at=%s WHERE id=%s",
                (status, 0 if saw_items else 1, now, source_id),
            )
    conn.commit()


def mark_source_ran(conn, source_id: int, now: datetime) -> None:
    """Record that a run touched this source without asserting anything about its
    inventory. Two callers need exactly this: a 304 (the site confirmed nothing
    byte-different since last time, so nothing can have disappeared either) and
    `ingest` (never a full-inventory read of a board, so a partial batch must not
    vouch for postings it didn't include).

    Deliberately never touches `status` or `last_ok_at` — advancing `last_ok_at`
    is what tells sweep's aging predicate every active posting was reconfirmed,
    which neither caller can claim.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE sources SET last_run_at=%s WHERE id=%s", (now, source_id))
    conn.commit()
