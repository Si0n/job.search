from __future__ import annotations

import hashlib
import re
from datetime import datetime

from jobsearch import normalize

MERGE_WINDOW_DAYS = 30
MIN_JACCARD = 0.6

_TOKEN = re.compile(r"\w+", re.UNICODE)


def fingerprint(
    company: str,
    title: str,
    location: str,
    employment_type: str,
    arrangement: str,
) -> str:
    # A remote role is the same opportunity whether the source wrote "Remote",
    # "Remote EU", or nothing at all — so location is dropped when remote.
    location_part = "" if arrangement == "remote" else normalize.location(location)
    parts = (
        normalize.company(company),
        normalize.title(title),
        location_part,
        employment_type or "unknown",
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def jaccard(a: str, b: str) -> float:
    tokens_a = set(_TOKEN.findall((a or "").lower()))
    tokens_b = set(_TOKEN.findall((b or "").lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def can_merge(
    *,
    existing_source_id: int,
    candidate_source_id: int,
    existing_last_seen: datetime,
    candidate_last_seen: datetime,
    existing_description: str | None,
    candidate_description: str | None,
) -> tuple[bool, str]:
    """Decide whether a fingerprint-matching posting joins an existing canonical job.

    The caller has already established that fingerprints are equal. False splits
    are cheap and visible; false merges destroy data silently — so every gate
    defaults to refusing.
    """
    if existing_source_id == candidate_source_id:
        return False, "same source — two requisitions, not one opportunity"

    gap = abs((candidate_last_seen - existing_last_seen).days)
    if gap > MERGE_WINDOW_DAYS:
        return False, f"outside {MERGE_WINDOW_DAYS}-day window ({gap} days) — treat as a repost"

    if existing_description and candidate_description:
        score = jaccard(existing_description, candidate_description)
        if score < MIN_JACCARD:
            return False, f"description similarity {score:.2f} below {MIN_JACCARD}"

    return True, "merged"
