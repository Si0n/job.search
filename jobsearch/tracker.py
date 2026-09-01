from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

from jobsearch.db import as_json

MAX_NOTE = 4000
MAX_ACTION = 255
MAX_TEXT = 8000
MAX_SHORT = 120
MAX_URL = 1024
MAX_ANSWERS = 20
MAX_LABEL = 64
STAGE_KINDS = ("active", "won", "lost")
ALLOWED_SCHEMES = ("http", "https")

EARLIEST = datetime(2000, 1, 1)
FUTURE_DAYS = 366 * 2

# Stages an application can sit in without anyone having answered. Used only by
# the response-rate metric: a rejection is a response, silence and a withdrawal
# are not.
NO_RESPONSE = frozenset({"applied", "ghosted", "withdrawn"})


def slug_for(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")[:MAX_LABEL]
    if not slug:
        raise ValueError(f"stage label has no usable characters: {label!r}")
    return slug


def _object(raw: str | bytes) -> dict:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("malformed JSON: expected an object")
    return payload


def _text(payload: dict, field: str, limit: int, *, required: bool = False) -> str | None:
    value = payload.get(field)
    if value is None or value == "":
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if len(stripped) > limit:
        raise ValueError(f"{field} too long: {len(stripped)} > {limit}")
    return stripped


def _int(payload: dict, field: str, *, required: bool = True) -> int | None:
    value = payload.get(field)
    if value is None and not required:
        return None
    # bool subclasses int, so {"stage_id": true} would otherwise become stage 1.
    if isinstance(value, bool):
        raise ValueError(f"invalid {field}: {value!r}")
    # float is rejected to avoid silent rounding: 45.7 must not become 45.
    if isinstance(value, float):
        raise ValueError(f"invalid {field}: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {field}: {value!r}") from None


def parse_when(value, field: str, *, now: datetime | None = None) -> datetime | None:
    """ISO-8601 date or date-time, bounded at both ends.

    Future dates are legal and load-bearing: an interview booked for next Friday
    is exactly what next_action_at is for. The ceiling only catches typos.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} is not an ISO-8601 date: {value!r}") from None
    # Normalize tz-aware datetimes to naive local time. The system stores only
    # naive datetimes, and comparing naive with aware datetimes raises TypeError.
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    when = _truncate_micros(when)
    if not EARLIEST <= when <= (now or datetime.now()) + timedelta(days=FUTURE_DAYS):
        raise ValueError(f"{field} out of range: {value!r}")
    return when


def parse_answers(value) -> list[dict] | None:
    """A board's own questions, as free pairs. There is deliberately no schema
    for the questions themselves — a new one costs a row, not a migration."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("answers must be a list")
    if len(value) > MAX_ANSWERS:
        raise ValueError(f"too many answers: {len(value)} > {MAX_ANSWERS}")
    out = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"answers[{index}] must be an object")
        question, answer = item.get("question"), item.get("answer")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"answers[{index}].question is empty")
        if not isinstance(answer, str):
            raise ValueError(f"answers[{index}].answer must be a string")
        if len(question) > 255 or len(answer) > MAX_NOTE:
            raise ValueError(f"answers[{index}] too long")
        out.append({"question": question.strip(), "answer": answer.strip()})
    return out


def parse_stage_request(raw: str | bytes) -> dict:
    payload = _object(raw)
    label = _text(payload, "label", MAX_LABEL, required=True)
    weight = _int(payload, "weight")
    if not 0 <= weight <= 100:
        raise ValueError(f"weight out of range: {weight}")
    kind = payload.get("kind")
    if kind not in STAGE_KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    return {"label": label, "slug": slug_for(label), "weight": weight, "kind": kind}


