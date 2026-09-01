# Application Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every job application — its stage history, the CV and text submitted with it, and the statistics derived from both — in the existing jobsearch dashboard.

**Architecture:** Four new tables (`stages`, `applications`, `application_events`, `cv_files`) beside the existing pipeline, with the old `applications` triage table renamed to `triage`. Business logic lives in pure functions that take rows and return payloads (the pattern `dashboard.fetch_rows` + `dashboard.build_view` already establishes); SQL lives in thin fetchers beside them. A second static page consumes a JSON API served by the same `http.server` handler.

**Tech Stack:** Python 3.13 stdlib `http.server`, PyMySQL, httpx, BeautifulSoup + lxml, pytest, vanilla JS. No framework, no ORM, no build step.

**Spec:** `docs/superpowers/specs/2026-09-01-application-tracker-design.md`

## Global Constraints

- Python `>=3.13`. `cgi` does not exist — never reach for it. Uploads are base64 in JSON.
- No new dependencies. Everything here uses what `pyproject.toml` already lists.
- **Tests are pure.** This repo has no test database and none is being added. Test pure functions with dict fixtures, as `tests/test_dashboard.py` does. SQL functions are verified by running commands against the dev database and reading the JSON, in an explicit verification step.
- Every write endpoint validates before touching SQL, mirroring `server.parse_status_request` and `drafts.parse_draft`: whitelist or type-check each field, and reject rather than coerce.
- Comments explain *why*, never *what*. Match the density of the surrounding file.
- MySQL 8: `rank` is a reserved word — the sort column is `weight`.
- Migrations are re-runnable: `CREATE TABLE IF NOT EXISTS`, `INSERT IGNORE`, and guarded DDL for anything else (`db.migrate` records a migration only after it fully applies, so a mid-file failure re-runs the whole file).
- Money, dates and text limits: `note` ≤ 4000, `next_action` ≤ 255, `cover_letter` / `why_company` ≤ 8000, `answers` ≤ 20 pairs, stage `label` ≤ 64, `weight` 0–100, dates ISO-8601 between 2000-01-01 and two years ahead.
- `var/` is gitignored: uploaded CVs never enter git.

## Where this plan departs from the spec

Four places, each deliberate:

1. **Ordering happens in Python, not in an `ORDER BY`.** The spec shows the sort as SQL. `dashboard.build_view` already sorts in Python, and it makes the one rule most likely to be got wrong testable without a database. The order itself is unchanged.
2. **The spec's `tests/test_server.py` additions are verification steps, not pytest cases.** This repo's server tests cover pure parsers only; there is no HTTP test harness, and adding one is scaffolding the spec did not ask for. Route dispatch, the content-type guard, magic-byte rejection and the download headers are all still verified — by `curl`, in Tasks 10 and 12, with exact expected status codes.
3. **`/api/cvs` is an extra route**, absent from the spec's table: the add-application form needs a picker of already-uploaded CVs, and re-uploading the same file to list it would be absurd.
4. **Ashby has no single-posting endpoint.** That branch fetches the employer's board and selects the requested id, where Greenhouse and Lever each fetch one posting.

## File Structure

**Created**

| file | responsibility |
|---|---|
| `migrations/016_add_application_tracker.sql` | rename, four tables, stage seeds, the `manual` source |
| `jobsearch/tracker.py` | request parsers, stage vocabulary, statistics buckets, list/detail/activity builders, and the SQL that feeds them |
| `jobsearch/cv.py` | magic-byte sniffing, content-addressed storage, retrieval |
| `jobsearch/manual.py` | `safe_fetch_url` plus the extraction chain (ATS API → JSON-LD → `og:`/`<title>`) |
| `jobsearch/static/app.css` | theme tokens and shared component styles for both pages |
| `jobsearch/static/util.js` | `esc`, `age`, `safeUrl`, `postJSON` — shared by both pages |
| `jobsearch/static/applications.html` | the tracker page |
| `tests/test_tracker.py` | parsers, slugs, buckets, statistics, list ordering |
| `tests/test_cv.py` | sniffing, hashing, storage |
| `tests/test_manual.py` | SSRF guard, extraction chain |
| `tests/fixtures/manual/*` | one JSON-LD page, one bare page, one Greenhouse API payload |

**Modified**

| file | change |
|---|---|
| `jobsearch/models.py` | `APPLICATION_STATUSES` → `TRIAGE_STATUSES` |
| `jobsearch/store.py` | `set_status` and `list_jobs` point at `triage` |
| `jobsearch/dashboard.py` | `_JOBS_SQL`, `fetch_rows`, `_TODAY_STATS_SQL` point at `triage` |
| `jobsearch/filters.py` | `apply` skips jobs that have an application |
| `jobsearch/cli.py` | import rename; `apply` and `applications` commands |
| `jobsearch/commands.py` | the two new commands |
| `jobsearch/server.py` | route table, then thirteen handlers |
| `jobsearch/static/index.html` | shared CSS/JS extracted out, cross-link added |
| `docs/superpowers/specs/2026-08-25-job-search-design.md` | the `applications` section becomes `triage` |

`.claude/skills/review/SKILL.md` needs **no** change: it drives `jobsearch status` and `jobsearch list --status`, whose names and vocabulary are unchanged by the rename.

---

### Task 1: Rename triage, create the schema

**Files:**
- Create: `migrations/016_add_application_tracker.sql`
- Modify: `jobsearch/models.py`, `jobsearch/store.py:24-40`, `jobsearch/store.py:43-95`, `jobsearch/dashboard.py:38-52`, `jobsearch/dashboard.py:100-118`, `jobsearch/dashboard.py:135-152`, `jobsearch/cli.py:7`, `jobsearch/server.py:14`, `docs/superpowers/specs/2026-08-25-job-search-design.md:295-305`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `triage`, `stages`, `applications`, `application_events`, `cv_files`; the `manual` source row; the constant `models.TRIAGE_STATUSES`.

- [ ] **Step 1: Write the migration**

Create `migrations/016_add_application_tracker.sql`:

```sql
-- The old `applications` table has always been triage: one row per job, no
-- history, and the inbox is defined by the absence of a row. The tracker needs
-- that name for the thing that actually tracks applications, so triage takes
-- the name it means. Guarded rather than a bare RENAME because db.migrate
-- re-runs a whole file after a partial failure.
SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.tables
    WHERE table_schema = DATABASE() AND table_name = 'triage') = 0,
  'RENAME TABLE applications TO triage', 'DO 0')
;
PREPARE stmt FROM @sql
;
EXECUTE stmt
;
DEALLOCATE PREPARE stmt
;

CREATE TABLE IF NOT EXISTS stages (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  slug       VARCHAR(64) NOT NULL UNIQUE,
  label      VARCHAR(64) NOT NULL,
  weight     SMALLINT    NOT NULL,
  kind       ENUM('active','won','lost') NOT NULL DEFAULT 'active',
  builtin    BOOLEAN     NOT NULL DEFAULT FALSE,
  created_at DATETIME    NOT NULL,
  INDEX idx_stages_sort (kind, weight)
) ENGINE=InnoDB
;

INSERT IGNORE INTO stages (slug, label, weight, kind, builtin, created_at) VALUES
  ('applied',          'Applied',                     10, 'active', TRUE, NOW()),
  ('recruiter_screen', 'Recruiter screen',            20, 'active', TRUE, NOW()),
  ('waiting_feedback', 'Waiting for feedback',        25, 'active', TRUE, NOW()),
  ('test_task',        'Test task / take-home',       30, 'active', TRUE, NOW()),
  ('tech_interview',   'Technical interview',         40, 'active', TRUE, NOW()),
  ('team_interview',   'Team interview',              50, 'active', TRUE, NOW()),
  ('cto_interview',    'Interview with CTO / founder',60, 'active', TRUE, NOW()),
  ('final_interview',  'Final / culture interview',   70, 'active', TRUE, NOW()),
  ('reference_check',  'Reference check',             80, 'active', TRUE, NOW()),
  ('offer',            'Offer',                       90, 'won',    TRUE, NOW()),
  ('accepted',         'Accepted',                   100, 'won',    TRUE, NOW()),
  ('declined',         'Declined by them',             0, 'lost',   TRUE, NOW()),
  ('withdrawn',        'Withdrawn by me',              0, 'lost',   TRUE, NOW()),
  ('ghosted',          'Ghosted / no answer',          0, 'lost',   TRUE, NOW())
;

CREATE TABLE IF NOT EXISTS cv_files (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  sha256       CHAR(64)     NOT NULL UNIQUE,
  filename     VARCHAR(255) NOT NULL,
  content_type VARCHAR(100) NOT NULL,
  size_bytes   INT          NOT NULL,
  path         VARCHAR(512) NOT NULL,
  uploaded_at  DATETIME     NOT NULL
) ENGINE=InnoDB
;

CREATE TABLE IF NOT EXISTS applications (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  job_id             INT NOT NULL,
  applied_at         DATETIME NOT NULL,
  stage_id           INT NOT NULL,
  stage_at           DATETIME NOT NULL,
  next_action        VARCHAR(255) NULL,
  next_action_at     DATETIME NULL,
  cv_file_id         INT NULL,
  cover_letter       TEXT NULL,
  why_company        TEXT NULL,
  salary_expectation VARCHAR(120) NULL,
  notice_period      VARCHAR(120) NULL,
  answers            JSON NULL,
  created_at         DATETIME NOT NULL,
  updated_at         DATETIME NOT NULL,
  UNIQUE KEY uq_application_job (job_id),
  CONSTRAINT fk_application_job   FOREIGN KEY (job_id)     REFERENCES jobs(id),
  CONSTRAINT fk_application_stage FOREIGN KEY (stage_id)   REFERENCES stages(id),
  CONSTRAINT fk_application_cv    FOREIGN KEY (cv_file_id) REFERENCES cv_files(id),
  INDEX idx_application_next (next_action_at)
) ENGINE=InnoDB
;

CREATE TABLE IF NOT EXISTS application_events (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  application_id INT NOT NULL,
  kind           ENUM('applied','stage','note') NOT NULL,
  stage_id       INT NULL,
  occurred_at    DATETIME NOT NULL,
  note           TEXT NULL,
  next_action    VARCHAR(255) NULL,
  next_action_at DATETIME NULL,
  created_at     DATETIME NOT NULL,
  CONSTRAINT fk_event_application FOREIGN KEY (application_id) REFERENCES applications(id),
  CONSTRAINT fk_event_stage       FOREIGN KEY (stage_id)       REFERENCES stages(id),
  INDEX idx_event_app (application_id, occurred_at)
) ENGINE=InnoDB
;

-- enabled = FALSE is load-bearing twice over: harvest never tries to crawl a
-- source with no listing page, and sweep's two UPDATEs both require
-- s.enabled = TRUE, so a posting the owner has applied to is never aged out
-- when the company takes the ad down. Priority 90 keeps a real board's title
-- and company winning over a hand-pasted page when the two merge.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('manual', FALSE, 'http-html', 90, 'https://example.invalid')
;
```

- [ ] **Step 2: Point the code at `triage`**

In `jobsearch/models.py`, rename the constant and its comment:

```python
# Single source of truth for triage.status — mirrors the SQL ENUM in
# migrations/001_init.sql (renamed to `triage` in 016). cli.py and server.py
# both validate against this instead of each keeping their own copy.
TRIAGE_STATUSES = (
    "interested", "skipped", "applied", "replied",
    "rejected", "interviewing", "offer",
)
```

In `jobsearch/store.py`, `set_status` writes to `triage`:

```python
        cur.execute(
            "INSERT INTO triage (job_id, status, note, updated_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status=VALUES(status), note=VALUES(note), "
            "updated_at=VALUES(updated_at)",
            (job_id, status, note, now),
        )
```

and `list_jobs` joins it: `"LEFT JOIN triage a          ON a.job_id = j.id "`.

In `jobsearch/dashboard.py`: `_JOBS_SQL` becomes `LEFT JOIN triage a       ON a.job_id = j.id`, and in `_TODAY_STATS_SQL` the `triaged` entry becomes `"SELECT COUNT(*) FROM triage WHERE DATE(updated_at) = CURDATE()"`. `fetch_rows`'s `a.job_id IS NULL` and `a.status = %s` clauses are unchanged — the alias still resolves.

In `jobsearch/cli.py` and `jobsearch/server.py`, update the import and its uses:

```python
from jobsearch.models import TRIAGE_STATUSES
```

`server.VALID_STATUSES = set(TRIAGE_STATUSES)` keeps its name, so `tests/test_server.py` needs no change.

- [ ] **Step 3: Run the existing suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, same count as before the change. If anything fails, a call site was missed — grep for `applications` in `jobsearch/`.

