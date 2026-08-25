from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

DIMENSIONS = (
    "technical_fit",
    "seniority_fit",
    "compensation_fit",
    "arrangement_fit",
    "domain_fit",
    "company_fit",
    "growth_potential",
)


@dataclass(frozen=True)
class Profile:
    version: int
    hash: str
    data: dict
    weights: dict[str, int]
    filters: dict


def compute_hash(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_weights(weights: dict[str, int]) -> dict[str, int]:
    if "red_flags" in weights:
        raise ValueError(
            "red_flags is a penalty applied to the final score, not a weighted dimension"
        )
    unknown = sorted(set(weights) - set(DIMENSIONS))
    if unknown:
        raise ValueError(f"unknown scoring dimension(s): {', '.join(unknown)}")
    total = sum(weights.values())
    if total != 100:
        raise ValueError(f"weights must sum to 100, got {total}")
    missing = sorted(set(DIMENSIONS) - set(weights))
    if missing:
        raise ValueError(f"missing scoring dimension(s): {', '.join(missing)}")
    return dict(weights)


def load_profile(path: str = "profile.yaml") -> Profile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Profile(
        version=int(data["version"]),
        hash=compute_hash(data),
        data=data,
        weights=validate_weights(data["weights"]),
        filters=dict(data.get("filters") or {}),
    )
