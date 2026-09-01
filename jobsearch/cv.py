from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

CV_DIR = "var/cv"
MAX_BYTES = 10 * 1024 * 1024
MAX_FILENAME = 255

# What a CV is allowed to be, decided by the file's own first bytes. The client's
# claimed content type is never consulted: it is the one field an attacker fully
# controls, and it decides how the download endpoint later labels the response.
SIGNATURES = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"PK\x03\x04",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
)


def sniff(data: bytes) -> tuple[str, str]:
    for magic, content_type, extension in SIGNATURES:
        if data.startswith(magic):
            return content_type, extension
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("unsupported file type: expected PDF, DOCX or UTF-8 text") from None
    return "text/plain; charset=utf-8", ".txt"


def parse_upload(raw: str | bytes) -> tuple[str, bytes]:
    """Validate an upload. Base64 inside JSON rather than multipart: `cgi` is
    gone in 3.13, and JSON keeps the CSRF guard that works precisely because a
    cross-origin HTML form cannot send application/json."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("malformed JSON: expected an object")

    filename = payload.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("filename is required")
    if len(filename) > MAX_FILENAME:
        raise ValueError(f"filename too long: {len(filename)} > {MAX_FILENAME}")
    # The download route quotes this filename straight into a Content-Disposition
    # header; strip what would let it inject a header or escape the quoting.
    filename = re.sub(r'[\r\n"\\]', "", filename.strip())
    if not filename:
        raise ValueError("filename is required")

    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("content is required")
    # Compute exact decoded size before touching base64.b64decode, so an
    # oversized file is never materialised in memory. Base64 encodes every 3
    # bytes as 4 characters; the decoded size is the inverse.
    if len(content) % 4 != 0:
        raise ValueError("content is not valid base64")
    padding = 0
    if content.endswith("="):
        padding = content.count("=")
    decoded_size = (len(content) // 4) * 3 - padding
    if decoded_size > MAX_BYTES:
        raise ValueError(f"file too large: {decoded_size} > {MAX_BYTES}")
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("content is not valid base64") from None
    if not data:
        raise ValueError("content is empty")
    return filename.strip(), data


def content_disposition(filename: str) -> str:
    """A Content-Disposition value that never crashes the response.

    CPython's http.server encodes header values as latin-1, strict — a stored
    filename holds whatever script the owner's CV was named in, and Cyrillic
    (or any codepoint past U+00FF) in a plain filename= would raise there. So
    the ASCII fallback carries only what latin-1 can encode, for the clients
    that read nothing else; filename*=UTF-8'' (RFC 6266) carries the real name
    percent-encoded, for the browsers that do. This does not replace
    parse_upload's strip of \\r \\n " \\\\ — that is still what stops header
    injection; this only keeps a legitimate name from breaking the header.
    """
    ascii_name = "".join(c for c in filename if 32 <= ord(c) <= 126)
    if not ascii_name:
        extension = "".join(c for c in Path(filename).suffix if 32 <= ord(c) <= 126)
        ascii_name = f"cv{extension}"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def write_file(data: bytes, extension: str, directory: str = CV_DIR) -> tuple[str, str]:
    """Store bytes under their own digest. Content-addressed, so re-using last
    week's CV costs nothing and an edited CV is a different file rather than an
    overwrite of the record of what was actually sent."""
    digest = hashlib.sha256(data).hexdigest()
    target = Path(directory) / f"{digest}{extension}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return digest, str(target)


def store(conn, filename: str, data: bytes, *, directory: str = CV_DIR,
          now: datetime | None = None) -> dict:
    content_type, extension = sniff(data)
    digest, path = write_file(data, extension, directory)
    with conn.cursor() as cur:
        cur.execute("SELECT id, filename FROM cv_files WHERE sha256 = %s", (digest,))
        existing = cur.fetchone()
        if existing:
            return {**existing, "sha256": digest, "reused": True}
        cur.execute(
            "INSERT INTO cv_files (sha256, filename, content_type, size_bytes, path, "
            "uploaded_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (digest, filename, content_type, len(data), path, now or datetime.now()),
        )
        cv_id = cur.lastrowid
    conn.commit()
    return {"id": cv_id, "filename": filename, "sha256": digest, "reused": False}


def fetch(conn, cv_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM cv_files WHERE id = %s", (cv_id,))
        return cur.fetchone()


def list_files(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, filename, content_type, size_bytes, uploaded_at "
                    "FROM cv_files ORDER BY uploaded_at DESC")
        return list(cur.fetchall())
