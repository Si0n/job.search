# Job Search Agent — Design

**Date:** 2026-08-25
**Status:** Revised after review, then revised again when the owner replaced
Telegram delivery with a local browser dashboard — approved for planning
**Revision:** 3

## Purpose

Collect job postings from several job sites, score them against the owner's skills and
preferences, store them in MySQL, and surface them in a local browser dashboard where
they can be read in detail and triaged.

The owner is the job seeker. This is the candidate-side mirror of the recruiter-side
`rp-*` skill pack already installed globally.

## Guiding split

**Python never judges. Claude never parses HTML on the happy path.**

- **Python** does everything deterministic: fetch, parse, normalize, dedupe, store,
  filter, and serving the dashboard. Free, fast, repeatable.
- **Claude** does only what needs judgment: scoring a posting against the profile, and
  repairing a parser when a site changes its markup.
- Claude touching markup is an exception that signals something broke.

The boundary is strict in both directions. Anything Python can determine from data —
salary against a floor, arrangement, employment type, required language, excluded
company or keyword — Python determines. Claude is never paid to rediscover a fact that
is sitting in a column.

## Pipeline

```
                    ┌──────────────┐
                    │   sources    │
                    └──────┬───────┘
                           │  fetch (HTTP) / ingest (browser)
                    ┌──────▼───────┐
                    │ raw_fetches  │
                    └──────┬───────┘
                           │  parse
                    ┌──────▼───────┐
                    │ job_sources  │   one posting on one site
                    └──────┬───────┘
                           │  dedupe / merge
                    ┌──────▼───────┐
                    │     jobs     │   one canonical opportunity
                    └──┬────────┬──┘
                       │        │
                  filtering   scoring
                       │        │
                       └───┬────┘
                    ┌──────▼────────┐
                    │   dashboard   │  localhost, reads live
                    └──────┬────────┘
                    ┌──────▼────────┐
                    │ applications  │  triage decisions
                    └───────────────┘
```

Each arrow is a separate, re-runnable CLI command. No stage mutates a previous stage's
output in place.

## Run model

Hybrid, because the sources differ in difficulty:

- **Scheduled, headless** — Djinni, DOU, RemoteOK, WeWorkRemotely. A cron entry runs
  `claude -p "/harvest"`, which is a real Claude session on the owner's existing plan.
  No Anthropic API key, no per-token billing.
- **Manual, browser-driven** — LinkedIn. Auth-walled, so it runs as `/harvest-linkedin`
  against the owner's live Chrome via Chrome MCP, invoked when convenient.
- **On demand** — `jobsearch serve`, a localhost-only dashboard the owner opens in a
  browser. It reads live from MySQL and writes triage decisions straight back, so it is
  never stale and never needs regenerating.

Default cadence: the owner runs `/harvest` from the Claude Code CLI a few times a day.
A cron entry is optional and documented, not required.

**Claude is orchestration, not infrastructure.** Every pipeline stage is a CLI command
runnable by hand or by any scheduler. If the Claude invocation mechanism changes, the
application survives unchanged — harvest, filter, and the dashboard still work; only scoring
stops happening, and unscored jobs simply queue up until it resumes.

## Sources in scope

| source | fetch mode | schedule | notes |
|---|---|---|---|
| Djinni | `http-html` | nightly | stable markup, no auth |
| DOU | `http-html` | nightly | stable markup, no auth |
| RemoteOK | `http-json` | nightly | public JSON feed |
| WeWorkRemotely | `http-json` | nightly | RSS/JSON feed |
| LinkedIn | `browser` | manual | live Chrome session only |

**Explicitly out of scope for v1:** Otta and Wellfound (JS SPAs, browser-only); Indeed
and Glassdoor (active anti-bot, not worth the maintenance).

**Two ways in, not one interface.** Source adapters cover HTTP sources and implement
`fetch`/`parse`. Browser ingestion covers LinkedIn, which has no adapter module at all —
it has a `sources` row so its postings get a `source_id`, and it enters exclusively
through `jobsearch ingest`. `jobsearch harvest` skips every source whose `fetch_mode`
is `browser`. LinkedIn is not an adapter and the code should not pretend otherwise.

**LinkedIn boundary:** no stored cookies, no headless bypass, no evasion. It reads a page
the owner is already logged into, when the owner asks. That is why it is a manual skill
and not a cron job.

## Repository layout

