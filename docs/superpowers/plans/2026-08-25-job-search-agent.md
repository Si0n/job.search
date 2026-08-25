# Job Search Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect job postings from five job sites, score them against the owner's profile, store them in MySQL, and push good matches to Telegram for phone triage.

**Architecture:** A Python CLI owns every deterministic stage (fetch → parse → normalize → dedupe → store → filter → notify), each stage a separate re-runnable command emitting JSON. Claude does only the two things that need judgment — scoring postings and repairing broken parsers — invoked through a nightly `claude -p "/harvest"` cron entry. A small always-on Telegram bot writes triage decisions back.

**Tech Stack:** Python 3.13, MySQL 8, `httpx`, `pymysql`, `beautifulsoup4` + `lxml`, `PyYAML`, `pytest`, stdlib `argparse`.

**Spec:** `docs/superpowers/specs/2026-08-25-job-search-design.md`

## Global Constraints

- **Python never judges, Claude never parses HTML on the happy path.** Anything derivable from a column — salary vs. floor, arrangement, employment type, language, excluded company or keyword — is decided in Python.
- **Every stage is re-runnable and derives its work from data, not from a lifecycle flag.** There is no `state` column anywhere.
- **Missing data never fails a filter.** `salary_source = absent` passes the salary floor. Absent is not a violation, in any filter.
- **Canonical entrypoint is the installed console script `jobsearch`.** `python -m jobsearch` is the non-installed fallback. Use `jobsearch` in all docs, skills, and cron entries.
- **All CLI commands print JSON to stdout.** Diagnostics go to stderr. Claude consumes stdout without parsing prose.
- **The app connects as MySQL user `job_search`, never `root`.** Credentials live only in `.env`, which is gitignored.
- **No ORM, no Alembic, no async framework.** Numbered `.sql` files applied by `db.py`; `argparse` for the CLI; raw Bot API over `httpx` for Telegram.
- **Tests are scoped to seven areas only** (normalize, salary, dedupe, filters, adapter parse, ParseResult classification, notification selection). No coverage target. No tests for CLI plumbing or DB round-trips.
- **Currency conversion never happens in the database.** `salary_monthly_eur` is derived at write time from a static rate table and is comparison-only.
- **Commit after every task.** Conventional commit prefixes (`feat:`, `test:`, `chore:`).

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | deps, `console_scripts: jobsearch = jobsearch.cli:main` |
| `jobsearch/__main__.py` | `python -m jobsearch` → `cli.main` |
| `jobsearch/cli.py` | argparse subcommands, JSON output. No logic. |
| `jobsearch/config.py` | `.env` → `Settings`, `profile.yaml` → `Profile`, currency rates |
| `jobsearch/db.py` | connection, migration runner |
| `jobsearch/models.py` | `RawFetch`, `RawPosting`, `ParseResult` — no behavior |
| `jobsearch/normalize.py` | company/title/location normalization, arrangement + employment detection |
| `jobsearch/salary.py` | compensation string → `Salary`, and EUR-monthly derivation |
| `jobsearch/dedupe.py` | fingerprint, Jaccard, the four merge conditions |
| `jobsearch/store.py` | all SQL: runs, raw_fetches, job_sources, jobs, scores, notifications |
| `jobsearch/filters.py` | hard-rule evaluation → `FilterVerdict` |
| `jobsearch/harvest.py` | orchestrates a harvest run across sources |
| `jobsearch/sweep.py` | activity/disappearance model |
| `jobsearch/scoring.py` | queue selection and score persistence |
| `jobsearch/notify.py` | unsent selection + insert-then-send protocol |
| `jobsearch/bot.py` | long-poll loop, callback → `applications` |
| `jobsearch/adapters/base.py` | `Adapter` ABC, `HtmlAdapter`, `JsonAdapter` |
| `jobsearch/adapters/{djinni,dou,remoteok,weworkremotely}.py` | per-source fetch + field mapping |
| `jobsearch/adapters/registry.py` | name → adapter instance |
| `migrations/001_init.sql` | full schema |
| `.claude/skills/{harvest,harvest-linkedin,review}/SKILL.md` | Claude-side orchestration |

The split that matters: `normalize`, `salary`, `dedupe`, and `filters` are **pure functions with no database and no I/O**. That is what makes the seven test areas cheap, and it is why they are separate modules rather than methods on a store class.

`HtmlAdapter` and `JsonAdapter` carry the generic selector-driven parse. Individual source modules only build a URL and post-process fields, so adding a source is roughly forty lines.

---

### Task 1: Package skeleton, settings, CLI entrypoint

**Files:**
- Create: `pyproject.toml`, `jobsearch/__init__.py`, `jobsearch/__main__.py`, `jobsearch/cli.py`, `jobsearch/config.py`, `.env.example`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `config.Settings` frozen dataclass: `db_host: str`, `db_port: int`, `db_name: str`, `db_user: str`, `db_password: str`, `telegram_token: str`, `telegram_chat_id: str`, `rates: dict[str, float]`
  - `config.load_settings(env_path: str = ".env") -> Settings`
  - `cli.main(argv: list[str] | None = None) -> int`
  - `cli.emit(payload: object) -> None` — the single JSON-to-stdout helper every command uses

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "jobsearch"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.27",
    "pymysql>=1.1",
    "beautifulsoup4>=4.12",
    "lxml>=5.2",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
jobsearch = "jobsearch.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["jobsearch*"]
```

- [ ] **Step 2: Write `jobsearch/config.py`**

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Static comparison rates. Not truth — only used to derive salary_monthly_eur
# so a floor comparison can happen across currencies. Edit when badly stale.
DEFAULT_RATES: dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
    "PLN": 0.23,
    "UAH": 0.022,
    "CHF": 1.05,
}


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    telegram_token: str
    telegram_chat_id: str
    rates: dict[str, float]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(env_path: str = ".env") -> Settings:
    file_values = _read_env_file(Path(env_path))

    def get(key: str, default: str | None = None) -> str:
        value = os.environ.get(key, file_values.get(key, default))
        if value is None:
            raise RuntimeError(f"Missing required setting: {key}")
        return value

    return Settings(
        db_host=get("DB_HOST", "127.0.0.1"),
        db_port=int(get("DB_PORT", "3306")),
        db_name=get("DB_NAME", "job_search"),
        db_user=get("DB_USER"),
        db_password=get("DB_PASSWORD"),
        telegram_token=get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=get("TELEGRAM_CHAT_ID", ""),
        rates=dict(DEFAULT_RATES),
    )
```

Environment variables win over `.env` so a cron entry can override without editing files.

- [ ] **Step 3: Write `jobsearch/cli.py` with the subcommand skeleton**

Every command lands here and delegates. This step registers only `init-db` as a stub so the wiring is provable; later tasks add their own subparser and handler.

```python
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def emit(payload: Any) -> None:
    """The only path from a command to stdout. Always JSON, never prose."""
    json.dump(payload, sys.stdout, ensure_ascii=False, default=str, indent=2)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobsearch")
    parser.add_argument("--env", default=".env", help="path to the env file")
    parser.add_argument("--profile", default="profile.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the schema and apply migrations")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-db":
        from jobsearch import commands

        emit(commands.init_db(args))
        return 0

    parser.error(f"unhandled command: {args.command}")
    return 2
```

- [ ] **Step 4: Write `jobsearch/__main__.py`**

```python
import sys

from jobsearch.cli import main

sys.exit(main())
```

- [ ] **Step 5: Write `.env.example` and extend `.gitignore`**

```bash
cat > .env.example <<'EOF'
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=job_search
DB_USER=job_search
DB_PASSWORD=
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
EOF

printf '%s\n' 'reports/' >> .gitignore
```

- [ ] **Step 6: Install the package into the existing venv and verify the entrypoint**

Run:
```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/jobsearch --help
```
Expected: usage text listing `init-db`. The console script resolves.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml jobsearch/ .env.example .gitignore
git commit -m "feat: package skeleton, settings loader, CLI entrypoint"
```

---

### Task 2: Database schema and migration runner

**Files:**
- Create: `migrations/001_init.sql`, `jobsearch/db.py`, `jobsearch/commands.py`
- Test: none (DB round-trips are explicitly out of test scope)

**Interfaces:**
- Consumes: `config.Settings`
- Produces:
  - `db.connect(settings: Settings) -> pymysql.Connection` — `DictCursor`, `autocommit=False`
  - `db.migrate(conn, migrations_dir: str = "migrations") -> list[str]` — returns applied filenames
  - `commands.init_db(args) -> dict`

- [ ] **Step 1: Create the database and the dedicated user**

Run (root password is the one already supplied; substitute a fresh password for `job_search`):
```bash
mysql -uroot -p"$MYSQL_ROOT_PASSWORD" <<'SQL'
CREATE DATABASE IF NOT EXISTS job_search
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'job_search'@'localhost'
  IDENTIFIED BY 'REPLACE_WITH_GENERATED_PASSWORD';
GRANT ALL PRIVILEGES ON `job_search`.* TO 'job_search'@'localhost';
FLUSH PRIVILEGES;
SQL
```

Then write the generated password into `.env` as `DB_PASSWORD`. Verify:
```bash
mysql -ujob_search -p'<generated>' job_search -e "SELECT 1;"
```
Expected: returns `1`. Confirm the user cannot reach other schemas:
```bash
mysql -ujob_search -p'<generated>' -e "SHOW DATABASES;"
```
Expected: only `job_search` and `information_schema`.

- [ ] **Step 2: Write `migrations/001_init.sql`**

```sql
CREATE TABLE schema_migrations (
  filename   VARCHAR(255) NOT NULL PRIMARY KEY,
  applied_at DATETIME     NOT NULL
) ENGINE=InnoDB;

CREATE TABLE sources (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  name              VARCHAR(64)  NOT NULL UNIQUE,
  enabled           BOOLEAN      NOT NULL DEFAULT TRUE,
  fetch_mode        ENUM('http-json','http-html','browser') NOT NULL,
  priority          TINYINT      NOT NULL DEFAULT 50,
  base_url          VARCHAR(512) NOT NULL,
  query             JSON         NULL,
  selectors         JSON         NULL,
  status            ENUM('ok','degraded') NOT NULL DEFAULT 'ok',
  consecutive_empty INT          NOT NULL DEFAULT 0,
  last_ok_at        DATETIME     NULL,
  last_run_at       DATETIME     NULL
) ENGINE=InnoDB;

CREATE TABLE runs (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  kind        ENUM('harvest','score') NOT NULL,
  source_id   INT      NULL,
  started_at  DATETIME NOT NULL,
  finished_at DATETIME NULL,
  fetched     INT      NULL,
  new         INT      NULL,
  error       TEXT     NULL,
  CONSTRAINT fk_runs_source FOREIGN KEY (source_id) REFERENCES sources(id),
  INDEX idx_runs_kind_started (kind, started_at)
) ENGINE=InnoDB;

CREATE TABLE raw_fetches (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  source_id     INT          NOT NULL,
  run_id        INT          NOT NULL,
  path          VARCHAR(512) NOT NULL,
  content_hash  CHAR(64)     NOT NULL,
  http_status   SMALLINT     NOT NULL,
  etag          VARCHAR(255) NULL,
  last_modified VARCHAR(255) NULL,
  fetched_at    DATETIME     NOT NULL,
  CONSTRAINT fk_raw_source FOREIGN KEY (source_id) REFERENCES sources(id),
  CONSTRAINT fk_raw_run    FOREIGN KEY (run_id)    REFERENCES runs(id),
  INDEX idx_raw_source_fetched (source_id, fetched_at)
) ENGINE=InnoDB;

CREATE TABLE jobs (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  fingerprint        CHAR(64)     NOT NULL,
  title              VARCHAR(255) NOT NULL,
  company            VARCHAR(255) NOT NULL,
  location           VARCHAR(255) NULL,
  arrangement        ENUM('remote','hybrid','onsite','unknown') NOT NULL DEFAULT 'unknown',
  employment_type    ENUM('full-time','part-time','contract','internship','unknown') NOT NULL DEFAULT 'unknown',
  salary_min         INT          NULL,
  salary_max         INT          NULL,
  salary_currency    CHAR(3)      NULL,
  salary_period      ENUM('hour','day','month','year') NULL,
  salary_type        ENUM('employee','contractor','unknown') NULL,
  salary_source      ENUM('posting','inferred','absent') NOT NULL DEFAULT 'absent',
  salary_monthly_eur INT          NULL,
  first_seen_at      DATETIME     NOT NULL,
  last_seen_at       DATETIME     NOT NULL,
  inactive_at        DATETIME     NULL,
  filtered_at        DATETIME     NULL,
  filter_reason      VARCHAR(255) NULL,
  latest_score_id    INT          NULL,
  INDEX idx_jobs_fingerprint (fingerprint),
  INDEX idx_jobs_latest_score (latest_score_id),
  INDEX idx_jobs_triage (inactive_at, filtered_at, last_seen_at)
) ENGINE=InnoDB;

CREATE TABLE job_sources (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  job_id           INT          NOT NULL,
  source_id        INT          NOT NULL,
  external_id      VARCHAR(255) NOT NULL,
  url              VARCHAR(1024) NOT NULL,
  raw_fetch_id     INT          NULL,
  description      MEDIUMTEXT   NULL,
  description_hash CHAR(64)     NULL,
  salary_raw       VARCHAR(255) NULL,
  posted_at        DATETIME     NULL,
  first_seen_at    DATETIME     NOT NULL,
  last_seen_at     DATETIME     NOT NULL,
  inactive_at      DATETIME     NULL,
  missed_runs      INT          NOT NULL DEFAULT 0,
  source_meta      JSON         NULL,
  UNIQUE KEY uq_source_external (source_id, external_id),
  CONSTRAINT fk_js_job    FOREIGN KEY (job_id)       REFERENCES jobs(id),
  CONSTRAINT fk_js_source FOREIGN KEY (source_id)    REFERENCES sources(id),
  CONSTRAINT fk_js_raw    FOREIGN KEY (raw_fetch_id) REFERENCES raw_fetches(id),
  INDEX idx_js_job (job_id)
) ENGINE=InnoDB;

CREATE TABLE scores (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  job_id          INT      NOT NULL,
  run_id          INT      NOT NULL,
  `pass`          TINYINT  NOT NULL,
  score           TINYINT  NOT NULL,
  dimensions      JSON     NULL,
  red_flag_penalty TINYINT NOT NULL DEFAULT 0,
  hard_concerns   JSON     NULL,
  strengths       JSON     NULL,
  weaknesses      JSON     NULL,
  verdict         TEXT     NULL,
  profile_version INT      NOT NULL,
  profile_hash    CHAR(64) NOT NULL,
  scored_at       DATETIME NOT NULL,
  CONSTRAINT fk_scores_job FOREIGN KEY (job_id) REFERENCES jobs(id),
  CONSTRAINT fk_scores_run FOREIGN KEY (run_id) REFERENCES runs(id),
  INDEX idx_scores_job_pass (job_id, `pass`, profile_hash)
) ENGINE=InnoDB;

CREATE TABLE notifications (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  job_id              INT         NOT NULL,
  channel             VARCHAR(32) NOT NULL,
  chat_id             VARCHAR(64) NOT NULL,
  telegram_message_id BIGINT      NULL,
  score_id            INT         NULL,
  queued_at           DATETIME    NOT NULL,
  sent_at             DATETIME    NULL,
  UNIQUE KEY uq_job_channel (job_id, channel),
  CONSTRAINT fk_notif_job   FOREIGN KEY (job_id)   REFERENCES jobs(id),
  CONSTRAINT fk_notif_score FOREIGN KEY (score_id) REFERENCES scores(id),
  INDEX idx_notif_message (telegram_message_id)
) ENGINE=InnoDB;

CREATE TABLE applications (
  job_id     INT NOT NULL PRIMARY KEY,
  status     ENUM('interested','skipped','applied','replied','rejected','interviewing','offer') NOT NULL,
  note       TEXT     NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT fk_app_job FOREIGN KEY (job_id) REFERENCES jobs(id)
) ENGINE=InnoDB;

INSERT INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('djinni',         TRUE, 'http-html', 10, 'https://djinni.co'),
  ('dou',            TRUE, 'http-html', 20, 'https://jobs.dou.ua'),
  ('remoteok',       TRUE, 'http-json', 30, 'https://remoteok.com'),
  ('weworkremotely', TRUE, 'http-json', 40, 'https://weworkremotely.com'),
  ('linkedin',       TRUE, 'browser',    5, 'https://www.linkedin.com');
