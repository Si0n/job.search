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
the source's trailing baseline. If the cache aged out (prune-cache deletes files after 7
days but keeps the row), this fails with a clear message instead of a raw file error —
if that happens, there is nothing to recover for today and Step 2 is skipped; move
straight to deriving new selectors from a fresh harvest attempt once one succeeds. Read
the cached file:

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