```
job.search/
├── jobsearch/
│   ├── __main__.py            # `python -m jobsearch` → cli.main
│   ├── cli.py                 # command surface, JSON in/out
│   ├── config.py              # .env, profile.yaml, currency rates
│   ├── db.py                  # connection + migrations
│   ├── models.py              # RawFetch, RawPosting, ParseResult, Job
│   ├── adapters/
│   │   ├── base.py            # Adapter ABC
│   │   ├── registry.py
│   │   ├── djinni.py  dou.py  remoteok.py  weworkremotely.py
│   ├── normalize.py           # RawPosting → canonical fields
│   ├── salary.py              # compensation string → structured
│   ├── dedupe.py              # fingerprint + merge rules
│   ├── store.py               # upsert, queries
│   ├── filters.py             # hard rules from profile.yaml
│   ├── dashboard.py           # DB rows → the view model the page renders
│   ├── server.py              # localhost http.server: serves the page, accepts triage
│   └── static/index.html      # the page itself: markup, CSS, client-side JS
├── migrations/                # 001_init.sql, …
├── tests/                     # pytest, fixture-driven
│   └── fixtures/<source>/{valid,empty,changed-markup}.html.gz
├── .claude/skills/
│   ├── harvest/SKILL.md
│   ├── harvest-linkedin/SKILL.md
│   └── review/SKILL.md
├── var/raw/<source>/<run_id>.gz
├── reports/                   # repair proposals
├── profile.yaml
├── pyproject.toml             # console_scripts: jobsearch = jobsearch.cli:main
└── .env                       # gitignored
```

**Entrypoint:** the installed console script `jobsearch` is canonical and used
everywhere. `python -m jobsearch` is an equivalent fallback for a non-installed checkout.

## Data model

### `sources`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `name` | VARCHAR UNIQUE | `djinni`, `dou`, … |
| `enabled` | BOOL | |
| `fetch_mode` | ENUM | `http-json`, `http-html`, `browser` |
| `priority` | TINYINT | tie-break when merging canonical fields, lower wins |
| `base_url` | VARCHAR | |
| `query` | JSON | per-source search parameters |
| `selectors` | JSON | see below |
| `status` | ENUM | `ok`, `degraded` |
| `consecutive_empty` | INT | |
| `last_ok_at`, `last_run_at` | DATETIME | |

`selectors` shape:

```json
{
  "container": "...",
  "item": "...",
  "empty_state": "...",
  "minimum_items": 1,
  "fields": { "title": "...", "company": "...", "...": "..." }
}
```

`empty_state` is optional — not every site has a reliable "no results" marker.
`minimum_items` is the fallback: a container that matches but yields fewer than the
minimum is suspect, which catches a selector accidentally matching an unrelated
container and returning zero postings while looking superficially valid.

### `raw_fetches`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `source_id` | FK | |
| `run_id` | FK | |
| `path` | VARCHAR | `var/raw/<source>/<run_id>.gz` |
| `content_hash` | CHAR(64) | skip re-parse when unchanged |
| `http_status` | SMALLINT | |
| `etag`, `last_modified` | VARCHAR NULL | enables conditional requests |
| `fetched_at` | DATETIME | |

One row per fetch, not per job. `etag`/`last_modified` are replayed as request headers
on the next run — politeness and speed for free.

### `job_sources` — one posting on one site

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `job_id` | FK, indexed | canonical opportunity |
| `source_id` | FK | UNIQUE together with `external_id` |
| `external_id` | VARCHAR | |
| `url` | VARCHAR | |
| `raw_fetch_id` | FK NULL | null for browser-ingested |
| `description` | MEDIUMTEXT | normalized text, not HTML |
| `description_hash` | CHAR(64) | only rewrite `description` when this changes |
| `salary_raw` | VARCHAR NULL | the original string, verbatim |
| `posted_at` | DATETIME NULL | |
| `first_seen_at`, `last_seen_at` | DATETIME | |
| `inactive_at` | DATETIME NULL | stopped appearing at this source |
| `source_meta` | JSON | anything source-specific worth keeping |

Descriptions live here, not on `jobs` — two sources describe the same role differently,
and `description_hash` makes edit-and-repost detection per-posting, which is the only
level it means anything at.