```

`latest_score_id` is a plain indexed column with no foreign key — a real constraint would make `jobs` and `scores` circularly dependent. `missed_runs` on `job_sources` backs the activity model in Task 16.

- [ ] **Step 3: Write `jobsearch/db.py`**

```python
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
```

Splitting on `;` is adequate because the migrations contain no stored routines or triggers. If one is ever added, this runner needs a delimiter-aware split — keep routines out of migrations instead.

- [ ] **Step 4: Write `jobsearch/commands.py` with `init_db`**

```python
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
```

- [ ] **Step 5: Run the migration and verify the schema**

Run:
```bash
.venv/bin/jobsearch init-db
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "SHOW TABLES; SELECT name, fetch_mode, priority FROM sources;"
```
Expected: `init-db` reports `["001_init.sql"]`; nine tables listed; five seeded sources with `linkedin` at `fetch_mode=browser`.

Run it a second time. Expected: `{"applied": [], "count": 0}` — migrations are idempotent.

- [ ] **Step 6: Commit**

```bash
git add migrations/ jobsearch/db.py jobsearch/commands.py
git commit -m "feat: schema and migration runner"
```

---

### Task 3: Profile loading, hashing, and validation

**Files:**
- Create: `jobsearch/profile.py`
- Modify: `jobsearch/cli.py` (add `profile` subcommand)
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `profile.Profile` frozen dataclass: `version: int`, `hash: str`, `data: dict`, `weights: dict[str, int]`, `filters: dict`
  - `profile.load_profile(path: str = "profile.yaml") -> Profile`
  - `profile.compute_hash(data: dict) -> str` — sha256 over the canonically serialized profile

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_profile.py
import pytest

from jobsearch.profile import compute_hash, validate_weights


def test_hash_ignores_key_order_and_formatting():
    a = {"skills": {"expert": ["php", "vue"]}, "version": 1}
    b = {"version": 1, "skills": {"expert": ["php", "vue"]}}
    assert compute_hash(a) == compute_hash(b)


def test_hash_changes_when_a_value_changes():
    a = {"version": 1, "preferences": {"salary": {"min": 4000}}}
    b = {"version": 1, "preferences": {"salary": {"min": 5000}}}
    assert compute_hash(a) != compute_hash(b)


def test_weights_must_sum_to_100():
    with pytest.raises(ValueError, match="sum to 100"):
        validate_weights({"technical_fit": 30, "seniority_fit": 15})


def test_weights_reject_unknown_dimension():
    weights = {
        "technical_fit": 20, "seniority_fit": 15, "compensation_fit": 15,
        "arrangement_fit": 10, "domain_fit": 10, "company_fit": 10,
        "growth_potential": 10, "vibes": 10,
    }
    with pytest.raises(ValueError, match="vibes"):
        validate_weights(weights)


def test_weights_reject_red_flags_as_a_dimension():
    weights = {
        "technical_fit": 25, "seniority_fit": 15, "compensation_fit": 15,
        "arrangement_fit": 10, "domain_fit": 10, "company_fit": 10,
        "growth_potential": 10, "red_flags": 5,
    }
    with pytest.raises(ValueError, match="red_flags"):
        validate_weights(weights)
```

The last test encodes a design decision from the spec: red flags are a penalty applied to the final score, never a positively-weighted dimension.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_profile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.profile'`

- [ ] **Step 3: Write `jobsearch/profile.py`**

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

DIMENSIONS = (
    "technical_fit",
    "seniority_fit",
    "compensation_fit",
    "arrangement_fit",
    "domain_fit",
    "company_fit",
    "growth_potential",
)


@dataclass(frozen=True)
class Profile:
    version: int
    hash: str
    data: dict
    weights: dict[str, int]
    filters: dict


