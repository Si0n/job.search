from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from pathlib import Path

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

    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("content is required")
    # Checked before decoding: base64 inflates by 4/3, so refusing here means
    # never materialising an oversized file in memory.
    if len(content) > (MAX_BYTES // 3) * 4 + 4:
        raise ValueError(f"file too large: limit is {MAX_BYTES} bytes")
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("content is not valid base64") from None
    if not data:
        raise ValueError("content is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"file too large: {len(data)} > {MAX_BYTES}")
    return filename.strip(), data


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
