from __future__ import annotations

import json
from datetime import datetime

BLOCKS = ("cover_letter", "email", "why_fit")
MAX_CHARS = 8000
MAX_NOTE = 1000

_CONTEXT_SQL = """
SELECT j.id, j.title, j.company, j.location, j.arrangement, j.employment_type,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period, j.salary_source,
       sc.score, sc.dimensions, sc.dimension_notes, sc.hard_concerns,
       sc.strengths, sc.weaknesses, sc.verdict,
       d.note AS pending_note, d.applied_note,
       (SELECT js2.description FROM job_sources js2 WHERE js2.job_id = j.id
         AND js2.inactive_at IS NULL
         ORDER BY (js2.source_id = j.canonical_source_id) DESC,
                  CHAR_LENGTH(js2.description) DESC LIMIT 1) AS description,
       (SELECT js3.url FROM job_sources js3 WHERE js3.job_id = j.id
         AND js3.inactive_at IS NULL
         ORDER BY (js3.source_id = j.canonical_source_id) DESC, js3.id LIMIT 1) AS url,
       (SELECT GROUP_CONCAT(DISTINCT s.name) FROM job_sources js4
         JOIN sources s ON s.id = js4.source_id WHERE js4.job_id = j.id) AS sources
FROM jobs j
LEFT JOIN scores sc ON sc.id = j.latest_score_id
LEFT JOIN drafts d  ON d.job_id = j.id
WHERE j.id = %s
"""


def parse_draft(raw: str | bytes) -> dict:
    """Validate a draft before it reaches the database.

    Every block is required. A partial draft would render as a card with one
    empty section, which reads as "written and had nothing to say" rather than
    "not written" — the same failure the scoring payload guards against.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("malformed JSON: expected an object")

    unexpected = sorted(set(payload) - set(BLOCKS))
    if unexpected:
        raise ValueError(f"unexpected key(s): {', '.join(unexpected)}")

    out = {}
    for block in BLOCKS:
        value = payload.get(block)
        if not isinstance(value, str):
            raise ValueError(f"{block} must be a string, got {type(value).__name__}")
        if not value.strip():
            raise ValueError(f"{block} is empty")
        if len(value) > MAX_CHARS:
            raise ValueError(f"{block} too long: {len(value)} > {MAX_CHARS}")
        out[block] = value
    return out


def parse_note(raw: str | bytes) -> tuple[int, str | None]:
    """Validate a rewrite instruction from the dashboard.

    Returns `(job_id, note)` with note None when the field is blank — clearing a
    stale instruction has to be possible, and storing whitespace would leave the
    next /draft run following an empty order.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("malformed JSON: expected an object")

    raw_id = payload.get("id")
    if isinstance(raw_id, bool):
        raise ValueError(f"invalid job id: {raw_id!r}")
    try:
        job_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError(f"invalid job id: {raw_id!r}") from None

    note = payload.get("note")
    if note is None:
        return job_id, None
    if not isinstance(note, str):
        raise ValueError(f"note must be a string, got {type(note).__name__}")
    if len(note) > MAX_NOTE:
        raise ValueError(f"note too long: {len(note)} > {MAX_NOTE}")
    return job_id, note.strip() or None


def set_note(conn, job_id: int, note: str | None, profile_hash: str) -> dict:
    """A note can be left on a job that has no draft yet — that is the ordinary
    case for a first request, so this inserts a row rather than requiring one."""
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO drafts (job_id, note, profile_hash, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE note=VALUES(note), updated_at=VALUES(updated_at)",
            (job_id, note, profile_hash, now, now),
        )
    conn.commit()
    return {"command": "draft-note", "job_id": job_id, "note": note}


def context(conn, job_id: int) -> dict:
    """Everything the drafting skill needs about one job, in a single call."""
    with conn.cursor() as cur:
        cur.execute(_CONTEXT_SQL, (job_id,))
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no job with id {job_id}")
    for field in ("dimensions", "dimension_notes", "hard_concerns", "strengths", "weaknesses"):
        raw = row.get(field)
        if isinstance(raw, str):
            try:
                row[field] = json.loads(raw)
            except ValueError:
                pass
    return {"command": "draft-context", "job": row}


def save(conn, job_id: int, blocks: dict, profile_hash: str) -> dict:
    """Storing a draft consumes any pending note: it moves to `applied_note` so
    the text can explain its own shape later, and `note` clears so the next run
    does not silently re-apply an instruction already followed."""
    now = datetime.now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO drafts (job_id, cover_letter, email, why_fit, profile_hash, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE cover_letter=VALUES(cover_letter), "
            "email=VALUES(email), why_fit=VALUES(why_fit), "
            "applied_note=note, note=NULL, "
            "profile_hash=VALUES(profile_hash), updated_at=VALUES(updated_at)",
            (job_id, blocks["cover_letter"], blocks["email"], blocks["why_fit"],
             profile_hash, now, now),
        )
    conn.commit()
    return {"command": "draft", "job_id": job_id, "blocks": sorted(blocks)}
