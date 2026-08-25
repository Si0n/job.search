from __future__ import annotations

import json

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "PLN": "zł", "UAH": "₴"}

_JOBS_SQL = """
SELECT j.id, j.title, j.company, j.location, j.arrangement, j.employment_type,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
       j.salary_source, j.salary_monthly_eur, j.first_seen_at, j.canonical_source_id,
       sc.score, sc.red_flag_penalty, sc.dimensions, sc.hard_concerns,
       sc.strengths, sc.weaknesses, sc.verdict,
       a.status
FROM jobs j
LEFT JOIN scores sc      ON sc.id = j.latest_score_id
LEFT JOIN applications a ON a.job_id = j.id
WHERE j.inactive_at IS NULL AND j.filtered_at IS NULL
"""

_POSTINGS_SQL = """
SELECT js.job_id, js.source_id, s.name AS source_name, js.url, js.posted_at,
       js.description
FROM job_sources js
JOIN sources s ON s.id = js.source_id
WHERE js.inactive_at IS NULL
ORDER BY js.job_id, s.priority
"""


def fetch_rows(conn, *, min_score: int | None = None,
               include_triaged: bool = False) -> tuple[list[dict], list[dict]]:
    sql, params = _JOBS_SQL, []
    if min_score is not None:
        # COALESCE, not a bare comparison: sc.score is NULL for the 114 unscored
        # jobs (LEFT JOIN), and `NULL >= n` is never true in SQL. Without this,
        # min_score=0 ("no floor") would silently exclude every unscored job —
        # the opposite of what "no floor" means. Unscored jobs still lose to any
        # real threshold (COALESCE(...,0) >= 7 is false), so the inbox view is
        # unaffected.
        sql += " AND COALESCE(sc.score, 0) >= %s"
        params.append(min_score)
    if not include_triaged:
        sql += " AND a.job_id IS NULL"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        jobs = list(cur.fetchall())
        cur.execute(_POSTINGS_SQL)
        postings = list(cur.fetchall())
    return jobs, postings


def _as_json(value, fallback):
    """MySQL JSON columns arrive as str from some drivers and as parsed objects from
    others. Accept both rather than depending on the driver's mood."""
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def format_salary(row: dict) -> dict:
    monthly = row.get("salary_monthly_eur")
    if row.get("salary_source") != "posting" or not row.get("salary_currency"):
        return {"text": "not stated", "stated": False, "monthly_eur": monthly}

    symbol = SYMBOLS.get(row["salary_currency"], row["salary_currency"] + " ")
    low, high, period = row.get("salary_min"), row.get("salary_max"), row.get("salary_period")

    if low and high and low != high:
        text = f"{symbol}{low}–{high}/{period}"
    elif low and high:
        text = f"{symbol}{low}/{period}"
    elif high:
        text = f"up to {symbol}{high}/{period}"
    elif low:
        text = f"from {symbol}{low}/{period}"
    else:
        return {"text": "not stated", "stated": False, "monthly_eur": monthly}

    return {"text": text, "stated": True, "monthly_eur": monthly}


def _pick_description(job: dict, postings: list[dict]) -> str:
    canonical = job.get("canonical_source_id")
    preferred = [p for p in postings if p["source_id"] == canonical and p.get("description")]
    if preferred:
        return preferred[0]["description"]
    with_text = [p for p in postings if p.get("description")]
    if not with_text:
        return ""
    return max(with_text, key=lambda p: len(p["description"]))["description"]


def build_view(job_rows: list[dict], posting_rows: list[dict]) -> list[dict]:
    """Pure. Rows in, cards out — no database, no formatting decisions left to the page."""
    by_job: dict[int, list[dict]] = {}
    for posting in posting_rows:
        by_job.setdefault(posting["job_id"], []).append(posting)

    cards = []
    for job in job_rows:
        postings = by_job.get(job["id"], [])
        if not postings:
            # Every active job has at least one active posting. If it doesn't, the
            # sweep and the harvest disagree — don't render a card with no link.
            continue

        cards.append({
            "id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "location": job.get("location") or "",
            "arrangement": job.get("arrangement") or "unknown",
            "employment_type": job.get("employment_type") or "unknown",
            "salary": format_salary(job),
            "score": job.get("score"),
            "red_flag_penalty": job.get("red_flag_penalty") or 0,
            "dimensions": _as_json(job.get("dimensions"), {}),
            "hard_concerns": _as_json(job.get("hard_concerns"), []),
            "strengths": _as_json(job.get("strengths"), []),
            "weaknesses": _as_json(job.get("weaknesses"), []),
            "verdict": job.get("verdict") or "",
            "status": job.get("status"),
            "is_new": job.get("status") is None,
            "first_seen_at": str(job.get("first_seen_at") or ""),
            "description": _pick_description(job, postings),
            "sources": [
                {"name": p["source_name"], "url": p["url"],
                 "posted_at": str(p["posted_at"]) if p.get("posted_at") else None}
                for p in postings
            ],
        })

    cards.sort(key=lambda c: (c["score"] is not None, c["score"] or 0), reverse=True)
    return cards
