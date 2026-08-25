from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

_LEGAL_SUFFIXES = (
    "ltd", "ltd.", "limited", "llc", "l.l.c.", "inc", "inc.", "incorporated",
    "gmbh", "ag", "bv", "b.v.", "nv", "n.v.", "sa", "s.a.", "srl", "s.r.l.",
    "oy", "ab", "as", "aps", "plc", "co", "co.", "corp", "corp.", "corporation",
    "sp", "z", "o", "o.o.", "sp.", "zoo", "s.r.o.", "d.o.o.", "tov", "pp", "fop",
)

_TITLE_ALIASES = {
    "sr": "senior",
    "sr.": "senior",
    "jr": "junior",
    "jr.": "junior",
    "lead": "lead",
    "eng": "engineer",
    "dev": "developer",
}

_PUNCT = re.compile(r"[^\w\s+#]", flags=re.UNICODE)
_SPACE = re.compile(r"\s+")
_PARENS = re.compile(r"\([^)]*\)")

_REMOTE = re.compile(r"\b(fully\s+remote|full\s+remote|remote|віддалено|удалённо)\b", re.I)
_HYBRID = re.compile(r"\b(hybrid|гібрид|гибрид)\b", re.I)
_ONSITE = re.compile(r"\b(on[-\s]?site|in[-\s]?office|офіс|office[-\s]?based)\b", re.I)

_CONTRACT = re.compile(r"\b(b2b|contract|contractor|freelance|договір|гіг)\b", re.I)
_PART = re.compile(r"\bpart[-\s]?time\b", re.I)
_INTERN = re.compile(r"\b(internships?|interns?|trainees?)\b|\bстажув\w*", re.I)
_FULL = re.compile(r"\bfull[-\s]?time\b", re.I)

_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr")


def _squash(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def company(raw: str) -> str:
    text = _PUNCT.sub(" ", (raw or "").lower())
    tokens = [t for t in _squash(text).split(" ") if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def title(raw: str) -> str:
    text = _PARENS.sub(" ", raw or "")
    text = _PUNCT.sub(" ", text.lower())
    tokens = [_TITLE_ALIASES.get(t, t) for t in _squash(text).split(" ") if t]
    return " ".join(tokens)


def location(raw: str | None) -> str:
    if not raw:
        return ""
    return _squash(_PUNCT.sub(" ", raw.lower()))


def arrangement(hint: str | None, text: str) -> str:
    # A source's own label is authoritative; description prose is a fallback.
    for candidate, pattern in (("hybrid", _HYBRID), ("remote", _REMOTE), ("onsite", _ONSITE)):
        if hint and pattern.search(hint):
            return candidate
    haystack = text or ""
    if _HYBRID.search(haystack):
        return "hybrid"
    if _REMOTE.search(haystack):
        return "remote"
    if _ONSITE.search(haystack):
        return "onsite"
    return "unknown"


def employment(hint: str | None, text: str) -> str:
    for candidate, pattern in (
        ("internship", _INTERN), ("contract", _CONTRACT),
        ("part-time", _PART), ("full-time", _FULL),
    ):
        if hint and pattern.search(hint):
            return candidate
    haystack = text or ""
    for candidate, pattern in (
        ("internship", _INTERN), ("contract", _CONTRACT),
        ("part-time", _PART), ("full-time", _FULL),
    ):
        if pattern.search(haystack):
            return candidate
    return "unknown"


def description(html_or_text: str) -> str:
    soup = BeautifulSoup(html_or_text or "", "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    blocks = [_squash(block) for block in soup.get_text().split("\n")]
    return "\n\n".join(b for b in blocks if b)


def description_hash(text: str) -> str:
    return hashlib.sha256(_squash(text or "").lower().encode("utf-8")).hexdigest()