- [ ] **Step 4: Apply the migration**

Run: `.venv/bin/jobsearch init-db`
Expected: JSON listing `016_add_application_tracker.sql` as applied.

- [ ] **Step 5: Verify the rename did not break reads**

```bash
.venv/bin/jobsearch list --status applied --limit 3
.venv/bin/python -c "
from jobsearch import config, db, dashboard
c = db.connect(config.load_settings('.env'))
print(dashboard.daily_stats(c))
print(len(dashboard.fetch_rows(c, status_filter='applied')[0]), 'applied jobs')
c.close()"
```

Expected: the 8 rows previously in `applications` are still readable (`triaged` count non-zero, `applied` list non-empty). A `ProgrammingError: Table ... doesn't exist` means a query was missed.

- [ ] **Step 6: Update the v1 spec's table section**

In `docs/superpowers/specs/2026-08-25-job-search-design.md`, retitle `### applications` to `### triage` and add one line under it:

```markdown
Renamed from `applications` in migration 016; the tracker's own tables are
specified in `2026-09-01-application-tracker-design.md`.
```

- [ ] **Step 7: Commit**

```bash
git add migrations/016_add_application_tracker.sql jobsearch/models.py \
        jobsearch/store.py jobsearch/dashboard.py jobsearch/cli.py \
        jobsearch/server.py docs/superpowers/specs/2026-08-25-job-search-design.md
git commit -m "feat: rename triage and add the application tracker schema"
```

---

### Task 2: Stage vocabulary and request parsers

**Files:**
- Create: `jobsearch/tracker.py`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `slug_for(label) -> str`, `parse_stage_request(raw) -> dict`, `parse_transition(raw, *, now=None) -> dict`, `parse_application(raw, *, now=None) -> dict`, `parse_answers(value) -> list[dict] | None`, `parse_when(value, field, *, now=None) -> datetime | None`, and the constants `STAGE_KINDS`, `NO_RESPONSE`, `MAX_NOTE`, `MAX_ACTION`, `MAX_TEXT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tracker.py`:

```python
import json
from datetime import datetime

import pytest

from jobsearch.tracker import (
    STAGE_KINDS, parse_answers, parse_application, parse_stage_request,
    parse_transition, parse_when, slug_for,
)

NOW = datetime(2026, 9, 1, 14, 30)


def body(**payload) -> bytes:
    return json.dumps(payload).encode()


def test_a_label_becomes_a_slug():
    assert slug_for("Pair programming round") == "pair_programming_round"
    assert slug_for("  Final / culture interview ") == "final_culture_interview"


def test_a_label_with_no_usable_characters_is_rejected():
    with pytest.raises(ValueError, match="usable"):
        slug_for("!!! ???")


def test_a_custom_stage_parses():
    assert parse_stage_request(body(label="Pair round", weight=45, kind="active")) == {
        "label": "Pair round", "slug": "pair_round", "weight": 45, "kind": "active"}


@pytest.mark.parametrize("payload", [
    {"label": "X", "weight": 101, "kind": "active"},
    {"label": "X", "weight": -1, "kind": "active"},
    {"label": "X", "weight": 50, "kind": "pending"},
    {"label": "", "weight": 50, "kind": "active"},
])
def test_a_bad_stage_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_stage_request(body(**payload))


def test_every_kind_is_accepted():
    for kind in STAGE_KINDS:
        assert parse_stage_request(body(label="X", weight=50, kind=kind))["kind"] == kind


def test_a_transition_carries_a_future_next_action():
    # The whole point: today's move to a stage, with the call booked for the 5th.
    parsed = parse_transition(body(
        stage_id=5, note="scheduled with the CTO",
        next_action="tech interview call", next_action_at="2026-09-05T11:00"), now=NOW)
    assert parsed["stage_id"] == 5
    assert parsed["occurred_at"] == NOW
    assert parsed["next_action_at"] == datetime(2026, 9, 5, 11, 0)


def test_a_transition_can_be_back_dated():
    parsed = parse_transition(body(stage_id=5, occurred_at="2026-08-27"), now=NOW)
    assert parsed["occurred_at"] == datetime(2026, 8, 27, 0, 0)


@pytest.mark.parametrize("value", ["1998-01-01", "2099-01-01", "not-a-date", "2026-13-01"])
def test_an_out_of_range_or_malformed_date_is_rejected(value):
    with pytest.raises(ValueError, match="occurred_at"):
        parse_transition(body(stage_id=5, occurred_at=value), now=NOW)


def test_a_missing_date_is_none_not_an_error():
    assert parse_when(None, "next_action_at", now=NOW) is None
    assert parse_when("", "next_action_at", now=NOW) is None


def test_an_overlong_note_is_rejected():
    with pytest.raises(ValueError, match="note"):
        parse_transition(body(stage_id=5, note="x" * 4001), now=NOW)


def test_answers_are_question_and_answer_pairs():
    assert parse_answers([{"question": "Visa?", "answer": "EU citizen"}]) == [
        {"question": "Visa?", "answer": "EU citizen"}]


@pytest.mark.parametrize("value", [
    [{"question": "", "answer": "x"}],
    [{"question": "q"}],
    "not a list",
    [{"question": "q", "answer": "x"}] * 21,
])
def test_bad_answers_are_rejected(value):
    with pytest.raises(ValueError, match="answers"):
        parse_answers(value)


def test_creating_from_an_existing_job_needs_only_a_job_id():
    parsed = parse_application(body(job_id=42, applied_at="2026-08-30"), now=NOW)
    assert parsed["job_id"] == 42
    assert parsed["posting"] is None
    assert parsed["application"]["applied_at"] == datetime(2026, 8, 30, 0, 0)


def test_creating_from_a_url_carries_the_corrected_posting():
    parsed = parse_application(body(
        url="https://acme.com/careers/42", title="Backend Engineer", company="Acme",
        description="PHP and Postgres.", cover_letter="Dear team,"), now=NOW)
    assert parsed["job_id"] is None
    assert parsed["posting"]["title"] == "Backend Engineer"
    assert parsed["posting"]["url"] == "https://acme.com/careers/42"
    assert parsed["application"]["cover_letter"] == "Dear team,"


@pytest.mark.parametrize("payload", [
    {},                                                   # neither job_id nor url
    {"url": "javascript:alert(1)", "title": "X", "company": "Y"},
    {"url": "https://acme.com/x", "company": "Y"},         # url without a title
])
def test_an_unusable_create_request_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_application(body(**payload), now=NOW)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tracker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.tracker'`

- [ ] **Step 3: Write the implementation**

Create `jobsearch/tracker.py`:

```python
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
    if len(value) > limit:
        raise ValueError(f"{field} too long: {len(value)} > {limit}")
    return value.strip() or None


def _int(payload: dict, field: str, *, required: bool = True) -> int | None:
    value = payload.get(field)
    if value is None and not required:
        return None
    # bool subclasses int, so {"stage_id": true} would otherwise become stage 1.
    if isinstance(value, bool):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tracker.py -q`
Expected: PASS — every test in the file green

- [ ] **Step 5: Commit**

```bash
git add jobsearch/tracker.py tests/test_tracker.py
git commit -m "feat: stage vocabulary and request validation for the tracker"
```

---

### Task 3: Statistics buckets and the five metrics

**Files:**
- Modify: `jobsearch/tracker.py`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `NO_RESPONSE` from Task 2.
- Produces: `buckets(now) -> dict[str, tuple[datetime, datetime]]`, `build_stats(applications, events, now) -> dict`, and the internal `_when(value) -> datetime` used by Task 4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracker.py`:

```python
from jobsearch.tracker import build_stats, buckets

APPS = [
    {"id": 1, "applied_at": datetime(2026, 8, 31, 9, 0)},   # current week (Mon)
    {"id": 2, "applied_at": datetime(2026, 8, 27, 9, 0)},   # previous week
    {"id": 3, "applied_at": datetime(2026, 8, 31, 18, 0)},  # current week, no reply
]
EVENTS = [
    {"application_id": 1, "kind": "applied", "occurred_at": datetime(2026, 8, 31, 9, 0),
     "stage_slug": "applied", "stage_kind": "active", "stage_weight": 10},
    {"application_id": 1, "kind": "stage", "occurred_at": datetime(2026, 9, 1, 10, 0),
     "stage_slug": "tech_interview", "stage_kind": "active", "stage_weight": 40},
    {"application_id": 2, "kind": "stage", "occurred_at": datetime(2026, 8, 28, 10, 0),
     "stage_slug": "declined", "stage_kind": "lost", "stage_weight": 0},
    {"application_id": 3, "kind": "applied", "occurred_at": datetime(2026, 8, 31, 18, 0),
     "stage_slug": "applied", "stage_kind": "active", "stage_weight": 10},
]


def test_buckets_are_half_open_and_week_starts_on_monday():
    # 2026-09-01 is a Tuesday.
    windows = buckets(NOW)
    assert windows["current_week"][0] == datetime(2026, 8, 31, 0, 0)
    assert windows["previous_week"] == (datetime(2026, 8, 24), datetime(2026, 8, 31))
    assert windows["previous_day"] == (datetime(2026, 8, 31), datetime(2026, 9, 1))
    assert windows["current_month"][0] == datetime(2026, 9, 1, 0, 0)
    assert windows["previous_month"] == (datetime(2026, 8, 1), datetime(2026, 9, 1))


def test_buckets_handle_a_year_boundary():
    windows = buckets(datetime(2027, 1, 4, 8, 0))     # a Monday
    assert windows["current_week"][0] == datetime(2027, 1, 4, 0, 0)
    assert windows["previous_month"] == (datetime(2026, 12, 1), datetime(2027, 1, 1))


def test_sent_counts_by_applied_at():
    stats = build_stats(APPS, EVENTS, NOW)
    assert stats["current_week"]["sent"] == 2
    assert stats["previous_week"]["sent"] == 1


def test_advanced_counts_moves_into_active_or_won_stages():
    stats = build_stats(APPS, EVENTS, NOW)
    # The tech interview on the 1st, and both 'applied' events on the 31st.
    assert stats["current_week"]["advanced"] == 3
    assert stats["previous_week"]["advanced"] == 0


def test_a_back_dated_event_lands_in_the_week_it_happened():
    stats = build_stats(APPS, EVENTS, NOW)
    assert stats["previous_week"]["lost"] == 1
    assert stats["current_week"]["lost"] == 0


def test_response_rate_is_a_cohort_of_the_bucket_that_was_sent():
    stats = build_stats(APPS, EVENTS, NOW)
    # Of the two sent this week, only #1 ever left "applied".
    assert stats["current_week"]["response_rate"] == 0.5


def test_response_rate_is_none_when_nothing_was_sent():
    # None, not 0.0 — an empty cohort has no rate, and 0% would read as failure.
    assert build_stats([], [], NOW)["previous_day"]["response_rate"] is None


