from __future__ import annotations

import json
import re

from jobsearch.profile import compute_hash

# Canonical name -> pattern. Aliases live here so the page never sees two names
# for one technology. Ambiguous short names carry their own guard: bare "Go" is
# also an English verb, so it is only matched when not followed by the words that
# make it one.
TECH_VOCAB: dict[str, str] = {
    "PHP": r"\bPHP\b", "Laravel": r"\bLaravel\b", "Symfony": r"\bSymfony\b",
    "Yii": r"\bYii\s?2?\b", "WordPress": r"\bWord\s?Press\b",
    "Magento": r"\bMagento\b", "Shopware": r"\bShopware\b", "Shopify": r"\bShopify\b",
    "MySQL": r"\bMy\s?SQL\b", "PostgreSQL": r"\b(?:PostgreSQL|Postgres)\b",
    "MongoDB": r"\bMongo\s?DB?\b", "Redis": r"\bRedis\b",
    "Elasticsearch": r"\b(?:Elasticsearch|ElasticSearch|OpenSearch)\b",
    "RabbitMQ": r"\bRabbit\s?MQ\b", "Kafka": r"\bKafka\b",
    "Docker": r"\bDocker\b", "Kubernetes": r"\b(?:Kubernetes|k8s)\b",
    "Terraform": r"\bTerraform\b", "Nginx": r"\bNginx\b",
    "AWS": r"\bAWS\b", "GCP": r"\b(?:GCP|Google Cloud)\b", "Azure": r"\bAzure\b",
    "Vue": r"\bVue(?:\.js|\s?[23])?\b", "React": r"\bReact(?:\.js)?\b",
    "Angular": r"\bAngular\b", "TypeScript": r"\bTypeScript\b",
    "Node.js": r"\bNode\.?\s?js\b|\bNodeJS\b",
    "Python": r"\bPython\b", "Django": r"\bDjango\b", "Flask": r"\bFlask\b",
    "Ruby": r"\bRuby\b", "Java": r"\bJava\b(?!Script)",
    "Go": r"\b(?:Golang|Go)\b(?!\s+(?:to|through|live|beyond|into|on|back|ahead|over|with|for|about|deep))",
    "GraphQL": r"\bGraphQL\b", "REST": r"\bREST(?:ful)?\b",
    "gRPC": r"\bgRPC\b", "CI/CD": r"\bCI\s?/\s?CD\b",
}

_CEFR = ["A1", "A2", "B1", "B2", "C1", "C2"]


SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "PLN": "zł", "UAH": "₴"}

_JOBS_SQL = """
SELECT j.id, j.fingerprint, j.title, j.company, j.location, j.arrangement, j.employment_type,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
       j.salary_source, j.salary_monthly_eur, j.first_seen_at, j.canonical_source_id,
       sc.score, sc.red_flag_penalty, sc.dimensions, sc.dimension_notes, sc.hard_concerns,
       sc.strengths, sc.weaknesses, sc.verdict,
       a.status,
       d.cover_letter, d.email AS draft_email, d.why_fit,
       d.note AS draft_note, d.applied_note,
       d.profile_hash AS draft_hash
FROM jobs j
LEFT JOIN scores sc      ON sc.id = j.latest_score_id
LEFT JOIN triage a       ON a.job_id = j.id
LEFT JOIN drafts d       ON d.job_id = j.id
WHERE j.inactive_at IS NULL AND j.filtered_at IS NULL
"""

_POSTINGS_SQL = """
SELECT js.job_id, js.source_id, s.name AS source_name, js.url, js.posted_at,
       js.description, js.source_meta
FROM job_sources js
JOIN sources s ON s.id = js.source_id
WHERE js.inactive_at IS NULL
ORDER BY js.job_id, s.priority
"""


# The dashboard's status filter vocabulary. "untriaged" is the working queue and
# stays the default; "all" drops the condition entirely. Everything else matches a
# single triage.status value, so this set is also the whitelist that keeps an
# arbitrary query string out of the SQL.
JOB_FILTERS = ("untriaged", "interested", "applied", "skipped", "all")