def compute_hash(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_weights(weights: dict[str, int]) -> dict[str, int]:
    if "red_flags" in weights:
        raise ValueError(
            "red_flags is a penalty applied to the final score, not a weighted dimension"
        )
    unknown = sorted(set(weights) - set(DIMENSIONS))
    if unknown:
        raise ValueError(f"unknown scoring dimension(s): {', '.join(unknown)}")
    missing = sorted(set(DIMENSIONS) - set(weights))
    if missing:
        raise ValueError(f"missing scoring dimension(s): {', '.join(missing)}")
    total = sum(weights.values())
    if total != 100:
        raise ValueError(f"weights must sum to 100, got {total}")
    return dict(weights)


def load_profile(path: str = "profile.yaml") -> Profile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Profile(
        version=int(data["version"]),
        hash=compute_hash(data),
        data=data,
        weights=validate_weights(data["weights"]),
        filters=dict(data.get("filters") or {}),
    )
```

`compute_hash` runs over the whole profile, so any edit — a new skill, a changed floor, a reworded no-go — invalidates prior scores without anyone remembering to bump `version`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_profile.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add jobsearch/profile.py tests/test_profile.py
git commit -m "feat: profile loading with content hashing and weight validation"
```

---

### Task 4: Author `profile.yaml`

**Files:**
- Create: `profile.yaml`
- Test: reuses `tests/test_profile.py` via a real load

**Interfaces:**
- Consumes: `profile.load_profile`
- Produces: a valid `profile.yaml` at the repo root

This task is data entry, not code. It needs input from the owner that exists in no file.

- [ ] **Step 1: Gather the derivable half**

Read the owner's LinkedIn profile, CV, and the local project directories they name. Extract: current title, years of experience, location, languages, and the skill tiers (`expert` / `strong` / `familiar`). Cross-check the skill tiers against what the local projects actually use — a framework appearing in three shipped projects is `expert`, one mentioned once in a CV is `familiar`.

- [ ] **Step 2: Ask the owner for the non-derivable half**

These appear in no CV and must be asked directly. Ask as one batch, not one at a time:

- salary floor, currency, and period
- acceptable locations and timezone tolerance
- arrangement: remote only, or hybrid acceptable
- employment types: full-time, contract, both
- domains to prefer and domains to refuse outright
- companies to exclude by name
- keywords that disqualify a posting on sight
- required working languages

- [ ] **Step 3: Write `profile.yaml`**

Use the exact structure from the spec's `profile.yaml` section. `weights` must be present and sum to 100; start with the spec's defaults:

```yaml
weights:
  technical_fit: 30
  seniority_fit: 15
  compensation_fit: 15
  arrangement_fit: 10
  domain_fit: 10
  company_fit: 10
  growth_potential: 10
```

`filters.min_salary_monthly_eur` must be expressed in monthly EUR regardless of how the owner stated their floor — convert once, here, and note the original in a comment.

- [ ] **Step 4: Verify it loads**

Run:
```bash
.venv/bin/python -c "from jobsearch.profile import load_profile; p = load_profile(); print(p.version, p.hash[:12], sorted(p.filters))"
```
Expected: prints the version, a hash prefix, and the filter keys. Any `ValueError` here is a malformed `weights` block.

- [ ] **Step 5: Commit**

```bash
git add profile.yaml
git commit -m "feat: owner profile with scoring weights and hard filters"
```

`profile.yaml` is committed deliberately — it holds no secrets, and its history is what makes `profile_hash` comparisons interpretable later.

---

### Task 5: Core models

**Files:**
- Create: `jobsearch/models.py`
- Test: none — these are data holders with no behavior

**Interfaces:**
- Consumes: nothing
- Produces:
  - `models.RawFetch(source_name: str, body: bytes, http_status: int, etag: str | None, last_modified: str | None, fetched_at: datetime, content_hash: str, path: str | None)`
  - `models.RawPosting(external_id: str, url: str, title: str, company: str, location: str | None, description: str, salary_raw: str | None, posted_at: datetime | None, arrangement_hint: str | None, employment_hint: str | None, meta: dict)`
  - `models.ParseResult(status: Literal["ok","empty","broken"], postings: list[RawPosting], diagnostics: dict)`

- [ ] **Step 1: Write `jobsearch/models.py`**

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ParseStatus = Literal["ok", "empty", "broken"]


@dataclass(frozen=True)
class RawFetch:
    source_name: str
    body: bytes
    http_status: int
    fetched_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    path: str | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class RawPosting:
    external_id: str
    url: str
    title: str
    company: str
    description: str
    location: str | None = None
    salary_raw: str | None = None
    posted_at: datetime | None = None
    arrangement_hint: str | None = None
    employment_hint: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    status: ParseStatus
    postings: list[RawPosting] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
```

`content_hash` is a property rather than a stored field so it cannot drift from `body`.

`arrangement_hint` and `employment_hint` hold whatever the source said verbatim (`"Full Remote"`, `"Договір"`); `normalize` turns hints plus description text into the canonical enum. The adapter never guesses.

- [ ] **Step 2: Verify the module imports and the hash is stable**

Run:
```bash
.venv/bin/python -c "
from datetime import datetime
from jobsearch.models import RawFetch
f = RawFetch('djinni', b'hello', 200, datetime(2026,8,25))
print(f.content_hash[:12], f.text())
"
```
Expected: a 12-char hex prefix and `hello`.

- [ ] **Step 3: Commit**

```bash
git add jobsearch/models.py
git commit -m "feat: core data models for fetch, posting, and parse result"
```

---

### Task 6: Normalization

**Files:**
- Create: `jobsearch/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `normalize.company(raw: str) -> str`
  - `normalize.title(raw: str) -> str`
  - `normalize.location(raw: str | None) -> str`
  - `normalize.arrangement(hint: str | None, text: str) -> str` → `remote|hybrid|onsite|unknown`
  - `normalize.employment(hint: str | None, text: str) -> str` → `full-time|part-time|contract|internship|unknown`
  - `normalize.description(html_or_text: str) -> str`
  - `normalize.description_hash(text: str) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_normalize.py
import pytest

from jobsearch import normalize


@pytest.mark.parametrize("raw,expected", [
    ("Acme Ltd.", "acme"),
    ("Acme GmbH", "acme"),
    ("ACME, LLC", "acme"),
    ("Acme  Software   Sp. z o.o.", "acme software"),
    ("Acme Inc", "acme"),
])
def test_company_strips_legal_suffixes(raw, expected):
    assert normalize.company(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Senior PHP Developer", "senior php developer"),
    ("Senior  PHP   Developer  (Remote)", "senior php developer"),
    ("Senior PHP Developer — Fintech", "senior php developer fintech"),
    ("Sr. PHP Developer", "senior php developer"),
])
def test_title_normalization(raw, expected):
    assert normalize.title(raw) == expected


@pytest.mark.parametrize("hint,text,expected", [
    ("Full Remote", "", "remote"),
    (None, "This is a fully remote position.", "remote"),
    (None, "Hybrid, 2 days per week in the Kyiv office", "hybrid"),
    (None, "On-site in Warsaw", "onsite"),
    (None, "We are hiring a developer.", "unknown"),
])
def test_arrangement_detection(hint, text, expected):
    assert normalize.arrangement(hint, text) == expected


def test_arrangement_hint_beats_description_text():
    # The description mentions an office, but the source labelled it remote.
    assert normalize.arrangement("Remote", "Our office is in Berlin") == "remote"


@pytest.mark.parametrize("hint,text,expected", [
    ("Full-time", "", "full-time"),
    (None, "B2B contract, 12 months", "contract"),
    (None, "Part time, 20h/week", "part-time"),
    (None, "Summer internship programme", "internship"),
    (None, "Join our team", "unknown"),
])
def test_employment_detection(hint, text, expected):
    assert normalize.employment(hint, text) == expected


def test_description_strips_markup_and_collapses_whitespace():
    html = "<div><p>We need   <b>PHP</b>.</p>\n\n<p>And Vue.</p></div>"
    assert normalize.description(html) == "We need PHP.\n\nAnd Vue."


def test_description_hash_is_stable_across_whitespace_noise():
    a = normalize.description("<p>We need PHP.</p>")
    b = normalize.description("<p>We   need\tPHP.</p>")
    assert normalize.description_hash(a) == normalize.description_hash(b)


def test_location_normalization():
    assert normalize.location("  Kyiv, Ukraine ") == "kyiv ukraine"
    assert normalize.location(None) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.normalize'`

- [ ] **Step 3: Write `jobsearch/normalize.py`**

```python
from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

_LEGAL_SUFFIXES = (
    "ltd", "ltd.", "limited", "llc", "l.l.c.", "inc", "inc.", "incorporated",
    "gmbh", "ag", "bv", "b.v.", "nv", "n.v.", "sa", "s.a.", "srl", "s.r.l.",
    "oy", "ab", "as", "aps", "plc", "co", "co.", "corp", "corp.", "corporation",
    "sp", "z", "o.o.", "sp.", "zoo", "s.r.o.", "d.o.o.", "tov", "pp", "fop",
)

_TITLE_ALIASES = {
    "sr": "senior",
    "sr.": "senior",
    "jr": "junior",
    "jr.": "junior",
    "lead": "lead",
    "eng": "engineer",
    "dev": "developer",
}

_PUNCT = re.compile(r"[^\w\s+#]", flags=re.UNICODE)
_SPACE = re.compile(r"\s+")
_PARENS = re.compile(r"\([^)]*\)")

_REMOTE = re.compile(r"\b(fully\s+remote|full\s+remote|remote|віддалено|удалённо)\b", re.I)
_HYBRID = re.compile(r"\bhybrid|гібрид|гибрид\b", re.I)
_ONSITE = re.compile(r"\b(on[-\s]?site|in[-\s]?office|офіс|office[-\s]?based)\b", re.I)

_CONTRACT = re.compile(r"\b(b2b|contract|contractor|freelance|договір|гіг)\b", re.I)
_PART = re.compile(r"\bpart[-\s]?time\b", re.I)
_INTERN = re.compile(r"\b(intern|internship|trainee|стажув)\w*", re.I)
_FULL = re.compile(r"\bfull[-\s]?time\b", re.I)


def _squash(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def company(raw: str) -> str:
    text = _PUNCT.sub(" ", (raw or "").lower())
    tokens = [t for t in _squash(text).split(" ") if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def title(raw: str) -> str:
    text = _PARENS.sub(" ", raw or "")
    text = _PUNCT.sub(" ", text.lower())
    tokens = [_TITLE_ALIASES.get(t, t) for t in _squash(text).split(" ") if t]
    return " ".join(tokens)


def location(raw: str | None) -> str:
    if not raw:
        return ""
    return _squash(_PUNCT.sub(" ", raw.lower()))


def arrangement(hint: str | None, text: str) -> str:
    # A source's own label is authoritative; description prose is a fallback.
    for candidate, pattern in (("remote", _REMOTE), ("hybrid", _HYBRID), ("onsite", _ONSITE)):
        if hint and pattern.search(hint):
            return candidate
    haystack = text or ""
    if _HYBRID.search(haystack):
        return "hybrid"
    if _REMOTE.search(haystack):
        return "remote"
    if _ONSITE.search(haystack):
        return "onsite"
    return "unknown"


def employment(hint: str | None, text: str) -> str:
    for candidate, pattern in (
        ("internship", _INTERN), ("contract", _CONTRACT),
        ("part-time", _PART), ("full-time", _FULL),
    ):
        if hint and pattern.search(hint):
            return candidate
    haystack = text or ""
    for candidate, pattern in (
        ("internship", _INTERN), ("contract", _CONTRACT),
        ("part-time", _PART), ("full-time", _FULL),
    ):
        if pattern.search(haystack):
            return candidate
    return "unknown"


def description(html_or_text: str) -> str:
    soup = BeautifulSoup(html_or_text or "", "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    blocks = [_squash(block) for block in soup.get_text("\n").split("\n")]
    return "\n\n".join(b for b in blocks if b)


def description_hash(text: str) -> str:
    return hashlib.sha256(_squash(text or "").lower().encode("utf-8")).hexdigest()
```

`_HYBRID` is checked before `_REMOTE` in the prose fallback because a hybrid posting almost always contains the word "remote" too; the reverse is not true.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_normalize.py -v`
Expected: 22 passed

- [ ] **Step 5: Commit**

```bash
git add jobsearch/normalize.py tests/test_normalize.py
git commit -m "feat: normalization for company, title, location, arrangement, employment"
```

---

### Task 7: Salary parsing

**Files:**
- Create: `jobsearch/salary.py`
- Test: `tests/test_salary.py`

**Interfaces:**
- Consumes: `config.DEFAULT_RATES`
- Produces:
  - `salary.Salary(min: int | None, max: int | None, currency: str | None, period: str | None, type: str, source: str)` — frozen
  - `salary.parse(raw: str | None) -> Salary`
  - `salary.to_monthly_eur(s: Salary, rates: dict[str, float]) -> int | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_salary.py
import pytest

from jobsearch.config import DEFAULT_RATES
from jobsearch.salary import Salary, parse, to_monthly_eur


@pytest.mark.parametrize("raw,expected", [
    ("€5,000/month",  Salary(5000, 5000, "EUR", "month", "unknown", "posting")),
    ("€60,000/year",  Salary(60000, 60000, "EUR", "year", "unknown", "posting")),
    ("$100k-$130k",   Salary(100000, 130000, "USD", "year", "unknown", "posting")),
    ("$40-$60/hour",  Salary(40, 60, "USD", "hour", "unknown", "posting")),
    ("PLN 25,000",    Salary(25000, 25000, "PLN", "month", "unknown", "posting")),
    ("£70k",          Salary(70000, 70000, "GBP", "year", "unknown", "posting")),
    ("€500/day",      Salary(500, 500, "EUR", "day", "contractor", "posting")),
    ("up to €8k",     Salary(None, 8000, "EUR", "month", "unknown", "posting")),
    ("from $5000",    Salary(5000, None, "USD", "month", "unknown", "posting")),
    ("3000-5000 EUR", Salary(3000, 5000, "EUR", "month", "unknown", "posting")),
])
def test_parse_the_format_zoo(raw, expected):
    assert parse(raw) == expected


@pytest.mark.parametrize("raw", ["competitive", "negotiable", "DOE", "", None, "attractive package"])
def test_unparseable_compensation_is_absent_not_zero(raw):
    result = parse(raw)
    assert result.source == "absent"
    assert result.min is None and result.max is None


def test_day_rate_implies_contractor():
    assert parse("€500/day").type == "contractor"
    assert parse("$60/hour").type == "contractor"


@pytest.mark.parametrize("raw,expected_eur", [
    ("€5,000/month", 5000),
    ("€60,000/year", 5000),
    ("$120,000/year", 9200),
    ("£70k", 6825),
    ("€500/day", 10500),      # 21 working days
    ("$50/hour", 6440),       # 168 working hours
])
def test_monthly_eur_derivation(raw, expected_eur):
    assert to_monthly_eur(parse(raw), DEFAULT_RATES) == expected_eur


def test_monthly_eur_uses_the_midpoint_of_a_range():
    assert to_monthly_eur(parse("€4,000-€6,000/month"), DEFAULT_RATES) == 5000


def test_monthly_eur_is_none_when_absent():
    assert to_monthly_eur(parse("competitive"), DEFAULT_RATES) is None


def test_monthly_eur_is_none_for_unknown_currency():
    assert to_monthly_eur(Salary(5000, 5000, "XYZ", "month", "unknown", "posting"), DEFAULT_RATES) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_salary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.salary'`

- [ ] **Step 3: Write `jobsearch/salary.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass

WORKING_DAYS_PER_MONTH = 21
WORKING_HOURS_PER_MONTH = 168

_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "₴": "UAH", "zł": "PLN"}
_CURRENCY_CODES = ("EUR", "USD", "GBP", "PLN", "UAH", "CHF")

_PERIOD_PATTERNS = (
    ("hour", re.compile(r"/\s*(hour|hr|h)\b|\bper\s+hour\b", re.I)),
    ("day", re.compile(r"/\s*(day|d)\b|\bper\s+day\b", re.I)),
    ("month", re.compile(r"/\s*(month|mo|mth)\b|\bper\s+month\b", re.I)),
    ("year", re.compile(r"/\s*(year|yr|annum|a)\b|\bper\s+year\b|\bp\.?a\.?\b", re.I)),
)

_AMOUNT = re.compile(r"(\d[\d\s,._]*)\s*(k)?", re.I)
_UP_TO = re.compile(r"\b(up\s+to|максимум|до)\b", re.I)
_FROM = re.compile(r"\b(from|starting|від|от)\b", re.I)


@dataclass(frozen=True)
class Salary:
    min: int | None
    max: int | None
    currency: str | None
    period: str | None
    type: str
    source: str


ABSENT = Salary(None, None, None, None, "unknown", "absent")


def _currency(text: str) -> str | None:
    upper = text.upper()
    for code in _CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            return code
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None


def _amounts(text: str) -> list[int]:
    found: list[int] = []
    for match in _AMOUNT.finditer(text):
        digits = re.sub(r"[\s,._]", "", match.group(1))
        if not digits:
            continue
        value = int(digits)
        if match.group(2):
            value *= 1000
        found.append(value)
    return found


def _period(text: str, amounts: list[int]) -> str:
    for name, pattern in _PERIOD_PATTERNS:
        if pattern.search(text):
            return name
    # No explicit period. Magnitude is the only signal left, and it is a reliable
    # one: nobody is paid 60,000 a month or 5,000 a year in these markets.
    biggest = max(amounts) if amounts else 0
    return "year" if biggest >= 20000 else "month"


def parse(raw: str | None) -> Salary:
    if not raw or not raw.strip():
        return ABSENT

    text = raw.strip()
    amounts = _amounts(text)
    currency = _currency(text)
    if not amounts or currency is None:
        return ABSENT

    period = _period(text, amounts)
    kind = "contractor" if period in ("hour", "day") else "unknown"

    low: int | None
    high: int | None
    if _UP_TO.search(text):
        low, high = None, max(amounts)
    elif _FROM.search(text):
        low, high = min(amounts), None
    elif len(amounts) >= 2:
        low, high = min(amounts), max(amounts)
    else:
        low = high = amounts[0]

    return Salary(low, high, currency, period, kind, "posting")


def to_monthly_eur(s: Salary, rates: dict[str, float]) -> int | None:
    if s.source == "absent" or s.currency is None or s.period is None:
        return None
    rate = rates.get(s.currency)
    if rate is None:
        return None

    figures = [v for v in (s.min, s.max) if v is not None]
    if not figures:
        return None
    amount = sum(figures) / len(figures)

    per_month = {
        "month": 1.0,
        "year": 1 / 12,
        "day": float(WORKING_DAYS_PER_MONTH),
        "hour": float(WORKING_HOURS_PER_MONTH),
    }[s.period]

    return int(round(amount * per_month * rate))
```

The magnitude heuristic in `_period` is the one guess in this module. It is confined to a single branch, and its threshold sits in a range no real monthly or annual figure occupies in these markets.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_salary.py -v`
Expected: 25 passed

- [ ] **Step 5: Commit**

```bash
git add jobsearch/salary.py tests/test_salary.py
git commit -m "feat: compensation parsing and EUR-monthly derivation"
```

---

### Task 8: Deduplication

**Files:**
- Create: `jobsearch/dedupe.py`
- Test: `tests/test_dedupe.py`

**Interfaces:**
- Consumes: `normalize`
- Produces:
  - `dedupe.fingerprint(company: str, title: str, location: str, employment_type: str, arrangement: str) -> str`
  - `dedupe.jaccard(a: str, b: str) -> float`
  - `dedupe.can_merge(*, existing_source_id: int, candidate_source_id: int, existing_last_seen: datetime, candidate_last_seen: datetime, existing_description: str | None, candidate_description: str | None) -> tuple[bool, str]` — the caller has already matched fingerprints; returns `(decision, reason)`
  - `dedupe.MERGE_WINDOW_DAYS = 30`, `dedupe.MIN_JACCARD = 0.6`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dedupe.py
from datetime import datetime, timedelta

from jobsearch.dedupe import can_merge, fingerprint, jaccard

NOW = datetime(2026, 8, 25, 9, 0)
LONG = "we are looking for a senior php developer with laravel and vue experience in fintech"
OTHER = "we need a marketing manager to run paid acquisition campaigns across europe"


def test_fingerprint_is_stable_across_cosmetic_differences():
    a = fingerprint("Acme Ltd.", "Senior PHP Developer", "Kyiv, Ukraine", "full-time", "onsite")
    b = fingerprint("ACME  LLC", "Sr. PHP Developer",   "kyiv ukraine",  "full-time", "onsite")
    assert a == b


def test_remote_postings_ignore_location_entirely():
    a = fingerprint("Acme", "Senior PHP Developer", "Remote EU", "full-time", "remote")
    b = fingerprint("Acme", "Senior PHP Developer", "Remote",    "full-time", "remote")
    assert a == b


def test_onsite_postings_still_distinguish_location():
    a = fingerprint("Acme", "Senior PHP Developer", "Kyiv",   "full-time", "onsite")
    b = fingerprint("Acme", "Senior PHP Developer", "Warsaw", "full-time", "onsite")
    assert a != b


def test_employment_type_separates_otherwise_identical_roles():
    a = fingerprint("Acme", "Senior PHP Developer", "Remote", "full-time", "remote")
    b = fingerprint("Acme", "Senior PHP Developer", "Remote", "contract",  "remote")
    assert a != b


def test_jaccard_is_one_for_identical_text_and_zero_for_disjoint():
    assert jaccard(LONG, LONG) == 1.0
    assert jaccard(LONG, OTHER) < 0.2


def _merge(**overrides):
    kwargs = dict(
        existing_source_id=1, candidate_source_id=2,
        existing_last_seen=NOW, candidate_last_seen=NOW,
        existing_description=LONG, candidate_description=LONG,
    )
    kwargs.update(overrides)
    return can_merge(**kwargs)


def test_merges_across_two_sources_seen_together_with_similar_text():
    decision, reason = _merge()
    assert decision is True
    assert reason == "merged"


def test_never_merges_two_postings_from_the_same_source():
    decision, reason = _merge(candidate_source_id=1)
    assert decision is False
    assert "same source" in reason


def test_never_merges_across_a_repost_gap():
    decision, reason = _merge(candidate_last_seen=NOW + timedelta(days=95))
    assert decision is False
    assert "window" in reason


def test_never_merges_when_descriptions_diverge():
    decision, reason = _merge(candidate_description=OTHER)
    assert decision is False
    assert "similarity" in reason


def test_merges_when_a_description_is_missing_on_either_side():
    # The similarity gate can only apply when both sides have text.
    assert _merge(candidate_description=None)[0] is True
    assert _merge(existing_description="")[0] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dedupe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.dedupe'`

- [ ] **Step 3: Write `jobsearch/dedupe.py`**

```python
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from jobsearch import normalize

MERGE_WINDOW_DAYS = 30
MIN_JACCARD = 0.6

_TOKEN = re.compile(r"\w+", re.UNICODE)


def fingerprint(
    company: str,
    title: str,
    location: str,
    employment_type: str,
    arrangement: str,
) -> str:
    # A remote role is the same opportunity whether the source wrote "Remote",
    # "Remote EU", or nothing at all — so location is dropped when remote.
    location_part = "" if arrangement == "remote" else normalize.location(location)
    parts = (
        normalize.company(company),
        normalize.title(title),
        location_part,
        employment_type or "unknown",
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def jaccard(a: str, b: str) -> float:
    tokens_a = set(_TOKEN.findall((a or "").lower()))
    tokens_b = set(_TOKEN.findall((b or "").lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def can_merge(
    *,
    existing_source_id: int,
    candidate_source_id: int,
    existing_last_seen: datetime,
    candidate_last_seen: datetime,
    existing_description: str | None,
    candidate_description: str | None,
) -> tuple[bool, str]:
    """Decide whether a fingerprint-matching posting joins an existing canonical job.

    The caller has already established that fingerprints are equal. False splits
    are cheap and visible; false merges destroy data silently — so every gate
    defaults to refusing.
    """
    if existing_source_id == candidate_source_id:
        return False, "same source — two requisitions, not one opportunity"

    gap = abs((candidate_last_seen - existing_last_seen).days)
    if gap > MERGE_WINDOW_DAYS:
        return False, f"outside {MERGE_WINDOW_DAYS}-day window ({gap} days) — treat as a repost"

    if existing_description and candidate_description:
        score = jaccard(existing_description, candidate_description)
        if score < MIN_JACCARD:
            return False, f"description similarity {score:.2f} below {MIN_JACCARD}"

    return True, "merged"
```

The similarity gate is skipped when either description is missing, because absent text is not evidence of difference — the same principle that governs the filters.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dedupe.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add jobsearch/dedupe.py tests/test_dedupe.py
git commit -m "feat: fingerprinting and conservative cross-source merge rules"
```

---

### Task 9: Store

**Files:**
- Create: `jobsearch/store.py`
- Test: none (DB round-trips are out of scope; the decision logic it calls is already covered by Task 8)

**Interfaces:**
- Consumes: `dedupe`, `normalize`, `salary`, `models.RawFetch`
- Produces:
  - `store.start_run(conn, kind: str, source_id: int | None = None) -> int`
  - `store.finish_run(conn, run_id: int, *, fetched: int | None = None, new: int | None = None, error: str | None = None) -> None`
  - `store.active_sources(conn, fetch_modes: tuple[str, ...]) -> list[dict]`
  - `store.source_by_name(conn, name: str) -> dict`
  - `store.last_fetch(conn, source_id: int) -> dict | None`
  - `store.record_fetch(conn, source_id: int, run_id: int, raw: RawFetch, path: str) -> int`
  - `store.upsert_posting(conn, source: dict, posting: RawPosting, raw_fetch_id: int | None, rates: dict, now: datetime) -> tuple[int, bool]` → `(job_id, is_new)`
  - `store.mark_source(conn, source_id: int, status: str, saw_items: bool, now: datetime) -> None`

- [ ] **Step 1: Add `canonical_source_id` to the schema**

The canonical merge rule needs to know which source currently owns the merged field values. Edit `migrations/001_init.sql`, adding to the `jobs` table immediately after `fingerprint`:

```sql
  canonical_source_id INT NULL,
```

Since the schema has already been applied, drop and re-create rather than writing a second migration — there is no data worth preserving yet:

```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" -e "DROP DATABASE job_search; CREATE DATABASE job_search CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
.venv/bin/jobsearch init-db
```

Expected: `{"applied": ["001_init.sql"], "count": 1}`.

- [ ] **Step 2: Write `jobsearch/store.py`**

```python
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


def last_fetch(conn, source_id: int) -> dict | None:
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
```

- [ ] **Step 3: Verify the module imports cleanly**

Run: `.venv/bin/python -c "import jobsearch.store; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add jobsearch/store.py migrations/001_init.sql
git commit -m "feat: store with priority-based canonical merge and specificity-based salary merge"
```

---

### Task 10: Adapter base classes and the Djinni adapter

**Files:**
- Create: `jobsearch/adapters/__init__.py`, `jobsearch/adapters/base.py`, `jobsearch/adapters/djinni.py`, `jobsearch/adapters/registry.py`
- Test: `tests/test_adapters_html.py`, fixtures under `tests/fixtures/djinni/`

**Interfaces:**
- Consumes: `models.RawFetch`, `models.RawPosting`, `models.ParseResult`
- Produces:
  - `base.Adapter` ABC — `name: str`, `fetch_mode: str`, `fetch(query, conditional) -> RawFetch`, `parse(raw, selectors) -> ParseResult`
  - `base.HtmlAdapter` — implements the selector-driven `parse`; subclasses implement only `build_url`
  - `base.extract_field(node, spec: dict, base_url: str) -> str | None`
  - `registry.get(name: str) -> Adapter`
  - `registry.ADAPTERS: dict[str, type[Adapter]]`

- [ ] **Step 1: Write the failing tests**

These use a hand-written miniature HTML document rather than a captured page, so the classification logic is tested independently of any real site's markup. Real-site fixtures arrive in Step 6.

```python
# tests/test_adapters_html.py
from datetime import datetime

from jobsearch.adapters.base import HtmlAdapter
from jobsearch.models import RawFetch

SELECTORS = {
    "container": "ul.jobs",
    "item": "li.job",
    "empty_state": "div.no-results",
    "minimum_items": 1,
    "fields": {
        "external_id": {"selector": "a.title", "attr": "href", "regex": r"/jobs/(\d+)"},
        "url":         {"selector": "a.title", "attr": "href", "absolute": True},
        "title":       {"selector": "a.title", "attr": "text"},
        "company":     {"selector": ".company", "attr": "text"},
        "location":    {"selector": ".location", "attr": "text"},
        "salary_raw":  {"selector": ".salary", "attr": "text"},
        "description": {"selector": ".desc", "attr": "html"},
    },
}

VALID = b"""<html><body><ul class="jobs">
<li class="job"><a class="title" href="/jobs/101">Senior PHP Developer</a>
<span class="company">Acme</span><span class="location">Remote</span>
<span class="salary">$5000</span><div class="desc"><p>Laravel and Vue.</p></div></li>
<li class="job"><a class="title" href="/jobs/102">Backend Engineer</a>
<span class="company">Globex</span><span class="location">Kyiv</span>
<div class="desc"><p>Go and Postgres.</p></div></li>
</ul></body></html>"""

EMPTY = b"""<html><body><ul class="jobs"></ul>
<div class="no-results">Nothing matches your filters</div></body></html>"""

CHANGED = VALID.replace(b'class="jobs"', b'class="job-list-v2"')

ITEMS_GONE = b"""<html><body><ul class="jobs"></ul></body></html>"""


class _Probe(HtmlAdapter):
    name = "probe"
    base_url = "https://example.test"

    def build_url(self, query):
        return self.base_url


def _raw(body: bytes) -> RawFetch:
    return RawFetch("probe", body, 200, datetime(2026, 8, 25))


def test_valid_markup_parses_ok():
    result = _Probe().parse(_raw(VALID), SELECTORS)
    assert result.status == "ok"
    assert len(result.postings) == 2
    first = result.postings[0]
    assert first.external_id == "101"
    assert first.title == "Senior PHP Developer"
    assert first.company == "Acme"
    assert first.url == "https://example.test/jobs/101"
    assert first.salary_raw == "$5000"
    assert "Laravel" in first.description


def test_missing_optional_field_is_none_not_a_failure():
    result = _Probe().parse(_raw(VALID), SELECTORS)
    assert result.postings[1].salary_raw is None


def test_explicit_empty_state_classifies_empty():
    result = _Probe().parse(_raw(EMPTY), SELECTORS)
    assert result.status == "empty"
    assert result.postings == []


def test_changed_container_markup_classifies_broken():
    result = _Probe().parse(_raw(CHANGED), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["container_matched"] == 0


def test_container_present_but_no_items_and_no_empty_marker_is_broken():
    result = _Probe().parse(_raw(ITEMS_GONE), SELECTORS)
    assert result.status == "broken"


def test_items_that_all_fail_required_fields_classify_broken():
    body = VALID.replace(b'class="title"', b'class="headline"')
    result = _Probe().parse(_raw(body), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["dropped"] == 2


def test_diagnostics_report_field_fill_rates():
    result = _Probe().parse(_raw(VALID), SELECTORS)
    assert result.diagnostics["fill_rates"]["title"] == 1.0
    assert result.diagnostics["fill_rates"]["salary_raw"] == 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_adapters_html.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.adapters'`

- [ ] **Step 3: Write `jobsearch/adapters/base.py`**

```python
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobsearch.models import ParseResult, RawFetch, RawPosting

REQUIRED_FIELDS = ("external_id", "url", "title", "company")
USER_AGENT = "jobsearch/0.1 (personal job search agent; contact via repository owner)"
TIMEOUT = httpx.Timeout(20.0)


class Adapter(ABC):
    name: str
    fetch_mode: str
    base_url: str

    @abstractmethod
    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch: ...

    @abstractmethod
    def parse(self, raw: RawFetch, selectors: dict) -> ParseResult: ...

    def _get(self, url: str, conditional: dict | None) -> RawFetch:
        headers = {"User-Agent": USER_AGENT}
        if conditional:
            if conditional.get("etag"):
                headers["If-None-Match"] = conditional["etag"]
            if conditional.get("last_modified"):
                headers["If-Modified-Since"] = conditional["last_modified"]

        response = httpx.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return RawFetch(
            source_name=self.name,
            body=response.content,
            http_status=response.status_code,
            fetched_at=datetime.now(),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )


def extract_field(node, spec: dict, base_url: str) -> str | None:
    found = node.select_one(spec["selector"])
    if found is None:
        return None

    attr = spec.get("attr", "text")
    if attr == "text":
        value = found.get_text(" ", strip=True)
    elif attr == "html":
        value = found.decode_contents()
    else:
        value = found.get(attr)

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    if spec.get("regex"):
        match = re.search(spec["regex"], value)
        if not match:
            return None
        value = match.group(1)

    if spec.get("absolute"):
        value = urljoin(base_url, value)

    return value


class HtmlAdapter(Adapter):
    """Selector-driven HTML parsing shared by every http-html source.

    Subclasses supply build_url and, optionally, post_process. Nothing about the
    markup lives in code — it all comes from sources.selectors, which is what
    lets a repair change behavior without a deploy.
    """

    fetch_mode = "http-html"

    @abstractmethod
    def build_url(self, query: dict) -> str: ...

    def post_process(self, posting: RawPosting, node) -> RawPosting:
        return posting

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        return self._get(self.build_url(query or {}), conditional)

    def parse(self, raw: RawFetch, selectors: dict) -> ParseResult:
        soup = BeautifulSoup(raw.text(), "lxml")
        fields = selectors["fields"]
        minimum = int(selectors.get("minimum_items", 1))

        containers = soup.select(selectors["container"])
        diagnostics: dict = {
            "container_matched": len(containers),
            "item_matched": 0,
            "dropped": 0,
            "fill_rates": {},
        }

        if not containers:
            return ParseResult("broken", [], diagnostics)

        nodes = []
        for container in containers:
            nodes.extend(container.select(selectors["item"]))
        diagnostics["item_matched"] = len(nodes)

        if not nodes:
            marker = selectors.get("empty_state")
            if marker and soup.select_one(marker):
                return ParseResult("empty", [], diagnostics)
            return ParseResult("broken", [], diagnostics)

        postings: list[RawPosting] = []
        counts = {name: 0 for name in fields}

        for node in nodes:
            values = {
                name: extract_field(node, spec, self.base_url)
                for name, spec in fields.items()
            }
            for name, value in values.items():
                if value:
                    counts[name] += 1

            if any(values.get(name) is None for name in REQUIRED_FIELDS):
                diagnostics["dropped"] += 1
                continue

            posting = RawPosting(
                external_id=values["external_id"],
                url=values["url"],
                title=values["title"],
                company=values["company"],
                description=values.get("description") or "",
                location=values.get("location"),
                salary_raw=values.get("salary_raw"),
                arrangement_hint=values.get("arrangement_hint"),
                employment_hint=values.get("employment_hint"),
            )
            postings.append(self.post_process(posting, node))

        diagnostics["fill_rates"] = {
            name: round(count / len(nodes), 3) for name, count in counts.items()
        }

        if not postings:
            return ParseResult("broken", [], diagnostics)
        if len(postings) < minimum:
            return ParseResult("broken", postings, diagnostics)
        return ParseResult("ok", postings, diagnostics)
```

Field fill rates are computed over every matched node, including dropped ones — that is precisely the number a repair report needs to show which field a markup change killed.

- [ ] **Step 4: Write `jobsearch/adapters/djinni.py`**

```python
from __future__ import annotations

from urllib.parse import urlencode

from jobsearch.adapters.base import HtmlAdapter


class DjinniAdapter(HtmlAdapter):
    name = "djinni"
    base_url = "https://djinni.co"

    def build_url(self, query: dict) -> str:
        params = {"primary_keyword": query.get("keyword", "PHP")}
        if query.get("remote"):
            params["employment"] = "remote"
        if query.get("experience"):
            params["exp_level"] = query["experience"]
        return f"{self.base_url}/jobs/?{urlencode(params)}"
```

- [ ] **Step 5: Write `jobsearch/adapters/registry.py` and `__init__.py`**

```python
# jobsearch/adapters/registry.py
from __future__ import annotations

from jobsearch.adapters.base import Adapter
from jobsearch.adapters.djinni import DjinniAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    DjinniAdapter.name: DjinniAdapter,
}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise LookupError(
            f"no adapter for source '{name}' — browser sources enter via `jobsearch ingest`"
        ) from None
```

```python
# jobsearch/adapters/__init__.py
```

(empty file)

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_adapters_html.py -v`
Expected: 7 passed

- [ ] **Step 7: Capture real Djinni fixtures and derive the selectors**

The selectors cannot be written from memory — capture the live page and read it.

```bash
mkdir -p tests/fixtures/djinni
.venv/bin/python -c "
from jobsearch.adapters.djinni import DjinniAdapter
import gzip, pathlib
raw = DjinniAdapter().fetch({'keyword': 'PHP'})
pathlib.Path('tests/fixtures/djinni/valid.html.gz').write_bytes(gzip.compress(raw.body))
print(len(raw.body), 'bytes')
"
```

Open the captured HTML, identify the results container, the repeated posting element, the "no results" marker, and each field. Write the resulting selector JSON into the `sources` row:

```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search <<'SQL'
UPDATE sources SET
  query = JSON_OBJECT('keyword','PHP','remote',TRUE),
  selectors = '{ ...the JSON you derived... }'
WHERE name = 'djinni';
SQL
```

Produce two more fixtures from the captured one:

```bash
.venv/bin/python -c "
import gzip, pathlib, re
valid = gzip.decompress(pathlib.Path('tests/fixtures/djinni/valid.html.gz').read_bytes())
# changed-markup: rename the container class so the container selector misses
changed = valid.replace(b'<CONTAINER_CLASS_HERE>', b'container-v2')
pathlib.Path('tests/fixtures/djinni/changed-markup.html.gz').write_bytes(gzip.compress(changed))
"
```

For `empty.html.gz`, fetch a query that genuinely returns nothing (a nonsense keyword) and save that response.

- [ ] **Step 8: Add the fixture-driven Djinni tests**

```python
# append to tests/test_adapters_html.py
import gzip
import json
import pathlib

import pytest

from jobsearch.adapters.djinni import DjinniAdapter

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "djinni"
SELECTORS_PATH = FIXTURES / "selectors.json"


def _fixture(name: str) -> RawFetch:
    body = gzip.decompress((FIXTURES / name).read_bytes())
    return RawFetch("djinni", body, 200, datetime(2026, 8, 25))


@pytest.fixture(scope="module")
def djinni_selectors():
    return json.loads(SELECTORS_PATH.read_text())


def test_djinni_valid_fixture_parses(djinni_selectors):
    result = DjinniAdapter().parse(_fixture("valid.html.gz"), djinni_selectors)
    assert result.status == "ok"
    assert len(result.postings) >= 5
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)


def test_djinni_empty_fixture_classifies_empty(djinni_selectors):
    result = DjinniAdapter().parse(_fixture("empty.html.gz"), djinni_selectors)
    assert result.status == "empty"


def test_djinni_changed_markup_classifies_broken(djinni_selectors):
    result = DjinniAdapter().parse(_fixture("changed-markup.html.gz"), djinni_selectors)
    assert result.status == "broken"
```

Save the same selector JSON you wrote into the database to `tests/fixtures/djinni/selectors.json`, so the test and the live source stay comparable. When a repair changes the live selectors, updating this file and re-running is how the change gets verified.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all green — 10 adapter tests plus the earlier normalize/salary/dedupe/profile tests.

- [ ] **Step 10: Commit**

```bash
git add jobsearch/adapters/ tests/test_adapters_html.py tests/fixtures/djinni/
git commit -m "feat: selector-driven HTML adapter with outcome classification, plus Djinni"
```

---

### Task 11: DOU adapter

**Files:**
- Create: `jobsearch/adapters/dou.py`, `tests/fixtures/dou/`
- Modify: `jobsearch/adapters/registry.py`, `tests/test_adapters_html.py`

**Interfaces:**
- Consumes: `base.HtmlAdapter`
- Produces: `dou.DouAdapter` with `name = "dou"`, `base_url = "https://jobs.dou.ua"`

DOU needs no new parsing machinery — it is `HtmlAdapter` with a different URL and different selectors in the database.

- [ ] **Step 1: Write `jobsearch/adapters/dou.py`**

```python
from __future__ import annotations

from urllib.parse import urlencode

from jobsearch.adapters.base import HtmlAdapter


class DouAdapter(HtmlAdapter):
    name = "dou"
    base_url = "https://jobs.dou.ua"

    def build_url(self, query: dict) -> str:
        params = {"category": query.get("category", "PHP")}
        if query.get("remote"):
            params["remote"] = ""
        if query.get("experience"):
            params["exp"] = query["experience"]
        return f"{self.base_url}/vacancies/?{urlencode(params)}"
```

- [ ] **Step 2: Register it**

```python
# jobsearch/adapters/registry.py — add the import and the entry
from jobsearch.adapters.dou import DouAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    DjinniAdapter.name: DjinniAdapter,
    DouAdapter.name: DouAdapter,
}
```

- [ ] **Step 3: Capture fixtures and derive selectors**

Identical procedure to Task 10 Step 7, against DOU:

```bash
mkdir -p tests/fixtures/dou
.venv/bin/python -c "
from jobsearch.adapters.dou import DouAdapter
import gzip, pathlib
raw = DouAdapter().fetch({'category': 'PHP'})
pathlib.Path('tests/fixtures/dou/valid.html.gz').write_bytes(gzip.compress(raw.body))
print(len(raw.body), 'bytes')
"
```

Read the captured HTML, derive `container` / `item` / `empty_state` / `fields`, write the JSON to both `tests/fixtures/dou/selectors.json` and the `sources` row for `dou`. Produce `empty.html.gz` from a nonsense category and `changed-markup.html.gz` by renaming the container class.

- [ ] **Step 4: Write the fixture tests**

```python
# append to tests/test_adapters_html.py
from jobsearch.adapters.dou import DouAdapter

DOU_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "dou"


def _dou_fixture(name: str) -> RawFetch:
    body = gzip.decompress((DOU_FIXTURES / name).read_bytes())
    return RawFetch("dou", body, 200, datetime(2026, 8, 25))


@pytest.fixture(scope="module")
def dou_selectors():
    return json.loads((DOU_FIXTURES / "selectors.json").read_text())


def test_dou_valid_fixture_parses(dou_selectors):
    result = DouAdapter().parse(_dou_fixture("valid.html.gz"), dou_selectors)
    assert result.status == "ok"
    assert len(result.postings) >= 5
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)


