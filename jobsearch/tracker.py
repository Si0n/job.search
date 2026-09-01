from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

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
    now = now or datetime.now()
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
    now = now or datetime.now()
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