def parse_job_filter(raw: str | None) -> str:
    """Pure. Resolve the status query parameter to one of JOB_FILTERS.

    Anything unrecognised — a typo, a stale bookmark, an injection attempt —
    falls back to the working queue rather than erroring, because a filter is a
    view preference and a broken one should not cost the owner the page.
    """
    return raw if raw in JOB_FILTERS else "untriaged"


def fetch_rows(conn, *, min_score: int | None = None,
               status_filter: str = "untriaged") -> tuple[list[dict], list[dict]]:
    sql, params = _JOBS_SQL, []
    if min_score is not None:
        # COALESCE, not a bare comparison: sc.score is NULL for the 114 unscored
        # jobs (LEFT JOIN), and `NULL >= n` is never true in SQL. Without this,
        # min_score=0 ("no floor") would silently exclude every unscored job —
        # the opposite of what "no floor" means. Unscored jobs still lose to any
        # real threshold (COALESCE(...,0) >= 7 is false), so the inbox view is
        # unaffected.
        sql += " AND COALESCE(sc.score, 0) >= %s"
        params.append(min_score)
    status_filter = parse_job_filter(status_filter)
    if status_filter == "untriaged":
        sql += " AND a.job_id IS NULL"
    elif status_filter != "all":
        sql += " AND a.status = %s"
        params.append(status_filter)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        jobs = list(cur.fetchall())
        cur.execute(_POSTINGS_SQL)
        postings = list(cur.fetchall())
    return jobs, postings


INBOX_THRESHOLD = 7

_TODAY_STATS_SQL = {
    # Harvest totals come from the run rows rather than the jobs table because
    # "fetched" only exists there — a posting seen again today updates no job row.
    "fetched": "SELECT COALESCE(SUM(fetched), 0) FROM runs "
               "WHERE kind = 'harvest' AND DATE(started_at) = CURDATE()",
    "new": "SELECT COUNT(*) FROM jobs WHERE DATE(first_seen_at) = CURDATE()",
    "scored": "SELECT COUNT(DISTINCT job_id) FROM scores "
              "WHERE DATE(scored_at) = CURDATE()",
    # Pass 2 only: a pass-1 triage score is a coarse integer, not a verdict, so
    # counting it here would inflate the number of jobs actually worth reading.
    "above": "SELECT COUNT(DISTINCT job_id) FROM scores "
             "WHERE DATE(scored_at) = CURDATE() AND `pass` = 2 AND score >= %s",
    # triage holds one row per job, overwritten in place, so this counts
    # jobs whose status was last touched today — not every triage action taken
    # today. Re-marking a job tomorrow moves it out of today's number.
    "triaged": "SELECT COUNT(*) FROM triage WHERE DATE(updated_at) = CURDATE()",
}


def daily_stats(conn) -> dict[str, int]:
    """Today's pipeline activity, for the strip under the dashboard filters."""
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for key, sql in _TODAY_STATS_SQL.items():
            cur.execute(sql, [INBOX_THRESHOLD] if "%s" in sql else [])
            row = cur.fetchone()
            value = list(row.values())[0] if isinstance(row, dict) else (row or [0])[0]
            out[key] = int(value or 0)
    out["threshold"] = INBOX_THRESHOLD
    return out


def _as_json(value, fallback):
    """MySQL JSON columns arrive as str from some drivers and as parsed objects from
    others. Accept both rather than depending on the driver's mood."""
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def match_tech(description: str | None, skills: dict[str, list[str]]) -> list[dict]:
    """Pure. Which technologies a posting names, and which tier of the owner's
    profile each sits in — `tier=None` means the job wants something the profile
    does not list, which is the half worth reading."""
    if not description:
        return []

    tier_of: dict[str, str] = {}
    for tier, entries in (skills or {}).items():
        for entry in entries or []:
            for canonical in TECH_VOCAB:
                if _norm(canonical) in _norm(entry):
                    tier_of.setdefault(canonical, tier)

    found = [
        {"name": name, "tier": tier_of.get(name)}
        for name, pattern in TECH_VOCAB.items()
        if re.search(pattern, description, re.I)
    ]
    order = {"expert": 0, "strong": 1, "familiar": 2}
    found.sort(key=lambda t: (order.get(t["tier"], 3), t["name"].lower()))
    return found