def test_dou_empty_fixture_classifies_empty(dou_selectors):
    assert DouAdapter().parse(_dou_fixture("empty.html.gz"), dou_selectors).status == "empty"


def test_dou_changed_markup_classifies_broken(dou_selectors):
    assert DouAdapter().parse(_dou_fixture("changed-markup.html.gz"), dou_selectors).status == "broken"
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_adapters_html.py -v`
Expected: 13 passed

- [ ] **Step 6: Commit**

```bash
git add jobsearch/adapters/dou.py jobsearch/adapters/registry.py tests/
git commit -m "feat: DOU adapter"
```

---

### Task 12: JSON adapter base and RemoteOK

**Files:**
- Create: `jobsearch/adapters/remoteok.py`, `tests/test_adapters_json.py`, `tests/fixtures/remoteok/`
- Modify: `jobsearch/adapters/base.py`, `jobsearch/adapters/registry.py`

**Interfaces:**
- Consumes: `base.Adapter`
- Produces:
  - `base.JsonAdapter` — `parse` driven by `{"root": <dot path or "">, "skip_first": bool, "fields": {name: <dot path>}}`
  - `remoteok.RemoteOkAdapter` with `name = "remoteok"`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_adapters_json.py
import json
from datetime import datetime

from jobsearch.adapters.base import JsonAdapter
from jobsearch.models import RawFetch

SELECTORS = {
    "root": "",
    "skip_first": True,
    "fields": {
        "external_id": "id",
        "url": "url",
        "title": "position",
        "company": "company",
        "location": "location",
        "salary_raw": "salary",
        "description": "description",
        "arrangement_hint": "arrangement",
    },
}

FEED = json.dumps([
    {"legal": "this first element is a legal notice, not a job"},
    {"id": "1001", "url": "https://remoteok.com/l/1001", "position": "Senior PHP Developer",
     "company": "Acme", "location": "Worldwide", "salary": "$70,000 - $90,000",
     "description": "<p>Laravel.</p>", "arrangement": "Remote"},
    {"id": "1002", "url": "https://remoteok.com/l/1002", "position": "Backend Engineer",
     "company": "Globex", "location": "Europe", "description": "<p>Go.</p>"},
]).encode()

EMPTY = json.dumps([{"legal": "notice"}]).encode()
NOT_JSON = b"<html>rate limited</html>"
WRONG_SHAPE = json.dumps({"error": "unauthorized"}).encode()


class _Probe(JsonAdapter):
    name = "probe"
    base_url = "https://example.test"

    def build_url(self, query):
        return self.base_url


def _raw(body: bytes) -> RawFetch:
    return RawFetch("probe", body, 200, datetime(2026, 8, 25))


def test_valid_feed_parses_and_skips_the_legal_element():
    result = _Probe().parse(_raw(FEED), SELECTORS)
    assert result.status == "ok"
    assert len(result.postings) == 2
    assert result.postings[0].external_id == "1001"
    assert result.postings[0].salary_raw == "$70,000 - $90,000"
    assert result.postings[1].salary_raw is None


def test_feed_with_only_the_legal_element_is_empty_not_broken():
    assert _Probe().parse(_raw(EMPTY), SELECTORS).status == "empty"


def test_non_json_body_is_broken():
    result = _Probe().parse(_raw(NOT_JSON), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["reason"] == "invalid json"


def test_json_of_the_wrong_shape_is_broken():
    result = _Probe().parse(_raw(WRONG_SHAPE), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["reason"] == "root is not a list"


def test_items_missing_required_fields_are_dropped_and_all_dropped_is_broken():
    body = json.dumps([{"legal": "x"}, {"id": "9", "company": "NoTitle"}]).encode()
    result = _Probe().parse(_raw(body), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["dropped"] == 1
```

An empty JSON feed is genuinely empty — unlike HTML, a JSON list has no ambiguity between "no results" and "markup changed", so `empty_state` has no equivalent here.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_adapters_json.py -v`
Expected: FAIL — `ImportError: cannot import name 'JsonAdapter'`

- [ ] **Step 3: Add `JsonAdapter` to `jobsearch/adapters/base.py`**

```python
# append to jobsearch/adapters/base.py
import json