def parse_transition(raw: str | bytes, *, now: datetime | None = None) -> dict:
    now = _truncate_micros(now or datetime.now())
    payload = _object(raw)
    return {
        "stage_id": _int(payload, "stage_id"),
        "occurred_at": parse_when(payload.get("occurred_at"), "occurred_at", now=now) or now,
        "note": _text(payload, "note", MAX_NOTE),
        "next_action": _text(payload, "next_action", MAX_ACTION),
        "next_action_at": parse_when(payload.get("next_action_at"), "next_action_at", now=now),
    }


def parse_application(raw: str | bytes, *, now: datetime | None = None) -> dict:
    """Validate a create request. Two shapes: an existing job (`job_id`), or a
    manually entered posting (`url` plus the fields the owner corrected after
    the lookup). The url is scheme-checked here for the reason ingest.run gives:
    it is rendered directly as a link, so a `javascript:` url must never reach
    the database rather than merely being neutralised at render time.
    """
    now = _truncate_micros(now or datetime.now())
    payload = _object(raw)
    job_id = _int(payload, "job_id", required=False)
    url = _text(payload, "url", MAX_URL)

    posting = None
    if job_id is None:
        if not url:
            raise ValueError("either job_id or url is required")
        if urlparse(url).scheme.lower() not in ALLOWED_SCHEMES:
            raise ValueError(f"unsupported url scheme: {url!r}")
        posting = {
            "url": url,
            "title": _text(payload, "title", 255, required=True),
            "company": _text(payload, "company", 255, required=True),
            "description": _text(payload, "description", 50_000) or "",
            "location": _text(payload, "location", 255),
            "salary_raw": _text(payload, "salary_raw", 255),
            "posted_at": parse_when(payload.get("posted_at"), "posted_at", now=now),
        }

    return {
        "job_id": job_id,
        "posting": posting,
        "application": {
            "applied_at": parse_when(payload.get("applied_at"), "applied_at", now=now) or now,
            "cv_file_id": _int(payload, "cv_file_id", required=False),
            "cover_letter": _text(payload, "cover_letter", MAX_TEXT),
            "why_company": _text(payload, "why_company", MAX_TEXT),
            "salary_expectation": _text(payload, "salary_expectation", MAX_SHORT),
            "notice_period": _text(payload, "notice_period", MAX_SHORT),
            "answers": parse_answers(payload.get("answers")),
        },
    }


def _when(value) -> datetime:
    """MySQL gives datetimes; JSON fixtures and query strings give ISO strings."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace(" ", "T"))


def _truncate_micros(value: datetime) -> datetime:
    """The DATETIME columns this module writes store whole seconds, and MySQL
    rounds rather than truncates on insert: an untruncated value can be stored
    as a moment that never happened — later than the value itself, which is
    enough to flip a `<=` guard compared against it in Python."""
    return value.replace(microsecond=0)


def buckets(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """The five reporting windows, half-open as [start, end). Weeks start Monday."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    return {
        "previous_day": (today - timedelta(days=1), today),
        "current_week": (week, now),
        "previous_week": (week - timedelta(days=7), week),
        "current_month": (month, now),
        # Step back one day from the 1st to land in the previous month, whatever
        # its length, then take that month's 1st. No calendar arithmetic needed.
        "previous_month": ((month - timedelta(days=1)).replace(day=1), month),
    }


def build_stats(applications: list[dict], events: list[dict], now: datetime) -> dict:
    """Pure. Five metrics per window.

    Events are counted by `occurred_at`, never `created_at`: recording Thursday's
    recruiter call on Monday must move Thursday's number. `response_rate` is the
    exception — it is a cohort measure over the applications SENT in the window,
    evaluated now, so last month's figure keeps rising as replies arrive.
    """
    by_application: dict[int, list[dict]] = {}
    for event in events:
        by_application.setdefault(event["application_id"], []).append(event)

    out = {}
    for name, (start, end) in buckets(now).items():
        sent = [a for a in applications if start <= _when(a["applied_at"]) < end]
        window = [e for e in events if start <= _when(e["occurred_at"]) < end]
        responded = sum(
            1 for a in sent
            if any(e.get("stage_slug") not in NO_RESPONSE
                   for e in by_application.get(a["id"], []))
        )
        out[name] = {
            "start": start,
            "end": end,
            "sent": len(sent),
            "advanced": sum(1 for e in window if e.get("stage_kind") in ("active", "won")),
            "offers": sum(1 for e in window if e.get("stage_kind") == "won"),
            "lost": sum(1 for e in window if e.get("stage_kind") == "lost"),
            "response_rate": round(responded / len(sent), 2) if sent else None,
        }
    return out


