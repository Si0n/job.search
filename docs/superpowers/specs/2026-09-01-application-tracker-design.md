# Application Tracker — Design

**Date:** 2026-09-01
**Status:** Approved for planning
**Revision:** 2
**Extends:** `2026-08-25-job-search-design.md`

## Purpose

The existing system ends at triage: a job is read on the dashboard and marked
`interested`, `applied` or `skipped`. What happens after that — when the application
went out, which CV went with it, what was written in the cover letter, which stage it
reached, what the next step is and when — lives nowhere.

This adds the second half: a record of every application, its stage history, the
material submitted with it, and the activity and statistics derived from both.

The tracker also accepts applications the harvest never found. A job applied to
directly on a company site is entered by pasting its URL; the system fetches and parses
it, and it becomes an ordinary `jobs` row.

## Relationship to the existing system

Triage and tracking are separate lifecycles and stay separate tables.

Triage answers *should I read this* and is defined by absence — the dashboard's inbox is
exactly `applications.job_id IS NULL`. Tracking answers *where did this go* and only
exists once an application has been sent. Merging them would put `skipped` in the same
vocabulary as `Interview with CTO`, and would force the inbox query, the `status` CLI
command, the keyboard triage and the `/review` skill through a rework that buys nothing.

The existing `applications` table is therefore renamed to `triage`, which is what it has
always meant, and the new table takes the name it deserves. Call sites: `store.set_status`,
`dashboard.fetch_rows`, `dashboard._TODAY_STATS_SQL`, `store.list_jobs`,
`models.APPLICATION_STATUSES` (which becomes `TRIAGE_STATUSES`, with its importers in
`cli.py` and `server.py`), the `applications` section of the v1 spec, and
`.claude/skills/review/SKILL.md`.

```
harvest ─► filter ─► score ─► dashboard ─► triage
                                   │
                                   └─► APPLICATIONS ─► stages ─► statistics
                                              ▲
                                   paste URL ─┘  (fetch, parse, becomes a jobs row)
```

## Data model

One migration, `016_add_application_tracker.sql`.

### `stages` — the vocabulary

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `slug` | VARCHAR(64) UNIQUE | `tech_interview` |
| `label` | VARCHAR(64) | `Technical interview` |
| `weight` | SMALLINT | sort weight. `rank` is reserved in MySQL 8 |
| `kind` | ENUM | `active`, `won`, `lost` |
| `builtin` | BOOLEAN | seeded rows are not deletable from the UI |
| `created_at` | DATETIME | |

Seeded:

| weight | kind | slug | label |
|---|---|---|---|
| 10 | active | `applied` | Applied |
| 20 | active | `recruiter_screen` | Recruiter screen |
| 25 | active | `waiting_feedback` | Waiting for feedback |
| 30 | active | `test_task` | Test task / take-home |
| 40 | active | `tech_interview` | Technical interview |
| 50 | active | `team_interview` | Team interview |
| 60 | active | `cto_interview` | Interview with CTO / founder |
| 70 | active | `final_interview` | Final / culture interview |
| 80 | active | `reference_check` | Reference check |
| 90 | won | `offer` | Offer |
| 100 | won | `accepted` | Accepted |
| 0 | lost | `declined` | Declined by them |
| 0 | lost | `withdrawn` | Withdrawn by me |
| 0 | lost | `ghosted` | Ghosted / no answer |

This vocabulary is the owner's workflow, not a universal recruitment state machine.
`waiting_feedback` is a state where the others are events, so
`Technical interview → Waiting for feedback → Technical interview` is a legal and
expected path — the ladder records where an application sits, and sitting still is a
place it can sit. Modelling waiting as separate metadata would buy nothing.

A stage is a row, not an enum, for one reason: a custom stage must sort and terminate
correctly. `Pair programming round` typed into a free-text column has no weight and no
terminality, so it cannot be placed in the ladder and cannot be told apart from a
rejection. Creating it as a row — label, weight, kind — costs one insert and makes it
available in the dropdown from then on. Its `slug` is derived from the label
(lowercased, runs of non-alphanumerics collapsed to `_`, trimmed to 64 chars); a
collision with an existing slug is rejected rather than silently reused, so two stages
can never share an identity.

### `applications`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `job_id` | INT FK UNIQUE | one application per job |
| `applied_at` | DATETIME | back-datable |
| `stage_id` | INT FK | current stage — denormalised from the newest event |
| `stage_at` | DATETIME | when the current stage was entered |
| `next_action` | VARCHAR(255) NULL | `tech interview call` |
| `next_action_at` | DATETIME NULL | future dates allowed |
| `cv_file_id` | INT FK NULL | the exact file sent |
| `cover_letter` | TEXT NULL | what was sent, not what was drafted |
| `why_company` | TEXT NULL | |
| `salary_expectation` | VARCHAR(120) NULL | |
| `notice_period` | VARCHAR(120) NULL | |
| `answers` | JSON NULL | `[{question, answer}]` — a board's own fields |
| `created_at` / `updated_at` | DATETIME | |