def _dig(payload, path: str):
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


class JsonAdapter(Adapter):
    """Path-driven parsing shared by every http-json source."""

    fetch_mode = "http-json"

    @abstractmethod
    def build_url(self, query: dict) -> str: ...

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        return posting

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        return self._get(self.build_url(query or {}), conditional)

    def parse(self, raw: RawFetch, selectors: dict) -> ParseResult:
        fields = selectors["fields"]
        diagnostics: dict = {"item_matched": 0, "dropped": 0, "fill_rates": {}}

        try:
            payload = json.loads(raw.text())
        except json.JSONDecodeError:
            diagnostics["reason"] = "invalid json"
            return ParseResult("broken", [], diagnostics)

        items = _dig(payload, selectors.get("root", ""))
        if not isinstance(items, list):
            diagnostics["reason"] = "root is not a list"
            return ParseResult("broken", [], diagnostics)

        if selectors.get("skip_first") and items:
            items = items[1:]
        diagnostics["item_matched"] = len(items)

        if not items:
            return ParseResult("empty", [], diagnostics)

        postings: list[RawPosting] = []
        counts = {name: 0 for name in fields}

        for item in items:
            values = {}
            for name, path in fields.items():
                value = _dig(item, path) if isinstance(item, dict) else None
                value = str(value).strip() if value not in (None, "") else None
                values[name] = value
                if value:
                    counts[name] += 1

            if any(values.get(name) is None for name in REQUIRED_FIELDS):
                diagnostics["dropped"] += 1
                continue

            postings.append(self.post_process(RawPosting(
                external_id=values["external_id"],
                url=values["url"],
                title=values["title"],
                company=values["company"],
                description=values.get("description") or "",
                location=values.get("location"),
                salary_raw=values.get("salary_raw"),
                arrangement_hint=values.get("arrangement_hint"),
                employment_hint=values.get("employment_hint"),
            ), item))

        diagnostics["fill_rates"] = {
            name: round(count / len(items), 3) for name, count in counts.items()
        }

        if not postings:
            return ParseResult("broken", [], diagnostics)
        return ParseResult("ok", postings, diagnostics)
```

- [ ] **Step 4: Write `jobsearch/adapters/remoteok.py` and register it**

```python
from __future__ import annotations

from jobsearch.adapters.base import JsonAdapter


class RemoteOkAdapter(JsonAdapter):
    name = "remoteok"
    base_url = "https://remoteok.com"

    def build_url(self, query: dict) -> str:
        tag = query.get("tag")
        return f"{self.base_url}/api?tag={tag}" if tag else f"{self.base_url}/api"
```

Add `RemoteOkAdapter.name: RemoteOkAdapter` to `ADAPTERS` in `registry.py`.

RemoteOK's feed puts a legal notice in element zero — hence `skip_first`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_adapters_json.py -v`
Expected: 5 passed

- [ ] **Step 6: Capture a fixture and store the selectors**

```bash
mkdir -p tests/fixtures/remoteok
.venv/bin/python -c "
from jobsearch.adapters.remoteok import RemoteOkAdapter
import gzip, pathlib
raw = RemoteOkAdapter().fetch({'tag': 'php'})
pathlib.Path('tests/fixtures/remoteok/valid.json.gz').write_bytes(gzip.compress(raw.body))
print(len(raw.body), 'bytes')
"
```

Inspect the JSON keys, confirm the field paths in `SELECTORS` above match the live feed, and write the JSON into the `remoteok` sources row and `tests/fixtures/remoteok/selectors.json`.

- [ ] **Step 7: Commit**

```bash
git add jobsearch/adapters/ tests/test_adapters_json.py tests/fixtures/remoteok/
git commit -m "feat: JSON adapter base and RemoteOK"
```

---

### Task 13: WeWorkRemotely adapter

**Files:**
- Create: `jobsearch/adapters/weworkremotely.py`, `tests/fixtures/weworkremotely/`
- Modify: `jobsearch/adapters/registry.py`

**Interfaces:**
- Consumes: `base.JsonAdapter`
- Produces: `weworkremotely.WeWorkRemotelyAdapter` with `name = "weworkremotely"`

WeWorkRemotely publishes RSS rather than JSON. Rather than add a third base class for one source, the adapter converts the feed to the list-of-dicts shape `JsonAdapter` already consumes.

- [ ] **Step 1: Write `jobsearch/adapters/weworkremotely.py`**

```python
from __future__ import annotations

import json
from xml.etree import ElementTree

from jobsearch.adapters.base import JsonAdapter
from jobsearch.models import RawFetch


class WeWorkRemotelyAdapter(JsonAdapter):
    """RSS in, JSON-shaped out.

    Converting in fetch keeps one parse implementation for every feed source.
    A third base class for a single RSS site would be an abstraction with one
    user, and repair would then have two code paths to reason about.
    """

    name = "weworkremotely"
    base_url = "https://weworkremotely.com"

    def build_url(self, query: dict) -> str:
        category = query.get("category", "remote-programming-jobs")
        return f"{self.base_url}/categories/{category}.rss"

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        raw = self._get(self.build_url(query or {}), conditional)
        items = []
        root = ElementTree.fromstring(raw.text())
        for item in root.iterfind(".//item"):
            entry = {child.tag.split("}")[-1]: (child.text or "") for child in item}
            entry["guid"] = entry.get("guid") or entry.get("link", "")
            items.append(entry)

        return RawFetch(
            source_name=self.name,
            body=json.dumps(items).encode("utf-8"),
            http_status=raw.http_status,
            fetched_at=raw.fetched_at,
            etag=raw.etag,
            last_modified=raw.last_modified,
        )
```

- [ ] **Step 2: Register it**

Add `WeWorkRemotelyAdapter.name: WeWorkRemotelyAdapter` to `ADAPTERS` in `registry.py`.

- [ ] **Step 3: Capture a fixture and derive the selectors**

```bash
mkdir -p tests/fixtures/weworkremotely
.venv/bin/python -c "
from jobsearch.adapters.weworkremotely import WeWorkRemotelyAdapter
import gzip, pathlib
raw = WeWorkRemotelyAdapter().fetch({})
pathlib.Path('tests/fixtures/weworkremotely/valid.json.gz').write_bytes(gzip.compress(raw.body))
print(raw.body[:400])
"
```

WWR packs company and title into one `title` element (`"Acme: Senior PHP Developer"`). Confirm this against the captured feed, then write selectors mapping `external_id` to `guid`, `url` to `link`, `title` to `title`, `company` to `title`, and `description` to `description`.

Mapping `company` to `title` looks wrong but is deliberate: the required-field check runs before any post-processing, so both fields must be populated for the posting to survive. `post_process` — the hook `JsonAdapter` calls on every posting — then splits them apart:

```python
# add to WeWorkRemotelyAdapter
    def post_process(self, posting, item):
        if ":" in posting.title:
            company, _, title = posting.title.partition(":")
            posting.company = company.strip()
            posting.title = title.strip()
        return posting
```

If the captured feed turns out to carry a separate company element, map `company` to it instead and delete this override.

- [ ] **Step 4: Write the fixture test**

```python
# append to tests/test_adapters_json.py
import gzip
import pathlib

from jobsearch.adapters.weworkremotely import WeWorkRemotelyAdapter

WWR = pathlib.Path(__file__).parent / "fixtures" / "weworkremotely"


def test_wwr_valid_fixture_parses():
    selectors = json.loads((WWR / "selectors.json").read_text())
    body = gzip.decompress((WWR / "valid.json.gz").read_bytes())
    result = WeWorkRemotelyAdapter().parse(
        RawFetch("weworkremotely", body, 200, datetime(2026, 8, 25)), selectors
    )
    assert result.status == "ok"
    assert all(p.company and p.title and p.company != p.title for p in result.postings)
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add jobsearch/adapters/ tests/
git commit -m "feat: WeWorkRemotely adapter via RSS-to-JSON normalization"
```

---

### Task 14: The harvest command

**Files:**
- Create: `jobsearch/harvest.py`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: none — the classification logic it consumes is covered by Tasks 10 and 12

**Interfaces:**
- Consumes: `store`, `registry`, `config`, `models.ParseResult`
- Produces:
  - `harvest.run(conn, settings, source_names: list[str] | None, dry_run: bool) -> dict`
  - `harvest.RAW_DIR = "var/raw"`
  - `commands.harvest(args) -> dict`

- [ ] **Step 1: Write `jobsearch/harvest.py`**

```python
from __future__ import annotations

import gzip
import json
import time
from datetime import datetime
from pathlib import Path

import httpx

from jobsearch import store
from jobsearch.adapters import registry

RAW_DIR = "var/raw"
DELAY_SECONDS = 2.0


def _persist(raw, source_name: str, run_id: int) -> str:
    directory = Path(RAW_DIR) / source_name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.gz"
    path.write_bytes(gzip.compress(raw.body))
    return str(path)


def _harvest_source(conn, settings, source: dict) -> dict:
    now = datetime.now()
    run_id = store.start_run(conn, "harvest", source["id"])
    summary = {
        "source": source["name"], "run_id": run_id, "status": None,
        "fetched": 0, "new": 0, "dropped": 0, "error": None,
    }

    try:
        adapter = registry.get(source["name"])
        previous = store.last_fetch(conn, source["id"])
        conditional = (
            {"etag": previous["etag"], "last_modified": previous["last_modified"]}
            if previous else None
        )

        query = json.loads(source["query"]) if source["query"] else {}
        selectors = json.loads(source["selectors"]) if source["selectors"] else None
        if not selectors:
            raise RuntimeError(f"source '{source['name']}' has no selectors configured")

        raw = adapter.fetch(query, conditional)
        path = _persist(raw, source["name"], run_id)
        fetch_id = store.record_fetch(conn, source["id"], run_id, raw, path)

        result = adapter.parse(raw, selectors)
        summary["status"] = result.status
        summary["fetched"] = len(result.postings)
        summary["dropped"] = result.diagnostics.get("dropped", 0)
        summary["diagnostics"] = result.diagnostics

        for posting in result.postings:
            _job_id, is_new = store.upsert_posting(
                conn, source, posting, fetch_id, settings.rates, now
            )
            summary["new"] += int(is_new)

        store.mark_source(
            conn, source["id"],
            "ok" if result.status in ("ok", "empty") else "degraded",
            saw_items=bool(result.postings), now=now,
        )
        store.finish_run(conn, run_id, fetched=summary["fetched"], new=summary["new"])

    except httpx.HTTPError as exc:
        # Network trouble is not a markup change. The source keeps its status so
        # the repair path is not triggered and the activity sweep is not misled.
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        store.finish_run(conn, run_id, error=summary["error"])
    except Exception as exc:  # one bad source must never kill the harvest
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        store.finish_run(conn, run_id, error=summary["error"])

    return summary


def run(conn, settings, source_names: list[str] | None = None, dry_run: bool = False) -> dict:
    sources = store.active_sources(conn, ("http-html", "http-json"))
    if source_names:
        sources = [s for s in sources if s["name"] in source_names]

    if dry_run:
        return {
            "command": "harvest", "dry_run": True,
            "would_harvest": [s["name"] for s in sources],
        }

    results = []
    for index, source in enumerate(sources):
        if index:
            time.sleep(DELAY_SECONDS)
        results.append(_harvest_source(conn, settings, source))

    return {
        "command": "harvest",
        "sources": results,
        "totals": {
            "fetched": sum(r["fetched"] for r in results),
            "new": sum(r["new"] for r in results),
            "degraded": [r["source"] for r in results if r["status"] == "broken"],
            "errors": [r["source"] for r in results if r["status"] == "error"],
        },
    }
```

`active_sources` is queried for `http-html` and `http-json` only, which is what keeps LinkedIn out of the nightly run without a special case anywhere else.

The retry-with-backoff required by the spec belongs in `Adapter._get`. Add it there:

```python
# in jobsearch/adapters/base.py, replacing the single httpx.get call in _get
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
                response.raise_for_status()
                break
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            raise last_error
```

Add `import time` to that module.

- [ ] **Step 2: Wire the CLI**

In `jobsearch/cli.py`, inside `build_parser`:

```python
    harvest_parser = sub.add_parser("harvest", help="fetch and store postings from HTTP sources")
    harvest_parser.add_argument("--source", action="append", dest="sources")
    harvest_parser.add_argument("--dry-run", action="store_true")
```

and in `main`:

```python
    if args.command == "harvest":
        from jobsearch import commands

        emit(commands.harvest(args))
        return 0
```

In `jobsearch/commands.py`:

```python
def harvest(args) -> dict:
    from jobsearch import harvest as harvest_mod

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return harvest_mod.run(conn, settings, getattr(args, "sources", None), args.dry_run)
    finally:
        conn.close()
```

- [ ] **Step 3: Verify against live sources**

Run:
```bash
.venv/bin/jobsearch harvest --dry-run
.venv/bin/jobsearch harvest --source remoteok
```
Expected: the dry run lists four sources; the real run reports `status: "ok"` with a non-zero `fetched` and `new`.

Then confirm the data landed and merged sanely:
```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "
SELECT COUNT(*) AS jobs FROM jobs;
SELECT COUNT(*) AS postings FROM job_sources;
SELECT j.company, j.title, COUNT(js.id) AS sources
  FROM jobs j JOIN job_sources js ON js.job_id = j.id
  GROUP BY j.id HAVING sources > 1 LIMIT 10;
"
```
Expected: `postings >= jobs`. Inspect any multi-source group by hand — each must be genuinely the same role. A wrong merge here means the Task 8 gates need tightening before more data accumulates.

- [ ] **Step 4: Run the whole harvest and check degraded detection**

Run: `.venv/bin/jobsearch harvest`
Expected: every source reports `ok` or `empty`. Any `broken` means that source's selectors are wrong — fix them in the `sources` row before continuing, and update the matching `tests/fixtures/<source>/selectors.json`.

- [ ] **Step 5: Commit**

```bash
git add jobsearch/harvest.py jobsearch/cli.py jobsearch/commands.py jobsearch/adapters/base.py
git commit -m "feat: harvest command with per-source isolation and retry backoff"
```

---

### Task 15: Hard filters

**Files:**
- Create: `jobsearch/filters.py`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- Consumes: `profile.Profile`
- Produces:
  - `filters.FilterVerdict(passed: bool, reason: str | None)` — frozen
  - `filters.evaluate(job: dict, rules: dict) -> FilterVerdict` — `job` carries `arrangement`, `employment_type`, `salary_monthly_eur`, `salary_source`, `company`, `text`
  - `filters.apply(conn, profile) -> dict`
  - `commands.filter_jobs(args) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_filters.py
import pytest

from jobsearch.filters import evaluate

RULES = {
    "require_arrangement": ["remote", "hybrid"],
    "require_employment": ["full-time", "contract"],
    "min_salary_monthly_eur": 4000,
    "exclude_keywords": ["casino", "gambling"],
    "exclude_companies": ["Evil Corp"],
    "languages_required": ["english"],
}


def job(**overrides):
    base = {
        "arrangement": "remote",
        "employment_type": "full-time",
        "salary_monthly_eur": 6000,
        "salary_source": "posting",
        "company": "Acme",
        "text": "senior php developer, english required, laravel and vue",
    }
    base.update(overrides)
    return base


def test_a_matching_job_passes():
    verdict = evaluate(job(), RULES)
    assert verdict.passed is True
    assert verdict.reason is None


def test_absent_salary_passes_the_floor():
    verdict = evaluate(job(salary_monthly_eur=None, salary_source="absent"), RULES)
    assert verdict.passed is True


def test_explicit_salary_below_the_floor_fails():
    verdict = evaluate(job(salary_monthly_eur=2500), RULES)
    assert verdict.passed is False
    assert "salary" in verdict.reason


def test_unknown_arrangement_passes():
    # Absent is not a violation — the same rule that governs salary.
    assert evaluate(job(arrangement="unknown"), RULES).passed is True


def test_onsite_fails_when_remote_or_hybrid_is_required():
    verdict = evaluate(job(arrangement="onsite"), RULES)
    assert verdict.passed is False
    assert "arrangement" in verdict.reason


def test_unknown_employment_passes_but_a_known_mismatch_fails():
    assert evaluate(job(employment_type="unknown"), RULES).passed is True
    assert evaluate(job(employment_type="internship"), RULES).passed is False


def test_excluded_keyword_fails_case_insensitively():
    verdict = evaluate(job(text="backend dev for a CASINO platform"), RULES)
    assert verdict.passed is False
    assert "casino" in verdict.reason


def test_excluded_company_fails_regardless_of_legal_suffix():
    assert evaluate(job(company="Evil Corp Ltd."), RULES).passed is False


def test_missing_required_language_fails_only_when_text_is_present():
    assert evaluate(job(text="senior php developer, laravel"), RULES).passed is False
    assert evaluate(job(text=""), RULES).passed is True


def test_empty_rules_pass_everything():
    assert evaluate(job(arrangement="onsite", salary_monthly_eur=1), {}).passed is True


def test_the_first_failing_rule_is_the_reported_reason():
    verdict = evaluate(job(arrangement="onsite", salary_monthly_eur=100), RULES)
    assert verdict.reason.startswith("arrangement")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_filters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.filters'`