def _due(when, now: datetime) -> str | None:
    """Where a next action sits relative to today. Compared by date, not by
    timestamp: an interview at 11:00 is still 'today' when it is 14:30."""
    if when is None:
        return None
    day, today = _when(when).date(), now.date()
    return "overdue" if day < today else "today" if day == today else "later"


def build_list(rows: list[dict], now: datetime) -> list[dict]:
    """Pure. Application rows in, cards out, in the order they should be read.

    The sort is here rather than in an ORDER BY for the same reason
    dashboard.build_view sorts in Python: it is the rule most worth testing, and
    a database is not needed to test it.
    """
    cards = [{
        "id": row["id"],
        "job_id": row["job_id"],
        "title": row["title"],
        "company": row["company"],
        "url": row.get("url"),
        "score": row.get("score"),
        "applied_at": str(row["applied_at"]),
        "stage": {"slug": row["stage_slug"], "label": row["stage_label"],
                  "kind": row["stage_kind"], "weight": row["stage_weight"]},
        "stage_at": str(row["stage_at"]),
        "days_in_stage": (now - _when(row["stage_at"])).days,
        "next_action": ({"text": row.get("next_action"),
                         "at": str(row["next_action_at"]) if row.get("next_action_at") else None,
                         "due": _due(row.get("next_action_at"), now)}
                        if row.get("next_action") or row.get("next_action_at") else None),
        "cv": ({"id": row["cv_file_id"], "filename": row.get("cv_filename")}
               if row.get("cv_file_id") else None),
    } for row in rows]

    cards.sort(key=lambda c: (c["stage"]["kind"] == "lost",
                              -c["stage"]["weight"],
                              -_when(c["stage_at"]).timestamp()))
    return cards


def build_timeline(events: list[dict]) -> list[dict]:
    """Pure. Newest first, by when things happened rather than when they were typed.

    `back_dated` exists so a timeline entered days later reads honestly instead
    of implying it was recorded as it happened.
    """
    items = [{
        "id": event["id"],
        "kind": event["kind"],
        "stage": event.get("stage_label"),
        "occurred_at": str(event["occurred_at"]),
        "created_at": str(event["created_at"]),
        "back_dated": _when(event["created_at"]).date() != _when(event["occurred_at"]).date(),
        "note": event.get("note"),
        "next_action": event.get("next_action"),
        "next_action_at": (str(event["next_action_at"])
                           if event.get("next_action_at") else None),
    } for event in events]
    items.sort(key=lambda i: (_when(i["occurred_at"]).timestamp(), i["id"]), reverse=True)
    return items


def build_detail(row: dict, events: list[dict], now: datetime) -> dict:
    card = build_list([row], now)[0]
    card["texts"] = {
        "cover_letter": row.get("cover_letter"),
        "why_company": row.get("why_company"),
        "salary_expectation": row.get("salary_expectation"),
        "notice_period": row.get("notice_period"),
    }
    card["answers"] = as_json(row.get("answers"), [])
    card["timeline"] = build_timeline(events)
    return card


def build_activity(rows: list[dict]) -> list[dict]:
    """The timeline across every application, each entry naming its job."""
    source = {row["id"]: row for row in rows}
    items = build_timeline(rows)
    for item in items:
        row = source[item["id"]]
        item["application_id"] = row["application_id"]
        item["title"] = row["title"]
        item["company"] = row["company"]
    return items


def stage_by_slug(conn, slug: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM stages WHERE slug = %s", (slug,))
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"unknown stage: {slug}")
    return row


