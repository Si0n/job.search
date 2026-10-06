# job.search

Personal job-search pipeline driven from Claude Code. Python collects postings from
job boards into MySQL and applies hard filters; Claude scores what survives against
`profile.yaml` and drafts applications; a local dashboard is where you triage.

## Requirements

- Python 3.13+
- MySQL
- [Claude Code](https://claude.com/claude-code)
- Chrome with the Claude in Chrome extension — only for LinkedIn and justjoin.it

## Install

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Create the database and user:

```sql
CREATE DATABASE job_search CHARACTER SET utf8mb4;
CREATE USER 'job_search'@'localhost' IDENTIFIED BY '<password>';
GRANT ALL ON job_search.* TO 'job_search'@'localhost';
```

```bash
cp .env.example .env      # set DB_PASSWORD
jobsearch init-db         # applies migrations/, seeds the sources
```

`init-db` is also how you apply new migrations after a `git pull`.

## First-time setup

1. Copy your CV into the repository root, e.g. `my-cv.pdf`. `*.pdf` and `profile.yaml`
   are gitignored, so neither is ever committed.
2. Start Claude Code from the repository root with the venv active (the skills call
   `jobsearch` directly):

   ```bash
   source .venv/bin/activate
   claude --chrome
   ```

3. Build the profile from the CV:

   > Read `my-cv.pdf` in this directory and create `profile.yaml` from
   > `profile.example.yaml`: identity, skills by level, domains. Take everything from
   > the CV, invent nothing. Ask me for the salary floor, arrangement, employment
   > types and exclusions instead of guessing them.

4. Review `profile.yaml`. `filters` are hard rules enforced in Python; everything above
   them is context Claude scores against. Editing the file re-queues every job for
   re-scoring automatically.
5. `/draft` reads the CV by a fixed filename and quotes the owner's own facts. Point it
   at yours:

   > Update `.claude/skills/draft/SKILL.md` for my CV `my-cv.pdf`: replace the filename
   > and the facts in Step 3 with ones from my CV.

   `/draft` also needs the `crafting-programmer-cvs` skill in `~/.claude/skills/`.

## Daily

Dashboard, in its own terminal (blocks until stopped):

```bash
jobsearch serve --open    # http://127.0.0.1:8765/
```

`--lan` also serves it to other devices on the network, with no password.

Then in `claude --chrome`, with Chrome open and logged in to LinkedIn:

> Daily run: collect justjoinit with `/harvest-browser justjoinit` and LinkedIn with
> `/harvest-linkedin` — ingest only, skip their scoring. Then run `/harvest` to fetch
> the HTTP sources and filter and score everything.

Without Chrome, `/harvest` alone covers every HTTP source.

After the harvest:

| Prompt | What it does |
|---|---|
| `/review` | Triage scored jobs in the terminal: interested / skip / applied |
| `/draft <job-id>` | Cover letter, email and a private "why I fit" note for one job |
| `Record that I applied to <url>` | Tracks an application from a posting URL (`jobsearch apply --url`) |

Job ids are shown in the dashboard and by `jobsearch list`.

## Unattended

HTTP sources can run from cron; the browser skills are manual by design.

```cron
0 7 * * * cd /path/to/job.search && . .venv/bin/activate && claude -p "/harvest" >> var/harvest.log 2>&1
```

## Sources

```bash
jobsearch sources             # every source, its fetch mode and health
jobsearch sources --degraded  # sources whose parser broke
```

`http-json` and `http-html` sources are fetched by `/harvest`. `browser` sources
(`linkedin`, `justjoinit`) are read through your own Chrome session — no stored
cookies, no CAPTCHA or bot-check bypass; if one appears, the run stops and says so.
A degraded source is repaired by `/harvest` itself (Step 5).

## Tests

```bash
pytest
```