### `jobs` — one canonical opportunity

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `fingerprint` | CHAR(64) indexed | see dedupe below |
| `title`, `company`, `location` | VARCHAR | merged, per source `priority` |
| `arrangement` | ENUM | `remote`, `hybrid`, `onsite`, `unknown` |
| `employment_type` | ENUM | `full-time`, `part-time`, `contract`, `internship`, `unknown` |
| `salary_min`, `salary_max` | INT NULL | |
| `salary_currency` | CHAR(3) NULL | |
| `salary_period` | ENUM NULL | `hour`, `day`, `month`, `year` |
| `salary_type` | ENUM NULL | `employee`, `contractor`, `unknown` |
| `salary_source` | ENUM | `posting`, `inferred`, `absent` |
| `salary_monthly_eur` | INT NULL | **derived**, comparison only |
| `first_seen_at`, `last_seen_at` | DATETIME | max across postings |
| `inactive_at` | DATETIME NULL | set when every posting is inactive |
| `filtered_at` | DATETIME NULL | |
| `filter_reason` | VARCHAR NULL | |
| `latest_score_id` | INT NULL, indexed | newest pass-2 score; plain column, not an enforced FK, to avoid a `jobs`↔`scores` circular constraint |

**No `state` column.** Filtering, scoring, notification, and application are independent
facts, each expressed by its own timestamp or table. A job can be scored 8, notified,
marked interested, still active, and visible on two sources simultaneously — and raising
the notification threshold from 7 to 8 is then just a re-query, not a lifecycle problem.

**Canonical field merge:** when postings disagree, the value from the source with the
lowest `priority` wins, except salary, where the most specific available value wins
(an explicit range beats a single figure beats absent) regardless of priority.

**`salary_monthly_eur` is derived and disposable.** Computed at write time from a static
rate table in config, recomputable at any point, used only for floor comparison and
sorting. Original currency, period, and raw string are never overwritten and remain the
source of truth. No FX conversion happens inside the database.

### `runs`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `kind` | ENUM | `harvest`, `score` |
| `source_id` | FK NULL | set for `harvest`, null for `score` |
| `started_at`, `finished_at` | DATETIME | |
| `fetched`, `new` | INT NULL | |
| `error` | TEXT NULL | |

One table rather than separate harvest and scoring run logs — same shape, same queries,
one fewer concept. A scoring run spans both passes, so pass 1 and pass 2 rows in
`scores` share a `run_id`.

### `scores`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `job_id` | FK, indexed | |
| `run_id` | FK | which scoring run produced this |
| `pass` | TINYINT | `1` coarse, `2` deep |
| `score` | TINYINT | 0–10, after red-flag penalty |
| `dimensions` | JSON | per-dimension raw scores |
| `hard_concerns` | JSON | structured blockers |
| `strengths`, `weaknesses` | JSON | structured, not prose |
| `verdict` | TEXT | one-paragraph summary for the notification |
| `profile_version` | INT | human-facing |
| `profile_hash` | CHAR(64) | authoritative |
| `scored_at` | DATETIME | |

History is kept; nothing is updated in place.

**Profile identity is the hash, not the integer.** `profile_hash` is computed over the
normalized profile at load time. `profile_version` is a convenience label. Comparing
score history across a profile edit keys on the hash, so a forgotten version bump cannot
silently make historical data misleading.

### `applications`

| column | type | notes |
|---|---|---|
| `job_id` | INT PK/FK | created lazily on first status change |
| `status` | ENUM | `interested`, `skipped`, `applied`, `replied`, `rejected`, `interviewing`, `offer` |
| `note` | TEXT NULL | |
| `updated_at` | DATETIME | |

Business state only — and the inbox marker: a job with no row here has not been
triaged. The dashboard's default view is exactly `applications.job_id IS NULL`.

### `schema_migrations`

Numbered `.sql` files applied by `db.py`. No ORM, no Alembic.

## Deduplication

`fingerprint = sha256(norm_company | norm_title | norm_location | norm_employment_type)`

Normalization strips legal suffixes (`Ltd`, `GmbH`, `LLC`), seniority decorations that
duplicate the title, punctuation, and case. When `arrangement` is `remote`, the location
component is dropped entirely — `Remote EU` and `Remote` are the same opportunity and
must not fingerprint apart.

**Merging is conservative and explicit.** Two postings merge into one canonical job only
when all four hold:

1. equal `fingerprint`
2. **different `source_id`** — two postings on the *same* board are two requisitions, and
   stay separate
3. both `last_seen_at` within a **30-day window** — a repost three months later is a
   genuinely new opportunity and gets its own canonical job
4. if both carry descriptions, token Jaccard similarity ≥ 0.6 — this is what separates
   two different departments that happen to share a title

