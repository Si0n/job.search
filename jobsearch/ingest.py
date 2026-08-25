from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from jobsearch import store
from jobsearch.models import RawPosting

REQUIRED = ("external_id", "url", "title", "company")
ALLOWED_SCHEMES = ("http", "https")


def run(conn, settings, source_name: str, payload: list[dict]) -> dict:
    """Accept postings gathered outside the adapter path.

    This is the seam that makes a browser-sourced posting indistinguishable from
    an HTTP-sourced one everywhere downstream: same normalization, same
    fingerprinting, same merge rules, same filters, same scoring.

    It is also the widest-open door in the system: the HTTP adapters at least
    parse markup through fixed selectors, but this accepts whatever JSON is
    handed to it. `url` gets a scheme check because it is the one field other
    consumers (the dashboard's link-outs, /review, a future export) render
    directly — a `javascript:` URL must never reach the database, not merely
    be neutralized at render time.
    """
    source = store.source_by_name(conn, source_name)
    now = datetime.now()
    run_id = store.start_run(conn, "harvest", source["id"])

    accepted, rejected, new = 0, [], 0
    for index, item in enumerate(payload):
        missing = [field for field in REQUIRED if not item.get(field)]
        if missing:
            rejected.append({
                "index": index,
                "reason": f"missing required field(s): {', '.join(missing)}",
            })
            continue

        scheme = urlparse(item["url"]).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            rejected.append({
                "index": index,
                "reason": f"unsupported url scheme {scheme!r} (must be http or https): {item['url']!r}",
            })
            continue

        posting = RawPosting(
            external_id=str(item["external_id"]),
            url=item["url"],
            title=item["title"],
            company=item["company"],
            description=item.get("description") or "",
            location=item.get("location"),
            salary_raw=item.get("salary_raw"),
            arrangement_hint=item.get("arrangement_hint"),
            employment_hint=item.get("employment_hint"),
        )
        _job_id, is_new = store.upsert_posting(conn, source, posting, None, settings.rates, now)
        accepted += 1
        new += int(is_new)

    store.mark_source(conn, source["id"], "ok", saw_items=bool(accepted), now=now)
    store.finish_run(conn, run_id, fetched=accepted, new=new)

    return {"command": "ingest", "source": source_name, "run_id": run_id,
            "accepted": accepted, "new": new, "rejected": rejected}
