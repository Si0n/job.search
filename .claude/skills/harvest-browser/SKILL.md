---
name: harvest-browser
description: Collect postings from a board that has no usable feed or API by reading it in the owner's Chrome session, then feed them into the normal pipeline. Manual — never run from cron.
user_invocable: true
allowed-tools: [Bash, Read, Write]
---

# /harvest-browser — Browser Collection for Feedless Boards

Usage: `/harvest-browser <source>`. The source must already exist with
`fetch_mode = 'browser'`; run `jobsearch sources` to see which do.

Most boards do not need this. Try an adapter first — a feed or JSON API is cheaper,
runs unattended, and does not depend on the owner being at the machine. Reach for this
only when there is genuinely nothing to fetch.

**LinkedIn is not this skill.** It is auth-walled and has its own boundaries; use
`/harvest-linkedin`.

## Boundaries — read before starting

- No stored cookies, no headless bypass, no UA spoofing, no evasion of any kind.
- **Never solve or bypass a CAPTCHA, a Cloudflare challenge, or any other bot check.**
  If one appears, stop and tell the owner which board and what you saw. This rules some
  boards out permanently, and that is the correct outcome, not a problem to route around.
- If a board demands a login the owner is not already in, stop and ask.
- Collect only the search results the owner asked for. Do not crawl outward.
- A 503 to a plain HTTP client is not by itself permission to browse around a block —
  it means the board renders for real browsers. If the board is instead clearly refusing
  automated access, stop.

## Step 1 — Read the source's configuration

```bash
jobsearch sources
```

Find the row for `<source>`. Its `query` JSON carries `search_url` (where to navigate)
and `notes` (anything the operator should know). If `fetch_mode` is not `browser`,
stop — the board has an adapter and should be harvested with `jobsearch harvest`.

Read `profile.yaml` too, for the keywords and seniority the search should reflect if
`search_url` needs adjusting.

## Step 2 — Load the browser tools

One ToolSearch call, not several:

```
ToolSearch: select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__tabs_close_mcp
```

Call `tabs_context_mcp` first. Create a new tab rather than reusing one of the owner's.

## Step 3 — Read the results

Navigate to `search_url`. Read the results list, scroll, and re-read until no new
postings appear or you have 50 — whichever comes first.

For each posting capture: a stable id from its URL, the URL, title, company, location,
any salary text, any arrangement label, and the description.

Capture the salary text **exactly as written**, including the currency and the period
("20 000 - 25 000 PLN/month"). Do not convert it, round it, or normalise it — the
pipeline parses currency and period itself, and a helpfully-tidied figure is how a
monthly salary silently becomes an annual one.

If the description only exists on the detail page, open the postings that look
plausible rather than all of them. A posting with no description still ingests fine —
absent data never fails anything in this system.

## Step 4 — Ingest

Write the postings to a JSON array and pipe it in:

```bash
cat /tmp/<source>-postings.json | jobsearch ingest --source <source>
```

Each element needs `external_id`, `url`, `title`, `company`. Optional: `location`,
`description`, `salary_raw`, `arrangement_hint`, `employment_hint`. `url` must be a
real `http(s)://` link — anything else is rejected rather than stored.

Ingest is the same seam the adapters feed, so everything downstream — normalization,
fingerprinting, dedupe, filters, scoring — treats these exactly like fetched postings.

## Step 5 — Report

Say how many were `accepted`, how many were `new`, and anything in `rejected` with its
reason. If you stopped early — a challenge, a login wall, a layout you could not read —
say so plainly and say how many you got before stopping. A short honest count is worth
more than a full-looking one that quietly skipped half the page.
