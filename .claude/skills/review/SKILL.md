---
name: review
description: Terminal triage of scored jobs — review the queue, set application status, and see what the filters discarded.
user_invocable: true
allowed-tools: [Bash]
---

# /review — Terminal Triage

Work from the repository root. Every `jobsearch` command prints JSON; read the JSON,
do not infer state from prose.

## Default: the untriaged queue

```bash
jobsearch list --min-score 7 --limit 20
```

**If `count` is 0, check why before reporting an empty queue as normal.** Run
`jobsearch list --limit 5` (no `--min-score`) — if that also returns nothing, the
database itself is empty or every job is filtered/inactive, worth flagging outright.
If it returns jobs but every `score` is `null`, no scoring run has happened yet: say
so plainly and point the owner at `/harvest` instead of silently showing nothing. Do
not soften this into "no strong matches today" — that reads as market feedback based
on zero actual evaluations, and it isn't.

When scored jobs exist, **group by score band** (e.g. 9–10, 7–8, 6 and below if shown)
rather than a flat table — the owner is deciding what to spend the next hour on, not
reading a report. Within each band, present each job compactly: score, title @
company, location, salary (or "not stated"), and the one-line verdict.

**Always show `hard_concerns` when the list is non-empty** — never bury them inside a
table cell or omit them for brevity. Those are the things that would make the owner
refuse the job outright; hiding them defeats the point of a triage pass. If a job has
none, say so ("no hard concerns") rather than leaving the field blank.

For each job, offer a decision: interested / skip / applied. Apply it with:

```bash
jobsearch status --id <id> --status interested --note "<optional>"
jobsearch status --id <id> --status skipped    --note "<optional>"
jobsearch status --id <id> --status applied    --note "<optional>"
```

(`interviewing`, `offer`, `replied`, `rejected` are also valid statuses — use them
when the owner reports a later pipeline stage, not during first-pass triage.)

## On request: the pipeline

```bash
jobsearch list --status applied
jobsearch list --status interviewing
```

Report counts and a one-line summary per job — this is a status check, not another
full triage pass.

## On request: what got discarded

```bash
jobsearch list --limit 200
mysql -ujob_search -p"$(grep DB_PASSWORD .env | cut -d= -f2)" job_search -e \
  "SELECT filter_reason, COUNT(*) FROM jobs WHERE filtered_at IS NOT NULL GROUP BY filter_reason;"
```

`filter_reason` embeds the specific number or keyword that tripped the rule (e.g.
`"salary €2300/mo below floor €6500/mo"`), so identical-looking rows rarely share an
exact string — grouping by the literal column undercounts the real pattern. Read the
reasons as categories, not exact-match strings: bucket by their prefix (`salary ...
below floor`, `arrangement ... not in`, `excluded company`, `excluded keyword`, `no
required language`) and report counts per category.

**If one category dominates, say so plainly** — a filter eating most of the queue is a
misconfigured rule, not a bad market. Keep the owner's actual floor in mind while
judging "dominates": it's €6,500/mo, and most postings on these boards state no salary
at all (absent salary always passes the filter — only an explicit figure below €6,500
gets rejected). So a handful of salary-floor rejections against a much larger pool of
salary-unstated postings that passed through is the filter working as intended, not a
sign it's misconfigured. It would only be worth flagging if salary-floor rejections
were a large fraction of *all* postings, active and filtered combined — not just a
large fraction of the (usually small) filtered set.
