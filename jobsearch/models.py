from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ParseStatus = Literal["ok", "empty", "broken"]

# Single source of truth for triage.status — mirrors the SQL ENUM in
# migrations/001_init.sql (renamed to `triage` in 016). cli.py and server.py
# both validate against this instead of each keeping their own copy.
TRIAGE_STATUSES = (
    "interested", "skipped", "applied", "replied",
    "rejected", "interviewing", "offer",
)


@dataclass(frozen=True)
class RawFetch:
    source_name: str
    body: bytes
    http_status: int
    fetched_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    path: str | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class RawPosting:
    external_id: str
    url: str
    title: str
    company: str
    description: str
    location: str | None = None
    salary_raw: str | None = None
    posted_at: datetime | None = None
    arrangement_hint: str | None = None
    employment_hint: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    status: ParseStatus
    postings: list[RawPosting] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