`stage_id`, `stage_at`, `next_action` and `next_action_at` duplicate the newest event.
This is the same trade `jobs.latest_score_id` already makes: the list view sorts and
filters on them, and recomputing the newest event per row would mean a correlated
subquery for every application on every page load. Both writes happen in one
transaction in a single store function, so they cannot drift.

Four fixed text columns plus `answers`: cover letter, why-this-company, salary
expectation and notice period recur on nearly every application and are worth counting
and searching; everything else a given board asks is a question/answer pair, and putting
those in JSON means a new question costs a row rather than a migration.

`UNIQUE(job_id)` states that re-applying to the same posting is not a case worth
modelling yet. Two roles at the same company are two `jobs` rows and are unaffected.
Dropping the constraint later is a one-line migration; the list and detail queries do
not assume it.

### `application_events` — the timeline

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `application_id` | INT FK | |
| `kind` | ENUM | `applied`, `stage`, `note` |
| `stage_id` | INT FK NULL | the stage moved *into*; NULL for `note` |
| `occurred_at` | DATETIME | when it happened — back- or future-datable |
| `note` | TEXT NULL | |
| `next_action` | VARCHAR(255) NULL | |
| `next_action_at` | DATETIME NULL | |
| `created_at` | DATETIME | when it was recorded |
| | | INDEX `(application_id, occurred_at)` |

`occurred_at` and `next_action_at` answer different questions and both accept dates in
the future. Moving to `Technical interview` today with the call booked for the 5th is
one event: `occurred_at` = today, `next_action` = "tech interview call",
`next_action_at` = 2026-09-05. Recording last Thursday's recruiter call is the same
event shape with `occurred_at` back-dated — which is why statistics count `occurred_at`
and not `created_at`: a call belongs to the week it happened in.

### `cv_files`

| column | type | notes |
|---|---|---|
| `id` | INT PK | |
| `sha256` | CHAR(64) UNIQUE | same bytes twice = one row |
| `filename` | VARCHAR(255) | the name it was uploaded under, and how it is listed |
| `content_type` | VARCHAR(100) | as sniffed, not as claimed — the download header reads it |
| `size_bytes` | INT | |
| `path` | VARCHAR(512) | `var/cv/<sha256><ext>` |
| `uploaded_at` | DATETIME | |

Content-addressed because the point is fidelity: the CV is edited over time, and the
record must be of the file actually sent, not of whatever `serhii-drozh-cv.pdf` happens
to contain today. Re-using last week's CV across ten applications stores it once.

It is a file table, not a document manager. `sha256` plus immutable files is the whole
idea; `content_type` exists because `/api/cv/<id>` has to send one, `size_bytes` because
it is free, and the filename is how a CV is identified in the picker. No labels, no
versions, no tags.

## Manual entry by URL

`POST /api/lookup-url` takes a URL, fetches it, and returns extracted fields for review.
It writes nothing. Extraction is tried in order:

1. **Known ATS single-posting APIs.** `boards.greenhouse.io/<slug>/jobs/<id>`,
   `jobs.lever.co/<slug>/<id>`, `jobs.ashbyhq.com/<slug>/<id>` — the vendor's own JSON,
   flattened by the `_greenhouse` / `_lever` / `_ashby` readers that already exist in
   `adapters/ats.py`. Imported, not rewritten: this step is three URL patterns and three
   imports, and it would not be worth writing from scratch for this feature alone.
2. **`schema.org/JobPosting` JSON-LD** in the page: `title`,
   `hiringOrganization.name`, `description`, `datePosted`, `jobLocation`, `baseSalary`,
   `employmentType`. Most career pages publish it because Google Jobs requires it. This
   is the path that carries most URLs and is built first.
3. **`og:` / `<title>` fallback**, description from the largest text block. Returned with
   `needs_review: true`, which the form uses to flag the guessed fields rather than
   presenting a scraped `Careers | Acme` as a job title.

Structured extraction is trusted; the fallback needs review; the human corrects either.
That is the whole hierarchy — there is no confidence score and no third state.

The response pre-fills an editable form. `POST /api/applications` carries the URL and
the fields as the owner corrected them — the server does not re-fetch, since the whole
point of the review step is that the human's version wins. Those fields are validated the
way `ingest.run` validates a browser-collected posting (required fields present, `url`
scheme in `http`/`https`), then built into a `RawPosting` and passed to
`store.upsert_posting` with the `manual` source, so a hand-added job is
indistinguishable downstream: same normalization, same fingerprint, same salary
parsing, same merge rules, same eligibility for scoring. This is the seam `ingest`
already established for browser-collected postings.

