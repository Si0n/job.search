from __future__ import annotations

from datetime import datetime

MISSED_RUNS_BEFORE_INACTIVE = 3


def run(conn) -> dict:
    """Age out postings that stopped appearing, then deactivate jobs whose
    every posting is gone.

    Degraded sources are excluded entirely, at every step. A broken parser
    returns zero postings, which is indistinguishable from every job being
    pulled at once — sweeping a degraded source would mark its whole inventory
    dead on the exact day it needed repair. That applies to aging missed_runs
    up AND to finalizing a deactivation off a missed_runs count accumulated
    from before the source degraded.
    """
    now = datetime.now()

    with conn.cursor() as cur:
        cur.execute("SELECT name FROM sources WHERE status = 'degraded'")
        skipped = [row["name"] for row in cur.fetchall()]

        cur.execute(
            "UPDATE job_sources js JOIN sources s ON s.id = js.source_id "
            "SET js.missed_runs = js.missed_runs + 1 "
            "WHERE s.status = 'ok' AND s.enabled = TRUE AND js.inactive_at IS NULL "
            "AND js.last_seen_at < s.last_ok_at"
        )
        aged = cur.rowcount

        cur.execute(
            "UPDATE job_sources js JOIN sources s ON s.id = js.source_id "
            "SET js.inactive_at = %s "
            "WHERE s.status = 'ok' AND s.enabled = TRUE "
            "AND js.inactive_at IS NULL AND js.missed_runs >= %s",
            (now, MISSED_RUNS_BEFORE_INACTIVE),
        )
        deactivated_postings = cur.rowcount

        cur.execute(
            "UPDATE jobs j SET j.inactive_at = %s WHERE j.inactive_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM job_sources js "
            "                WHERE js.job_id = j.id AND js.inactive_at IS NULL)",
            (now,),
        )
        deactivated_jobs = cur.rowcount
    conn.commit()

    return {
        "command": "sweep",
        "skipped_degraded_sources": skipped,
        "postings_aged": aged,
        "postings_deactivated": deactivated_postings,
        "jobs_deactivated": deactivated_jobs,
    }
