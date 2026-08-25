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
`description`, `salary_raw`, `arrangement_hint`, `employment_hint` are optional. `url`
must be a real `http(s)://` link — anything else is rejected, not stored.

Report `accepted`, `new`, and anything in `rejected`.

## Step 5 — Filter and score

```bash
jobsearch filter
```

Then run Steps 3 and 4 of `.claude/skills/harvest/SKILL.md` — the scoring path is
identical. Nothing downstream knows or cares that these postings came from a browser.

## Step 6 — Close the tab