The `manual` source row: `priority = 90`, `fetch_mode = 'http-html'`,
**`enabled = FALSE`**. Three consequences, all wanted:

- `harvest` never tries to crawl it as a board (it has no listing page).
- `sweep` never ages its postings out — both its UPDATEs require `s.enabled = TRUE`. An
  application must not disappear because the company took the ad down; that is precisely
  when the tracker matters most.
- Its low priority means that when a manual posting merges into an already-harvested job,
  the harvested board keeps ownership of title and company.

Merging works in the tracker's favour, but not through `dedupe.can_merge`: fingerprinting
cannot see the URL at all — it is built from title, company, location, employment type and
arrangement, and a manually pasted posting carries no arrangement hint, so it normalises to
`unknown` and keeps location where a remote harvested job's fingerprint drops it. Identical
title and company can still produce different fingerprints. So `tracker.job_id_for_url`
checks `job_sources.url` for the pasted link (both trailing-slash forms) before any upsert
runs: pasting a Djinni URL already in the database attaches straight to the existing
canonical job, and the application lands on the job that already carries a score and a
draft. Fingerprint-based merging is the fallback, for a URL genuinely new to this run.

One change to `filters.apply`: its selection gains
`AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)`. A job applied to
must never be stamped `filtered_at = 'salary below floor'` — the decision has been made,
and the filter verdict would be a false statement about a live application. Tracker
queries never filter on `filtered_at` or `inactive_at` for the same reason.

## HTTP surface

`server.py`'s `do_GET`/`do_POST` if-chain is replaced by a list of
`(method, compiled regex, handler)` tuples matched once, with the host, content-type and
body-size guards unchanged and still in front of the dispatch.

| method | route | purpose |
|---|---|---|
| GET | `/applications` | the tracker page |
| GET | `/api/stages` | vocabulary for the dropdowns |
| GET | `/api/applications` | list view, sorted and filtered |
| GET | `/api/applications/<id>` | detail plus its timeline |
| GET | `/api/activity?limit=` | all events across applications, DESC |
| GET | `/api/tracker-stats` | the five buckets |
| GET | `/api/cv/<id>` | download the exact file sent |
| POST | `/api/lookup-url` | fetch and extract; writes nothing |
| POST | `/api/applications` | create, from `job_id` or from a looked-up URL |
| POST | `/api/applications/<id>` | edit fields |
| POST | `/api/applications/<id>/stage` | transition |
| POST | `/api/stages` | add a custom stage |
| POST | `/api/cv` | upload |

Validation mirrors `drafts.parse_draft` and `server.parse_status_request`: every field
checked against a whitelist or a type before it reaches SQL. Limits — `label` ≤ 64,
`weight` 0–100, `kind` in the enum, `note` ≤ 4000, `next_action` ≤ 255, `cover_letter`
and `why_company` ≤ 8000 each, `answers` ≤ 20 pairs, dates ISO-8601 and rejected before
2000-01-01 or beyond two years ahead.

## Security

Three exposures are new, and `--lan` means an unauthenticated writer on the same wifi.

**SSRF — `/api/lookup-url`.** This endpoint exists to make the server fetch a URL the
caller chooses, which is the shape of the attack. The defence is one function,
`manual.safe_fetch_url()`, not a networking layer: `http`/`https` only; the hostname is
resolved and rejected if it lands on loopback, private, link-local, multicast or reserved
space; redirects are followed manually, at most three, each hop re-checked; 20s timeout;
response body capped at 2 MB.

**Upload — `/api/cv`.** Base64 inside a JSON body, not multipart. `cgi` was removed in
Python 3.13 (this venv is 3.13), so multipart would mean hand-rolling a parser, and JSON
preserves the existing CSRF defence — which works precisely because a cross-origin HTML
form cannot send `application/json`. Cap 10 MB decoded, raised from the 64 KB body limit
for this route only. Type is decided by magic bytes (`%PDF-`, `PK\x03\x04`, or valid
UTF-8 text), never by the client's `content_type`.

**Download — `/api/cv/<id>`.** Served from the row's `path`, which is `<sha256><ext>`, so
a hostile filename has nothing to traverse. Sent with `Content-Disposition: attachment`,
the stored content type, and `X-Content-Type-Options: nosniff` — a booby-trapped upload
must not be able to execute on the dashboard's own origin.

## Dashboard

A second page, `static/applications.html`, served at `/applications`. The theme tokens
and the small shared helpers (`esc`, `age`, `safeUrl`, the fetch wrapper) move out of
`index.html` into `static/app.css` and `static/util.js`, which both pages load; the two
headers cross-link. No framework, no build step — the v1 spec's judgement that
`http.server` plus static HTML plus vanilla JS is the right level of engineering holds
here too.

### List