def test_a_ghosting_is_not_a_response():
    events = [{"application_id": 2, "kind": "stage",
               "occurred_at": datetime(2026, 8, 29), "stage_slug": "ghosted",
               "stage_kind": "lost", "stage_weight": 0}]
    assert build_stats([APPS[1]], events, NOW)["previous_week"]["response_rate"] == 0.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tracker.py -q -k "bucket or stats or response or advanced or sent or back_dated or ghosting"`
Expected: FAIL — `ImportError: cannot import name 'build_stats'`

- [ ] **Step 3: Write the implementation**

Append to `jobsearch/tracker.py`:

```python
def _when(value) -> datetime:
    """MySQL gives datetimes; JSON fixtures and query strings give ISO strings."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace(" ", "T"))


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tracker.py -q`
Expected: PASS — every test in the file green, Task 2's included

- [ ] **Step 5: Commit**

```bash
git add jobsearch/tracker.py tests/test_tracker.py
git commit -m "feat: reporting windows and the five application metrics"
```

---

### Task 4: List, detail and activity builders

**Files:**
- Modify: `jobsearch/tracker.py`, `jobsearch/db.py`, `jobsearch/dashboard.py:158-170`
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `_when` from Task 3.
- Produces: `build_list(rows, now) -> list[dict]`, `build_detail(row, events, now) -> dict`, `build_timeline(events) -> list[dict]`, `build_activity(rows) -> list[dict]`, and `db.as_json(value, fallback)`.

Sorting happens in Python, not in an `ORDER BY`. That is what `dashboard.build_view` already does, and it is what makes the ordering rule — the one thing in this feature most likely to be got wrong — testable without a database.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracker.py`:

```python
from jobsearch.tracker import build_activity, build_detail, build_list, build_timeline

def row(**over):
    base = {
        "id": 1, "job_id": 42, "title": "Backend Engineer", "company": "Acme",
        "url": "https://acme.com/jobs/1", "score": 8,
        "applied_at": datetime(2026, 8, 25, 9, 0),
        "stage_at": datetime(2026, 8, 28, 9, 0),
        "stage_slug": "tech_interview", "stage_label": "Technical interview",
        "stage_kind": "active", "stage_weight": 40,
        "next_action": None, "next_action_at": None,
        "cv_file_id": None, "cv_filename": None,
    }
    return {**base, **over}


def test_a_lost_application_sorts_below_every_active_one():
    cards = build_list([
        row(id=1, stage_slug="declined", stage_kind="lost", stage_weight=0),
        row(id=2, stage_slug="recruiter_screen", stage_kind="active", stage_weight=20),
    ], NOW)
    assert [c["id"] for c in cards] == [2, 1]


def test_heavier_stages_sort_first():
    cards = build_list([
        row(id=1, stage_weight=20), row(id=2, stage_weight=90, stage_kind="won"),
        row(id=3, stage_weight=40),
    ], NOW)
    assert [c["id"] for c in cards] == [2, 3, 1]


def test_within_a_stage_the_most_recently_moved_is_first():
    cards = build_list([
        row(id=1, stage_at=datetime(2026, 8, 20)),
        row(id=2, stage_at=datetime(2026, 8, 30)),
    ], NOW)
    assert [c["id"] for c in cards] == [2, 1]


def test_a_next_action_is_badged_against_today():
    cards = build_list([
        row(id=1, next_action="chase", next_action_at=datetime(2026, 8, 30)),
        row(id=2, next_action="call", next_action_at=datetime(2026, 9, 1, 11, 0)),
        row(id=3, next_action="call", next_action_at=datetime(2026, 9, 5, 11, 0)),
    ], NOW)
    due = {c["id"]: c["next_action"]["due"] for c in cards}
    assert due == {1: "overdue", 2: "today", 3: "later"}


def test_no_next_action_is_none_rather_than_an_empty_badge():
    assert build_list([row()], NOW)[0]["next_action"] is None


def test_days_in_stage_is_counted_from_stage_at():
    assert build_list([row()], NOW)[0]["days_in_stage"] == 4


def test_a_cv_is_reported_only_when_one_is_attached():
    assert build_list([row()], NOW)[0]["cv"] is None
    attached = build_list([row(cv_file_id=3, cv_filename="cv.pdf")], NOW)[0]
    assert attached["cv"] == {"id": 3, "filename": "cv.pdf"}


EVENTS_ONE = [
    {"id": 1, "application_id": 1, "kind": "applied", "stage_label": "Applied",
     "occurred_at": datetime(2026, 8, 25, 9, 0), "created_at": datetime(2026, 8, 25, 9, 0),
     "note": None, "next_action": None, "next_action_at": None},
    {"id": 2, "application_id": 1, "kind": "stage", "stage_label": "Technical interview",
     "occurred_at": datetime(2026, 8, 28, 9, 0), "created_at": datetime(2026, 8, 31, 20, 0),
     "note": "with the CTO", "next_action": "call", "next_action_at": datetime(2026, 9, 5)},
]


def test_a_timeline_reads_newest_first():
    assert [e["id"] for e in build_timeline(EVENTS_ONE)] == [2, 1]


def test_an_event_recorded_later_than_it_happened_is_marked_back_dated():
    timeline = {e["id"]: e for e in build_timeline(EVENTS_ONE)}
    assert timeline[2]["back_dated"] is True
    assert timeline[1]["back_dated"] is False


def test_detail_carries_the_submitted_text_and_the_timeline():
    detail = build_detail(
        row(cover_letter="Dear team", why_company="payments", answers='[{"question": "Visa?", "answer": "EU"}]'),
        EVENTS_ONE, NOW)
    assert detail["texts"]["cover_letter"] == "Dear team"
    assert detail["answers"] == [{"question": "Visa?", "answer": "EU"}]
    assert [e["id"] for e in detail["timeline"]] == [2, 1]


def test_activity_names_the_job_each_event_belongs_to():
    rows = [{**EVENTS_ONE[1], "title": "Backend Engineer", "company": "Acme"}]
    assert build_activity(rows)[0]["company"] == "Acme"
    assert build_activity(rows)[0]["application_id"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tracker.py -q -k "sort or badged or timeline or detail or activity or cv or days_in"`
Expected: FAIL — `ImportError: cannot import name 'build_list'`

- [ ] **Step 3: Promote the JSON-column helper into `db.py`**

MySQL JSON columns arrive as `str` from some drivers and as parsed objects from others. `dashboard._as_json` already handles it; the tracker needs the same, so it moves to the layer that owns the quirk instead of being written twice.

Add to `jobsearch/db.py`:

```python
def as_json(value, fallback):
    """MySQL JSON columns arrive as str from some drivers and as parsed objects
    from others. Accept both rather than depending on the driver's mood."""
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
```

with `import json` at the top. In `jobsearch/dashboard.py`, delete the local `_as_json` definition, add `from jobsearch.db import as_json`, and rename its six call sites (`_as_json(` → `as_json(`).

- [ ] **Step 4: Write the builders**

Append to `jobsearch/tracker.py` (and add `from jobsearch.db import as_json` at the top):

```python
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
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — the tracker tests plus every existing test, since `dashboard`'s behaviour is unchanged by the helper move.

- [ ] **Step 6: Commit**

```bash
git add jobsearch/tracker.py jobsearch/db.py jobsearch/dashboard.py tests/test_tracker.py
git commit -m "feat: list, timeline and activity builders for the tracker"
```

---

### Task 5: Writes — create an application, transition a stage

**Files:**
- Modify: `jobsearch/tracker.py`, `jobsearch/filters.py:87-97`

**Interfaces:**
- Consumes: `parse_application`, `parse_transition` (Task 2).
- Produces: `stage_by_slug(conn, slug) -> dict`, `create_stage(conn, parsed, now) -> dict`, `create_application(conn, job_id, fields, *, now=None) -> dict`, `transition(conn, application_id, parsed, *, now=None) -> dict`.

These touch SQL, so they are verified by running them against the dev database rather than by unit tests — the repo has no test database and this plan does not add one.

- [ ] **Step 1: Write the writes**

Append to `jobsearch/tracker.py`:

```python
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
    now = now or datetime.now()
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
    now = now or datetime.now()
    applied_stage = stage_by_slug(conn, "applied")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications (job_id, applied_at, stage_id, stage_at, "
                "cv_file_id, cover_letter, why_company, salary_expectation, "
                "notice_period, answers, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (job_id, fields["applied_at"], applied_stage["id"], fields["applied_at"],
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
                (application_id, applied_stage["id"], fields["applied_at"], now),
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
    now = now or datetime.now()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM stages WHERE id = %s", (parsed["stage_id"],))
            if cur.fetchone() is None:
                raise LookupError(f"unknown stage id: {parsed['stage_id']}")

            cur.execute(
                "INSERT INTO application_events (application_id, kind, stage_id, "
                "occurred_at, note, next_action, next_action_at, created_at) "
                "VALUES (%s, 'stage', %s, %s, %s, %s, %s, %s)",
                (application_id, parsed["stage_id"], parsed["occurred_at"], parsed["note"],
                 parsed["next_action"], parsed["next_action_at"], now),
            )
            event_id = cur.lastrowid

            cur.execute(
                "UPDATE applications SET stage_id=%s, stage_at=%s, next_action=%s, "
                "next_action_at=%s, updated_at=%s WHERE id=%s AND stage_at <= %s",
                (parsed["stage_id"], parsed["occurred_at"], parsed["next_action"],
                 parsed["next_action_at"], now, application_id, parsed["occurred_at"]),
            )
            became_current = cur.rowcount == 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"application_id": application_id, "event_id": event_id,
            "stage_id": parsed["stage_id"], "current": became_current}
```

- [ ] **Step 2: Stop the filter from discarding a job that has been applied to**

In `jobsearch/filters.py`, `apply`'s selection gains one clause:

```python
            "FROM jobs j LEFT JOIN job_sources js ON js.job_id = j.id "
            # A job that has been applied to is no longer a candidate for
            # filtering: the decision is made, and stamping it
            # 'salary below floor' would be a false statement about a live
            # application — and would hide it from the dashboard entirely.
            "WHERE j.inactive_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id) "
            "GROUP BY j.id"
```

- [ ] **Step 3: Verify against the dev database**

```bash
.venv/bin/python - <<'EOF'
from datetime import datetime
from jobsearch import config, db, tracker
conn = db.connect(config.load_settings(".env"))
with conn.cursor() as cur:
    cur.execute("SELECT id, title, company FROM jobs ORDER BY id DESC LIMIT 1")
    job = cur.fetchone()

app = tracker.create_application(conn, job["id"], {
    "applied_at": datetime(2026, 8, 30, 10, 0), "cv_file_id": None,
    "cover_letter": "Dear team,", "why_company": "payments",
    "salary_expectation": "5500 EUR", "notice_period": "2 weeks",
    "answers": [{"question": "Visa?", "answer": "EU citizen"}]})
print("created:", app)

tech = tracker.stage_by_slug(conn, "tech_interview")
print("forward:", tracker.transition(conn, app["id"], {
    "stage_id": tech["id"], "occurred_at": datetime(2026, 9, 1, 9, 0),
    "note": "with the CTO", "next_action": "tech interview call",
    "next_action_at": datetime(2026, 9, 5, 11, 0)}))

screen = tracker.stage_by_slug(conn, "recruiter_screen")
print("back-fill:", tracker.transition(conn, app["id"], {
    "stage_id": screen["id"], "occurred_at": datetime(2026, 8, 28, 9, 0),
    "note": "recorded late", "next_action": None, "next_action_at": None}))

with conn.cursor() as cur:
    cur.execute("SELECT stage_id, stage_at, next_action FROM applications WHERE id=%s", (app["id"],))
    print("current row:", cur.fetchone())
    cur.execute("SELECT COUNT(*) AS n FROM application_events WHERE application_id=%s", (app["id"],))
    print("events:", cur.fetchone())
conn.close()
EOF
```

Expected: `created` reports stage `applied`; the forward move returns `current: True`; the back-fill returns `current: False`; the row still points at `tech_interview` with `next_action` "tech interview call"; three events exist.

- [ ] **Step 4: Confirm the filter now spares it**

```bash
.venv/bin/jobsearch filter
.venv/bin/python -c "
from jobsearch import config, db
c = db.connect(config.load_settings('.env'))
cur = c.cursor()
cur.execute('SELECT j.id, j.filtered_at FROM jobs j JOIN applications a ON a.job_id=j.id')
print(cur.fetchall()); c.close()"
```

Expected: `filtered_at` is NULL for every job with an application. (A job filtered *before* this change keeps its old stamp; that is fine — re-running `filter` no longer re-stamps it, and nothing reads `filtered_at` in the tracker.)

- [ ] **Step 5: Clean up the probe rows**

```bash
.venv/bin/python -c "
from jobsearch import config, db
c = db.connect(config.load_settings('.env')); cur = c.cursor()
cur.execute('DELETE FROM application_events'); cur.execute('DELETE FROM applications')
c.commit(); print('probe rows removed'); c.close()"
```

- [ ] **Step 6: Commit**

```bash
git add jobsearch/tracker.py jobsearch/filters.py
git commit -m "feat: create applications and record stage transitions"
```

---

### Task 6: Reads — the queries behind every view

**Files:**
- Modify: `jobsearch/tracker.py`

**Interfaces:**
- Consumes: `build_list`, `build_detail`, `build_activity`, `build_stats` (Tasks 3–4).
- Produces: `list_stages(conn)`, `fetch_list(conn, *, kind=None)`, `fetch_detail(conn, application_id)`, `fetch_activity(conn, limit=100)`, `fetch_stats_rows(conn)`.

- [ ] **Step 1: Write the queries**

Append to `jobsearch/tracker.py`:

```python
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
```

- [ ] **Step 2: Verify each query against the dev database**

```bash
.venv/bin/python - <<'EOF'
from datetime import datetime
from jobsearch import config, db, tracker
conn = db.connect(config.load_settings(".env"))
print("stages:", len(tracker.list_stages(conn)), "first:", tracker.list_stages(conn)[0])
with conn.cursor() as cur:
    cur.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
    job_id = cur.fetchone()["id"]
app = tracker.create_application(conn, job_id, {"applied_at": datetime.now()})
tech = tracker.stage_by_slug(conn, "tech_interview")
tracker.transition(conn, app["id"], {"stage_id": tech["id"], "occurred_at": datetime.now(),
                                     "note": "probe", "next_action": "call",
                                     "next_action_at": datetime(2026, 9, 5)})
now = datetime.now()
print("list:", tracker.build_list(tracker.fetch_list(conn), now))
row, events = tracker.fetch_detail(conn, app["id"])
print("detail:", tracker.build_detail(row, events, now)["timeline"])
print("activity:", tracker.build_activity(tracker.fetch_activity(conn, 10))[:2])
apps, evs = tracker.fetch_stats_rows(conn)
print("stats:", tracker.build_stats(apps, evs, now)["current_week"])
with conn.cursor() as cur:
    cur.execute("DELETE FROM application_events"); cur.execute("DELETE FROM applications")
conn.commit(); conn.close()
EOF
```

Expected: 14 stages with `applied` first; one card carrying `title`, `company`, `url`, `stage.label` "Technical interview" and `next_action.due` "later"; a two-entry timeline; activity naming the company; `current_week.sent == 1`.

- [ ] **Step 3: Commit**

```bash
git add jobsearch/tracker.py
git commit -m "feat: queries for the application list, timeline, activity and stats"
```

---

### Task 7: CV storage

**Files:**
- Create: `jobsearch/cv.py`, `tests/test_cv.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `sniff(data) -> tuple[str, str]`, `parse_upload(raw) -> tuple[str, bytes]`, `write_file(data, ext, directory) -> tuple[str, str]`, `store(conn, filename, data, *, directory=CV_DIR, now=None) -> dict`, `fetch(conn, cv_id) -> dict | None`, `list_files(conn) -> list[dict]`, `MAX_BYTES`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cv.py`:

```python
import base64
import json

import pytest

from jobsearch.cv import MAX_BYTES, parse_upload, sniff, write_file

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n"
DOCX = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 20
TEXT = "Serhii Drozh — Backend Engineer\n".encode()


def upload(**payload) -> bytes:
    return json.dumps(payload).encode()


def test_a_pdf_is_recognised_by_its_magic_bytes():
    assert sniff(PDF) == ("application/pdf", ".pdf")


def test_a_docx_is_recognised_as_a_zip_container():
    assert sniff(DOCX)[1] == ".docx"


def test_utf8_text_is_accepted():
    assert sniff(TEXT)[1] == ".txt"


def test_a_file_that_is_none_of_those_is_rejected():
    # A client claiming content_type: application/pdf does not make it one.
    with pytest.raises(ValueError, match="unsupported"):
        sniff(b"\x00\x01\x02\xff\xfe")


def test_an_upload_carries_a_filename_and_base64_content():
    filename, data = parse_upload(upload(
        filename="cv.pdf", content=base64.b64encode(PDF).decode()))
    assert filename == "cv.pdf"
    assert data == PDF


@pytest.mark.parametrize("payload,match", [
    ({"filename": "cv.pdf", "content": "not base64!!"}, "base64"),
    ({"filename": "", "content": "AAAA"}, "filename"),
    ({"content": "AAAA"}, "filename"),
    ({"filename": "cv.pdf"}, "content"),
])
def test_a_malformed_upload_is_rejected(payload, match):
    with pytest.raises(ValueError, match=match):
        parse_upload(upload(**payload))


def test_an_oversized_upload_is_rejected_before_it_is_decoded():
    oversized = base64.b64encode(b"x" * (MAX_BYTES + 1)).decode()
    with pytest.raises(ValueError, match="too large"):
        parse_upload(upload(filename="cv.pdf", content=oversized))


def test_the_same_bytes_are_written_once(tmp_path):
    first_hash, first_path = write_file(PDF, ".pdf", str(tmp_path))
    second_hash, second_path = write_file(PDF, ".pdf", str(tmp_path))
    assert first_hash == second_hash
    assert first_path == second_path
    assert len(list(tmp_path.iterdir())) == 1


def test_the_stored_name_is_the_hash_not_the_uploaded_name(tmp_path):
    # Path traversal has nothing to grab: the client's filename never reaches disk.
    digest, path = write_file(PDF, ".pdf", str(tmp_path))
    assert path.endswith(f"{digest}.pdf")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cv.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.cv'`

- [ ] **Step 3: Write the implementation**

Create `jobsearch/cv.py`:

```python
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from pathlib import Path

CV_DIR = "var/cv"
MAX_BYTES = 10 * 1024 * 1024
MAX_FILENAME = 255

# What a CV is allowed to be, decided by the file's own first bytes. The client's
# claimed content type is never consulted: it is the one field an attacker fully
# controls, and it decides how the download endpoint later labels the response.
SIGNATURES = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"PK\x03\x04",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
)


def sniff(data: bytes) -> tuple[str, str]:
    for magic, content_type, extension in SIGNATURES:
        if data.startswith(magic):
            return content_type, extension
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("unsupported file type: expected PDF, DOCX or UTF-8 text") from None
    return "text/plain; charset=utf-8", ".txt"


def parse_upload(raw: str | bytes) -> tuple[str, bytes]:
    """Validate an upload. Base64 inside JSON rather than multipart: `cgi` is
    gone in 3.13, and JSON keeps the CSRF guard that works precisely because a
    cross-origin HTML form cannot send application/json."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("malformed JSON: expected an object")

    filename = payload.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("filename is required")
    if len(filename) > MAX_FILENAME:
        raise ValueError(f"filename too long: {len(filename)} > {MAX_FILENAME}")

    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("content is required")
    # Checked before decoding: base64 inflates by 4/3, so refusing here means
    # never materialising an oversized file in memory.
    if len(content) > (MAX_BYTES // 3) * 4 + 4:
        raise ValueError(f"file too large: limit is {MAX_BYTES} bytes")
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("content is not valid base64") from None
    if not data:
        raise ValueError("content is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"file too large: {len(data)} > {MAX_BYTES}")
    return filename.strip(), data


def write_file(data: bytes, extension: str, directory: str = CV_DIR) -> tuple[str, str]:
    """Store bytes under their own digest. Content-addressed, so re-using last
    week's CV costs nothing and an edited CV is a different file rather than an
    overwrite of the record of what was actually sent."""
    digest = hashlib.sha256(data).hexdigest()
    target = Path(directory) / f"{digest}{extension}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return digest, str(target)


def store(conn, filename: str, data: bytes, *, directory: str = CV_DIR,
          now: datetime | None = None) -> dict:
    content_type, extension = sniff(data)
    digest, path = write_file(data, extension, directory)
    with conn.cursor() as cur:
        cur.execute("SELECT id, filename FROM cv_files WHERE sha256 = %s", (digest,))
        existing = cur.fetchone()
        if existing:
            return {**existing, "sha256": digest, "reused": True}
        cur.execute(
            "INSERT INTO cv_files (sha256, filename, content_type, size_bytes, path, "
            "uploaded_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (digest, filename, content_type, len(data), path, now or datetime.now()),
        )
        cv_id = cur.lastrowid
    conn.commit()
    return {"id": cv_id, "filename": filename, "sha256": digest, "reused": False}


def fetch(conn, cv_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM cv_files WHERE id = %s", (cv_id,))
        return cur.fetchone()


def list_files(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, filename, content_type, size_bytes, uploaded_at "
                    "FROM cv_files ORDER BY uploaded_at DESC")
        return list(cur.fetchall())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cv.py -q`
Expected: PASS

- [ ] **Step 5: Verify storage and dedupe against the dev database**

```bash
.venv/bin/python - <<'EOF'
from jobsearch import config, cv, db
conn = db.connect(config.load_settings(".env"))
data = open("serhii-drozh-cv.pdf", "rb").read()
print("first:", cv.store(conn, "serhii-drozh-cv.pdf", data))
print("again:", cv.store(conn, "serhii-drozh-cv.pdf", data))
print("listed:", cv.list_files(conn))
conn.close()
EOF
ls -la var/cv/
```

Expected: the first call reports `reused: False`, the second `reused: True` with the same id; exactly one file in `var/cv/`, named by its digest.

- [ ] **Step 6: Commit**

```bash
git add jobsearch/cv.py tests/test_cv.py
git commit -m "feat: content-addressed CV storage"
```

---

### Task 8: The SSRF guard

**Files:**
- Create: `jobsearch/manual.py`, `tests/test_manual.py`

**Interfaces:**
- Consumes: `USER_AGENT` from `jobsearch.adapters.base`.
- Produces: `check_target(url) -> str`, `safe_fetch_url(url, *, transport=None) -> tuple[str, str]`, `MAX_BODY`, `MAX_REDIRECTS`.

`/api/lookup-url` exists to make the server fetch a URL the caller chooses — the shape of an SSRF. The defence is this one function, not a networking layer.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_manual.py`:

```python
import httpx
import pytest

from jobsearch import manual


def resolves_to(*ips):
    """Stand in for DNS so the guard can be tested without a network."""
    mapping = {}

    def fake(host, port, *args, **kwargs):
        ip = mapping.get(host, ips[0])
        return [(2, 1, 6, "", (ip, port or 443))]

    fake.mapping = mapping
    return fake


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10",
                                "169.254.169.254", "172.16.0.1", "::1"])
