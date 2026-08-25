---
name: harvest
description: Job harvest — collect from HTTP sources, sweep, filter, score in two passes. Run from the Claude Code CLI, or headless via `claude -p "/harvest"`.
user_invocable: true
allowed-tools: [Bash, Read, Write]
---

# /harvest — Nightly Collection and Scoring

Run the full nightly pipeline. Every step is a `jobsearch` command that prints JSON;
read the JSON, do not infer state from prose.

Work from the repository root. If any command exits non-zero, report the failure and
continue to the next step — a broken stage must not block the ones after it.

## Step 1 — Harvest

```bash
jobsearch harvest
```

Read `totals.fetched` and `totals.new`. Keep `totals.degraded` (source names whose
parser broke) and `totals.errors` (source names that hit a network/exception failure)
for Step 5 — do not act on them yet.

## Step 2 — Sweep, then filter

```bash
jobsearch sweep
jobsearch filter
```

Sweep runs first: it ages out postings that stopped appearing before filter
re-evaluates hard rules over what's still active.

`filter` returns `{"evaluated", "passed", "filtered"}`. If `filtered` is more than
roughly 80% of `evaluated`, say so explicitly in the final summary. That ratio is far
more often a wrong `min_salary_monthly_eur` or a stale currency rate than a genuinely
bad night — a filter eating the queue looks exactly like "no good jobs today" when the
actual cause is a misconfigured number.

## Step 3 — Score, pass 1 (coarse triage)

```bash
jobsearch run-start
```

Keep the returned `run_id` — every `score` call in Steps 3 and 4, and the `run-finish`
at the end of Step 4, need it.

```bash
jobsearch queue --unscored --limit 60
```

The payload carries `jobs`, `weights`, and `profile`. Each job's `description` is
truncated to 800 characters (`description_truncated: true`).

Compare the returned `count` against the `--limit` you passed (60). If they're equal,
the queue was capped and more unscored jobs almost certainly remain — note this
explicitly in the Step 8 summary. Without that check, a run that only got through part
of the backlog reads identically to one that cleared it completely.

This pass exists only to discard obvious mismatches before spending a full read on
them. On an 800-character fragment there is not enough text to support per-dimension
scores or a verdict — producing them here would be fabrication dressed as analysis.
Return **only**:

```json
{"score": 7}
```

One integer, 0–10, overall promise. Be fast and coarse. Record it:

```bash
jobsearch score --id <job_id> --run-id <run_id> --pass 1 --json '{"score": <n>}'
```

## Step 4 — Score, pass 2 (full evaluation)

```bash
jobsearch queue --coarse-passed --min 6 --limit 60
```

Each job now carries its full, untruncated description — unbounded, unlike pass 1's
800-character fragment, so `--limit` matters even more here: it's the only thing
capping how many full descriptions this step sends. Compare the returned `count`
against the `--limit` you passed (60), exactly as in Step 3 — if they're equal, more
coarse-passed survivors remain and the summary must say so.

Score every job against `profile` using `weights` — both come from this payload, from
`profile.yaml`. Never hardcode a weight or a dimension list in this file; read them
from the JSON.

### The rubric

Seven dimensions, always all seven:

- **`technical_fit`** — is the stack a genuine match against `profile.skills`
  (expert/strong/familiar), not just keyword overlap.
- **`seniority_fit`** — does the role's real scope match `years_experience` and current
  level. "Senior" titles vary wildly in what they actually ask.
- **`compensation_fit`** — Python already rejected any explicit figure below the salary
  floor (see "Already decided" below). Judge what's left ambiguous: is "competitive
  salary" with no number a warning sign or just this market's convention; does a stated
  range's low end matter.
- **`arrangement_fit`** — see below. Most postings that reach you already state remote
  explicitly; score timezone practicality and arrangement certainty, not "is it remote"
  itself.
- **`domain_fit`** — proximity to `profile.domains.preferred` (fintech, payments,
  banking, crypto). Judge adjacency, not just an exact-name match.
- **`company_fit`** — size, stage, and reputation signals present in the posting text
  itself; the profile records no explicit company-size preference, so don't invent one.
- **`growth_potential`** — what the role plausibly leads to next.

#### `arrangement_fit`, defined explicitly

The hard filter only rejects an arrangement that is explicitly stated and not `remote`
— absent data never fails a filter, so a posting whose arrangement came back `unknown`
passes through with no guarantee at all about how it actually works. Most of what
reaches you already states remote, so scoring "is it remote" as a yes/no would waste
this dimension's weight on something usually already settled. Score two other signals
instead:

1. **Timezone overlap practicality from Warsaw (CET/CEST).** A posting whose hours are
   CET/EU-adjacent scores higher than one that demands US-only overlap. A US-hours
   requirement is a **deduction, never an exclusion** — the owner has said a strong
   enough role outweighs the timezone cost. Score it down; do not zero it out or treat
   it as disqualifying.