def language_requirement(meta: dict | None, profile_languages: list[str]) -> dict | None:
    """Pure. A stated language requirement, and whether it exceeds what the owner
    has. Returns None when the posting states nothing — absence is not an
    all-clear, and the page must be able to tell the two apart."""
    hint = (meta or {}).get("language_hint") or ""
    if not hint.strip():
        return None

    match = re.search(r"\b([A-Z][a-z]+)\b.*?\b([ABC][12])\b", hint)
    if not match:
        return {"text": hint.strip(), "level": None, "gap": False}
    language, level = match.group(1), match.group(2)

    own = ""
    for entry in profile_languages or []:
        if language.lower() in entry.lower():
            if "native" in entry.lower():
                return {"text": hint.strip(), "level": level, "gap": False}
            levels = re.findall(r"[ABC][12]", entry)
            if levels:
                own = max(levels, key=_CEFR.index)
            break

    gap = True if not own else _CEFR.index(level) > _CEFR.index(own)
    return {"text": hint.strip(), "level": level, "gap": gap}


def format_salary(row: dict) -> dict:
    monthly = row.get("salary_monthly_eur")
    if row.get("salary_source") != "posting" or not row.get("salary_currency"):
        return {"text": "not stated", "stated": False, "monthly_eur": monthly}

    symbol = SYMBOLS.get(row["salary_currency"], row["salary_currency"] + " ")
    low, high, period = row.get("salary_min"), row.get("salary_max"), row.get("salary_period")

    if low and high and low != high:
        text = f"{symbol}{low}–{high}/{period}"
    elif low and high:
        text = f"{symbol}{low}/{period}"
    elif high:
        text = f"up to {symbol}{high}/{period}"
    elif low:
        text = f"from {symbol}{low}/{period}"
    else:
        return {"text": "not stated", "stated": False, "monthly_eur": monthly}

    return {"text": text, "stated": True, "monthly_eur": monthly}


def _dimension_detail(dimensions: dict, notes: dict, weights: dict) -> list[dict]:
    """What each dimension's number actually did to the score.

    Two dimensions showing 9 look identical on a card, but a 9 at weight 30
    contributes three times what a 9 at weight 10 does. Ordered by contribution
    so the dimension that decided the score reads first. `note` is the scorer's
    own reasoning where it recorded any, and None where it did not — an absent
    note must not read as "considered and had nothing to say".
    """
    if not weights:
        return []
    total = sum(weights.values()) or 1
    detail = []
    for name, value in (dimensions or {}).items():
        weight = weights.get(name, 0)
        try:
            contribution = round(float(value) * weight / total, 2)
        except (TypeError, ValueError):
            contribution = 0.0
        detail.append({
            "name": name, "value": value, "weight": weight,
            "contribution": contribution,
            "note": (notes or {}).get(name) or None,
        })
    detail.sort(key=lambda d: d["contribution"], reverse=True)
    return detail


def _merged_meta(postings: list[dict]) -> dict:
    """Meta from every posting on this job, canonical source last so it wins.
    Only one board publishes these hints today, so in practice this picks the
    one that has them rather than resolving a conflict."""
    merged: dict = {}
    for posting in postings:
        raw = posting.get("source_meta")
        if not raw:
            continue
        parsed = _as_json(raw, {})
        if isinstance(parsed, dict):
            merged.update(parsed)
    return merged