def test_an_address_off_the_public_internet_is_refused(monkeypatch, ip):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to(ip))
    with pytest.raises(ValueError, match="non-public"):
        manual.check_target("https://looks-fine.example.com/jobs/1")


def test_a_public_address_passes(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))
    assert manual.check_target("https://example.com/jobs/1") == "https://example.com/jobs/1"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                 "ftp://example.com/x", "gopher://example.com"])
def test_a_non_http_scheme_is_refused(url):
    with pytest.raises(ValueError, match="scheme"):
        manual.check_target(url)


def test_an_unresolvable_host_is_refused(monkeypatch):
    def boom(*args, **kwargs):
        raise manual.socket.gaierror("nope")
    monkeypatch.setattr(manual.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="resolve"):
        manual.check_target("https://nowhere.invalid/x")


def test_a_redirect_into_private_space_is_refused(monkeypatch):
    dns = resolves_to("93.184.216.34")
    dns.mapping.update({"public.example.com": "93.184.216.34",
                        "internal.example.com": "10.0.0.5"})
    monkeypatch.setattr(manual.socket, "getaddrinfo", dns)

    def handler(request):
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"Location": "https://internal.example.com/x"})
        return httpx.Response(200, text="never reached")

    with pytest.raises(ValueError, match="non-public"):
        manual.safe_fetch_url("https://public.example.com/x",
                              transport=httpx.MockTransport(handler))


def test_a_public_redirect_is_followed(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, text="<html>ok</html>")

    url, body = manual.safe_fetch_url("https://example.com/old",
                                      transport=httpx.MockTransport(handler))
    assert url.endswith("/new")
    assert "ok" in body


def test_an_endless_redirect_chain_stops(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))

    def handler(request):
        return httpx.Response(302, headers={"Location": "https://example.com/again"})

    with pytest.raises(ValueError, match="redirects"):
        manual.safe_fetch_url("https://example.com/x",
                              transport=httpx.MockTransport(handler))


def test_an_oversized_body_is_refused(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))

    def handler(request):
        return httpx.Response(200, content=b"x" * (manual.MAX_BODY + 10))

    with pytest.raises(ValueError, match="larger"):
        manual.safe_fetch_url("https://example.com/x",
                              transport=httpx.MockTransport(handler))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_manual.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.manual'`

- [ ] **Step 3: Write the guard**

Create `jobsearch/manual.py`:

```python
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from jobsearch.adapters.base import TIMEOUT, USER_AGENT

MAX_BODY = 2 * 1024 * 1024
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = ("http", "https")


def check_target(url: str) -> str:
    """Refuse anything that is not a public http(s) endpoint.

    Resolution happens here, before the request, because the hostname is the
    attacker's field: `internal.example.com` can resolve to 10.0.0.5, and the
    dashboard answers on the LAN with no password. `is_global` is what draws
    the line — it already excludes loopback, private, link-local (including the
    169.254.169.254 metadata address), and reserved space.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported url scheme: {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"url has no host: {url!r}")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {host!r}: {exc}") from None

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise ValueError(f"refusing to fetch a non-public address: {host} -> {address}")
    return url


def safe_fetch_url(url: str, *, transport=None) -> tuple[str, str]:
    """Fetch a caller-chosen URL. Returns the final URL and the decoded body.

    Redirects are followed by hand so every hop passes check_target — a public
    first hop redirecting to 10.0.0.5 is the standard way past a guard that only
    checks the URL it was handed.
    """
    for _ in range(MAX_REDIRECTS + 1):
        check_target(url)
        with httpx.Client(follow_redirects=False, timeout=TIMEOUT, transport=transport) as client:
            with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect without a location header")
                    url = str(response.url.join(location))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                # Streamed and counted rather than .content: the cap has to hold
                # for a server that answers with an endless body.
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise ValueError(f"response larger than {MAX_BODY} bytes")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
            return str(response.url), b"".join(chunks).decode(encoding, errors="replace")
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_manual.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jobsearch/manual.py tests/test_manual.py
git commit -m "feat: SSRF-guarded fetch for owner-supplied URLs"
```