2. **Arrangement certainty.** A posting that states "remote" explicitly scores above
   one where `arrangement` came back `unknown`. This is exactly where the filter's
   guarantee runs out — an explicit commitment is worth more than an assumption that
   merely happened to pass through unchecked.

#### Red flags — a penalty, not a dimension

`red_flag_penalty` is **not** one of the seven dimensions and carries no weight in
`weights`. It is a separate integer, 0 to 3, subtracted from the final score after the
weighted sum. Use it for things that would make the owner refuse the job outright
regardless of how well it otherwise scores: an undisclosed unpaid trial, an obvious
agency reposting rather than the real employer, a domain in `profile.domains.avoid`.
Name every flag raised in `hard_concerns`. There is no such thing as scoring well *on*
red flags — the penalty only ever subtracts, never adds, and it never appears inside
`dimensions`.

#### Already decided — do not re-derive it

Python settled these before you ever see the job; re-judging them wastes the pass and
can silently contradict a decision already made: **salary against the floor,
arrangement, employment type, and excluded keywords.** Your judgment belongs only on
what is genuinely ambiguous: whether the stack is a real match, whether the seniority
level fits, whether "competitive salary" is a warning sign, whether the domain is close
enough to real experience, whether a posting reads like an agency reposting rather than
the employer itself.

### Payload — exact keys, self-checked before you send it

```json
{
  "score": 8,
  "dimensions": {
    "technical_fit": 9, "seniority_fit": 7, "compensation_fit": 8,
    "arrangement_fit": 10, "domain_fit": 6, "company_fit": 7,
    "growth_potential": 6
  },
  "red_flag_penalty": 0,
  "hard_concerns": [],
  "strengths": ["..."],
  "weaknesses": ["..."],
  "verdict": "One paragraph, written to be read on a phone."
}
```

`score` is the weighted sum of `dimensions` — each dimension's value times its weight
from `weights`, summed — divided by the total of all the weights (sum `weights`'
values yourself; don't hardcode 100, even though that's what it is today), rounded,
**minus** `red_flag_penalty`, clamped to 0–10.

Worked from the example above: `(9×30 + 7×15 + 8×15 + 10×10 + 6×10 + 7×10 + 6×10) / 100
= 785 / 100 = 7.85` → rounds to 8, matching `"score": 8` above.

Before calling `score`, check your own payload: all seven dimension keys from `weights`
must be present in `dimensions`, each a 0–10 integer. `jobsearch score --pass 2` stores
`payload.get("dimensions") or {}` — a payload missing even one key is still accepted and
stores whatever partial dict you sent. That reads later as "analyzed, nothing found,"
not "not analyzed," and nothing downstream catches the difference. If a dimension is
missing, fix the payload yourself before sending. Never call `score` with a partial one.

Write the payload to a file with the Write tool (e.g. `/tmp/jobsearch-payload.json`),
then pass it via command substitution — **double-quoted**:

```bash
jobsearch score --id <job_id> --run-id <run_id> --pass 2 --json "$(cat /tmp/jobsearch-payload.json)"
```

Verdicts, strengths, and weaknesses routinely contain apostrophes ("the company's
stack," "it's a strong match"). A single apostrophe ends a single-quoted `'<payload>'`
argument early and the command fails — silently, per the rule at the top of this file
("if a command exits non-zero, report and continue"), so the score just never gets
recorded and nothing in the summary flags it. Double quotes don't have that problem:
the file's content is inserted as one literal argument, with apostrophes, `$`, and
backticks passed through as-is rather than re-parsed. Do not "simplify" this back to
single quotes.

When every job in the pass-2 queue is recorded, close the run:

```bash
jobsearch run-finish --id <run_id>
```

## Step 5 — Repair degraded sources

Only if Step 1's `totals.degraded` was non-empty. Follow
`.claude/skills/harvest/repair.md` — that file is the procedure for this step.

## Step 6 — Nothing to deliver

The dashboard reads live from MySQL, so a finished harvest is visible the moment the
owner reloads `http://127.0.0.1:8765/`. This is not a notification step — there is
nothing to push. If the dashboard isn't already running, `jobsearch serve` starts it
(it blocks in the foreground until stopped; that's for the owner to run, not something
to launch as part of this pipeline).

## Step 7 — Prune the raw cache

```bash
jobsearch prune-cache
```

Deletes cached raw fetches older than 7 days. Nothing depends on cached bytes that old;
the 7-day cap only holds because this step runs every night.

## Step 8 — Summarize

Three or four lines, written to be read in a cron log six weeks from now: counts
harvested and new, how many scored at each pass, how many now sit above 7 in the
dashboard, any degraded source or error, the Step 2 filter-ratio warning if it
triggered, and whether Step 3's queue was capped (say so plainly if more unscored jobs
remain for next run).