```sql
ORDER BY (st.kind = 'lost') ASC,   -- declined, withdrawn, ghosted sink
         st.weight DESC,           -- offer above CTO above screen
         a.stage_at DESC           -- within a stage, most recently moved first
```

Each row: company · title, the stage chip coloured by kind, time in stage, the next
action badged **overdue** / **today** / dated, a CV chip that downloads, the pipeline
score where the job has one, and a link out to the posting. Filters: stage kind, free
text, and a *needs attention* toggle (`next_action_at <= today`).

An overdue action badges but does not reorder. Stage weight is the requested sort, and a
chase-up should not outrank an offer.

### Detail

The inbox's slide-over panel, reused: timeline DESC, the submitted texts, CV download,
and the transition form — stage dropdown grouped active / won / lost with a *custom…*
option, occurred-on date defaulting to today, note, next action, next-action date. One
POST writes the event and the denormalised current stage in a single transaction.
A *prefill from draft* button copies `drafts.cover_letter` and `drafts.why_fit` into the
form for editing; what is stored is what was actually sent.

### Activity

The same events across all applications, DESC, joined to title and company — *applied to
Backend Engineer at Acme*, *moved Acme → Technical interview*, notes inline. A segmented
control at the top switches List / Activity, keeping it one page.

## Statistics

Boundaries come from a pure `buckets(now)` function so they can be tested rather than
buried in SQL.

| bucket | window |
|---|---|
| previous day | yesterday 00:00–23:59 |
| current week | Monday 00:00 → now |
| previous week | the Monday–Sunday before |
| current month | 1st 00:00 → now |
| previous month | the whole preceding calendar month |

Per bucket:

| metric | definition |
|---|---|
| sent | applications with `applied_at` in the window |
| advanced | stage moves into a stage of kind `active` or `won`, excluding the event that creates the application |
| offers | events into kind `won` |
| lost | events into kind `lost` |
| response rate | share of that bucket's *sent* that ever left `Applied` |

Five metrics, and no sixth. An earlier draft counted *interviews* as stages of
`weight >= 30`, which silently called a take-home an interview; the honest version of
that number is `advanced`, which is already here.

Response rate is a cohort measure over the applications sent in the window, evaluated at
query time, so last month's figure rises as replies arrive. It is labelled as a cohort in
the UI; the other five count events by `occurred_at`.

## CLI surface

Deliberately thin — the tracker is a web tool, and the CLI exists so a skill can read
state without a browser.

```
jobsearch apply --url <url> [--applied-at YYYY-MM-DD]   # lookup, create, emit the row
jobsearch applications [--kind active|won|lost]         # the list view as JSON
```

No CLI for stage transitions or uploads.

## Testing

`tests/test_tracker.py`

- list ordering: a `lost` offer sorts below an `active` recruiter screen; equal weights
  break by `stage_at`
- `buckets()` across a month boundary, a year boundary, and a Monday
- statistics aggregation from fixture rows, including an event back-dated into the
  previous week
- custom-stage validation: weight bounds, unknown kind, duplicate slug
- one transition leaves the event and the denormalised columns agreeing

`tests/test_manual.py`

- extraction from fixture HTML: a Greenhouse posting, a page carrying JSON-LD, and one
  with neither — the last must set `needs_review` rather than assert a wrong title
- the SSRF guard table: `localhost`, `127.0.0.1`, `10.0.0.5`, `169.254.169.254`, and a
  public URL redirecting to a private one
- a manual posting whose fingerprint matches a harvested job merges into it

`tests/test_server.py` additions

- route-table dispatch, including a 404 for an unknown path
- content-type and body-size guards on each new POST
- upload rejected by magic bytes; download carries `attachment` and `nosniff`

## Build order

1. Migration: rename `applications` → `triage`, create the four tables, seed `stages` and
   the `manual` source. Update the existing call sites and the v1 spec's table section.
2. `jobsearch/tracker.py` — validation, queries, transitions, statistics. Pure functions
   first, with their tests.
3. `jobsearch/manual.py` — `safe_fetch_url()`, then JSON-LD extraction, then the ATS
   patterns, then the `og:`/`<title>` fallback, with fixtures for each.
4. `jobsearch/cv.py` — hashing, storage, magic-byte sniffing.
5. `server.py` — route table, then the handlers.
6. `static/app.css`, `static/util.js`, `static/applications.html`, and the cross-links.

## Out of scope

- Authentication and multi-user support — unchanged from v1
- Reminders, notifications, calendar sync for `next_action_at`
- Parsing recruiter email to advance stages automatically
- More than one application per job
- Offer comparison, per-stage SLAs, funnel charts beyond the six metrics above
- Editing or versioning a stored CV in place — a changed CV is a new upload
- Any schema for the `answers` questions themselves — no field definitions, no form
  templates, no question table. It is a list of `{question, answer}` and stays one