- [ ] **Step 3: Write `jobsearch/filters.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from jobsearch import normalize


@dataclass(frozen=True)
class FilterVerdict:
    passed: bool
    reason: str | None = None


PASSED = FilterVerdict(True, None)


def evaluate(job: dict, rules: dict) -> FilterVerdict:
    """Hard rules only. Absent data never fails a rule.

    Roughly two-thirds of postings on these boards carry no salary, no explicit
    arrangement, or no stated employment type. Treating a missing value as a
    violation would silently discard most of the queue, which is exactly the
    failure that looks like a broken scraper.
    """
    text = (job.get("text") or "").lower()

    allowed = rules.get("require_arrangement")
    arrangement = job.get("arrangement", "unknown")
    if allowed and arrangement != "unknown" and arrangement not in allowed:
        return FilterVerdict(False, f"arrangement '{arrangement}' not in {allowed}")

    allowed = rules.get("require_employment")
    employment = job.get("employment_type", "unknown")
    if allowed and employment != "unknown" and employment not in allowed:
        return FilterVerdict(False, f"employment '{employment}' not in {allowed}")

    floor = rules.get("min_salary_monthly_eur")
    amount = job.get("salary_monthly_eur")
    if floor and amount is not None and amount < floor:
        return FilterVerdict(False, f"salary €{amount}/mo below floor €{floor}/mo")

    excluded = normalize.company(job.get("company") or "")
    for name in rules.get("exclude_companies") or []:
        if normalize.company(name) == excluded:
            return FilterVerdict(False, f"excluded company '{name}'")

    for keyword in rules.get("exclude_keywords") or []:
        if keyword.lower() in text:
            return FilterVerdict(False, f"excluded keyword '{keyword.lower()}'")

    required = rules.get("languages_required") or []
    if required and text:
        missing = [lang for lang in required if lang.lower() not in text]
        if len(missing) == len(required):
            return FilterVerdict(False, f"no required language mentioned: {required}")

    return PASSED


def apply(conn, profile) -> dict:
    """Recompute filter verdicts for every active job. Idempotent by design:
    editing a rule and re-running clears jobs that no longer match."""
    rules = profile.filters
    now = datetime.now()

    with conn.cursor() as cur:
        # GROUP_CONCAT truncates at 1024 bytes by default, which would silently
        # hide keywords past the first paragraph and let excluded jobs through.
        cur.execute("SET SESSION group_concat_max_len = 1000000")
        cur.execute(
            "SELECT j.id, j.arrangement, j.employment_type, j.salary_monthly_eur, "
            "j.salary_source, j.company, "
            "CONCAT_WS(' ', j.title, GROUP_CONCAT(js.description SEPARATOR ' ')) AS text "
            "FROM jobs j LEFT JOIN job_sources js ON js.job_id = j.id "
            "WHERE j.inactive_at IS NULL GROUP BY j.id"
        )
        jobs = list(cur.fetchall())

    filtered = passed = 0
    with conn.cursor() as cur:
        for job in jobs:
            verdict = evaluate(job, rules)
            if verdict.passed:
                cur.execute(
                    "UPDATE jobs SET filtered_at=NULL, filter_reason=NULL WHERE id=%s",
                    (job["id"],),
                )
                passed += 1
            else:
                cur.execute(
                    "UPDATE jobs SET filtered_at=%s, filter_reason=%s WHERE id=%s",
                    (now, verdict.reason[:255], job["id"]),
                )
                filtered += 1
    conn.commit()

    return {"command": "filter", "evaluated": len(jobs), "passed": passed, "filtered": filtered}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_filters.py -v`
Expected: 12 passed

- [ ] **Step 5: Wire the CLI**

In `build_parser`: `sub.add_parser("filter", help="apply hard rules from profile.yaml")`.

In `main`:
```python
    if args.command == "filter":
        from jobsearch import commands

        emit(commands.filter_jobs(args))
        return 0
```

In `commands.py`:
```python
def filter_jobs(args) -> dict:
    from jobsearch import filters
    from jobsearch.profile import load_profile

    settings = load_settings(args.env)
    profile = load_profile(args.profile)
    conn = db.connect(settings)
    try:
        return filters.apply(conn, profile)
    finally:
        conn.close()
```

- [ ] **Step 6: Verify against real data and audit what gets discarded**

Run:
```bash
.venv/bin/jobsearch filter
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "
SELECT filter_reason, COUNT(*) AS n FROM jobs WHERE filtered_at IS NOT NULL
GROUP BY filter_reason ORDER BY n DESC;"
```
Expected: a reason breakdown. Read it. If `salary` dominates, the floor or the currency conversion is wrong — the whole point of keeping filtered rows with reasons is that this is visible rather than silent.

- [ ] **Step 7: Commit**

```bash
git add jobsearch/filters.py jobsearch/cli.py jobsearch/commands.py tests/test_filters.py
git commit -m "feat: hard filters where absent data never fails a rule"
```

---

### Task 16: Activity sweep

**Files:**
- Create: `jobsearch/sweep.py`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: none — the rule is a SQL predicate, verified against real data in Step 3

**Interfaces:**
- Consumes: `store`
- Produces:
  - `sweep.MISSED_RUNS_BEFORE_INACTIVE = 3`
  - `sweep.run(conn) -> dict`
  - `commands.sweep(args) -> dict`

- [ ] **Step 1: Write `jobsearch/sweep.py`**

```python
from __future__ import annotations

from datetime import datetime

MISSED_RUNS_BEFORE_INACTIVE = 3


def run(conn) -> dict:
    """Age out postings that stopped appearing, then deactivate jobs whose
    every posting is gone.

    Degraded sources are excluded entirely. A broken parser returns zero
    postings, which is indistinguishable from every job being pulled at once —
    sweeping a degraded source would mark its whole inventory dead on the exact
    day it needed repair.
    """
    now = datetime.now()

    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM sources WHERE status = 'degraded'")
        skipped = [row["name"] for row in cur.fetchall()]

        cur.execute(
            "UPDATE job_sources js JOIN sources s ON s.id = js.source_id "
            "SET js.missed_runs = js.missed_runs + 1 "
            "WHERE s.status = 'ok' AND s.enabled = TRUE AND js.inactive_at IS NULL "
            "AND js.last_seen_at < s.last_ok_at"
        )
        aged = cur.rowcount

        cur.execute(
            "UPDATE job_sources SET inactive_at = %s "
            "WHERE inactive_at IS NULL AND missed_runs >= %s",
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
```

A job survives as long as one posting survives — a role pulled from Djinni but still live on LinkedIn is still live.

- [ ] **Step 2: Wire the CLI**

`sub.add_parser("sweep", help="age out postings that stopped appearing")`, a `main` branch, and:

```python
def sweep(args) -> dict:
    from jobsearch import sweep as sweep_mod

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return sweep_mod.run(conn)
    finally:
        conn.close()
```

- [ ] **Step 3: Verify the degraded guard actually holds**

Run:
```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "UPDATE sources SET status='degraded' WHERE name='remoteok';"
.venv/bin/jobsearch sweep
```
Expected: `skipped_degraded_sources` contains `remoteok`, and no RemoteOK posting has its `missed_runs` incremented:
```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "
SELECT s.name, MAX(js.missed_runs) AS max_missed FROM job_sources js
JOIN sources s ON s.id=js.source_id GROUP BY s.name;"
```
Then restore: `UPDATE sources SET status='ok' WHERE name='remoteok';`

- [ ] **Step 4: Commit**

```bash
git add jobsearch/sweep.py jobsearch/cli.py jobsearch/commands.py
git commit -m "feat: activity sweep that never runs for a degraded source"
```

---

### Task 17: Scoring queue and score persistence

**Files:**
- Create: `jobsearch/scoring.py`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: none — pure SQL selection, verified against real data in Step 4

**Interfaces:**
- Consumes: `profile.Profile`, `store.start_run`, `store.finish_run`
- Produces:
  - `scoring.COARSE_CHARS = 800`
  - `scoring.unscored(conn, profile, limit: int) -> list[dict]`
  - `scoring.coarse_passed(conn, profile, minimum: int) -> list[dict]`
  - `scoring.record(conn, job_id: int, run_id: int, pass_no: int, payload: dict, profile) -> dict`
  - `commands.run_start(args)`, `commands.run_finish(args)`, `commands.queue(args)`, `commands.score(args)`

- [ ] **Step 1: Write `jobsearch/scoring.py`**

```python
from __future__ import annotations

import json
from datetime import datetime

COARSE_CHARS = 800

_BASE_SELECT = """
SELECT j.id, j.title, j.company, j.location, j.arrangement, j.employment_type,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
       j.salary_source, j.salary_monthly_eur,
       GROUP_CONCAT(DISTINCT s.name)  AS sources,
       (SELECT js2.url FROM job_sources js2 WHERE js2.job_id = j.id
         ORDER BY (js2.source_id = j.canonical_source_id) DESC, js2.id LIMIT 1) AS url,
       (SELECT js3.description FROM job_sources js3 WHERE js3.job_id = j.id
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


def unscored(conn, profile, limit: int = 60) -> list[dict]:
    """Jobs with no pass-2 score under the current profile hash.

    Keying on the hash means a profile edit re-queues everything automatically,
    and a run that dies between passes resumes rather than stranding postings.
    """
    with conn.cursor() as cur:
        cur.execute(
            _BASE_SELECT + _NO_PASS + " GROUP BY j.id ORDER BY j.first_seen_at DESC LIMIT %s",
            (2, profile.hash, limit),
        )
        return _shape(list(cur.fetchall()), COARSE_CHARS)


def coarse_passed(conn, profile, minimum: int = 6) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            _BASE_SELECT + _NO_PASS +
            """
              AND EXISTS (
                SELECT 1 FROM scores sc1
                WHERE sc1.job_id = j.id AND sc1.`pass` = 1
                  AND sc1.profile_hash = %s AND sc1.score >= %s
              )
            GROUP BY j.id ORDER BY j.first_seen_at DESC
            """,
            (2, profile.hash, profile.hash, minimum),
        )
        return _shape(list(cur.fetchall()), None)


def record(conn, job_id: int, run_id: int, pass_no: int, payload: dict, profile) -> dict:
    if pass_no == 1:
        stored = {
            "score": int(payload["score"]), "dimensions": None, "red_flag_penalty": 0,
            "hard_concerns": None, "strengths": None, "weaknesses": None, "verdict": None,
        }
    else:
        stored = {
            "score": int(payload["score"]),
            "dimensions": json.dumps(payload.get("dimensions") or {}),
            "red_flag_penalty": int(payload.get("red_flag_penalty", 0)),
            "hard_concerns": json.dumps(payload.get("hard_concerns") or []),
            "strengths": json.dumps(payload.get("strengths") or []),
            "weaknesses": json.dumps(payload.get("weaknesses") or []),
            "verdict": payload.get("verdict"),
        }

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scores (job_id, run_id, `pass`, score, dimensions, red_flag_penalty, "
            "hard_concerns, strengths, weaknesses, verdict, profile_version, profile_hash, "
            "scored_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, run_id, pass_no, stored["score"], stored["dimensions"],
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
```

Pass 1 stores a bare number and nothing else. Dimensions, concerns, and a verdict derived from an 800-character truncation would be fabrication dressed as analysis, so the shape of the table refuses them.

- [ ] **Step 2: Wire the CLI**

```python
    sub.add_parser("run-start", help="open a scoring run").add_argument(
        "--kind", default="score", choices=["score"])
    finish_parser = sub.add_parser("run-finish", help="close a scoring run")
    finish_parser.add_argument("--id", type=int, required=True)

    queue_parser = sub.add_parser("queue", help="jobs awaiting scoring")
    mode = queue_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--unscored", action="store_true")
    mode.add_argument("--coarse-passed", action="store_true")
    queue_parser.add_argument("--limit", type=int, default=60)
    queue_parser.add_argument("--min", type=int, default=6)

    score_parser = sub.add_parser("score", help="record a scoring verdict")
    score_parser.add_argument("--id", type=int, required=True)
    score_parser.add_argument("--run-id", type=int, required=True)
    score_parser.add_argument("--pass", type=int, choices=[1, 2], required=True, dest="pass_no")
    score_parser.add_argument("--json", required=True, help="the scoring payload as JSON")
```

In `commands.py`:

```python
def run_start(args) -> dict:
    from jobsearch import store

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return {"command": "run-start", "run_id": store.start_run(conn, args.kind)}
    finally:
        conn.close()


def run_finish(args) -> dict:
    from jobsearch import store

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        store.finish_run(conn, args.id)
        return {"command": "run-finish", "run_id": args.id}
    finally:
        conn.close()


def queue(args) -> dict:
    from jobsearch import scoring
    from jobsearch.profile import load_profile

    settings = load_settings(args.env)
    profile = load_profile(args.profile)
    conn = db.connect(settings)
    try:
        if args.unscored:
            jobs = scoring.unscored(conn, profile, args.limit)
            mode = "unscored"
        else:
            jobs = scoring.coarse_passed(conn, profile, args.min)
            mode = "coarse-passed"
    finally:
        conn.close()
    return {"command": "queue", "mode": mode, "profile_hash": profile.hash,
            "count": len(jobs), "jobs": jobs}


def score(args) -> dict:
    import json as json_mod

    from jobsearch import scoring
    from jobsearch.profile import load_profile

    settings = load_settings(args.env)
    profile = load_profile(args.profile)
    payload = json_mod.loads(args.json)
    conn = db.connect(settings)
    try:
        return scoring.record(conn, args.id, args.run_id, args.pass_no, payload, profile)
    finally:
        conn.close()
```

Add the four `main` branches following the existing pattern.

- [ ] **Step 3: Add the weights and rubric to the queue payload**

The scoring skill needs the rubric alongside the jobs. Extend `commands.queue`'s return with `"weights": profile.weights` and `"profile": profile.data` so a single command call carries everything a scoring pass needs.

- [ ] **Step 4: Verify the two-pass resume behavior by hand**

```bash
RUN=$(.venv/bin/jobsearch run-start | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
.venv/bin/jobsearch queue --unscored --limit 3
JOB=<pick an id from that output>
.venv/bin/jobsearch score --id $JOB --run-id $RUN --pass 1 --json '{"score": 8}'
.venv/bin/jobsearch queue --unscored --limit 3      # the job is STILL listed
.venv/bin/jobsearch queue --coarse-passed --min 6   # the job IS listed here
.venv/bin/jobsearch score --id $JOB --run-id $RUN --pass 2 --json '{"score":8,"dimensions":{"technical_fit":9},"red_flag_penalty":0,"hard_concerns":[],"strengths":["stack match"],"weaknesses":[],"verdict":"Strong fit."}'
.venv/bin/jobsearch queue --unscored --limit 3      # now it is gone
.venv/bin/jobsearch run-finish --id $RUN
```
Expected exactly that sequence. A pass-1 score must not remove a job from `--unscored` — if it does, a crash between passes would strand jobs at their coarse score forever.

- [ ] **Step 5: Commit**

```bash
git add jobsearch/scoring.py jobsearch/cli.py jobsearch/commands.py
git commit -m "feat: two-pass scoring queue keyed on profile hash"
```

---

### Task 18: The `/harvest` skill

**Files:**
- Create: `.claude/skills/harvest/SKILL.md`
- Test: none — verified by running it

**Interfaces:**
- Consumes: every CLI command from Tasks 14–17
- Produces: the nightly orchestration Claude follows

- [ ] **Step 1: Write `.claude/skills/harvest/SKILL.md`**

````markdown
---
name: harvest
description: Nightly job harvest — collect from HTTP sources, sweep, filter, score in two passes, notify. Invoked by cron via `claude -p "/harvest"`.
user_invocable: true
allowed-tools: [Bash, Read, Write]
---

# /harvest — Nightly Collection and Scoring

Run the full nightly pipeline. Every step is a `jobsearch` command that prints JSON;
read the JSON, do not infer state from prose.

Work from the repository root. If any command exits non-zero, report the failure and
continue to the next step — a broken stage must not block the ones after it.

## Step 1 — Collect

```bash
jobsearch harvest
```

Read `totals`. Note `totals.degraded` and `totals.errors` for Step 5; do not act on
them yet.

## Step 2 — Sweep and filter