def _pick_description(job: dict, postings: list[dict]) -> str:
    canonical = job.get("canonical_source_id")
    preferred = [p for p in postings if p["source_id"] == canonical and p.get("description")]
    if preferred:
        return preferred[0]["description"]
    with_text = [p for p in postings if p.get("description")]
    if not with_text:
        return ""
    return max(with_text, key=lambda p: len(p["description"]))["description"]


def build_view(job_rows: list[dict], posting_rows: list[dict],
               profile: dict | None = None) -> list[dict]:
    """Pure. Rows in, cards out — no database, no formatting decisions left to the page.

    `profile` is optional: without it the cards simply carry no technology or
    language chips, rather than carrying wrong ones."""
    skills = (profile or {}).get("skills") or {}
    weights = (profile or {}).get("weights") or {}
    # Staleness is measured against the LIVE profile, never against the job's own
    # score: a job can carry an out-of-date score while its draft is current, and
    # comparing the two would flag exactly the wrong one.
    current_hash = compute_hash(profile) if profile else None
    languages = ((profile or {}).get("identity") or {}).get("languages") or []
    by_job: dict[int, list[dict]] = {}
    for posting in posting_rows:
        by_job.setdefault(posting["job_id"], []).append(posting)

    # A fingerprint appearing on two different canonical jobs means the same role
    # was found twice and the cross-source merge gate refused it — usually because
    # one board truncates its listing snippet harder than the other, which drags
    # the Jaccard similarity below the threshold. Flagging it here does not fix the
    # merge; it stops the owner applying to the same job twice.
    fingerprint_counts: dict[str, int] = {}
    for job in job_rows:
        fp = job.get("fingerprint")
        if fp:
            fingerprint_counts[fp] = fingerprint_counts.get(fp, 0) + 1

    cards = []
    for job in job_rows:
        postings = by_job.get(job["id"], [])
        if not postings:
            # Every active job has at least one active posting. If it doesn't, the
            # sweep and the harvest disagree — don't render a card with no link.
            continue

        cards.append({
            "id": job["id"],
            "draft": ({"cover_letter": job["cover_letter"], "email": job["draft_email"],
                       "why_fit": job["why_fit"],
                       "applied_note": job.get("applied_note"),
                       "stale": bool(current_hash and job.get("draft_hash") != current_hash)}
                      if job.get("cover_letter") else None),
            "pending_note": job.get("draft_note"),
            "is_duplicate": fingerprint_counts.get(job.get("fingerprint") or "", 0) > 1,
            "title": job["title"],
            "company": job["company"],
            "location": job.get("location") or "",
            "arrangement": job.get("arrangement") or "unknown",
            "employment_type": job.get("employment_type") or "unknown",
            "salary": format_salary(job),
            "score": job.get("score"),
            "red_flag_penalty": job.get("red_flag_penalty") or 0,
            "dimensions": _as_json(job.get("dimensions"), {}),
            "dimension_detail": _dimension_detail(
                _as_json(job.get("dimensions"), {}),
                _as_json(job.get("dimension_notes"), {}),
                weights),
            "hard_concerns": _as_json(job.get("hard_concerns"), []),
            "strengths": _as_json(job.get("strengths"), []),
            "weaknesses": _as_json(job.get("weaknesses"), []),
            "verdict": job.get("verdict") or "",
            "status": job.get("status"),
            "is_new": job.get("status") is None,
            "first_seen_at": str(job.get("first_seen_at") or ""),
            "description": _pick_description(job, postings),
            "tech": match_tech(_pick_description(job, postings), skills),
            "language": language_requirement(_merged_meta(postings), languages),
            "experience_hint": (_merged_meta(postings) or {}).get("experience_hint"),
            "sources": [
                {"name": p["source_name"], "url": p["url"],
                 "posted_at": str(p["posted_at"]) if p.get("posted_at") else None}
                for p in postings
            ],
        })

    cards.sort(key=lambda c: (c["score"] is not None, c["score"] or 0), reverse=True)
    return cards
