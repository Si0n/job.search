# Job Search Agent — Design

**Date:** 2026-08-25
**Status:** Approved for planning

## Purpose

Collect job postings from several job sites, score them against the owner's skills
and preferences, store them in MySQL, and push good matches to Telegram where they
can be triaged from a phone.

The owner is the job seeker. This is the candidate-side mirror of the recruiter-side
`rp-*` skill pack already installed globally.

## Guiding split

**Python never judges. Claude never parses HTML on the happy path.**

- **Python** does the deterministic work: fetch, parse, normalize, dedupe, store,
  filter, notify. Free, fast, repeatable.
- **Claude** does the judgment: scoring a posting against the profile, and repairing
  a parser when a site changes its markup.
- Claude touching markup is an exception that signals something broke.

## Run model

Hybrid, because the sources differ in difficulty:

- **Scheduled, headless** — Djinni, DOU, RemoteOK, WeWorkRemotely. A cron entry runs
  `claude -p "/harvest"`, which is a real Claude session on the owner's existing plan.
  No Anthropic API key, no per-token billing.
- **Manual, browser-driven** — LinkedIn. Auth-walled, so it runs as `/harvest-linkedin`
  against the owner's live Chrome via Chrome MCP, invoked when convenient.
- **Always on** — the Telegram bot process, handling inline-button callbacks.

Default cadence: daily at 09:00 local. Configurable in the cron entry.

## Sources in scope

| source | fetch mode | schedule | notes |
|---|---|---|---|
| Djinni | `http-html` | nightly | stable markup, no auth |
| DOU | `http-html` | nightly | stable markup, no auth |
| RemoteOK | `http-json` | nightly | public JSON feed |
| WeWorkRemotely | `http-json` | nightly | RSS/JSON feed |
| LinkedIn | `browser` | manual | live Chrome session only |

**Explicitly out of scope for v1:** Otta and Wellfound (JS SPAs, need a browser and
would only work in the manual path); Indeed and Glassdoor (active anti-bot, not worth
the maintenance). Adding a source later means one adapter module plus one `sources` row.

LinkedIn has a `sources` row (so its jobs get a `source_id`) but **no adapter module** —
it has no `fetch`/`parse` to run. `jobsearch harvest` skips every source whose
`fetch_mode` is `browser`; those enter exclusively through `jobsearch ingest`.

**LinkedIn boundary:** no stored cookies, no headless bypass, no evasion. It reads a
page the owner is already logged into, when the owner asks. That is why it is a manual
skill and not a cron job.

## Repository layout

```
job.search/
├── jobsearch/
│   ├── __main__.py            # python -m jobsearch
│   ├── cli.py                 # command surface, JSON in/out
│   ├── config.py              # .env + profile.yaml loading
│   ├── db.py                  # connection + migrations
│   ├── models.py              # RawFetch, RawPosting, Job dataclasses
│   ├── adapters/
│   │   ├── base.py            # Adapter ABC
│   │   ├── registry.py
│   │   ├── djinni.py
│   │   ├── dou.py
│   │   ├── remoteok.py
│   │   └── weworkremotely.py
│   ├── normalize.py           # RawPosting → Job + fingerprint
│   ├── store.py               # upsert, dedupe, queries
│   ├── filters.py             # hard rules from profile.yaml
│   ├── notify.py              # Telegram send
│   └── bot.py                 # long-running bot, button callbacks
├── migrations/                # 001_init.sql, 002_....sql
├── .claude/skills/
│   ├── harvest/SKILL.md
│   ├── harvest-linkedin/SKILL.md
│   └── review/SKILL.md
├── var/raw/<source>/<run_id>.gz   # cached fetches, pruned at 7 days
├── reports/                   # repair proposals
├── profile.yaml
├── .env                       # gitignored
└── docs/superpowers/specs/
```

## Components

### Adapters

The interface is split in two on purpose:

```python
class Adapter(ABC):
    name: str
    fetch_mode: str            # http-json | http-html | browser

    def fetch(self, query) -> RawFetch: ...          # → bytes + metadata, cached
    def parse(self, raw: RawFetch, selectors: dict) -> list[RawPosting]: ...
```

Splitting fetch from parse is what makes self-repair possible: when `parse` yields
nothing, Claude re-parses the already-cached bytes instead of hitting the site again.

Selectors live in the `sources.selectors` JSON column, not in code, so a repair
updates data rather than requiring a deploy. Each adapter declares three:

- `container` — the results list wrapper
- `item` — a single posting within it
- `empty_state` — the site's own "no results" marker

The third one prevents a quiet Sunday from being mistaken for a broken parser.

### Normalizer

`RawPosting → Job`. Canonical fields, plus a `fingerprint` hashed over normalized
company + title + location, so the same role found on both Djinni and LinkedIn
collapses into one entry.

### Store

MySQL. Upsert keyed on `(source_id, external_id)`. Cross-source linking via fingerprint.

### Filter

Applies hard rules from `profile.yaml`. Marks jobs `filtered` **with a reason** rather
than deleting them — seeing what is being discarded is what makes the rules tunable.

### Scorer