Failing any condition creates a new canonical job rather than merging. False splits are
cheap and visible; false merges destroy data and are nearly impossible to notice.

## Activity model

`last_seen_at` is stamped on every `job_sources` row observed in a harvest. A posting not
seen in a completed, healthy run for **3 consecutive runs** gets `inactive_at` set. A
canonical job goes inactive only when every one of its postings is inactive — a role
pulled from Djinni but still live on LinkedIn is still live.

**The disappearance sweep never runs for a `degraded` source.** A broken parser returns
zero postings, which is indistinguishable from every job being pulled at once; running
the sweep would mark an entire source's inventory dead on the day it needed repair.
Degraded sources are skipped until they return to `ok`.

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

weights:              # scoring rubric, tunable without a code change
  technical_fit: 30
  seniority_fit: 15
  compensation_fit: 15
  arrangement_fit: 10
  domain_fit: 10
  company_fit: 10
  growth_potential: 10

filters:              # HARD rules — Python enforces, no LLM involved
  require_arrangement: [remote, hybrid]
  require_employment: [full-time, contract]
  min_salary_monthly_eur: ...
  languages_required: [...]
  exclude_keywords: [...]
  exclude_companies: [...]
```

Generated from the owner's LinkedIn profile, CV, and local project paths, then
hand-corrected. Salary floor, locations, and hard no-gos appear in none of those sources
and must be supplied directly.

## Filtering

Python evaluates `filters` and stamps `filtered_at` + `filter_reason`. Filtered jobs are
kept, never deleted — seeing what is being discarded is what makes the rules tunable.
`jobsearch filter` is idempotent and recomputes from scratch, so editing a rule and
re-running clears jobs that no longer match.

**Missing data never fails a filter.** Roughly 60–70% of Djinni and DOU postings carry no
salary at all. A posting with `salary_source = absent` **passes** `min_salary_monthly_eur`
and is flagged for Claude to judge; only an explicit figure below the floor is filtered
out. The same rule holds for every filter: absent is not a violation. Filtering on
unknowns would silently eat most of the queue, which is exactly the failure that looks
like "the scraper stopped working".

## Scoring

Adapted from the existing `/rp-score` rubric, flipped candidate-side. Weights live in
`profile.yaml` and sum to 100.

| dimension | weight | what it covers |
|---|---|---|
| `technical_fit` | 30 | stack, languages, frameworks, depth against the skill tiers |
| `seniority_fit` | 15 | scope and level against years and current title |
| `compensation_fit` | 15 | including judging vague or absent figures |
| `arrangement_fit` | 10 | remote/hybrid/onsite and timezone reality |
| `domain_fit` | 10 | industry proximity to actual experience |
| `company_fit` | 10 | size, stage, reputation signals |
| `growth_potential` | 10 | what the role leads to |

`skills_match` and `stack_overlap` are merged into a single `technical_fit`. Kept apart
they double-count: for a PHP/Laravel/Vue developer the same five technologies score in
both, making stack worth 40%+ of a rubric that claims 25%.

**`red_flags` is a penalty, not a dimension.** It is not in the weighted sum. Claude
returns a penalty of 0 to −3 applied to the final 0–10 score, with each flag named in
`hard_concerns`. A weighted-positive "red flags: 5" is incoherent — scoring well on
having red flags is not a thing.

**Structured output, not prose.** Every scoring call returns:

```json
{
  "score": 8,
  "dimensions": { "technical_fit": 9, "seniority_fit": 7, "...": 0 },
  "red_flag_penalty": 0,
  "hard_concerns": [],
  "strengths": ["..."],
  "weaknesses": ["..."],
  "verdict": "one paragraph for the notification"
}
```

`verdict` is for the human to read; everything else is queryable.

**Pass 1 returns only `{"score": N}`.** The full structure above is pass 2 exclusively —
producing dimensions, concerns, and a verdict from an 800-character truncation would be
fabrication dressed as analysis. `jobsearch score --pass 1` accepts and stores nothing
else.

**Two passes**, because forty full job descriptions is roughly 200k tokens a night for
mostly-junk:

1. **Coarse** — `jobsearch queue --unscored` returns unfiltered, unscored jobs with
   descriptions truncated to 800 characters. Claude returns a single 0–10 per posting,
   written as a `pass = 1` row. Default limit 60 per run.
2. **Deep** — `jobsearch queue --coarse-passed --min 6` returns pass-1 survivors with the
   full description. Claude returns the full structure above, written as `pass = 2`, and
   `jobs.latest_score_id` is updated to point at it.

`--unscored` means *no pass-2 row exists for the current `profile_hash`*, so a run that
dies between passes resumes correctly, and a profile edit naturally re-queues everything
without a manual reset.

Dashboard default filter: score ≥ 7. It is a view setting, not a gate — every scored
job stays queryable, and the threshold is a control on the page rather than a number
baked into a delivery step.

## Flows

### Nightly (`cron` → `claude -p "/harvest"`)

1. `jobsearch harvest` — opens a `runs` row per source; fetch → record `raw_fetches` →
   parse → normalize → dedupe → upsert `job_sources` and `jobs`. JSON summary out.
2. `jobsearch sweep` — activity model; skips degraded sources.
3. `jobsearch filter` — hard rules stamp `filtered_at` + `filter_reason`.
4. **Score** — Claude runs pass 1 then pass 2 under one `run_id`, writing back via
   `jobsearch score`.
5. **Repair** — for any `degraded` source, Claude reads that source's cached bytes,
   extracts the postings so nothing is lost that day, ingests them, and writes a proposal
   to `reports/repair-<date>.md`. Selector changes are **never** auto-applied.
6. Nothing to deliver. The dashboard reads live, so a completed harvest is visible the
   moment the owner reloads the page.

### LinkedIn (`/harvest-linkedin`, manual)

Chrome MCP opens the search results in the owner's live session, reads and extracts the
postings, then pipes them to `jobsearch ingest --source linkedin --stdin`. From there the
path is identical. `ingest` is the shared seam, so browser-sourced and HTTP-sourced
postings are indistinguishable downstream.

### Triage

`jobsearch serve` binds `127.0.0.1:8765` and opens on the inbox: scored, unfiltered,
active jobs with no `applications` row, ranked by score.

Each card carries the score and its dimension breakdown, the verdict, any hard concerns,
salary as posted, the arrangement, every source the posting was found on with a link to
each, and the full description behind a disclosure. Three buttons — Interested, Skip,
Applied — POST the decision, write `applications`, and drop the card from the inbox.

Filters on the page: minimum score, source, arrangement, and a free-text search across
title, company, and description. All client-side over the JSON the page already loaded.

`/review` gives the same triage in the terminal when the owner would rather stay in the
CLI.

## Adapter interface

```python
class Adapter(ABC):
    name: str
    fetch_mode: str                 # http-json | http-html

    def fetch(self, query: dict, conditional: dict | None) -> RawFetch: ...
    def parse(self, raw: RawFetch, selectors: dict) -> ParseResult: ...