---

### Task 9: The extraction chain

**Files:**
- Modify: `jobsearch/manual.py`, `tests/test_manual.py`
- Create: `tests/fixtures/manual/jsonld.html`, `tests/fixtures/manual/bare.html`

**Interfaces:**
- Consumes: `safe_fetch_url` (Task 8); `_greenhouse`, `_lever`, `_ashby` from `jobsearch.adapters.ats`; `parse_posted_at` from `jobsearch.adapters.base`; `normalize.description`.
- Produces: `ats_target(url) -> tuple[str, str, str] | None`, `from_jsonld(html) -> dict | None`, `from_meta(html) -> dict`, `extract(url, *, transport=None) -> dict`.

Order matters and is the opposite of how the spec lists it: JSON-LD is written first because it carries most URLs. The ATS branch is three patterns and three imports on top of readers that already exist in `adapters/ats.py` — it is not new parsing work.

- [ ] **Step 1: Write the fixtures**

Create `tests/fixtures/manual/jsonld.html`:

```html
<!doctype html>
<html><head><title>Careers | Acme</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Senior Backend Engineer",
 "datePosted":"2026-08-20",
 "employmentType":"FULL_TIME",
 "hiringOrganization":{"@type":"Organization","name":"Acme Payments"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Berlin","addressCountry":"DE"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"EUR",
   "value":{"@type":"QuantitativeValue","minValue":6000,"maxValue":7500,"unitText":"MONTH"}},
 "description":"<p>PHP, Laravel and Postgres. Remote within the EU.</p>"}
</script></head>
<body><h1>Senior Backend Engineer</h1></body></html>
```

Create `tests/fixtures/manual/bare.html`:

```html
<!doctype html>
<html><head><title>Careers | Acme</title>
<meta property="og:site_name" content="Acme Payments">
</head>
<body><main><p>We are hiring a backend engineer to work on payments.</p></main></body></html>
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_manual.py`:

```python
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "manual"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_json_ld_gives_a_complete_posting():
    posting = manual.from_jsonld(fixture("jsonld.html"))
    assert posting["title"] == "Senior Backend Engineer"
    assert posting["company"] == "Acme Payments"
    assert "Laravel" in posting["description"]
    assert "<p>" not in posting["description"]      # markup stripped, not shown
    assert posting["location"] == "Berlin, DE"
    assert posting["salary_raw"] == "6000-7500 EUR MONTH"
    assert posting["posted_at"].date().isoformat() == "2026-08-20"


def test_a_page_without_json_ld_yields_nothing_rather_than_a_guess():
    assert manual.from_jsonld(fixture("bare.html")) is None


def test_the_fallback_never_passes_a_page_title_off_as_a_job_title():
    posting = manual.from_meta(fixture("bare.html"))
    assert posting["title"] == "Careers | Acme"
    assert posting["company"] == "Acme Payments"
    assert "backend engineer" in posting["description"]


@pytest.mark.parametrize("url,vendor,slug,job_id", [
    ("https://boards.greenhouse.io/acme/jobs/4512345", "greenhouse", "acme", "4512345"),
    ("https://jobs.lever.co/acme/8a1b2c3d-4e5f-6789-abcd-ef0123456789",
     "lever", "acme", "8a1b2c3d-4e5f-6789-abcd-ef0123456789"),
    ("https://jobs.ashbyhq.com/acme/8a1b2c3d-4e5f-6789-abcd-ef0123456789",
     "ashby", "acme", "8a1b2c3d-4e5f-6789-abcd-ef0123456789"),
])
def test_an_ats_url_is_recognised(url, vendor, slug, job_id):
    assert manual.ats_target(url) == (vendor, slug, job_id)


def test_an_ordinary_careers_url_is_not_an_ats_url():
    assert manual.ats_target("https://acme.com/careers/backend-engineer") is None


def test_extract_prefers_json_ld_and_marks_it_trusted(monkeypatch):
    monkeypatch.setattr(manual, "safe_fetch_url",
                        lambda url, **kw: ("https://acme.com/x", fixture("jsonld.html")))
    result = manual.extract("https://acme.com/x")
    assert result["needs_review"] is False
    assert result["via"] == "jsonld"
    assert result["title"] == "Senior Backend Engineer"


def test_extract_flags_the_fallback_for_review(monkeypatch):
    monkeypatch.setattr(manual, "safe_fetch_url",
                        lambda url, **kw: ("https://acme.com/x", fixture("bare.html")))
    result = manual.extract("https://acme.com/x")
    assert result["needs_review"] is True
    assert result["via"] == "fallback"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_manual.py -q -k "json_ld or fallback or ats or extract"`
Expected: FAIL — `AttributeError: module 'jobsearch.manual' has no attribute 'from_jsonld'`

- [ ] **Step 4: Write the extraction chain**

Append to `jobsearch/manual.py` (adding `import json`, `import re`, `from bs4 import BeautifulSoup`, `from jobsearch import normalize`, `from jobsearch.adapters.ats import _ashby, _greenhouse, _lever`, `from jobsearch.adapters.base import parse_posted_at` at the top):

```python
# vendor -> (url pattern, single-posting API, reader). The readers are the ones
# the ATS board adapter already uses; a second copy would drift from the first
# the next time a vendor changes a field name.
ATS_VENDORS = (
    (re.compile(r"^https?://boards\.greenhouse\.io/(?P<slug>[^/?#]+)/jobs/(?P<id>\d+)"),
     "greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}", _greenhouse),
    (re.compile(r"^https?://jobs\.lever\.co/(?P<slug>[^/?#]+)/(?P<id>[0-9a-fA-F-]{36})"),
     "lever", "https://api.lever.co/v0/postings/{slug}/{id}", _lever),
    (re.compile(r"^https?://jobs\.ashbyhq\.com/(?P<slug>[^/?#]+)/(?P<id>[0-9a-fA-F-]{36})"),
     "ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}", _ashby),
)


def ats_target(url: str) -> tuple[str, str, str] | None:
    for pattern, vendor, _template, _reader in ATS_VENDORS:
        match = pattern.match(url)
        if match:
            return vendor, match.group("slug"), match.group("id")
    return None


def _flatten(item: dict, reader) -> dict:
    """One vendor posting in the shape the rest of this module speaks."""
    raw = reader(item)
    return {
        "title": raw.get("title"),
        "company": None,                       # vendor payloads omit it; the URL slug is not a name
        "description": normalize.description(raw.get("description") or ""),
        "location": raw.get("location"),
        "salary_raw": None,
        "posted_at": parse_posted_at(raw.get("posted_at")),
    }


def from_ats(url: str, *, transport=None) -> dict | None:
    target = ats_target(url)
    if not target:
        return None
    vendor, slug, job_id = target
    pattern, _vendor, template, reader = next(v for v in ATS_VENDORS if v[1] == vendor)
    _final, body = safe_fetch_url(template.format(slug=slug, id=job_id), transport=transport)
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if vendor == "ashby":
        # Ashby publishes a board, not a posting: find the one that was asked for.
        items = [i for i in (payload.get("jobs") or []) if str(i.get("id")) == job_id]
        if not items:
            return None
        return _flatten(items[0], reader)
    return _flatten(payload, reader)


def _nodes(text: str):
    """Every JSON-LD object in one script tag, flattening lists and @graph."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return []
    pending = parsed if isinstance(parsed, list) else [parsed]
    out = []
    for node in pending:
        if not isinstance(node, dict):
            continue
        out.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            out.extend(n for n in graph if isinstance(n, dict))
    return out


def _address(node) -> str | None:
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return None
    address = node.get("address")
    if not isinstance(address, dict):
        return None
    parts = [address.get("addressLocality"), address.get("addressRegion"),
             address.get("addressCountry")]
    parts = [p for p in parts if isinstance(p, str) and p]
    return ", ".join(parts) or None


def _salary(node) -> str | None:
    """The posting's own words about pay, left as text for salary.parse to read."""
    if not isinstance(node, dict):
        return None
    value = node.get("value")
    currency = node.get("currency") or node.get("salaryCurrency") or ""
    if not isinstance(value, dict):
        return None
    low, high = value.get("minValue"), value.get("maxValue")
    unit = value.get("unitText") or ""
    amount = f"{low}-{high}" if low and high else str(low or high or "")
    return " ".join(p for p in (amount, currency, unit) if p) or None


def from_jsonld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        for node in _nodes(tag.string or tag.get_text() or ""):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" not in types:
                continue
            organisation = node.get("hiringOrganization")
            company = (organisation.get("name") if isinstance(organisation, dict)
                       else organisation if isinstance(organisation, str) else None)
            return {
                "title": node.get("title"),
                "company": company,
                "description": normalize.description(node.get("description") or ""),
                "location": _address(node.get("jobLocation")),
                "salary_raw": _salary(node.get("baseSalary")),
                "posted_at": parse_posted_at(node.get("datePosted")),
            }
    return None


def from_meta(html: str) -> dict:
    """Last resort. Everything here is a guess, and the caller says so."""
    soup = BeautifulSoup(html, "lxml")

    def meta(prop: str) -> str | None:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else None

    title = meta("og:title") or (soup.title.get_text().strip() if soup.title else None)
    body = soup.find("main") or soup.body or soup
    return {
        "title": title,
        "company": meta("og:site_name"),
        "description": normalize.description(str(body)),
        "location": None,
        "salary_raw": None,
        "posted_at": None,
    }


def extract(url: str, *, transport=None) -> dict:
    """What the paste-a-URL form gets back. Nothing here writes to the database.

    `needs_review` is a fact about the extraction, not a score: either the page
    told us what this job is, or we guessed from a page title and the owner has
    to look. There is no third state.
    """
    posting = from_ats(url, transport=transport)
    if posting and posting.get("title"):
        return {**posting, "url": url, "via": "ats", "needs_review": posting["company"] is None}

    final_url, html = safe_fetch_url(url, transport=transport)
    posting = from_jsonld(html)
    if posting and posting.get("title") and posting.get("company"):
        return {**posting, "url": final_url, "via": "jsonld", "needs_review": False}

    return {**from_meta(html), "url": final_url, "via": "fallback", "needs_review": True}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_manual.py -q`
Expected: PASS. If `test_json_ld_gives_a_complete_posting` fails on `salary_raw`, check `_salary`'s ordering — the assertion expects `"6000-7500 EUR MONTH"`.

- [ ] **Step 6: Verify against a real posting**

```bash
.venv/bin/python -c "
from jobsearch import manual
import json
print(json.dumps(manual.extract('https://boards.greenhouse.io/anthropic/jobs/4020295008'),
                 default=str, indent=2))"
```

Expected: a title and description, `via: "ats"`. Any public JobPosting URL works; if the one above is gone, use any live posting. A `ValueError` naming a non-public address means the guard is doing its job on a URL that resolves internally — pick another.

- [ ] **Step 7: Commit**

```bash
git add jobsearch/manual.py tests/test_manual.py tests/fixtures/manual
git commit -m "feat: extract a posting from a pasted URL"
```

---

### Task 10: Route table

