"""Vercel entry point: exposes the stdlib HTTP API from canon/api.py as WSGI `app`.

Vercel functions can only write to /tmp, and /tmp is per instance and
ephemeral, so the SQLite database does not survive cold starts or span
instances. To keep a demo usable, each new instance loads the sample patient
from samples/maria_chen (set CANON_SEED_SAMPLES=0 to skip). Set CANON_API_KEYS
in the Vercel project to require `Authorization: Bearer <key>`.
"""

from __future__ import annotations

import glob
import io
import os
import sys
from email.message import Message
from http import HTTPStatus

from canon.api import make_handler
from canon.service import Canon, CanonError

ROOT = os.path.dirname(os.path.abspath(__file__))


def _seed(canon: Canon) -> None:
    pid = None
    for path in sorted(glob.glob(os.path.join(ROOT, "samples", "maria_chen", "*"))):
        with open(path, "rb") as fh:
            data = fh.read()
        name = os.path.basename(path)
        try:
            r = canon.ingest(data, filename=name, actor="seed")
        except CanonError as e:
            if e.code != "patient_unidentified" or not pid:
                print(f"seed: skipped {name}: {e.message}", file=sys.stderr)
                continue
            r = canon.ingest(data, filename=name, patient_id=pid, actor="seed")
        pid = pid or r["patient_id"]


_canon = Canon(os.environ.get("CANON_DB", "/tmp/canon.db"))
if os.environ.get("CANON_SEED_SAMPLES", "1") != "0" and not _canon.list_patients():
    _seed(_canon)


class _Handler(make_handler(_canon)):
    """Runs the BaseHTTPRequestHandler routes without a socket, capturing the response."""

    def __init__(self, environ: dict):
        self.status, self.response_headers = 500, []
        self.path = environ.get("PATH_INFO") or "/"
        if environ.get("QUERY_STRING"):
            self.path += "?" + environ["QUERY_STRING"]
        self.headers = Message()
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                self.headers[k[5:].replace("_", "-").title()] = v
        for k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(k):
                self.headers[k.replace("_", "-").title()] = environ[k]
        n = int(environ.get("CONTENT_LENGTH") or 0)
        self.rfile = io.BytesIO(environ["wsgi.input"].read(n) if n else b"")
        self.wfile = io.BytesIO()

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.response_headers.append((keyword, str(value)))

    def end_headers(self):
        pass


def app(environ, start_response):
    h = _Handler(environ)
    method = environ.get("REQUEST_METHOD", "GET")
    if method in ("GET", "POST"):
        h._route(method)
    else:
        h._err(405, "method_not_allowed", f"{method} is not supported.")
    start_response(f"{h.status} {HTTPStatus(h.status).phrase}", h.response_headers)
    return [h.wfile.getvalue()]