def create_stage(conn, parsed: dict, now: datetime | None = None) -> dict:
    """Add a custom stage. A slug collision is refused rather than reused: two
    stages sharing an identity would silently merge in every list and count."""
    now = _truncate_micros(now or datetime.now())
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM stages WHERE slug = %s", (parsed["slug"],))
        if cur.fetchone():
            raise ValueError(f"a stage named {parsed['label']!r} already exists")
        cur.execute(
            "INSERT INTO stages (slug, label, weight, kind, builtin, created_at) "
            "VALUES (%s, %s, %s, %s, FALSE, %s)",
            (parsed["slug"], parsed["label"], parsed["weight"], parsed["kind"], now),
        )
        stage_id = cur.lastrowid
    conn.commit()
    return {"id": stage_id, **parsed}


def create_application(conn, job_id: int, fields: dict, *, now: datetime | None = None) -> dict:
    """Open an application on a job, at stage `applied`.

    The row and its first event are written together: an application with no
    event would show an empty timeline for something that demonstrably happened.
    """
    now = _truncate_micros(now or datetime.now())
    # Truncated here, not trusted from the caller: fields["applied_at"] lands in
    # three columns below, and a caller that skips parse_application (a script,
    # a future CLI command) would otherwise hand this an untruncated moment.
    applied_at = _truncate_micros(fields["applied_at"])
    applied_stage = stage_by_slug(conn, "applied")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications (job_id, applied_at, stage_id, stage_at, "
                "cv_file_id, cover_letter, why_company, salary_expectation, "
                "notice_period, answers, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (job_id, applied_at, applied_stage["id"], applied_at,
                 fields.get("cv_file_id"), fields.get("cover_letter"),
                 fields.get("why_company"), fields.get("salary_expectation"),
                 fields.get("notice_period"),
                 json.dumps(fields["answers"]) if fields.get("answers") else None,
                 now, now),
            )
            application_id = cur.lastrowid
            cur.execute(
                "INSERT INTO application_events (application_id, kind, stage_id, "
                "occurred_at, created_at) VALUES (%s, 'applied', %s, %s, %s)",
                (application_id, applied_stage["id"], applied_at, now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": application_id, "job_id": job_id, "stage": applied_stage["slug"]}


def transition(conn, application_id: int, parsed: dict, *, now: datetime | None = None) -> dict:
    """Record a stage change, and advance the denormalised current stage with it.

    The UPDATE is guarded by `stage_at <= occurred_at` so that back-filling
    history cannot demote a live application: recording last Tuesday's recruiter
    call on a job already at technical interview appends to the timeline and
    leaves the current stage alone. `current` says which of the two happened.
    """
    now = _truncate_micros(now or datetime.now())
    # Truncated here, not trusted from the caller, for the same reason as
    # create_application: occurred_at feeds the stage_at <= occurred_at guard
    # directly, so an untruncated value reopens the exact hole this fixes.
    occurred_at = _truncate_micros(parsed["occurred_at"])
    next_action_at = (_truncate_micros(parsed["next_action_at"])
                       if parsed["next_action_at"] is not None else None)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM stages WHERE id = %s", (parsed["stage_id"],))
            if cur.fetchone() is None:
                raise LookupError(f"unknown stage id: {parsed['stage_id']}")

            cur.execute(
                "INSERT INTO application_events (application_id, kind, stage_id, "
                "occurred_at, note, next_action, next_action_at, created_at) "
                "VALUES (%s, 'stage', %s, %s, %s, %s, %s, %s)",
                (application_id, parsed["stage_id"], occurred_at, parsed["note"],
                 parsed["next_action"], next_action_at, now),
            )
            event_id = cur.lastrowid

            cur.execute(
                "UPDATE applications SET stage_id=%s, stage_at=%s, next_action=%s, "
                "next_action_at=%s, updated_at=%s WHERE id=%s AND stage_at <= %s",
                (parsed["stage_id"], occurred_at, parsed["next_action"],
                 next_action_at, now, application_id, occurred_at),
            )
            became_current = cur.rowcount == 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"application_id": application_id, "event_id": event_id,
            "stage_id": parsed["stage_id"], "current": became_current}


# No inactive_at or filtered_at conditions anywhere in this module, deliberately:
# an application outlives the posting it came from. The company taking the ad
# down is not a reason to lose the interview scheduled for Friday.
_LIST_SQL = """
SELECT a.id, a.job_id, a.applied_at, a.stage_at, a.next_action, a.next_action_at,
       a.cv_file_id, cf.filename AS cv_filename,
       st.slug AS stage_slug, st.label AS stage_label,
       st.kind AS stage_kind, st.weight AS stage_weight,
       j.title, j.company, sc.score,
       (SELECT js.url FROM job_sources js WHERE js.job_id = j.id
         ORDER BY (js.source_id = j.canonical_source_id) DESC, js.id LIMIT 1) AS url
FROM applications a
JOIN stages st   ON st.id = a.stage_id
JOIN jobs j      ON j.id  = a.job_id
LEFT JOIN cv_files cf ON cf.id = a.cv_file_id
LEFT JOIN scores sc   ON sc.id = j.latest_score_id
"""

_EVENTS_SQL = """
SELECT e.id, e.application_id, e.kind, e.occurred_at, e.created_at, e.note,
       e.next_action, e.next_action_at, st.label AS stage_label
FROM application_events e
LEFT JOIN stages st ON st.id = e.stage_id
WHERE e.application_id = %s
"""

_ACTIVITY_SQL = """
SELECT e.id, e.application_id, e.kind, e.occurred_at, e.created_at, e.note,
       e.next_action, e.next_action_at, st.label AS stage_label,
       j.title, j.company
FROM application_events e
JOIN applications a ON a.id = e.application_id
JOIN jobs j         ON j.id = a.job_id
LEFT JOIN stages st ON st.id = e.stage_id
ORDER BY e.occurred_at DESC, e.id DESC
LIMIT %s
"""


def list_stages(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, slug, label, weight, kind, builtin FROM stages "
                    "ORDER BY FIELD(kind,'active','won','lost'), weight")
        return list(cur.fetchall())


def fetch_list(conn, *, kind: str | None = None) -> list[dict]:
    """Rows for the list view. `kind` is checked against the enum rather than
    interpolated — it arrives from a query string."""
    sql, params = _LIST_SQL, []
    if kind in STAGE_KINDS:
        sql += " WHERE st.kind = %s"
        params.append(kind)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def fetch_detail(conn, application_id: int) -> tuple[dict | None, list[dict]]:
    with conn.cursor() as cur:
        cur.execute(_LIST_SQL + " WHERE a.id = %s", (application_id,))
        row = cur.fetchone()
        if row is None:
            return None, []
        cur.execute(
            "SELECT cover_letter, why_company, salary_expectation, notice_period, answers "
            "FROM applications WHERE id = %s", (application_id,))
        row.update(cur.fetchone())
        cur.execute(_EVENTS_SQL, (application_id,))
        return row, list(cur.fetchall())


def fetch_activity(conn, limit: int = 100) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(_ACTIVITY_SQL, (max(1, min(int(limit), 500)),))
        return list(cur.fetchall())


def fetch_stats_rows(conn) -> tuple[list[dict], list[dict]]:
    """Everything build_stats needs, in two flat reads.

    Whole-table reads because the windows overlap and the volume is hundreds of
    rows; bucketing in SQL would mean five queries per metric to save nothing.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, applied_at FROM applications")
        applications = list(cur.fetchall())
        cur.execute(
            "SELECT e.application_id, e.kind, e.occurred_at, st.slug AS stage_slug, "
            "st.kind AS stage_kind, st.weight AS stage_weight "
            "FROM application_events e LEFT JOIN stages st ON st.id = e.stage_id")
        events = list(cur.fetchall())
    return applications, events