**Files:**
- Modify: `jobsearch/server.py:96-230`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ROUTES` (list of `(method, compiled pattern, handler name)`), and the handler conventions every later route follows: `handler(self, match, parsed)`, plus `self._body(limit=...)` and `self._send_bytes(...)`.

Pure refactor. Behaviour must not change; the next two tasks add routes to the table rather than to an if-chain.

- [ ] **Step 1: Restructure the handler**

In `jobsearch/server.py`, add `import re` and replace the bodies of `do_GET` and `do_POST` with a table and a dispatcher. The host guard, the CSRF content-type guard and the size cap keep their current wording and reasons — they simply move in front of the dispatch:

```python
    ROUTES = [
        ("GET",  re.compile(r"^/(?:index\.html)?$"),                "page_dashboard"),
        ("GET",  re.compile(r"^/static/(?P<name>[A-Za-z0-9._-]+)$"), "static_asset"),
        ("GET",  re.compile(r"^/api/jobs$"),                        "api_jobs"),
        ("GET",  re.compile(r"^/api/stats$"),                       "api_stats"),
        ("POST", re.compile(r"^/api/status$"),                      "api_status"),
        ("POST", re.compile(r"^/api/draft-note$"),                  "api_draft_note"),
    ]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload, content_type="application/json"):
            ...unchanged...

        def _send_bytes(self, code: int, data: bytes, content_type: str,
                        headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _body(self, limit: int = 64 * 1024) -> bytes | None:
            """Read a validated request body, or answer the client and return None.

            CSRF: a cross-origin fetch() sending application/json is preflighted
            and blocked (this server answers no OPTIONS) — but an HTML form with
            enctype="text/plain" is not preflighted, and the classic name/value
            trick makes such a body parse as valid JSON. This whitelist is what
            actually stops it; a form can only send urlencoded, multipart, or
            text/plain, never application/json.
            """
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self._send(415, {"error": "Content-Type must be application/json"})
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                self._send(413, {"error": "body too large"})
                return None
            return self.rfile.read(length)

        def _dispatch(self, method: str) -> None:
            # Reads are guarded too, not just writes: the dashboard renders the
            # owner's job list, drafts and applications, so a rebinding attack
            # that only ever GETs still walks off with all of it.
            if not host_allowed(self.headers.get("Host", ""), port, lan):
                self._send(400, {"error": "invalid host"})
                return
            parsed = urlparse(self.path)
            for verb, pattern, name in ROUTES:
                match = pattern.match(parsed.path)
                if verb == method and match:
                    getattr(self, name)(match, parsed)
                    return
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def page_dashboard(self, match, parsed):
            self._send_bytes(200, (STATIC / "index.html").read_bytes(),
                             "text/html; charset=utf-8")

        def static_asset(self, match, parsed):
            types = {".css": "text/css", ".js": "text/javascript",
                     ".html": "text/html; charset=utf-8"}
            path = STATIC / match.group("name")
            # The regex admits no slash or dot-dot, and resolve() confirms it:
            # a static route is the classic way to read the rest of the disk.
            if path.suffix not in types or not path.resolve().is_relative_to(STATIC.resolve()) \
                    or not path.is_file():
                self._send(404, {"error": "not found"})
                return
            self._send_bytes(200, path.read_bytes(), types[path.suffix])
```

Move the existing `/api/jobs`, `/api/stats`, `/api/status` and `/api/draft-note` bodies into methods named in the table, each taking `(self, match, parsed)`; the two POST handlers begin with `raw = self._body()` / `if raw is None: return` instead of repeating the content-type and length checks.

- [ ] **Step 2: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `parse_status_request` and the rest are untouched.

- [ ] **Step 3: Verify every existing endpoint still answers**

```bash
.venv/bin/jobsearch serve --port 8799 &
sleep 2
curl -s -o /dev/null -w "index %{http_code}\n"  http://127.0.0.1:8799/
curl -s -o /dev/null -w "jobs %{http_code}\n"   "http://127.0.0.1:8799/api/jobs?status=all"
curl -s -o /dev/null -w "stats %{http_code}\n"  http://127.0.0.1:8799/api/stats
curl -s -o /dev/null -w "404 %{http_code}\n"    http://127.0.0.1:8799/nope
curl -s -o /dev/null -w "form-post %{http_code}\n" -X POST -d 'x=1' http://127.0.0.1:8799/api/status
curl -s -o /dev/null -w "bad-host %{http_code}\n" -H 'Host: evil.example.com' http://127.0.0.1:8799/
curl -s -o /dev/null -w "traversal %{http_code}\n" "http://127.0.0.1:8799/static/..%2f..%2fserver.py"
kill %1
```

Expected: `index 200`, `jobs 200`, `stats 200`, `404 404`, `form-post 415`, `bad-host 400`, `traversal 404`.

- [ ] **Step 4: Commit**

```bash
git add jobsearch/server.py
git commit -m "refactor: dispatch server routes from a table"
```

---

### Task 11: Tracker endpoints

**Files:**
- Modify: `jobsearch/server.py`, `jobsearch/tracker.py`

**Interfaces:**
- Consumes: everything from Tasks 2–6.
- Produces: `tracker.parse_edit(raw) -> dict`, `tracker.update_application(conn, application_id, fields) -> dict`, and the routes `/api/stages`, `/api/applications`, `/api/applications/<id>`, `/api/applications/<id>/stage`, `/api/activity`, `/api/tracker-stats`.

- [ ] **Step 1: Add the edit path to `tracker.py`**

```python
EDITABLE = ("cover_letter", "why_company", "salary_expectation", "notice_period")


def parse_edit(raw: str | bytes) -> dict:
    """Validate an edit. Only the submission fields are editable — stage lives in
    the timeline, and applied_at is history."""
    payload = _object(raw)
    unexpected = sorted(set(payload) - set(EDITABLE) - {"cv_file_id", "answers"})
    if unexpected:
        raise ValueError(f"unexpected key(s): {', '.join(unexpected)}")
    limits = {"cover_letter": MAX_TEXT, "why_company": MAX_TEXT,
              "salary_expectation": MAX_SHORT, "notice_period": MAX_SHORT}
    fields = {name: _text(payload, name, limits[name]) for name in EDITABLE if name in payload}
    if "cv_file_id" in payload:
        fields["cv_file_id"] = _int(payload, "cv_file_id", required=False)
    if "answers" in payload:
        fields["answers"] = parse_answers(payload.get("answers"))
    if not fields:
        raise ValueError("nothing to update")
    return fields


def update_application(conn, application_id: int, fields: dict) -> dict:
    assignments = ", ".join(f"{name} = %s" for name in fields)
    values = [json.dumps(v) if k == "answers" and v is not None else v
              for k, v in fields.items()]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE applications SET {assignments}, updated_at = %s WHERE id = %s",
                    [*values, datetime.now(), application_id])
        changed = cur.rowcount
    conn.commit()
    return {"id": application_id, "updated": bool(changed), "fields": sorted(fields)}
```

The f-string builds column names only, every one of them from the `EDITABLE` whitelist `parse_edit` enforces; values stay parameterised.

- [ ] **Step 2: Add the routes**

Extend `ROUTES`:

```python
        ("GET",  re.compile(r"^/applications$"),                       "page_applications"),
        ("GET",  re.compile(r"^/api/stages$"),                         "api_stages"),
        ("GET",  re.compile(r"^/api/applications$"),                   "api_applications"),
        ("GET",  re.compile(r"^/api/applications/(?P<id>\d+)$"),       "api_application"),
        ("GET",  re.compile(r"^/api/activity$"),                       "api_activity"),
        ("GET",  re.compile(r"^/api/tracker-stats$"),                  "api_tracker_stats"),
        ("POST", re.compile(r"^/api/stages$"),                         "post_stage"),
        ("POST", re.compile(r"^/api/applications$"),                   "post_application"),
        ("POST", re.compile(r"^/api/applications/(?P<id>\d+)$"),       "post_application_edit"),
        ("POST", re.compile(r"^/api/applications/(?P<id>\d+)/stage$"), "post_application_stage"),
