from __future__ import annotations

import json
from datetime import datetime

COARSE_CHARS = 800

_BASE_SELECT = """
SELECT j.id, j.title, j.company, j.location, j.arrangement, j.employment_type,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
       j.salary_source, j.salary_monthly_eur,
       GROUP_CONCAT(DISTINCT s.name)  AS sources,
       (SELECT js2.url FROM job_sources js2 WHERE js2.job_id = j.id AND js2.inactive_at IS NULL
         ORDER BY (js2.source_id = j.canonical_source_id) DESC, js2.id LIMIT 1) AS url,
       (SELECT js3.description FROM job_sources js3 WHERE js3.job_id = j.id AND js3.inactive_at IS NULL
         ORDER BY (js3.source_id = j.canonical_source_id) DESC,
                  CHAR_LENGTH(js3.description) DESC LIMIT 1) AS description
FROM jobs j
JOIN job_sources js ON js.job_id = j.id
JOIN sources s      ON s.id = js.source_id
WHERE j.inactive_at IS NULL AND j.filtered_at IS NULL
"""

_NO_PASS = """
  AND NOT EXISTS (
    SELECT 1 FROM scores sc
    WHERE sc.job_id = j.id AND sc.`pass` = %s AND sc.profile_hash = %s
  )
"""


def _shape(rows: list[dict], truncate: int | None) -> list[dict]:
    shaped = []
    for row in rows:
        description = row.get("description") or ""
        shaped.append({
            **row,
            "description": description[:truncate] if truncate else description,
            "description_truncated": bool(truncate and len(description) > truncate),
        })
    return shaped


def unscored(conn, profile, minimum: int = 6, limit: int = 60) -> list[dict]:
    """Jobs still awaiting a coarse pass under the current profile hash.

    Keying on the hash means a profile edit re-queues everything automatically,
    and a run that dies between passes resumes rather than stranding postings —
    a job that got a passing pass-1 score but no pass 2 must come back.

    A job REJECTED at pass 1 must not. Testing only for a missing pass-2 row
    cannot tell the two apart, so every triaged-and-discarded job returned in
    every later queue: 44 of one 60-job batch were reruns of coarse scores
    already recorded, crowding out jobs that had never been looked at.
    """
    with conn.cursor() as cur:
        cur.execute(
            _BASE_SELECT + _NO_PASS +
            """
              AND NOT EXISTS (
                SELECT 1 FROM scores sc1
                WHERE sc1.job_id = j.id AND sc1.`pass` = 1
                  AND sc1.profile_hash = %s AND sc1.score < %s
              )
            """ +
            " GROUP BY j.id ORDER BY j.first_seen_at DESC LIMIT %s",
            (2, profile.hash, profile.hash, minimum, limit),
        )
        return _shape(list(cur.fetchall()), COARSE_CHARS)


def coarse_passed(conn, profile, minimum: int = 6, limit: int = 60) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            _BASE_SELECT + _NO_PASS +
            """
              AND EXISTS (
                SELECT 1 FROM scores sc1
                WHERE sc1.job_id = j.id AND sc1.`pass` = 1
                  AND sc1.profile_hash = %s AND sc1.score >= %s
              )
            GROUP BY j.id ORDER BY j.first_seen_at DESC LIMIT %s
            """,
            (2, profile.hash, profile.hash, minimum, limit),
        )
        return _shape(list(cur.fetchall()), None)


def record(conn, job_id: int, run_id: int, pass_no: int, payload: dict, profile) -> dict:
    if pass_no == 1:
        stored = {
            "score": int(payload["score"]), "dimensions": None, "dimension_notes": None,
            "red_flag_penalty": 0,
            "hard_concerns": None, "strengths": None, "weaknesses": None, "verdict": None,
        }
    else:
        stored = {
            "score": int(payload["score"]),
            "dimensions": json.dumps(payload.get("dimensions") or {}),
            "dimension_notes": json.dumps(payload["dimension_notes"]) if payload.get("dimension_notes") else None,
            "red_flag_penalty": int(payload.get("red_flag_penalty", 0)),
            "hard_concerns": json.dumps(payload.get("hard_concerns") or []),
            "strengths": json.dumps(payload.get("strengths") or []),
            "weaknesses": json.dumps(payload.get("weaknesses") or []),
            "verdict": payload.get("verdict"),
        }

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scores (job_id, run_id, `pass`, score, dimensions, dimension_notes, "
            "red_flag_penalty, "
            "hard_concerns, strengths, weaknesses, verdict, profile_version, profile_hash, "
            "scored_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, run_id, pass_no, stored["score"], stored["dimensions"],
             stored["dimension_notes"],
             stored["red_flag_penalty"], stored["hard_concerns"], stored["strengths"],
             stored["weaknesses"], stored["verdict"], profile.version, profile.hash,
             datetime.now()),
        )
        score_id = cur.lastrowid
        if pass_no == 2:
            cur.execute("UPDATE jobs SET latest_score_id=%s WHERE id=%s", (score_id, job_id))
    conn.commit()

    return {"command": "score", "job_id": job_id, "score_id": score_id,
            "pass": pass_no, "score": stored["score"]}