Claude, inside the scheduled session. Reads `jobsearch queue --unscored` as JSON,
scores against `profile.yaml`, writes back through the CLI. No scoring logic in Python.

### Notifier

Python. Sends jobs above threshold that have not been notified, then marks them.

### Bot

Separate systemd user service — the only long-running process. Inline-button callbacks
write to `applications`.

## Data model

### `sources`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `name` | VARCHAR UNIQUE | `djinni`, `dou`, … |
| `enabled` | BOOL | |
| `fetch_mode` | ENUM | `http-json`, `http-html`, `browser` |
| `base_url` | VARCHAR | |
| `query` | JSON | per-source search parameters |
| `selectors` | JSON | `container`, `item`, `empty_state`, field paths |
| `status` | ENUM | `ok`, `degraded` |
| `consecutive_empty` | INT | |
| `last_ok_at`, `last_run_at` | DATETIME | |

### `jobs`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `source_id` | FK | |
| `external_id` | VARCHAR | UNIQUE together with `source_id` |
| `fingerprint` | CHAR(64) | indexed, cross-source dedupe |
| `url`, `title`, `company`, `location` | VARCHAR | |
| `arrangement` | ENUM | `remote`, `hybrid`, `onsite`, `unknown` |
| `salary_min`, `salary_max` | INT NULL | |
| `currency` | CHAR(3) NULL | |
| `description` | MEDIUMTEXT | |
| `posted_at` | DATETIME NULL | |
| `first_seen_at`, `last_seen_at` | DATETIME | |
| `state` | ENUM | `new`, `filtered`, `scored`, `notified` |
| `filter_reason` | VARCHAR NULL | |
| `raw_ref` | VARCHAR | path to the cached fetch |

State machine: `new → filtered` (terminal, kept for auditing) or `new → scored → notified`.

### `scores`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `job_id` | FK, indexed | |
| `pass` | TINYINT | `1` coarse, `2` deep |
| `score` | TINYINT | 0–10 |
| `verdict` | TEXT | prose explanation |
| `dimensions` | JSON | per-dimension breakdown |
| `profile_version` | INT | from `profile.yaml` |
| `scored_at` | DATETIME | |

History is kept. Re-scoring after a profile edit is comparable against the old run.

### `applications`

| column | type | notes |
|---|---|---|
| `job_id` | INT PK/FK | created lazily on first status change |
| `status` | ENUM | `interested`, `skipped`, `applied`, `replied`, `rejected`, `interviewing`, `offer` |
| `note` | TEXT NULL | |
| `updated_at` | DATETIME | |

Separate from `jobs` because scraped data is overwritten on re-fetch and the owner's
decisions must never be.

### `runs`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `source_id` | FK | |
| `started_at`, `finished_at` | DATETIME | |
| `fetched`, `new` | INT | |
| `error` | TEXT NULL | |

### `schema_migrations`

Numbered `.sql` files applied by `db.py`. No ORM, no Alembic.

## profile.yaml

```yaml
version: 1

identity:
  name: ...
  headline: ...
  years_experience: ...
  location: ...
  timezone: ...
  languages: [...]

skills:
  expert: [...]
  strong: [...]
  familiar: [...]

domains:
  preferred: [...]
  avoid: [...]

preferences:
  employment: [full-time, contract]
  arrangement: [remote, hybrid]
  locations: [...]
  salary:
    min: ...
    currency: ...
    period: month | year
  company_size:
    min: ...
    max: ...

filters:            # HARD rules — Python enforces these, no LLM involved
  require_arrangement: [...]
  min_salary: ...
  exclude_keywords: [...]
  exclude_companies: [...]
  languages_required: [...]
```

Generated from the owner's LinkedIn profile, CV, and local project paths, then
hand-corrected. Salary floor, locations, and hard no-gos are not derivable from any
of those sources and must be supplied directly.

`filters` are hard and enforced in Python. Everything above it is soft context for
Claude's scoring.

## Scoring

Adapted from the existing `/rp-score` rubric, flipped from recruiter-side to
candidate-side. Weights sum to 100.

| dimension | weight |
|---|---|
| `skills_match` | 25 |
| `seniority_fit` | 15 |
| `stack_overlap` | 15 |
| `domain_fit` | 10 |
| `comp_fit` | 10 |
| `arrangement_fit` | 10 |
| `company_signal` | 5 |
| `growth_potential` | 5 |
| `red_flags` | 5 (penalty) |

**Two passes**, because forty full job descriptions is roughly 200k tokens a night for
mostly-junk:

1. **Coarse** — `jobsearch queue --unscored` returns the whole unscored queue with
   descriptions truncated to 800 characters. Claude returns a single 0–10 number per
   posting, written back as a `pass = 1` row. Default limit 60 postings per run.
2. **Deep** — `jobsearch queue --coarse-passed --min 6` returns pass-1 survivors with the
   full JD. Claude returns the final score, prose verdict, and dimension breakdown,
   written back as a `pass = 2` row.

`--unscored` means *no pass-2 row exists*, and a job moves to `state = scored` only after
pass 2. So a run that dies between the passes resumes correctly rather than stranding
postings at their coarse score.

Notification threshold: score ≥ 7.