```

and add the handlers. They follow one shape — connect, call `tracker`, close — with the same `traceback.print_exc` + 400 treatment the existing write path uses for a client error that trips a constraint:

```python
        def _with_conn(self, work, error: str):
            conn = db.connect(settings)
            try:
                payload = work(conn)
            except (ValueError, LookupError) as exc:
                self._send(400, {"error": str(exc)})
                return
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send(400, {"error": error})
                return
            finally:
                conn.close()
            self._send(200, payload)

        def page_applications(self, match, parsed):
            self._send_bytes(200, (STATIC / "applications.html").read_bytes(),
                             "text/html; charset=utf-8")

        def api_stages(self, match, parsed):
            self._with_conn(tracker.list_stages, "could not load stages")

        def api_applications(self, match, parsed):
            kind = parse_qs(parsed.query).get("kind", [None])[0]
            self._with_conn(
                lambda conn: tracker.build_list(tracker.fetch_list(conn, kind=kind),
                                                datetime.now()),
                "could not load applications")

        def api_application(self, match, parsed):
            application_id = int(match.group("id"))

            def work(conn):
                row, events = tracker.fetch_detail(conn, application_id)
                if row is None:
                    raise LookupError(f"no application {application_id}")
                return tracker.build_detail(row, events, datetime.now())

            self._with_conn(work, "could not load the application")

        def api_activity(self, match, parsed):
            limit = parse_qs(parsed.query).get("limit", ["100"])[0]
            self._with_conn(
                lambda conn: tracker.build_activity(
                    tracker.fetch_activity(conn, int(limit) if limit.isdigit() else 100)),
                "could not load activity")

        def api_tracker_stats(self, match, parsed):
            def work(conn):
                applications, events = tracker.fetch_stats_rows(conn)
                return tracker.build_stats(applications, events, datetime.now())

            self._with_conn(work, "could not load statistics")

        def post_stage(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            parsed_stage = tracker.parse_stage_request(raw)
            self._with_conn(lambda conn: tracker.create_stage(conn, parsed_stage),
                            "could not create the stage")

        def post_application(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            request = tracker.parse_application(raw)

            def work(conn):
                job_id = request["job_id"]
                if job_id is None:
                    posting = request["posting"]
                    source = store.source_by_name(conn, "manual")
                    # The URL's digest is the external id, so re-pasting the same
                    # link updates that posting instead of creating a second one.
                    raw_posting = RawPosting(
                        external_id=hashlib.sha256(posting["url"].encode()).hexdigest()[:32],
                        url=posting["url"], title=posting["title"], company=posting["company"],
                        description=posting["description"], location=posting["location"],
                        salary_raw=posting["salary_raw"], posted_at=posting["posted_at"])
                    job_id, _is_new = store.upsert_posting(
                        conn, source, raw_posting, None, settings.rates, datetime.now())
                return tracker.create_application(conn, job_id, request["application"])

            self._with_conn(work, "could not create the application (already tracked?)")

        def post_application_edit(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            fields = tracker.parse_edit(raw)
            application_id = int(match.group("id"))
            self._with_conn(lambda conn: tracker.update_application(conn, application_id, fields),
                            "could not update the application")

        def post_application_stage(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            transition = tracker.parse_transition(raw)
            application_id = int(match.group("id"))
            self._with_conn(lambda conn: tracker.transition(conn, application_id, transition),
                            "could not record the transition")
```

Add the imports `hashlib`, `datetime`, `tracker`, and `RawPosting` at the top of `server.py`. The `parse_*` calls sit outside `_with_conn` so a malformed request never opens a database connection; they raise `ValueError`, which the existing outer handling already turns into a 400 — wrap each `parse_*` in the same `try/except ValueError` the current `/api/status` handler uses.

- [ ] **Step 3: Verify the endpoints end to end**

```bash
.venv/bin/jobsearch serve --port 8799 &
sleep 2
JOB=$(.venv/bin/python -c "
from jobsearch import config, db
c = db.connect(config.load_settings('.env')); cur = c.cursor()
cur.execute('SELECT id FROM jobs ORDER BY id DESC LIMIT 1'); print(cur.fetchone()['id']); c.close()")
post() { curl -s -H 'Content-Type: application/json' -d "$2" "http://127.0.0.1:8799$1"; echo; }

curl -s http://127.0.0.1:8799/api/stages | head -c 200; echo
post /api/applications "{\"job_id\": $JOB, \"applied_at\": \"2026-08-30\", \"cover_letter\": \"Dear team\"}"
STAGE=$(curl -s http://127.0.0.1:8799/api/stages | .venv/bin/python -c "
import json,sys; print(next(s['id'] for s in json.load(sys.stdin) if s['slug']=='tech_interview'))")
post /api/applications/1/stage "{\"stage_id\": $STAGE, \"note\": \"with the CTO\", \"next_action\": \"call\", \"next_action_at\": \"2026-09-05T11:00\"}"
post /api/applications/1 '{"why_company": "payments"}'
post /api/stages '{"label": "Pair programming round", "weight": 45, "kind": "active"}'
curl -s http://127.0.0.1:8799/api/applications | head -c 400; echo
curl -s http://127.0.0.1:8799/api/activity | head -c 300; echo
curl -s http://127.0.0.1:8799/api/tracker-stats | head -c 300; echo
post /api/applications/1/stage '{"stage_id": 999999}'
post /api/stages '{"label": "X", "weight": 500, "kind": "active"}'
kill %1
```

Expected: the create returns an id and stage `applied`; the transition returns `current: true`; the list shows the card at Technical interview with `next_action.due` "later"; activity has two entries; stats show `sent: 1`; the unknown stage id returns 400 `unknown stage id`, and weight 500 returns 400 `weight out of range`.

- [ ] **Step 4: Verify a pasted URL merges into a job already harvested**

The whole reason manual entry goes through `store.upsert_posting` is that pasting a link already in the database must attach to the job that already carries a score and a draft, rather than creating a twin.

```bash
.venv/bin/jobsearch serve --port 8799 &
sleep 2
URL=$(.venv/bin/python -c "
from jobsearch import config, db
c = db.connect(config.load_settings('.env')); cur = c.cursor()
cur.execute('''SELECT js.url, js.job_id FROM job_sources js JOIN jobs j ON j.id=js.job_id
                WHERE j.inactive_at IS NULL ORDER BY js.id DESC LIMIT 1''')
row = cur.fetchone(); print(row['url'], row['job_id']); c.close()")
echo "pasting: $URL"
curl -s -H 'Content-Type: application/json' \
     -d "{\"url\": \"${URL%% *}\", \"title\": \"probe\", \"company\": \"probe\"}" \
     http://127.0.0.1:8799/api/applications; echo
kill %1
```

Expected: the created application's `job_id` is the id printed beside the URL — the same canonical job, not a new one. A different id means the fingerprint did not match; check `dedupe.can_merge`'s gates before moving on, since every later manual entry inherits this behaviour.

- [ ] **Step 5: Remove the probe rows**

```bash
.venv/bin/python -c "
from jobsearch import config, db
c = db.connect(config.load_settings('.env')); cur = c.cursor()
cur.execute('DELETE FROM application_events'); cur.execute('DELETE FROM applications')
cur.execute(\"DELETE FROM stages WHERE builtin = FALSE\")
c.commit(); c.close(); print('probe rows removed')"
```

- [ ] **Step 6: Commit**

```bash
git add jobsearch/server.py jobsearch/tracker.py
git commit -m "feat: serve the application tracker API"
```

---

### Task 12: Upload, download, and URL lookup

**Files:**
- Modify: `jobsearch/server.py`

**Interfaces:**
- Consumes: `cv` (Task 7), `manual.extract` (Task 9).
- Produces: routes `/api/cv`, `/api/cv/<id>`, `/api/cvs`, `/api/lookup-url`.

- [ ] **Step 1: Add the routes**

```python
        ("GET",  re.compile(r"^/api/cvs$"),               "api_cvs"),
        ("GET",  re.compile(r"^/api/cv/(?P<id>\d+)$"),    "api_cv_download"),
        ("POST", re.compile(r"^/api/cv$"),                "post_cv"),
        ("POST", re.compile(r"^/api/lookup-url$"),        "post_lookup_url"),
```

```python
        def api_cvs(self, match, parsed):
            self._with_conn(cv.list_files, "could not list CVs")

        def api_cv_download(self, match, parsed):
            conn = db.connect(settings)
            try:
                row = cv.fetch(conn, int(match.group("id")))
            finally:
                conn.close()
            if row is None:
                self._send(404, {"error": "not found"})
                return
            try:
                data = Path(row["path"]).read_bytes()
            except OSError:
                self._send(404, {"error": "file missing from disk"})
                return
            # attachment + nosniff: the stored bytes came from outside, and this
            # server's origin is the same one the dashboard runs on. A file that
            # renders as HTML here would run as the dashboard.
            self._send_bytes(200, data, row["content_type"], {
                "Content-Disposition": f'attachment; filename="{row["filename"]}"',
                "X-Content-Type-Options": "nosniff",
            })

        def post_cv(self, match, parsed):
            raw = self._body(limit=cv.MAX_BYTES * 2)
            if raw is None:
                return
            try:
                filename, data = cv.parse_upload(raw)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._with_conn(lambda conn: cv.store(conn, filename, data),
                            "could not store the CV")

        def post_lookup_url(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            try:
                payload = json.loads(raw)
                url = payload.get("url")
                if not isinstance(url, str) or not url.strip():
                    raise ValueError("url is required")
                result = manual.extract(url.strip())
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                self._send(400, {"error": f"could not read that page ({type(exc).__name__})"})
                return
            self._send(200, result)
```

Add `from pathlib import Path` (already imported), plus `cv` and `manual` to the `jobsearch` import line. `filename` reaches a header here — `cv.parse_upload` caps its length, and the download route quotes it; strip any `"` or newline from it in `parse_upload` before storing, so a crafted filename cannot inject a header.

- [ ] **Step 2: Harden the filename in `cv.parse_upload`**

```python
    filename = re.sub(r'[\r\n"\\]', "", filename.strip())
    if not filename:
        raise ValueError("filename is required")
```

with `import re`, placed immediately after the length check. Add the matching test to `tests/test_cv.py`:

```python
def test_a_filename_cannot_inject_a_response_header():
    filename, _ = parse_upload(upload(
        filename='cv".pdf\r\nX-Evil: 1', content=base64.b64encode(PDF).decode()))
    assert "\r" not in filename and '"' not in filename
```

- [ ] **Step 3: Verify upload, download and lookup**

```bash
.venv/bin/python -m pytest tests/test_cv.py -q
.venv/bin/jobsearch serve --port 8799 &
sleep 2
B64=$(.venv/bin/python -c "import base64;print(base64.b64encode(open('serhii-drozh-cv.pdf','rb').read()).decode())")
.venv/bin/python -c "
import json;print(json.dumps({'filename':'serhii-drozh-cv.pdf','content':'''$B64'''}))" > /tmp/cv.json
curl -s -H 'Content-Type: application/json' --data-binary @/tmp/cv.json http://127.0.0.1:8799/api/cv; echo
curl -s -D- -o /tmp/out.pdf http://127.0.0.1:8799/api/cv/1 | grep -i "content-disposition\|nosniff\|content-type"
cmp serhii-drozh-cv.pdf /tmp/out.pdf && echo "bytes identical"
curl -s -H 'Content-Type: application/json' -d '{"url":"http://169.254.169.254/latest/meta-data/"}' \
     http://127.0.0.1:8799/api/lookup-url; echo
curl -s -H 'Content-Type: application/json' -d '{"url":"https://boards.greenhouse.io/anthropic/jobs/4020295008"}' \
     http://127.0.0.1:8799/api/lookup-url | head -c 300; echo
kill %1
```

Expected: the upload returns an id; the download carries `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` and `application/pdf`, and the bytes match the original; the metadata address is refused with `non-public address`; the real posting returns a title.

- [ ] **Step 4: Commit**

```bash
git add jobsearch/server.py jobsearch/cv.py tests/test_cv.py
git commit -m "feat: CV upload and download, and URL lookup endpoint"
```

---

### Task 13: Shared assets and the tracker page

**Files:**
- Create: `jobsearch/static/app.css`, `jobsearch/static/util.js`, `jobsearch/static/applications.html`
- Modify: `jobsearch/static/index.html`, `pyproject.toml:26-28`

**Interfaces:**
- Consumes: every `/api/...` route from Tasks 11–12.
- Produces: the page at `/applications`.

- [ ] **Step 1: Extract the shared theme and helpers**

Create `jobsearch/static/app.css` with the `:root` light and dark token blocks, the `*`, `body`, `header`, `h1`, `.controls`, `.card`, `.tag`, `.empty`, `#backdrop` and `#panel` rules **moved verbatim** out of `index.html`'s `<style>`, plus two additions the tracker needs:

```css
.chip{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);
  color:var(--muted);white-space:nowrap}
.chip.k-active{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 45%,transparent);
  background:color-mix(in srgb,var(--accent) 10%,transparent)}
.chip.k-won{color:var(--good);border-color:color-mix(in srgb,var(--good) 50%,transparent);
  background:color-mix(in srgb,var(--good) 14%,transparent);font-weight:650}
.chip.k-lost{opacity:.62}
.due-overdue{color:var(--bad);font-weight:650}
.due-today{color:var(--warn);font-weight:650}
.tabs{display:flex;gap:4px}
.tabs button{background:var(--card);color:var(--muted);border:1px solid var(--line);
  border-radius:7px;padding:6px 12px;font:inherit;cursor:pointer}
.tabs button.on{color:var(--ink);border-color:var(--accent)}
.stats{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin-top:10px}
.stats b{color:var(--ink);font-weight:650}
.nav{margin-left:auto;font-size:13px}
```

Create `jobsearch/static/util.js` with the helpers lifted from `index.html` — `$`, `esc`, `age`, `ageCls`, `safeUrl` — unchanged, plus the two both pages now need:

```js
async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  return res.json();
}

async function postJSON(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
```

In `index.html`: delete the moved rules and helpers, add `<link rel="stylesheet" href="/static/app.css">` and `<script src="/static/util.js"></script>` (the script before the page's own `<script>`), and add a link to the tracker in the header: `<a class="nav" href="/applications">applications →</a>`.

Extend `pyproject.toml`'s package data so the new files ship:

```toml
[tool.setuptools.package-data]
jobsearch = ["static/*.html", "static/*.css", "static/*.js"]
```

- [ ] **Step 2: Verify the inbox is unchanged**

```bash
.venv/bin/jobsearch serve --port 8799 --open
```

Expected: the inbox renders exactly as before — cards, scores, filters, the draft panel, `i`/`a`/`s` triage keys — with an "applications →" link in the header. Check the browser console is clean; a 404 on `/static/app.css` means the static route or the package data is wrong. Stop the server when done.

- [ ] **Step 3: Write the tracker page**

Create `jobsearch/static/applications.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>applications</title>
<link rel="stylesheet" href="/static/app.css">
<style>
main{padding:18px 20px;max-width:1000px;margin:0 auto;display:flex;flex-direction:column;gap:10px}
.row{display:flex;gap:12px;align-items:baseline}
.row .co{font-weight:650}
.row .ti{color:var(--muted);font-size:13.5px}
.right{margin-left:auto;display:flex;gap:8px;align-items:center}
.act{font-size:12.5px;color:var(--muted);margin-top:6px}
form.new{display:none;gap:8px;flex-direction:column;background:var(--card);
  border:1px solid var(--line);border-radius:11px;padding:14px}
form.new.on{display:flex}
form.new input,form.new textarea,form.new select{background:var(--bg);color:var(--ink);
  border:1px solid var(--line);border-radius:7px;padding:7px 9px;font:inherit;width:100%}
form.new textarea{min-height:70px;resize:vertical}
.warn{color:var(--warn);font-size:12.5px}
.tl{border-left:2px solid var(--line);padding-left:12px;margin-top:12px}
.tl .ev{margin-bottom:12px}
.tl .when{font-size:11.5px;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>applications</h1>
  <div class="controls">
    <div class="tabs">
      <button id="tab-list" class="on">List</button>
      <button id="tab-activity">Activity</button>
    </div>
    <select id="kind">
      <option value="">all stages</option>
      <option value="active">active</option>
      <option value="won">won</option>
      <option value="lost">lost</option>
    </select>
    <label><input type="checkbox" id="attention"> needs attention</label>
    <input type="search" id="q" placeholder="company or title">
    <button id="add">+ add by URL</button>
    <a class="nav" href="/">← inbox</a>
  </div>
  <div class="stats" id="stats"></div>
</header>

<main>
  <form class="new" id="new">
    <input id="u-url" placeholder="https://company.com/careers/backend-engineer">
    <div class="row"><button type="button" id="u-look">look up</button>
      <span class="warn" id="u-warn"></span></div>
    <input id="u-title" placeholder="title"><input id="u-company" placeholder="company">
    <input id="u-applied" type="date"><textarea id="u-cover" placeholder="cover letter"></textarea>
    <textarea id="u-why" placeholder="why this company"></textarea>
    <div class="row"><select id="u-cv"></select>
      <input type="file" id="u-file" accept=".pdf,.docx,.txt"></div>
    <div class="row"><button type="button" id="u-save">save application</button>
      <span class="warn" id="u-err"></span></div>
  </form>
  <div id="list"></div>
</main>

<div id="backdrop"></div>
<aside id="panel"><div class="p-head"><div class="t" id="p-title"></div>
  <button id="p-close">close</button></div>
  <div class="p-body" id="p-body"></div></aside>

<script src="/static/util.js"></script>
<script>
let APPS = [], STAGES = [], CVS = [], VIEW = "list", OPEN = null;

const fmtDate = s => s ? String(s).slice(0, 10) : "";

async function boot() {
  [STAGES, CVS] = await Promise.all([getJSON("/api/stages"), getJSON("/api/cvs")]);
  $("#u-cv").innerHTML = '<option value="">no CV</option>' +
    CVS.map(c => `<option value="${c.id}">${esc(c.filename)}</option>`).join("");
  $("#u-applied").value = new Date().toISOString().slice(0, 10);
  await refresh();
}

async function refresh() {
  const kind = $("#kind").value;
  const [apps, stats] = await Promise.all([
    getJSON("/api/applications" + (kind ? "?kind=" + kind : "")),
    getJSON("/api/tracker-stats")]);
  APPS = apps;
  renderStats(stats);
  VIEW === "list" ? renderList() : renderActivity();
}

function renderStats(stats) {
  const cell = (name, label) => {
    const s = stats[name];
    const rate = s.response_rate === null ? "—" : Math.round(s.response_rate * 100) + "%";
    return `<span>${label}: <b>${s.sent}</b> sent · <b>${s.advanced}</b> advanced ·
            <b>${s.offers}</b> offers · <b>${s.lost}</b> lost · replied <b>${rate}</b></span>`;
  };
  $("#stats").innerHTML = [
    cell("previous_day", "yesterday"), cell("current_week", "this week"),
    cell("previous_week", "last week"), cell("current_month", "this month"),
    cell("previous_month", "last month")].join("");
}

function visible() {
  const q = $("#q").value.toLowerCase().trim();
  const attention = $("#attention").checked;
  return APPS.filter(a =>
    (!q || (a.company + " " + a.title).toLowerCase().includes(q)) &&
    (!attention || (a.next_action && a.next_action.due !== "later")));
}

function renderList() {
  const rows = visible();
  $("#list").innerHTML = rows.length ? rows.map(card).join("")
    : '<div class="empty">No applications yet. Add one with “+ add by URL”, or mark a job applied in the inbox.</div>';
  $("#list").querySelectorAll("article.card").forEach(el =>
    el.addEventListener("click", () => openPanel(Number(el.dataset.id))));
}

function card(a) {
  const action = a.next_action ? `<div class="act due-${a.next_action.due}">
      ${esc(a.next_action.text || "next step")}${a.next_action.at ? " · " + fmtDate(a.next_action.at) : ""}
      ${a.next_action.due === "overdue" ? "· overdue" : a.next_action.due === "today" ? "· today" : ""}
    </div>` : "";
  return `<article class="card" data-id="${a.id}">
    <div class="row"><span class="co">${esc(a.company)}</span>
      <span class="ti">${esc(a.title)}</span>
      <span class="right">
        ${a.score !== null && a.score !== undefined ? `<span class="tag">score ${a.score}</span>` : ""}
        ${a.cv ? `<a class="tag" href="/api/cv/${a.cv.id}">${esc(a.cv.filename)}</a>` : ""}
        <span class="chip k-${a.stage.kind}">${esc(a.stage.label)}</span>
      </span></div>
    <div class="act">${a.days_in_stage}d in stage · applied ${fmtDate(a.applied_at)}
      ${a.url ? ` · <a href="${safeUrl(a.url)}" target="_blank" rel="noopener">posting</a>` : ""}</div>
    ${action}</article>`;
}

async function renderActivity() {
  const events = await getJSON("/api/activity?limit=100");
  $("#list").innerHTML = events.map(e => `<article class="card">
    <div class="row"><span class="co">${esc(e.company)}</span>
      <span class="ti">${esc(e.title)}</span>
      <span class="right"><span class="when">${fmtDate(e.occurred_at)}</span></span></div>
    <div class="act">${e.kind === "applied" ? "applied" : "moved to " + esc(e.stage || "")}
      ${e.back_dated ? " · recorded later" : ""}${e.note ? " — " + esc(e.note) : ""}</div>
  </article>`).join("") || '<div class="empty">Nothing recorded yet.</div>';
}

function stageOptions(current) {
  const group = kind => STAGES.filter(s => s.kind === kind)
    .map(s => `<option value="${s.id}" ${s.id === current ? "selected" : ""}>${esc(s.label)}</option>`).join("");
  return `<optgroup label="active">${group("active")}</optgroup>
          <optgroup label="won">${group("won")}</optgroup>
          <optgroup label="lost">${group("lost")}</optgroup>`;
}

async function openPanel(id) {
  const a = await getJSON("/api/applications/" + id);
  OPEN = id;
  $("#p-title").textContent = `${a.company} — ${a.title}`;
  $("#p-body").innerHTML = `
    <div class="row"><span class="chip k-${a.stage.kind}">${esc(a.stage.label)}</span>
      ${a.cv ? `<a class="tag" href="/api/cv/${a.cv.id}">download CV</a>` : ""}</div>
    <div class="act">applied ${fmtDate(a.applied_at)}</div>
    <h3>Move to</h3>
    <select id="t-stage">${stageOptions(null)}</select>
    <input id="t-when" type="date" value="${new Date().toISOString().slice(0,10)}">
    <textarea id="t-note" placeholder="what happened"></textarea>
    <input id="t-action" placeholder="next action">
    <input id="t-action-at" type="date">
    <div class="row"><button id="t-save">record</button><span class="warn" id="t-err"></span></div>
    ${a.texts.cover_letter ? `<h3>Cover letter</h3><p>${esc(a.texts.cover_letter)}</p>` : ""}
    ${a.texts.why_company ? `<h3>Why this company</h3><p>${esc(a.texts.why_company)}</p>` : ""}
    ${(a.answers || []).map(x => `<h3>${esc(x.question)}</h3><p>${esc(x.answer)}</p>`).join("")}
    <h3>Timeline</h3>
    <div class="tl">${a.timeline.map(e => `<div class="ev">
      <div class="when">${fmtDate(e.occurred_at)}${e.back_dated ? " · recorded later" : ""}</div>
      <div>${e.kind === "applied" ? "applied" : "moved to " + esc(e.stage || "")}</div>
      ${e.note ? `<div class="act">${esc(e.note)}</div>` : ""}
      ${e.next_action ? `<div class="act">next: ${esc(e.next_action)} ${fmtDate(e.next_action_at)}</div>` : ""}
    </div>`).join("")}</div>`;
  $("#t-save").addEventListener("click", saveTransition);
  $("#panel").classList.add("open");
  $("#backdrop").classList.add("on");
}

async function saveTransition() {
  try {
    await postJSON(`/api/applications/${OPEN}/stage`, {
      stage_id: Number($("#t-stage").value),
      occurred_at: $("#t-when").value || undefined,
      note: $("#t-note").value || undefined,
      next_action: $("#t-action").value || undefined,
      next_action_at: $("#t-action-at").value || undefined,
    });
    closePanel();
    await refresh();
  } catch (err) { $("#t-err").textContent = err.message; }
}

function closePanel() {
  $("#panel").classList.remove("open");
  $("#backdrop").classList.remove("on");
  OPEN = null;
}

async function lookup() {
  $("#u-warn").textContent = "";
  try {
    const found = await postJSON("/api/lookup-url", {url: $("#u-url").value});
    $("#u-title").value = found.title || "";
    $("#u-company").value = found.company || "";
    window.LOOKED_UP = found;
    if (found.needs_review)
      $("#u-warn").textContent = "guessed from the page — check the title and company";
  } catch (err) { $("#u-warn").textContent = err.message; }
}

async function saveApplication() {
  $("#u-err").textContent = "";
  try {
    let cvId = $("#u-cv").value ? Number($("#u-cv").value) : undefined;
    const file = $("#u-file").files[0];
    if (file) {
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      cvId = (await postJSON("/api/cv", {filename: file.name, content})).id;
    }
    const found = window.LOOKED_UP || {};
    await postJSON("/api/applications", {
      url: $("#u-url").value,
      title: $("#u-title").value,
      company: $("#u-company").value,
      description: found.description || "",
      location: found.location || undefined,
      salary_raw: found.salary_raw || undefined,
      applied_at: $("#u-applied").value || undefined,
      cover_letter: $("#u-cover").value || undefined,
      why_company: $("#u-why").value || undefined,
      cv_file_id: cvId,
    });
    $("#new").classList.remove("on");
    await boot();
  } catch (err) { $("#u-err").textContent = err.message; }
}

function switchView(view) {
  VIEW = view;
  $("#tab-list").classList.toggle("on", view === "list");
  $("#tab-activity").classList.toggle("on", view === "activity");
  refresh();
}

$("#tab-list").addEventListener("click", () => switchView("list"));
$("#tab-activity").addEventListener("click", () => switchView("activity"));
$("#kind").addEventListener("change", refresh);
["#q", "#attention"].forEach(s => $(s).addEventListener("input", renderList));
$("#add").addEventListener("click", () => $("#new").classList.toggle("on"));
$("#u-look").addEventListener("click", lookup);
$("#u-save").addEventListener("click", saveApplication);
$("#p-close").addEventListener("click", closePanel);
$("#backdrop").addEventListener("click", closePanel);
boot();
</script>
</body>
</html>
```

- [ ] **Step 4: Verify the page against the real database**

```bash
.venv/bin/jobsearch serve --port 8799 --open
```

Walk it: open `/applications`; add an application by pasting a real posting URL (look up, check the prefill, attach the CV, save); confirm it appears with stage **Applied**; open it, move it to **Technical interview** with a note and a next action dated in the future; confirm the card shows "later", the stats strip counts it under this week, and Activity lists both events newest-first. Then move it to **Declined by them** and confirm it sinks below every active row. Download the CV from the card and confirm it opens.

Expected: all of the above, with a clean browser console. Stop the server, then remove the probe rows as in Task 11 Step 4 if you used a throwaway posting.

- [ ] **Step 5: Commit**

```bash
git add jobsearch/static pyproject.toml
git commit -m "feat: the applications page, on shared styles and helpers"
```

---

### Task 14: CLI and documentation

**Files:**
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`, `docs/superpowers/specs/2026-08-25-job-search-design.md`

**Interfaces:**
- Consumes: `manual.extract`, `tracker.create_application`, `tracker.fetch_list`, `tracker.build_list`.
- Produces: `jobsearch apply --url <url> [--applied-at]`, `jobsearch applications [--kind]`.

- [ ] **Step 1: Add the commands**

In `jobsearch/cli.py`, inside `build_parser`:

```python
    apply_parser = sub.add_parser("apply", help="record an application from a posting URL")
    apply_parser.add_argument("--url", required=True)
    apply_parser.add_argument("--applied-at", dest="applied_at")

    apps_parser = sub.add_parser("applications", help="list tracked applications")
    apps_parser.add_argument("--kind", choices=["active", "won", "lost"])
```

and the two dispatch blocks matching the file's existing shape:

```python
    if args.command == "apply":
        from jobsearch import commands

        emit(commands.apply_url(args))
        return 0

    if args.command == "applications":
        from jobsearch import commands

        emit(commands.applications(args))
        return 0
```

In `jobsearch/commands.py`:

```python
def apply_url(args) -> dict:
    """Look a posting up, store it as a manual job, and open an application on it."""
    import hashlib
    from datetime import datetime

    from jobsearch import manual, store, tracker
    from jobsearch.models import RawPosting

    settings = load_settings(args.env)
    found = manual.extract(args.url)
    if not found.get("title") or not found.get("company"):
        raise SystemExit(f"could not read a title and company from {args.url} — "
                         "use the dashboard, which lets you correct them")
    conn = db.connect(settings)
    try:
        source = store.source_by_name(conn, "manual")
        posting = RawPosting(
            external_id=hashlib.sha256(found["url"].encode()).hexdigest()[:32],
            url=found["url"], title=found["title"], company=found["company"],
            description=found.get("description") or "", location=found.get("location"),
            salary_raw=found.get("salary_raw"), posted_at=found.get("posted_at"))
        job_id, _is_new = store.upsert_posting(
            conn, source, posting, None, settings.rates, datetime.now())
        applied_at = (tracker.parse_when(args.applied_at, "applied_at")
                      if args.applied_at else datetime.now())
        result = tracker.create_application(conn, job_id, {"applied_at": applied_at})
    finally:
        conn.close()
    return {"command": "apply", "needs_review": found.get("needs_review"), **result}


def applications(args) -> dict:
    from datetime import datetime

    from jobsearch import tracker

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        rows = tracker.fetch_list(conn, kind=args.kind)
        return {"command": "applications",
                "applications": tracker.build_list(rows, datetime.now())}
    finally:
        conn.close()
```

- [ ] **Step 2: Verify both commands**

```bash
.venv/bin/jobsearch apply --url https://boards.greenhouse.io/anthropic/jobs/4020295008 --applied-at 2026-08-30
.venv/bin/jobsearch applications
.venv/bin/jobsearch applications --kind lost
```

Expected: `apply` emits an application id and stage `applied`; `applications` lists it with company, title and stage; the `lost` filter returns an empty list. Remove the probe row afterwards if the posting was not a real application.

- [ ] **Step 3: Document the two commands in the v1 spec**

In `docs/superpowers/specs/2026-08-25-job-search-design.md`, under `## CLI surface`, add:

```
apply --url <url> [--applied-at YYYY-MM-DD]   record an application from a posting URL
applications [--kind active|won|lost]         list tracked applications
```

- [ ] **Step 4: Run everything one last time**

```bash
.venv/bin/python -m pytest -q
.venv/bin/jobsearch sources
.venv/bin/jobsearch filter
```

Expected: the whole suite passes; `sources` lists `manual` as disabled; `filter` runs without touching any job that has an application.

- [ ] **Step 5: Commit**

```bash
git add jobsearch/cli.py jobsearch/commands.py docs/superpowers/specs/2026-08-25-job-search-design.md
git commit -m "feat: apply and applications commands"
```
