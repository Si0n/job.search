from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from jobsearch import normalize


@dataclass(frozen=True)
class FilterVerdict:
    passed: bool
    reason: str | None = None


PASSED = FilterVerdict(True, None)


def evaluate(job: dict, rules: dict, now: datetime | None = None) -> FilterVerdict:
    """Hard rules only. Absent data never fails a rule.

    Roughly two-thirds of postings on these boards carry no salary, no explicit
    arrangement, or no stated employment type. Treating a missing value as a
    violation would silently discard most of the queue, which is exactly the
    failure that looks like a broken scraper.
    """
    text = (job.get("text") or "").lower()

    # Boards re-list a posting for as long as it is open, so a job harvested
    # today may have been published months ago — first_seen_at measures when we
    # noticed it, not how old it is, and cannot stand in for this.
    max_age = rules.get("max_age_days")
    posted = job.get("posted_at")
    if max_age and posted is not None:
        age = ((now or datetime.now()) - posted).days
        if age > max_age:
            return FilterVerdict(False, f"posted {age}d ago, older than {max_age}d")

    allowed = rules.get("require_arrangement")
    arrangement = job.get("arrangement", "unknown")
    if allowed and arrangement != "unknown" and arrangement not in allowed:
        return FilterVerdict(False, f"arrangement '{arrangement}' not in {allowed}")

    allowed = rules.get("require_employment")
    employment = job.get("employment_type", "unknown")
    if allowed and employment != "unknown" and employment not in allowed:
        return FilterVerdict(False, f"employment '{employment}' not in {allowed}")

    # The floor is a full-month figure, so it is only a fair comparison against
    # employment that bills a full month. A part-time posting states a smaller
    # number because it buys fewer hours, not because it pays badly, and no
    # board publishes the fraction — so there is nothing to pro-rate by and the
    # honest move is to let scoring's compensation_fit judge it instead.
    floor = rules.get("min_salary_monthly_eur")
    amount = job.get("salary_monthly_eur")
    exempt = rules.get("salary_floor_exempt_employment") or []
    if floor and amount is not None and employment not in exempt and amount < floor:
        return FilterVerdict(False, f"salary €{amount}/mo below floor €{floor}/mo")

    excluded = normalize.company(job.get("company") or "")
    for name in rules.get("exclude_companies") or []:
        if normalize.company(name) == excluded:
            return FilterVerdict(False, f"excluded company '{name}'")

    for keyword in rules.get("exclude_keywords") or []:
        if keyword.lower() in text:
            return FilterVerdict(False, f"excluded keyword '{keyword.lower()}'")

    # No languages_required rule: a posting simply not mentioning a language is
    # absent data, not a violation, and a keyword-presence check can't tell that
    # apart from "explicitly requires a language the owner lacks" — the one case
    # that should fail. Undetectable reliably from free text, so this is left to
    # scoring, same treatment as domain_fit.

    return PASSED


def apply(conn, profile) -> dict:
    """Recompute filter verdicts for every active job. Idempotent by design:
    editing a rule and re-running clears jobs that no longer match."""
    rules = profile.filters
    now = datetime.now()

    with conn.cursor() as cur:
        # GROUP_CONCAT truncates at 1024 bytes by default, which would silently
        # hide keywords past the first paragraph and let excluded jobs through.
        cur.execute("SET SESSION group_concat_max_len = 1000000")
        cur.execute(
            "SELECT j.id, j.arrangement, j.employment_type, j.salary_monthly_eur, "
            "j.salary_source, j.company, "
            # MAX, not MIN: a job merged from several boards is as fresh as the
            # most recent publication of it. Taking the oldest would age a job
            # out on the strength of whichever board listed it first.
            "MAX(js.posted_at) AS posted_at, "
            "CONCAT_WS(' ', j.title, GROUP_CONCAT(js.description SEPARATOR ' ')) AS text "
            "FROM jobs j LEFT JOIN job_sources js ON js.job_id = j.id "
            "WHERE j.inactive_at IS NULL GROUP BY j.id"
        )
        jobs = list(cur.fetchall())

    filtered = passed = 0
    with conn.cursor() as cur:
        for job in jobs:
            verdict = evaluate(job, rules, now)
            if verdict.passed:
                cur.execute(
                    "UPDATE jobs SET filtered_at=NULL, filter_reason=NULL WHERE id=%s",
                    (job["id"],),
                )
                passed += 1
            else:
                cur.execute(
                    "UPDATE jobs SET filtered_at=%s, filter_reason=%s WHERE id=%s",
                    (now, verdict.reason[:255], job["id"]),
                )
                filtered += 1
    conn.commit()

    return {"command": "filter", "evaluated": len(jobs), "passed": passed, "filtered": filtered}
