from __future__ import annotations

import html as htmlmod
import json
import re

from jobsearch.adapters.base import JsonAdapter, RawFetch

ALGOLIA = "https://hn.algolia.com/api/v1"
# The monthly thread is posted by the whoishiring account, which also runs a
# "Who wants to be hired?" thread the same day. Only one of them is job adverts.
_HIRING_TITLE = re.compile(r"who\s+is\s+hiring", re.I)
_REMOTE = re.compile(r"\bREMOTE\b", re.I)
_ONSITE_ONLY = re.compile(r"\b(?:onsite|on-site)\s+only\b|\bno\s+remote\b", re.I)
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)


def _without_url(segment: str) -> str:
    """A header segment with any bare URL removed, empty if that was all it held."""
    return _URL.sub("", segment or "").strip(" -–—,;")


def _plain(text: str) -> str:
    """HN comment HTML to plain text. Paragraphs are <p>, links are anchors, and
    everything else is entity-escaped."""
    text = re.sub(r"<p>", "\n\n", text or "", flags=re.I)
    text = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return htmlmod.unescape(text).strip()


class HackerNewsWhoIsHiringAdapter(JsonAdapter):
    """The monthly "Ask HN: Who is hiring?" thread, read through Algolia's API.

    Unlike every other source here, the postings are not records — they are
    top-level comments in a discussion, roughly 380 of them a month, written to
    a convention rather than a schema:

        Company | Role | Location | REMOTE | stack | contact

    The convention is followed often enough to be worth parsing and loosely
    enough that a record which does not yield a company and a role is dropped
    rather than guessed at. That is the trade this source makes: high signal for
    US and EU remote roles that appear on no board, in exchange for postings
    that arrive without an employer field, a salary or a canonical URL.

    Keyword filtering happens here rather than in the hard filters, for the same
    reason the other boards put `search_keywords` in their query: a month's
    thread is a catalogue, and scoring 380 comments to surface five is waste.
    """

    name = "hnwhoishiring"
    base_url = "https://news.ycombinator.com"

    def build_url(self, query: dict) -> str:
        return f"{ALGOLIA}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        query = query or {}
        raw = self._get(self.build_url(query), conditional)
        if raw.http_status == 304:
            return raw

        story_id = self._latest_thread(raw.text())
        items: list[dict] = []
        if story_id:
            thread = self._get(f"{ALGOLIA}/items/{story_id}", None)
            items = self._postings(thread.text(), query.get("keywords") or [])

        return RawFetch(
            source_name=self.name,
            body=json.dumps(items).encode("utf-8"),
            http_status=raw.http_status,
            fetched_at=raw.fetched_at,
            etag=raw.etag,
            last_modified=raw.last_modified,
        )

    @staticmethod
    def _latest_thread(payload: str) -> str | None:
        """The newest hiring thread's id, or None if the search returns none.

        Returning None rather than raising lets parse() classify the run empty:
        a month where the thread has not been posted yet is not a broken source.
        """
        try:
            hits = json.loads(payload).get("hits") or []
        except json.JSONDecodeError:
            return None
        for hit in hits:
            if _HIRING_TITLE.search(hit.get("title") or ""):
                return str(hit.get("objectID") or "") or None
        return None

    @classmethod
    def _postings(cls, payload: str, keywords: list[str]) -> list[dict]:
        try:
            children = json.loads(payload).get("children") or []
        except json.JSONDecodeError:
            return []

        wanted = re.compile("|".join(re.escape(k) for k in keywords), re.I) if keywords else None
        items = []
        for child in children:
            body = _plain(child.get("text") or "")
            if not body:
                # Deleted or flagged comments arrive with a null text.
                continue
            if wanted and not wanted.search(body):
                continue
            if record := cls._as_record(child, body):
                items.append(record)
        return items

    @staticmethod
    def _as_record(child: dict, body: str) -> dict | None:
        """One comment to a posting-shaped dict, or None if it is not one.

        The first line carries the pipe-separated header. A comment with no
        pipes at all is prose — an aside, a question, a recruiter complaint —
        and is skipped rather than filed under an invented job title.
        """
        header = body.split("\n", 1)[0].strip()
        parts = [_without_url(p) for p in header.split("|")]
        # Posters often give the company's URL its own segment, or append it to
        # the company name. Neither is a job title, so URL-only segments are
        # dropped before company and role are taken by position.
        parts = [p for p in parts if p]
        if len(parts) < 2:
            return None

        comment_id = str(child.get("id") or "")
        if not comment_id:
            return None

        company, title = parts[0], parts[1]
        # Some posters lead with the role and follow with the company. Neither
        # order is detectable in general, so the convention is taken at face
        # value and the full comment is kept as the description either way.
        location = " / ".join(parts[2:4]) if len(parts) > 2 else None

        record = {
            "external_id": f"hn-{comment_id}",
            "url": f"https://news.ycombinator.com/item?id={comment_id}",
            "title": title[:250],
            "company": company[:250],
            "description": body,
            "posted_by": child.get("author"),
        }
        if location:
            record["location"] = location[:250]
        if _REMOTE.search(header) and not _ONSITE_ONLY.search(header):
            record["arrangement_hint"] = "remote"
        return record