```bash
jobsearch sweep
jobsearch filter
```

If `filter` reports more than 80% filtered, say so in the summary — that usually means
a wrong salary floor or currency rate, not a genuinely bad night.

## Step 3 — Score, pass 1

```bash
jobsearch run-start
```

Keep the `run_id`. Then:

```bash
jobsearch queue --unscored --limit 60
```

The payload carries `jobs`, `weights`, and `profile`. For each job, read the truncated
description and return a single integer 0–10 for overall promise. Be fast and coarse —
this pass exists to discard obvious mismatches, not to be right about the good ones.

Record each:

```bash
jobsearch score --id <job_id> --run-id <run_id> --pass 1 --json '{"score": <n>}'
```

## Step 4 — Score, pass 2

```bash
jobsearch queue --coarse-passed --min 6
```

Each job now carries its full description. Score it against `profile` using `weights`.

Return, per job:

```json
{
  "score": 8,
  "dimensions": {"technical_fit": 9, "seniority_fit": 7, "compensation_fit": 8,
                 "arrangement_fit": 10, "domain_fit": 6, "company_fit": 7,
                 "growth_potential": 6},
  "red_flag_penalty": 0,
  "hard_concerns": [],
  "strengths": ["..."],
  "weaknesses": ["..."],
  "verdict": "One paragraph, written to be read on a phone."
}
```

Rules:

- Every dimension in `weights` must appear in `dimensions`, each 0–10.
- `score` is the weighted sum divided by 10, rounded, **minus** `red_flag_penalty`,
  clamped to 0–10.
- `red_flag_penalty` is 0 to 3. Use it for things that would make the owner refuse the
  job outright regardless of fit — undisclosed unpaid trial, obvious agency
  reposting, a domain in `profile.domains.avoid`. Name each one in `hard_concerns`.
- Do not re-derive facts the pipeline already decided. Salary against the floor,
  arrangement, employment type, and excluded keywords were settled in Python. Judge
  what is genuinely ambiguous: whether the stack is a real match, whether the seniority
  is right, whether "competitive salary" is a warning sign, whether the domain is close
  to real experience.

Record each with `--pass 2` and the full JSON.

Then close the run:

```bash
jobsearch run-finish --id <run_id>
```

## Step 5 — Repair degraded sources

Only if Step 1 reported anything in `totals.degraded`. Follow
`.claude/skills/harvest/repair.md`.

## Step 6 — Notify

```bash
jobsearch notify --min-score 7
```

## Step 7 — Prune the raw cache

```bash
jobsearch prune-cache
```

Nothing depends on cached bytes older than a week; the 7-day cap only holds because
this runs.

## Step 8 — Summarize

Print a short summary: counts harvested and new, how many scored at each pass, how many
notified, any degraded source, any error. Three or four lines. This lands in the cron
log, so it should be readable at a glance six weeks later.
````

- [ ] **Step 2: Verify the skill runs end to end**

Run: `claude -p "/harvest"` from the repository root.
Expected: each step's JSON appears, pass 2 scores a smaller set than pass 1, and the summary reports non-zero counts. Step 5 is skipped when nothing is degraded; Step 6 will report zero until Task 19 exists.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/harvest/SKILL.md
git commit -m "feat: /harvest skill orchestrating the nightly pipeline"
```

---

### Task 19: Telegram notification

**Files:**
- Create: `jobsearch/notify.py`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `config.Settings`
- Produces:
  - `notify.needs_send(row: dict) -> bool`
  - `notify.format_message(job: dict) -> str`
  - `notify.send_all(conn, settings, min_score: int, sender=None) -> dict`
  - `commands.notify(args) -> dict`

Telegram is reached with plain `httpx` calls against the Bot API. Sending a message and answering a callback are two endpoints; a full bot framework would be a dependency with two users.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notify.py
from jobsearch.notify import format_message, needs_send


def test_a_job_with_no_notification_row_needs_sending():
    assert needs_send({"notification_id": None, "sent_at": None}) is True


def test_a_queued_but_unsent_row_needs_sending():
    # The process died between INSERT and the API call. Retry it.
    assert needs_send({"notification_id": 7, "sent_at": None}) is True


def test_an_already_sent_row_is_never_resent():
    assert needs_send({"notification_id": 7, "sent_at": "2026-08-25 09:00:00"}) is False


def test_message_leads_with_score_title_and_company():
    text = format_message({
        "id": 1, "score": 8, "title": "Senior PHP Developer", "company": "Acme",
        "location": "Remote (EU)", "arrangement": "remote",
        "salary_min": 5000, "salary_max": 7000, "salary_currency": "EUR",
        "salary_period": "month", "salary_source": "posting",
        "verdict": "Fintech, Laravel + Vue. Strong stack overlap.",
        "url": "https://djinni.co/jobs/101", "sources": "djinni,linkedin",
    })
    assert text.splitlines()[0] == "8/10 — Senior PHP Developer @ Acme"
    assert "€5000–7000/month" in text
    assert "Strong stack overlap" in text
    assert "djinni.co/jobs/101" in text


def test_message_says_salary_is_unstated_rather_than_omitting_it():
    text = format_message({
        "id": 2, "score": 7, "title": "Backend Engineer", "company": "Globex",
        "location": "Kyiv", "arrangement": "hybrid",
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "salary_period": None, "salary_source": "absent",
        "verdict": "Good stack, comp unknown.", "url": "https://x.test/1",
        "sources": "dou",
    })
    assert "salary not stated" in text


def test_message_shows_a_single_figure_without_a_range_dash():
    text = format_message({
        "id": 3, "score": 9, "title": "Dev", "company": "Acme", "location": "Remote",
        "arrangement": "remote", "salary_min": 6000, "salary_max": 6000,
        "salary_currency": "EUR", "salary_period": "month", "salary_source": "posting",
        "verdict": "x", "url": "https://x.test/2", "sources": "djinni",
    })
    assert "€6000/month" in text
    assert "–" not in text.split("\n")[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobsearch.notify'`

- [ ] **Step 3: Write `jobsearch/notify.py`**

```python
from __future__ import annotations

from datetime import datetime

import httpx

API = "https://api.telegram.org/bot{token}/{method}"
SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "PLN": "zł", "UAH": "₴"}
TIMEOUT = httpx.Timeout(15.0)

_PENDING_SQL = """
SELECT j.id, j.title, j.company, j.location, j.arrangement,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period, j.salary_source,
       sc.id AS score_id, sc.score, sc.verdict,
       n.id AS notification_id, n.sent_at,
       GROUP_CONCAT(DISTINCT s.name) AS sources,
       (SELECT js2.url FROM job_sources js2 WHERE js2.job_id = j.id
         ORDER BY (js2.source_id = j.canonical_source_id) DESC, js2.id LIMIT 1) AS url
FROM jobs j
JOIN scores sc      ON sc.id = j.latest_score_id
JOIN job_sources js ON js.job_id = j.id
JOIN sources s      ON s.id = js.source_id
LEFT JOIN notifications n ON n.job_id = j.id AND n.channel = 'telegram'
WHERE j.inactive_at IS NULL AND j.filtered_at IS NULL AND sc.score >= %s
GROUP BY j.id
ORDER BY sc.score DESC, j.first_seen_at DESC
"""


def needs_send(row: dict) -> bool:
    """True when no notification exists, or one was queued but never confirmed sent."""
    return row.get("sent_at") is None


def _salary_line(job: dict) -> str:
    if job.get("salary_source") != "posting" or not job.get("salary_currency"):
        return "salary not stated"
    symbol = SYMBOLS.get(job["salary_currency"], job["salary_currency"] + " ")
    low, high, period = job.get("salary_min"), job.get("salary_max"), job.get("salary_period")
    if low and high and low != high:
        return f"{symbol}{low}–{high}/{period}"
    figure = low or high
    return f"{symbol}{figure}/{period}" if figure else "salary not stated"


def format_message(job: dict) -> str:
    place = job.get("location") or job.get("arrangement", "")
    return "\n".join([
        f"{job['score']}/10 — {job['title']} @ {job['company']}",
        f"{place} · {_salary_line(job)} · {job.get('sources', '')}",
        "",
        job.get("verdict") or "",
        "",
        job["url"],
    ])


def _keyboard(job_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "Interested", "callback_data": f"s:{job_id}:interested"},
        {"text": "Skip",       "callback_data": f"s:{job_id}:skipped"},
        {"text": "Applied",    "callback_data": f"s:{job_id}:applied"},
    ]]}


def _default_sender(settings, job: dict) -> int:
    response = httpx.post(
        API.format(token=settings.telegram_token, method="sendMessage"),
        json={
            "chat_id": settings.telegram_chat_id,
            "text": format_message(job),
            "reply_markup": _keyboard(job["id"]),
            "disable_web_page_preview": False,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["result"]["message_id"]


def send_all(conn, settings, min_score: int = 7, sender=None) -> dict:
    """Insert the notification row BEFORE calling Telegram.

    UNIQUE(job_id, channel) then makes a duplicate structurally impossible in
    the common case. A crash after the API call but before the confirming UPDATE
    leaves sent_at NULL, which the next run retries — at-least-once with a very
    narrow window, rather than a silent duplicate on every crash.
    """
    send = sender or _default_sender

    with conn.cursor() as cur:
        cur.execute(_PENDING_SQL, (min_score,))
        candidates = [row for row in cur.fetchall() if needs_send(row)]

    sent, failed = 0, []
    for job in candidates:
        try:
            with conn.cursor() as cur:
                if job["notification_id"] is None:
                    cur.execute(
                        "INSERT INTO notifications (job_id, channel, chat_id, score_id, queued_at) "
                        "VALUES (%s,'telegram',%s,%s,%s)",
                        (job["id"], settings.telegram_chat_id, job["score_id"], datetime.now()),
                    )
                    notification_id = cur.lastrowid
                else:
                    notification_id = job["notification_id"]
            conn.commit()

            message_id = send(settings, job)

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notifications SET telegram_message_id=%s, sent_at=%s WHERE id=%s",
                    (message_id, datetime.now(), notification_id),
                )
            conn.commit()
            sent += 1
        except Exception as exc:
            failed.append({"job_id": job["id"], "error": f"{type(exc).__name__}: {exc}"})

    return {"command": "notify", "candidates": len(candidates), "sent": sent, "failed": failed}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: 6 passed

- [ ] **Step 5: Create the bot and wire the CLI**

Create a bot with @BotFather, put the token in `.env` as `TELEGRAM_TOKEN`. Get the chat id by messaging the bot once, then:

```bash
curl -s "https://api.telegram.org/bot$(grep TELEGRAM_TOKEN .env | cut -d= -f2)/getUpdates" | python3 -m json.tool
```
Take `result[0].message.chat.id` into `.env` as `TELEGRAM_CHAT_ID`.

Add the `notify` subparser (`--min-score`, default 7), the `main` branch, and:

```python
def notify(args) -> dict:
    from jobsearch import notify as notify_mod

    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        return notify_mod.send_all(conn, settings, args.min_score)
    finally:
        conn.close()
```

- [ ] **Step 6: Verify idempotency against the real database**

```bash
.venv/bin/jobsearch notify --min-score 7     # messages arrive
.venv/bin/jobsearch notify --min-score 7     # sent: 0, candidates: 0
```
Expected: the second run sends nothing. Then simulate the crash window:
```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "
UPDATE notifications SET sent_at=NULL WHERE id=(SELECT MAX(id) FROM (SELECT id FROM notifications) t);"
.venv/bin/jobsearch notify --min-score 7
```
Expected: exactly one message re-sent — the retry path works and does not fan out.

- [ ] **Step 7: Commit**

```bash
git add jobsearch/notify.py jobsearch/cli.py jobsearch/commands.py tests/test_notify.py
git commit -m "feat: idempotent Telegram notification with insert-before-send"
```

---

### Task 20: The Telegram bot process

**Files:**
- Create: `jobsearch/bot.py`, `deploy/jobsearch-bot.service`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: none — a long-poll loop against a live API

**Interfaces:**
- Consumes: `config.Settings`, `db.connect`
- Produces:
  - `bot.handle_callback(conn, data: str, message_id: int) -> str` — returns the toast text
  - `bot.run(settings) -> None`
  - `commands.bot(args) -> dict`

- [ ] **Step 1: Write `jobsearch/bot.py`**

```python
from __future__ import annotations

import time
from datetime import datetime

import httpx

from jobsearch import db
from jobsearch.notify import API, TIMEOUT

POLL_SECONDS = 50
VALID_STATUSES = {"interested", "skipped", "applied", "replied",
                  "rejected", "interviewing", "offer"}


def handle_callback(conn, data: str, message_id: int) -> str:
    """callback_data is 's:<job_id>:<status>'.

    The job id travels in the payload because it is small and stable, but the
    message id is recorded too — that is what lets a later feature edit the
    original message in place.
    """
    try:
        marker, raw_job_id, status = data.split(":", 2)
    except ValueError:
        return "Unrecognized action"

    if marker != "s" or status not in VALID_STATUSES:
        return "Unrecognized action"

    job_id = int(raw_job_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applications (job_id, status, updated_at) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE status=VALUES(status), updated_at=VALUES(updated_at)",
            (job_id, status, datetime.now()),
        )
        cur.execute(
            "UPDATE notifications SET telegram_message_id=%s WHERE job_id=%s AND channel='telegram'",
            (message_id, job_id),
        )
    conn.commit()
    return f"Marked {status}"


def run(settings) -> None:
    offset = None
    while True:
        try:
            response = httpx.get(
                API.format(token=settings.telegram_token, method="getUpdates"),
                params={"timeout": POLL_SECONDS, "offset": offset,
                        "allowed_updates": '["callback_query"]'},
                timeout=httpx.Timeout(POLL_SECONDS + 15),
            )
            response.raise_for_status()
            updates = response.json().get("result", [])
        except httpx.HTTPError:
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            query = update.get("callback_query")
            if not query:
                continue

            conn = db.connect(settings)
            try:
                text = handle_callback(
                    conn, query.get("data", ""), query["message"]["message_id"]
                )
            finally:
                conn.close()

            httpx.post(
                API.format(token=settings.telegram_token, method="answerCallbackQuery"),
                json={"callback_query_id": query["id"], "text": text},
                timeout=TIMEOUT,
            )
```

A fresh connection per callback rather than one held open for weeks — MySQL closes idle connections, and reconnect logic would be more code than this costs.

- [ ] **Step 2: Wire the CLI**

`sub.add_parser("bot", help="run the Telegram bot (long-running)")`, a `main` branch, and:

```python
def bot(args) -> dict:
    from jobsearch import bot as bot_mod

    bot_mod.run(load_settings(args.env))
    return {"command": "bot", "status": "stopped"}
```

- [ ] **Step 3: Verify by hand**

Run `.venv/bin/jobsearch bot` in one terminal, then tap a button on a notification in Telegram. Expected: the toast reads `Marked interested`, and:
```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "SELECT * FROM applications;"
```
shows the row. Tap a different button on the same message — expected: the status updates rather than erroring on the primary key.

- [ ] **Step 4: Write `deploy/jobsearch-bot.service`**

```ini
[Unit]
Description=Job search Telegram bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/www/job.search
ExecStart=%h/www/job.search/.venv/bin/jobsearch bot
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Install and enable it:
```bash
mkdir -p ~/.config/systemd/user
cp deploy/jobsearch-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jobsearch-bot
systemctl --user status jobsearch-bot --no-pager
loginctl enable-linger "$USER"     # keeps it running when not logged in
```
Expected: `active (running)`.

- [ ] **Step 5: Commit**

```bash
git add jobsearch/bot.py deploy/ jobsearch/cli.py jobsearch/commands.py
git commit -m "feat: Telegram bot with inline-button triage and systemd unit"
```

---

### Task 21: Terminal triage and listing

**Files:**
- Create: `.claude/skills/review/SKILL.md`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`, `jobsearch/store.py`
- Test: none

**Interfaces:**
- Consumes: `db.connect`
- Produces:
  - `store.list_jobs(conn, *, status=None, min_score=None, since=None, source=None, limit=50) -> list[dict]`
  - `store.set_status(conn, job_id: int, status: str, note: str | None) -> dict`
  - `commands.list_jobs(args)`, `commands.set_status(args)`

- [ ] **Step 1: Add the two store functions**

```python
# append to jobsearch/store.py
def list_jobs(conn, *, status=None, min_score=None, since=None, source=None, limit=50) -> list[dict]:
    clauses = ["j.inactive_at IS NULL", "j.filtered_at IS NULL"]
    params: list = []

    if min_score is not None:
        clauses.append("sc.score >= %s")
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
            "         ORDER BY (js2.source_id = j.canonical_source_id) DESC, js2.id "
            "         LIMIT 1) AS url, "
            "       GROUP_CONCAT(DISTINCT s.name) AS sources "
            "FROM jobs j "
            "LEFT JOIN scores sc      ON sc.id = j.latest_score_id "
            "LEFT JOIN applications a ON a.job_id = j.id "
            "JOIN job_sources js      ON js.job_id = j.id "
            "JOIN sources s           ON s.id = js.source_id "
            f"WHERE {' AND '.join(clauses)} "
            "GROUP BY j.id ORDER BY sc.score DESC, j.first_seen_at DESC LIMIT %s",
            params,
        )
        return list(cur.fetchall())


