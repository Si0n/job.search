from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jobsearch import dashboard, db, store

VALID_STATUSES = {"interested", "skipped", "applied", "replied",
                  "rejected", "interviewing", "offer"}
MAX_NOTE = 2000
STATIC = Path(__file__).parent / "static"


def parse_status_request(body: bytes) -> tuple[int, str, str | None]:
    """Pure. Validate a triage request before it reaches the database.

    Every field is checked against a whitelist or a type, because this is the only
    endpoint that writes, and a browser is not a trusted caller even on loopback.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("malformed JSON: expected an object")

    raw_id = payload.get("id")
    try:
        job_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError(f"invalid job id: {raw_id!r}") from None

    status = payload.get("status")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("invalid note: expected a string")
        if len(note) > MAX_NOTE:
            raise ValueError(f"note too long: {len(note)} > {MAX_NOTE}")

    return job_id, status, note


def _make_handler(settings):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict | list, content_type="application/json"):
            data = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                page = (STATIC / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return

            if parsed.path == "/api/jobs":
                params = parse_qs(parsed.query)
                min_score = params.get("min_score", [None])[0]
                include_triaged = params.get("all", ["0"])[0] == "1"
                conn = db.connect(settings)
                try:
                    jobs, postings = dashboard.fetch_rows(
                        conn,
                        min_score=int(min_score) if min_score else None,
                        include_triaged=include_triaged,
                    )
                finally:
                    conn.close()
                self._send(200, dashboard.build_view(jobs, postings))
                return

            self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/status":
                self._send(404, {"error": "not found"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                self._send(413, {"error": "body too large"})
                return

            try:
                job_id, status, note = parse_status_request(self.rfile.read(length))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return

            conn = db.connect(settings)
            try:
                result = store.set_status(conn, job_id, status, note)
            finally:
                conn.close()
            self._send(200, result)

        def log_message(self, fmt, *args):
            # Default logging writes to stderr on every request, including the
            # polling the page does. Keep the terminal usable.
            pass

    return Handler


def run(settings, host: str = "127.0.0.1", port: int = 8765,
        open_browser: bool = False) -> None:
    httpd = ThreadingHTTPServer((host, port), _make_handler(settings))
    url = f"http://{host}:{port}/"
    print(f"jobsearch dashboard on {url}  (ctrl-c to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
