"""HTTP API (stdlib only).

    POST /v1/documents                  raw body (any format) or JSON {content, encoding, ...}
    GET  /v1/documents/{id}[?items=1]
    GET  /v1/patients
    GET  /v1/patients/{id}/record       canonical record
    GET  /v1/patients/{id}/summary      agent-friendly summary
    GET  /v1/patients/{id}/observations?names=a1c,ldl&since=2025-01-01
    GET  /v1/patients/{id}/fhir         FHIR R4 Bundle
    GET  /v1/tools?format=anthropic|openai|mcp
    POST /v1/tools/{name}               JSON args -> tool result
    GET  /v1/audit[?patient_id=]        hash-chained access log
    GET  /v1/audit/verify
    GET  /v1/usage?days=30              this API key's request volume, errors, latency, LLM tokens
    GET  /dashboard                     customer usage dashboard (HTML; signs in with the API key)

Auth: set CANON_API_KEYS="key1:client-a,key2:client-b" and send
`Authorization: Bearer key1`. Unset = local sandbox mode (no auth).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import usage
from .service import Canon, CanonError
from .tools import TOOLS_BY_NAME, anthropic_tools, call_tool, mcp_tools, openai_tools

MAX_BODY = 25 * 1024 * 1024
DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
UNMETERED = {"health", "usage"}  # requests about the account, not agent work


def _operation(method: str, path: str) -> str:
    """Stable operation name for metering, e.g. patients.summary or tool.get_conflicts."""
    if path in ("", "/healthz"):
        return "health"
    if path == "/v1/usage":
        return "usage"
    if path == "/v1/documents":
        return "documents.ingest" if method == "POST" else "unknown_route"
    if re.fullmatch(r"/v1/documents/[\w-]+", path):
        return "documents.get"
    if path == "/v1/patients":
        return "patients.list"
    m = re.fullmatch(r"/v1/patients/[\w-]+/(record|summary|observations|fhir)", path)
    if m:
        return f"patients.{m.group(1)}"
    if path == "/v1/tools":
        return "tools.list"
    m = re.fullmatch(r"/v1/tools/(\w+)", path)
    if m and method == "POST":
        return f"tool.{m.group(1)}" if m.group(1) in TOOLS_BY_NAME else "unknown_route"
    if path in ("/v1/audit", "/v1/audit/verify"):
        return path[4:].replace("/", ".")
    return "unknown_route"


def _keys() -> dict[str, str]:
    raw = os.environ.get("CANON_API_KEYS", "")
    return dict(p.split(":", 1) for p in raw.split(",") if ":" in p)


def make_handler(canon: Canon):
    lock = threading.Lock()
    keys = _keys()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Canon/0.1"

        def log_message(self, fmt, *args):  # quieter logs
            if os.environ.get("CANON_LOG"):
                super().log_message(fmt, *args)

        # ---------------------------------------------------------- helpers
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, indent=2).encode()
            self._status, self._bytes_out, self._resp = status, len(data), body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _err(self, status: int, code: str, message: str) -> None:
            self._send(status, {"error": {"code": code, "message": message}})

        def _client(self) -> str | None:
            if not keys:
                return "sandbox"
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            return keys.get(token or "")

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise CanonError("too_large", "Document exceeds 25 MB.", 413)
            return self.rfile.read(n) if n else b""

        def _dashboard(self) -> None:
            with open(DASHBOARD, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str):
            u = urlparse(self.path)
            path = u.path.rstrip("/")
            if method == "GET" and path == "/dashboard":  # static page; its data calls carry the key
                return self._dashboard()
            client = self._client()
            if client is None:
                return self._err(401, "unauthorized", "Missing or invalid API key.")
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            actor = f"client:{client}"
            self._status, self._bytes_out, self._resp, self._pid = 500, 0, None, None
            started = time.perf_counter()
            try:
                with lock:
                    return self._dispatch(method, path, q, actor, client)
            except CanonError as e:
                return self._err(e.status, e.code, e.message)
            except (ValueError, KeyError) as e:
                return self._err(400, "bad_request", str(e))
            finally:
                self._meter(method, path, client, (time.perf_counter() - started) * 1000)

        def _meter(self, method: str, path: str, client: str, latency_ms: float) -> None:
            op = _operation(method, path)
            if op in UNMETERED:
                return
            m = re.fullmatch(r"/v1/patients/([\w-]+)/\w+", path)
            resp = self._resp if isinstance(self._resp, dict) else {}
            pid = m.group(1) if m else self._pid or resp.get("patient_id")
            try:
                usage.record(canon.store, client_id=client, channel="api", operation=op, status=self._status,
                             latency_ms=latency_ms, bytes_in=int(self.headers.get("Content-Length") or 0),
                             bytes_out=self._bytes_out, patient_id=pid, body=resp)
            except Exception:  # metering must never break the request
                pass

        def _dispatch(self, method: str, path: str, q: dict, actor: str, client: str):
            if method == "GET" and path in ("", "/healthz"):
                return self._send(200, {"ok": True, "service": "canon", "docs": "/v1/tools"})
            if method == "POST" and path == "/v1/documents":
                body = self._body()
                ct = self.headers.get("Content-Type", "")
                if "application/json" in ct and b'"content"' in body[:4096]:
                    j = json.loads(body)
                    data = base64.b64decode(j["content"]) if j.get("encoding") == "base64" else \
                        j["content"].encode("utf-8")
                    r = canon.ingest(data, filename=j.get("filename"), fmt=j.get("format"),
                                     patient_id=j.get("patient_id"), source_name=j.get("source_name"),
                                     use_llm=j.get("use_llm"), actor=actor)
                else:
                    r = canon.ingest(body, filename=q.get("filename") or self.headers.get("X-Filename"),
                                     content_type=ct, fmt=q.get("format"), patient_id=q.get("patient_id"),
                                     source_name=q.get("source_name"),
                                     use_llm={"1": True, "0": False}.get(q.get("use_llm", "")), actor=actor)
                return self._send(200 if r["document"].get("duplicate") else 201, r)
            m = re.fullmatch(r"/v1/documents/([\w-]+)", path)
            if method == "GET" and m:
                return self._send(200, canon.get_document(m.group(1), include_items=q.get("items") == "1"))
            if method == "GET" and path == "/v1/patients":
                return self._send(200, {"patients": canon.list_patients()})
            m = re.fullmatch(r"/v1/patients/([\w-]+)/(record|summary|observations|fhir)", path)
            if method == "GET" and m:
                pid, view = m.groups()
                if view == "record":
                    return self._send(200, canon.record(pid, actor=actor))
                if view == "summary":
                    return self._send(200, canon.summary(pid, actor=actor))
                if view == "fhir":
                    return self._send(200, canon.fhir(pid))
                names = [n for n in q.get("names", "").split(",") if n] or None
                return self._send(200, {"observations": canon.observations(pid, names, q.get("since"))})
            if method == "GET" and path == "/v1/tools":
                fmt = q.get("format", "anthropic")
                tools = {"anthropic": anthropic_tools, "openai": openai_tools, "mcp": mcp_tools}[fmt]()
                return self._send(200, {"format": fmt, "tools": tools})
            m = re.fullmatch(r"/v1/tools/(\w+)", path)
            if method == "POST" and m:
                if m.group(1) not in TOOLS_BY_NAME:
                    return self._err(404, "unknown_tool", m.group(1))
                args = json.loads(self._body() or b"{}")
                self._pid = args.get("patient_id") if isinstance(args, dict) else None
                out = call_tool(canon, m.group(1), args, actor=actor)
                return self._send(200 if "error" not in out else 400, out)
            if method == "GET" and path == "/v1/audit":
                return self._send(200, {"entries": canon.audit_log(q.get("patient_id"), int(q.get("limit", 100)))})
            if method == "GET" and path == "/v1/audit/verify":
                return self._send(200, canon.store.verify_audit_chain())
            if method == "GET" and path == "/v1/usage":
                scope = None if not keys else client  # sandbox: one tenant, show everything
                return self._send(200, usage.summarize(canon.store, scope, int(q.get("days", 30))))
            return self._err(404, "not_found", f"No route {method} {path}")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8080, db: str = "canon.db") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(Canon(db)))
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description="Canon clinical data normalization API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=os.environ.get("CANON_DB", "canon.db"))
    a = ap.parse_args()
    httpd = serve(a.host, a.port, a.db)
    mode = "API keys" if _keys() else "sandbox (no auth)"
    print(f"Canon listening on http://{a.host}:{a.port}  db={a.db}  auth={mode}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