def set_status(conn, job_id: int, status: str, note: str | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applications (job_id, status, note, updated_at) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE status=VALUES(status), "
            "note=COALESCE(VALUES(note), note), updated_at=VALUES(updated_at)",
            (job_id, status, note, datetime.now()),
        )
    conn.commit()
    return {"command": "status", "job_id": job_id, "status": status}
```

- [ ] **Step 2: Wire `list` and `status` into the CLI**

```python
    list_parser = sub.add_parser("list", help="list jobs")
    list_parser.add_argument("--status")
    list_parser.add_argument("--min-score", type=int)
    list_parser.add_argument("--since")
    list_parser.add_argument("--source")
    list_parser.add_argument("--limit", type=int, default=50)

    status_parser = sub.add_parser("status", help="set application status")
    status_parser.add_argument("--id", type=int, required=True)
    status_parser.add_argument("--status", required=True)
    status_parser.add_argument("--note")
```

with the matching `commands.list_jobs` and `commands.set_status` following the established open-connection/finally-close pattern, plus `main` branches.

Also add `sources` and `prune-cache`:

```python
def sources(args) -> dict:
    settings = load_settings(args.env)
    conn = db.connect(settings)
    try:
        with conn.cursor() as cur:
            sql = "SELECT name, enabled, fetch_mode, status, consecutive_empty, last_ok_at FROM sources"
            if args.degraded:
                sql += " WHERE status = 'degraded'"
            cur.execute(sql)
            return {"command": "sources", "sources": list(cur.fetchall())}
    finally:
        conn.close()


def prune_cache(args) -> dict:
    import time
    from pathlib import Path

    from jobsearch.harvest import RAW_DIR

    cutoff = time.time() - 7 * 86400
    removed = []
    for path in Path(RAW_DIR).rglob("*.gz"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(str(path))
    return {"command": "prune-cache", "removed": len(removed)}
```

`sources` takes a `--degraded` flag; `prune-cache` takes none.

- [ ] **Step 3: Write `.claude/skills/review/SKILL.md`**

````markdown
---
name: review
description: Terminal triage of scored jobs — review the queue, set application status, and see what the filters discarded.
user_invocable: true
allowed-tools: [Bash]
---

# /review — Terminal Triage

## Default: the untriaged queue

```bash
jobsearch list --min-score 7 --limit 20
```

Present each job compactly: score, title @ company, location, salary, the verdict, and
any `hard_concerns`. Group by score band rather than listing a flat table — the owner is
deciding what to spend an hour on, not reading a report.

For each, offer: interested / skip / applied. Apply choices with:

```bash
jobsearch status --id <id> --status <status> --note "<optional>"
```

## On request: the pipeline

```bash
jobsearch list --status applied
jobsearch list --status interviewing
```

## On request: what got discarded

```bash
jobsearch list --limit 200
mysql ... -e "SELECT filter_reason, COUNT(*) FROM jobs WHERE filtered_at IS NOT NULL GROUP BY filter_reason;"
```

If one reason dominates, say so plainly — a filter eating most of the queue is a
misconfigured rule, not a bad market.
````

- [ ] **Step 4: Verify**

Run: `.venv/bin/jobsearch list --min-score 7 --limit 5` and `.venv/bin/jobsearch sources --degraded`
Expected: JSON in both cases. Set a status and confirm it round-trips through `list --status`.

- [ ] **Step 5: Commit**

```bash
git add jobsearch/store.py jobsearch/cli.py jobsearch/commands.py .claude/skills/review/
git commit -m "feat: list, status, sources, prune-cache commands and /review skill"
```

---

### Task 22: Browser ingestion and the LinkedIn skill

**Files:**
- Create: `jobsearch/ingest.py`, `.claude/skills/harvest-linkedin/SKILL.md`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: none

**Interfaces:**
- Consumes: `store.upsert_posting`, `store.source_by_name`
- Produces:
  - `ingest.run(conn, settings, source_name: str, payload: list[dict]) -> dict`
  - `commands.ingest(args) -> dict`

- [ ] **Step 1: Write `jobsearch/ingest.py`**

```python
from __future__ import annotations

from datetime import datetime

from jobsearch import store
from jobsearch.models import RawPosting

REQUIRED = ("external_id", "url", "title", "company")


def run(conn, settings, source_name: str, payload: list[dict]) -> dict:
    """Accept postings gathered outside the adapter path.

    This is the seam that makes a browser-sourced posting indistinguishable from
    an HTTP-sourced one everywhere downstream: same normalization, same
    fingerprinting, same merge rules, same filters, same scoring.
    """
    source = store.source_by_name(conn, source_name)
    now = datetime.now()
    run_id = store.start_run(conn, "harvest", source["id"])

    accepted, rejected, new = 0, [], 0
    for index, item in enumerate(payload):
        missing = [field for field in REQUIRED if not item.get(field)]
        if missing:
            rejected.append({"index": index, "missing": missing})
            continue

        posting = RawPosting(
            external_id=str(item["external_id"]),
            url=item["url"],
            title=item["title"],
            company=item["company"],
            description=item.get("description") or "",
            location=item.get("location"),
            salary_raw=item.get("salary_raw"),
            arrangement_hint=item.get("arrangement_hint"),
            employment_hint=item.get("employment_hint"),
        )
        _job_id, is_new = store.upsert_posting(conn, source, posting, None, settings.rates, now)
        accepted += 1
        new += int(is_new)

    store.mark_source(conn, source["id"], "ok", saw_items=bool(accepted), now=now)
    store.finish_run(conn, run_id, fetched=accepted, new=new)

    return {"command": "ingest", "source": source_name, "run_id": run_id,
            "accepted": accepted, "new": new, "rejected": rejected}
```

- [ ] **Step 2: Wire the CLI**

```python
    ingest_parser = sub.add_parser("ingest", help="ingest postings from stdin as JSON")
    ingest_parser.add_argument("--source", required=True)
    ingest_parser.add_argument("--stdin", action="store_true", default=True)
```

```python
def ingest(args) -> dict:
    import json as json_mod
    import sys

    from jobsearch import ingest as ingest_mod

    settings = load_settings(args.env)
    payload = json_mod.load(sys.stdin)
    conn = db.connect(settings)
    try:
        return ingest_mod.run(conn, settings, args.source, payload)
    finally:
        conn.close()
```

- [ ] **Step 3: Verify with a synthetic posting**

```bash
echo '[{"external_id":"test-1","url":"https://linkedin.com/jobs/view/test-1","title":"Senior PHP Developer","company":"Acme","location":"Remote (EU)","description":"Laravel and Vue.","salary_raw":"€6000/month","arrangement_hint":"Remote"}]' \
  | .venv/bin/jobsearch ingest --source linkedin
```
Expected: `accepted: 1, new: 1`. Confirm the job normalized correctly:
```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "
SELECT title, company, arrangement, salary_monthly_eur FROM jobs
WHERE id = (SELECT job_id FROM job_sources WHERE external_id='test-1');"
```
Expected: `arrangement = remote`, `salary_monthly_eur = 6000`. Then delete the test rows.

- [ ] **Step 4: Write `.claude/skills/harvest-linkedin/SKILL.md`**

````markdown
---
name: harvest-linkedin
description: Collect LinkedIn job postings using the owner's live Chrome session, then feed them into the normal pipeline. Manual — never run from cron.
user_invocable: true
allowed-tools: [Bash, Read, Write]
---

# /harvest-linkedin — Browser Collection

LinkedIn is auth-walled, so this reads the page the owner is already logged into. It is
manual by design.

**Boundaries:** no stored cookies, no headless bypass, no evasion, no scraping beyond the
search results the owner asked for. If LinkedIn shows a login wall or a challenge, stop
and tell the owner — do not work around it.

## Step 1 — Load the browser tools

One ToolSearch call, not several:

```
ToolSearch: select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__tabs_close_mcp
```

Call `tabs_context_mcp` first. Create a new tab rather than reusing one of the owner's.

## Step 2 — Build the search

Read `profile.yaml` for the keywords, locations, and arrangement to search. Navigate to
LinkedIn's job search with those terms, most-recent first.

## Step 3 — Read the results

Read the results list. Scroll and re-read until no new postings appear or you have 50 —
whichever comes first. For each posting capture: the job id from its URL, the URL, title,
company, location, any salary text, any arrangement label, and the description.

If the description is only available on the detail page, open the ones that look
plausible rather than all of them. A posting with no description still ingests fine —
absent data is never a failure anywhere in this system.

## Step 4 — Ingest

Write the postings to a JSON array and pipe it in:

```bash
cat /tmp/linkedin-postings.json | jobsearch ingest --source linkedin
```

Each element needs `external_id`, `url`, `title`, `company`; `location`,
`description`, `salary_raw`, `arrangement_hint`, `employment_hint` are optional.

Report `accepted`, `new`, and anything in `rejected`.

## Step 5 — Filter, score, notify

```bash
jobsearch filter
```

Then run Steps 3, 4, and 6 of `.claude/skills/harvest/SKILL.md` — the scoring and
notification path is identical. Nothing downstream knows or cares that these postings
came from a browser.

## Step 6 — Close the tab
````

- [ ] **Step 5: Verify end to end**

Run `/harvest-linkedin` with Chrome open and logged into LinkedIn.
Expected: postings ingest, get filtered and scored, and the good ones arrive in Telegram.

- [ ] **Step 6: Commit**

```bash
git add jobsearch/ingest.py jobsearch/cli.py jobsearch/commands.py .claude/skills/harvest-linkedin/
git commit -m "feat: browser ingestion seam and /harvest-linkedin skill"
```

---

### Task 23: The repair path

**Files:**
- Create: `.claude/skills/harvest/repair.md`, `jobsearch/repair.py`
- Modify: `jobsearch/cli.py`, `jobsearch/commands.py`
- Test: none — the classification it reacts to is covered by Tasks 10 and 12

**Interfaces:**
- Consumes: `store`, `harvest.RAW_DIR`
- Produces:
  - `repair.context(conn, source_name: str) -> dict` — cached bytes plus the baseline a proposal is judged against
  - `repair.baseline(conn, source_id: int, runs: int = 7) -> dict`
  - `commands.repair_context(args) -> dict`

- [ ] **Step 1: Write `jobsearch/repair.py`**

```python
from __future__ import annotations

import gzip
import json
from pathlib import Path

from jobsearch import store


def baseline(conn, source_id: int, runs: int = 7) -> dict:
    """What this source normally produces, so a proposal has something to beat."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT AVG(fetched) AS avg_fetched, MAX(fetched) AS max_fetched, COUNT(*) AS runs "
            "FROM (SELECT fetched FROM runs WHERE kind='harvest' AND source_id=%s "
            "      AND error IS NULL AND fetched > 0 ORDER BY started_at DESC LIMIT %s) t",
            (source_id, runs),
        )
        row = cur.fetchone() or {}
    return {
        "avg_fetched": float(row.get("avg_fetched") or 0),
        "max_fetched": int(row.get("max_fetched") or 0),
        "runs_considered": int(row.get("runs") or 0),
    }


def context(conn, source_name: str) -> dict:
    source = store.source_by_name(conn, source_name)
    latest = store.last_fetch(conn, source["id"])
    if latest is None:
        raise RuntimeError(f"no cached fetch for '{source_name}' — run a harvest first")

    body = gzip.decompress(Path(latest["path"]).read_bytes())
    return {
        "command": "repair-context",
        "source": source_name,
        "status": source["status"],
        "consecutive_empty": source["consecutive_empty"],
        "current_selectors": json.loads(source["selectors"]) if source["selectors"] else None,
        "cached_fetch": {
            "path": latest["path"],
            "http_status": latest["http_status"],
            "fetched_at": latest["fetched_at"],
            "bytes": len(body),
        },
        "baseline": baseline(conn, source["id"]),
    }
```

The cached bytes stay on disk rather than being returned inline — the skill reads the file directly, so a 2MB page never travels through a JSON payload.

- [ ] **Step 2: Wire the CLI**

```python
    repair_parser = sub.add_parser("repair-context", help="what a repair proposal needs")
    repair_parser.add_argument("--source", required=True)
```

with `commands.repair_context` following the established pattern.

- [ ] **Step 3: Write `.claude/skills/harvest/repair.md`**

````markdown
# Repairing a degraded source

A source is `degraded` when it returned HTTP 200 and the parser produced nothing usable.
That means the markup changed, not that the network failed and not that there were no
jobs — those cases classify differently and never reach here.

Two obligations, in order: **lose nothing today**, then **propose a fix**.

## Step 1 — Gather context

```bash
jobsearch repair-context --source <name>
```

This gives the current selectors, the path to the cached bytes from the failed run, and
the source's trailing baseline. Read the cached file:

```bash
zcat <cached_fetch.path> | head -c 200000
```

## Step 2 — Recover today's postings

Extract the postings from the cached bytes by reading the markup directly. Write them as
a JSON array and ingest them:

```bash
cat /tmp/recovered.json | jobsearch ingest --source <name>
```

This is why the raw cache exists. A markup change should cost a repair, not a day of
missed jobs.

## Step 3 — Derive new selectors

Work out `container`, `item`, `empty_state`, and each field path from the cached markup.
Then test them against the cached bytes before proposing anything:

```bash
.venv/bin/python -c "
import gzip, json, sys
from datetime import datetime
from jobsearch.adapters import registry
from jobsearch.models import RawFetch
body = gzip.decompress(open('<path>','rb').read())
selectors = json.load(open('/tmp/proposed-selectors.json'))
result = registry.get('<name>').parse(RawFetch('<name>', body, 200, datetime.now()), selectors)
print(result.status, len(result.postings))
print(json.dumps(result.diagnostics, indent=2))
for p in result.postings[:3]:
    print(p)
"
```

## Step 4 — Write the proposal

To `reports/repair-<YYYY-MM-DD>-<source>.md`. It must contain:

- **Old and new selectors**, side by side
- **Extracted count**, next to `baseline.avg_fetched` — a proposal extracting 3 where the
  source normally yields 40 is wrong even though it "works"
- **Per-field fill rates** from `diagnostics.fill_rates`, with any field that went from
  populated to empty called out explicitly
- **Three full sample postings**, rendered, not summarized
- **What changed in the markup**, in one sentence

No confidence score. A number you assign to your own output carries no calibration; the
fill rates and the count delta are the evidence the owner will actually judge this on.

## Step 5 — Stop

Do **not** write the new selectors into the `sources` table. Do not edit adapter code.
Tell the owner the report is ready and where it is. Applying a repair is their call.

When they approve, the change is two things: the `sources.selectors` row, and the
matching `tests/fixtures/<source>/selectors.json` so the fixture tests keep testing what
production actually runs.
````

- [ ] **Step 4: Verify the repair path against a real break**

Force a break and confirm the whole chain reacts:

```bash
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e "
UPDATE sources SET selectors = JSON_SET(selectors, '$.container', 'div.definitely-not-there')
WHERE name = 'djinni';"
.venv/bin/jobsearch harvest --source djinni
.venv/bin/jobsearch sources --degraded
.venv/bin/jobsearch sweep
.venv/bin/jobsearch repair-context --source djinni
```

Expected, in order: harvest reports `status: "broken"`; `sources --degraded` lists djinni;
`sweep` reports djinni in `skipped_degraded_sources` and does **not** age its postings;
`repair-context` returns the cached path and a baseline. Then restore the correct
selector.

- [ ] **Step 5: Install the cron entry**

```bash
crontab -e
```

Add:
```
0 9 * * * cd $HOME/www/job.search && /usr/bin/claude -p "/harvest" >> var/harvest.log 2>&1
```

Verify the invocation works outside an interactive session:
```bash
cd ~/www/job.search && claude -p "/harvest" | tail -20
```

- [ ] **Step 6: Final verification**

```bash
.venv/bin/pytest tests/ -v
.venv/bin/jobsearch harvest && .venv/bin/jobsearch sweep && .venv/bin/jobsearch filter
.venv/bin/jobsearch sources
systemctl --user status jobsearch-bot --no-pager
```
Expected: all tests pass, every source `ok` or `empty`, bot `active (running)`.

- [ ] **Step 7: Commit**

```bash
git add jobsearch/repair.py jobsearch/cli.py jobsearch/commands.py .claude/skills/harvest/repair.md
git commit -m "feat: degraded-source repair path with evidence-based proposals"
```