## Flows

### Nightly (`cron` → `claude -p "/harvest"`)

1. `jobsearch harvest` — per enabled source: fetch → cache raw bytes → parse →
   normalize → upsert. JSON summary out.
2. `jobsearch filter` — hard rules mark `filtered` with a reason.
3. **Score** — Claude runs pass 1 then pass 2, writing back via `jobsearch score`.
4. **Repair** — for any `degraded` source, Claude reads that source's cached bytes,
   extracts the postings so nothing is lost that day, ingests them via
   `jobsearch ingest`, and writes proposed selectors to `reports/repair-<date>.md`.
   Selector changes are **not** auto-applied; they need the owner's sign-off.
5. `jobsearch notify --min-score 7` — sends unnotified matches, marks them `notified`.

### LinkedIn (`/harvest-linkedin`, manual)

Chrome MCP opens the search results in the owner's live session, reads and extracts the
postings, then pipes them to `jobsearch ingest --source linkedin --stdin`. From there the
filter/score/notify path is identical. `ingest` is the shared seam, so browser-sourced and
HTTP-sourced jobs are indistinguishable downstream.

### Triage

A Telegram message per match:

```
🎯 8/10 — Senior PHP Developer @ Acme
📍 Remote (EU) · 💰 €5–7k
Fintech, Laravel + Vue. Strong stack overlap, comp above floor.
[Interested] [Skip] [Applied]
```

Button callback → `jobsearch status --id N --status applied`.

`/review` gives the same triage in the terminal for longer sessions.

## CLI surface

```
jobsearch init-db
jobsearch harvest [--source X] [--dry-run]
jobsearch ingest --source X --stdin
jobsearch filter
jobsearch queue --unscored [--limit N]
jobsearch queue --coarse-passed --min N
jobsearch score --id N --pass 1|2 --score X [--verdict "..."] [--dimensions JSON]
jobsearch notify [--min-score N]
jobsearch status --id N --status S [--note "..."]
jobsearch list [--status S] [--min-score N] [--since D]
jobsearch sources [--degraded]
jobsearch prune-cache
```

All commands emit JSON so Claude can consume them without parsing prose.

## Failure handling

### Breakage vs. emptiness

| observed | meaning | action |
|---|---|---|
| non-200 / timeout | network or block | retry 3× with backoff, log to `runs`, status stays `ok` |
| 200, container present, 0 items, no empty-state marker | markup changed | `degraded` → repair path |
| 200, empty-state marker present | genuinely nothing today | normal, no flag |
| 200, container selector missing | page restructured | `degraded` → repair path |

Network problems are not markup problems. Conflating them burns tokens on pointless
repair runs.

### Everything else

- **Isolation** — each source runs in its own try/except and logs to `runs`. One site
  down never kills the harvest.
- **Raw cache** — gzipped per run at `var/raw/<source>/<run_id>.gz`, pruned at 7 days.
  Bounded disk, and repair always has material.
- **Idempotency** — `queue --unscored` and `notify` (unnotified only) mean a crashed run
  resumes on the next tick. No partial-state cleanup, no compensating logic.
- **Telegram down** — jobs stay unnotified and go out on the next run. The bot being
  down never loses data.
- **Politeness** — per-source delay, honest User-Agent, sequential rather than parallel.

## Security

- `.env` is gitignored and holds the MySQL credentials and the bot token.
- The app connects as a dedicated `job_search` MySQL user, not `root`, keeping it off the
  same account as unrelated databases on this host.
- No credentials in the repo, in `profile.yaml`, or in skill files.

## Testing

None. The project has no test setup and the owner chose not to introduce one, per the
standing rule in `~/.claude/CLAUDE.md`. Verification is running the CLI against live
sources and inspecting the JSON output.

## Out of scope for v1

- Otta, Wellfound, Indeed, Glassdoor
- Automatic application submission
- CV tailoring per posting (the `crafting-programmer-cvs` skill already covers authoring)
- Any web UI — Telegram and the terminal are the interfaces
- Auto-applying repaired selectors without sign-off

## Build order

Five phases, each independently useful and verifiable by running the CLI.

1. **Foundation** — `job_search` database and dedicated MySQL user, migrations, `db.py`,
   `config.py`, CLI skeleton, and `profile.yaml` generated from the owner's LinkedIn, CV,
   and local projects then hand-corrected.
2. **Harvest** — `Adapter` ABC, the four HTTP adapters, `normalize.py`, `store.py`,
   `filters.py`. Verifiable: `jobsearch harvest` fills the `jobs` table.
3. **Scoring** — `queue`/`score` commands and the `/harvest` skill wiring both passes.
   Verifiable: postings carry scores and verdicts.
4. **Notification** — `notify.py`, the Telegram bot, its systemd user unit, and the
   `/review` terminal triage. Verifiable: matches arrive on the phone and the buttons
   write status back.
5. **LinkedIn and repair** — `/harvest-linkedin` over Chrome MCP, plus the degraded-source
   repair path and its report format.

Phases 1–4 deliver a working nightly pipeline. Phase 5 adds the two things that need
Claude in the loop for reasons other than scoring.