```

```python
@dataclass
class ParseResult:
    status: Literal["ok", "empty", "broken"]
    postings: list[RawPosting]
    diagnostics: dict           # matched counts per selector, field fill rates
```

**The adapter classifies its own outcome.** `harvest` reads `ParseResult.status` and acts;
it does not re-derive breakage from a scatter of loosely defined conditions, and it does
not need to know that `http-json` and `http-html` fail differently. `diagnostics` is what
the repair report is built from.

## Failure handling

### Breakage vs. emptiness

| observed | `ParseResult.status` | action |
|---|---|---|
| non-200 / timeout | — (fetch raises) | retry 3× with backoff, log to `runs`, source stays `ok` |
| 200, empty-state marker present | `empty` | normal, no flag |
| 200, container present, items ≥ `minimum_items` | `ok` | normal |
| 200, container present, items below minimum, no empty-state | `broken` | `degraded` → repair |
| 200, container selector missing | `broken` | `degraded` → repair |

Network problems are not markup problems. Conflating them burns tokens on pointless
repair runs and, worse, feeds the activity sweep a false disappearance.

### Repair proposals

A repair report is only actionable if it proves the new selectors extract *sane* data,
not merely *some* data — syntactically valid selectors that yield garbage are the real
failure mode. `reports/repair-<date>.md` must contain:

- old selectors and new selectors, side by side
- extracted count, against the trailing 7-run average for that source
- per-field fill rate (`title` 100%, `salary` 34%, …), against the same baseline
- three full sample records, rendered
- any field that went from populated to empty, called out explicitly

No model-emitted confidence score. It carries no calibration and reads as authority it
has not earned; the fill rates and the count delta are the actual evidence.

### Everything else

- **Isolation** — each source runs in its own try/except and logs to `runs`. One site down
  never kills the harvest.
- **Raw cache** — gzipped per run, pruned at 7 days, indexed by `raw_fetches`. Fixtures
  for the test suite are copied out of it.
- **Idempotency** — every stage is re-runnable and derives its work from data rather than
  from a lifecycle flag. A crashed run resumes on the next tick with no cleanup.
- **Dashboard down** — nothing is lost; it holds no state of its own. Restart it.
- **Politeness** — per-source delay, honest User-Agent, conditional requests via
  `etag`/`last_modified`, sequential rather than parallel.

## Security

- `.env` is gitignored and holds the MySQL credentials. There are no other secrets.
- The dashboard binds `127.0.0.1` only — never `0.0.0.0`. It has no authentication
  because it is not reachable off the host, and that is the whole of its security model.
  Any change to the bind address needs authentication first.
- The app connects as a dedicated `job_search` MySQL user, never `root`, keeping it off
  the account that reaches unrelated databases on this host.
- No credentials in the repo, in `profile.yaml`, or in skill files.

## Testing

pytest, fixture-driven, deliberately small — roughly 10–20 tests across the seven areas
where a silent bug corrupts stored data and no amount of reading catches it. This is a
considered exception to the standing "no test setup, no tests" rule, because the
self-repair architecture is *defined* by whether a parser produced sane output, which
cannot be verified by inspection.

| area | what it protects |
|---|---|
| `normalize` | title/company/location normalization, arrangement detection |
| `salary` | the real format zoo: `€5,000/month`, `$100k-$130k`, `$40-$60/hour`, `PLN 25,000`, `£70k`, `€500/day`, `up to €8k`, `competitive` |
| `dedupe` | fingerprint stability, and the four merge conditions — especially the negative cases |
| `filters` | hard rules, and that absent data passes rather than fails |
| adapters | `parse` against saved fixtures per source: `valid`, `empty`, `changed-markup` |
| `ParseResult` | that `changed-markup` classifies `broken` and `empty` classifies `empty` |
| `dashboard` | row-set → view model: merged multi-source jobs, absent salary, missing score |
| `server` | triage request parsing — rejects unknown status values and non-integer job ids |

Fixtures come out of `var/raw/`, so they cost nothing to produce. Nothing else gets
tests — no coverage target, no tests for CLI plumbing or DB round-trips.

## CLI surface

```
jobsearch init-db
jobsearch harvest [--source X] [--dry-run]
jobsearch ingest --source X --stdin
jobsearch sweep
jobsearch filter
jobsearch queue --unscored [--limit N]
jobsearch queue --coarse-passed --min N
jobsearch score --id N --pass 1|2 --json '<structured result>'
jobsearch serve [--port N]
jobsearch status --id N --status S [--note "..."]
jobsearch list [--status S] [--min-score N] [--since D] [--source X]
jobsearch sources [--degraded]
jobsearch prune-cache
```

All commands emit JSON so Claude consumes them without parsing prose.

## Build order

Six phases, each independently useful and verifiable by running the CLI.

1. **Foundation** — `job_search` database and dedicated MySQL user, migrations, `db.py`,
   `config.py`, CLI skeleton, and `profile.yaml` generated from the owner's LinkedIn, CV,
   and local projects, then hand-corrected.
2. **Harvest** — `Adapter` ABC and `ParseResult`, the four HTTP adapters, `raw_fetches`,
   `normalize.py`, `salary.py`, `dedupe.py`, `store.py`. Verifiable: `jobsearch harvest`
   fills `job_sources` and `jobs` with correct merges.
3. **Filtering and activity** — `filters.py`, `jobsearch sweep`, plus the fixture tests
   for normalize/salary/dedupe/filters.
4. **Scoring** — `queue`/`score`, structured output, both passes, and the `/harvest` skill.
5. **Dashboard** — migration 003 dropping `notifications`, `dashboard.py`, `server.py`,
   the page, and `/review`.
6. **LinkedIn and repair** — `/harvest-linkedin` over Chrome MCP, the degraded-source
   repair path, and the repair report format.

Phases 1–5 deliver a working nightly pipeline. Phase 6 adds the two things that need
Claude in the loop for reasons other than scoring.

## Out of scope for v1

- Otta, Wellfound, Indeed, Glassdoor
- Automatic application submission
- CV tailoring per posting (`crafting-programmer-cvs` already covers authoring)
- Any remotely-reachable UI, authentication, or multi-user support
- Telegram or any other push delivery
- Auto-applying repaired selectors without sign-off
- Currency conversion as stored truth; only the derived comparison column
