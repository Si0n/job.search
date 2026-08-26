---
name: draft
description: Write the three application blocks for one job — cover letter, email to send with the CV, and a private "why I fit" note. On demand, one job at a time.
user_invocable: true
allowed-tools: [Bash, Read, Write, Skill]
---

# /draft — Application Blocks for One Job

Usage: `/draft <job-id>`. Job ids come from the dashboard or `jobsearch list`.

One job per run. These are written for jobs the owner has decided to apply to, not
generated in bulk.

## Step 1 — Load the writing rules. Do not skip this.

```
Skill: crafting-programmer-cvs
```

That skill carries the banned-phrase list, the seven structural tells that make AI
writing recognisable, and the English-level calibration rule. **Do not restate those
rules here or work from memory of them** — they live in one place so they cannot drift,
and this file would go stale the moment that one is improved.

The single test that matters, from that skill:

> Would this person actually say this sentence, in these words, out loud in an
> interview? If they'd understand it but wouldn't have produced that phrasing
> themselves, simplify it — even if it's grammatically fine and isn't on any banned list.

The owner's English is B1–B2. That means plain vocabulary and one idea per clause. It
does **not** mean broken English, and it does not mean simplifying technical vocabulary —
PSP, SEPA, idempotency, KYC stay exactly as he uses them daily.

## Step 2 — Get the job

```bash
jobsearch draft-context --id <job-id>
```

Returns the full description, the score with its per-dimension notes, hard concerns,
strengths and weaknesses, and the owner's profile. Read the dimension notes: they say
what is genuinely strong and genuinely weak about this match, and the cover letter
should lean on the strong parts rather than claiming everything is a fit.

Also read the CV at `serhii-drozh-cv.pdf` for the concrete facts. Everything you claim
must come from it or the profile. Never invent a number, a company, or a technology.

## Step 3 — Write three blocks

All three in **English**, regardless of the posting's language.

### `cover_letter`
Under 150 words. Three short paragraphs at most.

- Open with why **this** job, naming something specific from the posting. Not "I am
  writing to apply."
- One concrete thing from his actual work that maps to it. He built an in-house payment
  service provider connecting SEPA, SWIFT, FPS, CHAPS and Canadian e-Transfers; a
  platform peaking at €300k/day; a multi-tenant model serving 503 companies; a KYC/KYB
  stack with Sumsub, Idenfy, AWS Face Liveness. Pick the **one** that fits this job, not
  a list.
- Close plainly. No "I would welcome the opportunity to discuss."

### `email`
Shorter. A subject line, then three or four sentences. This is the message the CV is
attached to, not a second cover letter — say who he is, what he is applying for, and
that the CV is attached. Include the subject as the first line, formatted
`Subject: ...`.

### `why_fit`
**Written for the owner, not the employer.** Nobody else reads this, so it is the one
block that can be blunt.

Plain bullets covering: what genuinely matches, what does not, and what to ask on a
first call. If the dimension notes flagged a real gap — a language level above his, an
unclear seniority, an agency intermediary — say so here. A "why I fit" block that only
lists positives is useless for deciding whether to apply.

## Step 4 — Check your own writing before storing

Re-read all three blocks and cut:

- Any phrase from the banned list in `crafting-programmer-cvs`.
- Consecutive sentences ending in an "-ing" clause explaining why something mattered.
- Any sentence that restates what the previous one already said.
- Any claim not traceable to the CV or the profile.
- Any sentence failing the out-loud test above.

Then read the cover letter as the employer: does it say anything a hundred other
applicants could not have written? If not, it is not finished.

## Step 5 — Store

Write the payload to a file and pass it with **double quotes** — cover letters contain
apostrophes, and a single-quoted argument would break on the first one:

```bash
jobsearch draft --id <job-id> --json "$(cat /tmp/jobsearch-draft.json)"
```

```json
{"cover_letter": "...", "email": "Subject: ...\n\n...", "why_fit": "- ..."}
```

All three keys are required and must be non-empty; the command rejects a partial draft
rather than storing a card with one blank section.

## Step 6 — Show the owner

Print the three blocks in the terminal so they can be read without opening the
dashboard, and say which job they belong to.
