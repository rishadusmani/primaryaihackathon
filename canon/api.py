"""HTTP API. One framework-free `App`, served by http.server (`python -m canon serve`)
or by any WSGI server through `App.wsgi`.

Public
    GET  /                               web demo (normalizes documents in the browser session, stores nothing)
    GET  /about                          why Canon exists (static HTML)
    GET  /v1/playground/samples          the 7-document sample patient (?set=tricky: the tricky-note demo)
    POST /v1/playground/normalize        {documents: [...]} -> records; stateless, no model calls on visitor input
    GET  /healthz

API (Authorization: Bearer <key> when CANON_API_KEYS is set)
    POST /v1/documents                   raw body (any format) or JSON {content, encoding, ...}
    GET  /v1/documents/{id}[?items=1]
    GET  /v1/patients
    GET  /v1/patients/{id}/record | summary | observations | fhir
    GET  /v1/tools?format=anthropic|openai|mcp      POST /v1/tools/{name}
    POST /mcp                            MCP (Streamable HTTP, stateless, JSON responses)
    GET  /v1/audit[?patient_id=]

CANON_API_KEYS="key1,key2" makes the API require one of those keys. Without it the
API is open, so bind it to localhost or put it behind your own auth.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import playground
from .service import Canon, CanonError
from .store import Store
from .mcp_server import handle as mcp_handle
from .tools import TOOLS_BY_NAME, anthropic_tools, call_tool, mcp_tools, openai_tools

MAX_BODY = 25 * 1024 * 1024


class Response:
    def __init__(self, status: int, body: dict | list | str, content_type: str | None = None,
                 headers: dict[str, str] | None = None):
        self.status = status
        self.data = body if isinstance(body, dict) else None
        self.headers = headers or {}
        if isinstance(body, (dict, list)):
            self.body = json.dumps(body, indent=2).encode()
            self.content_type = "application/json"
        else:
            self.body = body.encode()
            self.content_type = content_type or "text/html; charset=utf-8"


def _err(status: int, code: str, message: str, **extra) -> Response:
    return Response(status, {"error": {"code": code, "message": message, **extra}})


_PAGES: dict[str, str] = {}


def _web_page(name: str) -> str:
    if name not in _PAGES:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", name), encoding="utf-8") as fh:
            _PAGES[name] = fh.read()
    return _PAGES[name]


class App:
    def __init__(self, store: Store | None = None, api_keys: set[str] | None = None):
        self.store = store or Store()
        self.canon = Canon(self.store)
        self.lock = threading.Lock()
        if api_keys is None:
            api_keys = {k.strip() for k in os.environ.get("CANON_API_KEYS", "").split(",") if k.strip()}
        self.api_keys = api_keys

    # ------------------------------------------------------------------ entry point
    def handle(self, method: str, raw_path: str, headers: dict[str, str], body: bytes) -> Response:
        u = urlparse(raw_path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"
        h = {k.lower(): v for k, v in headers.items()}
        if len(body) > MAX_BODY:
            return _err(413, "too_large", "Request exceeds 25 MB.")
        with self.lock:
            try:
                return self._route(method, path, q, h, body)
            except CanonError as e:
                return _err(e.status, e.code, e.message)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return _err(400, "bad_request", str(e))

    # ------------------------------------------------------------------ routing
    def _route(self, method: str, path: str, q: dict, h: dict, body: bytes) -> Response:
        # --- public
        if method == "GET" and path in ("/", "/demo"):
            return Response(200, _web_page("index.html"))
        if method == "GET" and path == "/about":
            return Response(200, _web_page("about.html"))
        if method == "GET" and path == "/v1/playground/samples":
            if q.get("set") == "tricky":
                return Response(200, {"documents": playground.samples("tricky"), "walkthrough": playground.walkthrough()})
            return Response(200, {"documents": playground.samples()})
        if method == "POST" and path == "/v1/playground/normalize":
            return Response(200, playground.normalize(json.loads(body or b"{}").get("documents")))
        if method == "GET" and path == "/healthz":
            return Response(200, {"ok": True, "service": "canon", "auth": bool(self.api_keys)})
        if path == "/mcp" and method != "POST":  # stateless server: no SSE stream, no sessions to delete
            return Response(405, {"error": {"code": "method_not_allowed", "message": "POST JSON-RPC to /mcp"}},
                            headers={"Allow": "POST"})

        # --- API
        if self.api_keys and not self._authorized(h):
            r = _err(401, "unauthorized", "Missing or invalid API key (Authorization: Bearer <key>).")
            r.headers["WWW-Authenticate"] = 'Bearer realm="canon"'
            return r
        canon = self.canon
        actor = "api"

        if method == "POST" and path == "/mcp":
            return self._mcp(canon, actor, body)
        if method == "POST" and path == "/v1/documents":
            ct = h.get("content-type", "")
            if "application/json" in ct and b'"content"' in body[:4096]:
                j = json.loads(body)
                data = base64.b64decode(j["content"]) if j.get("encoding") == "base64" else j["content"].encode()
                r = canon.ingest(data, filename=j.get("filename"), fmt=j.get("format"), patient_id=j.get("patient_id"),
                                 source_name=j.get("source_name"), use_llm=j.get("use_llm"), actor=actor)
            else:
                r = canon.ingest(body, filename=q.get("filename") or h.get("x-filename"), content_type=ct,
                                 fmt=q.get("format"), patient_id=q.get("patient_id"), source_name=q.get("source_name"),
                                 use_llm={"1": True, "0": False}.get(q.get("use_llm", "")), actor=actor)
            return Response(200 if r["document"].get("duplicate") else 201, r)
        m = re.fullmatch(r"/v1/documents/([\w-]+)", path)
        if method == "GET" and m:
            return Response(200, canon.get_document(m.group(1), include_items=q.get("items") == "1"))
        if method == "GET" and path == "/v1/patients":
            return Response(200, {"patients": canon.list_patients()})
        m = re.fullmatch(r"/v1/patients/([\w-]+)/(record|summary|observations|fhir)", path)
        if method == "GET" and m:
            pid, view = m.groups()
            if view == "record":
                return Response(200, canon.record(pid, actor=actor))
            if view == "summary":
                return Response(200, canon.summary(pid, actor=actor))
            if view == "fhir":
                return Response(200, canon.fhir(pid))
            names = [n for n in q.get("names", "").split(",") if n] or None
            return Response(200, {"observations": canon.observations(pid, names, q.get("since"))})
        if method == "GET" and path == "/v1/tools":
            fmt = q.get("format", "anthropic")
            if fmt not in ("anthropic", "openai", "mcp"):
                return _err(400, "bad_request", "format must be anthropic, openai or mcp")
            tools = {"anthropic": anthropic_tools, "openai": openai_tools, "mcp": mcp_tools}[fmt]()
            return Response(200, {"format": fmt, "tools": tools})
        m = re.fullmatch(r"/v1/tools/(\w+)", path)
        if method == "POST" and m:
            name = m.group(1)
            if name not in TOOLS_BY_NAME:
                return _err(404, "unknown_tool", name)
            args = json.loads(body or b"{}")
            out = call_tool(canon, name, args, actor=actor)
            return Response(200 if "error" not in out else 400, out)
        if method == "GET" and path == "/v1/audit":
            return Response(200, {"entries": canon.audit_log(q.get("patient_id"), min(int(q.get("limit", 100)), 1000))})
        if method == "GET" and path == "/v1/audit/verify":
            v = self.store.verify_audit_chain()
            return Response(200, {"valid": v["valid"], "entries_checked": v["entries_checked"]})
        return _err(404, "not_found", f"No route {method} {path}")

    def _mcp(self, canon: Canon, actor: str, body: bytes) -> Response:
        """MCP Streamable HTTP: one JSON-RPC message (or a batch) per POST, answered as JSON.
        Notifications alone get 202 with no body."""
        try:
            msg = json.loads(body or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})

        if isinstance(msg, list):
            out = [r for r in (mcp_handle(canon, m, actor) for m in msg) if r is not None]
        else:
            out = mcp_handle(canon, msg, actor)
        if not out:
            return Response(202, "", "text/plain")
        return Response(200, out)

    def _authorized(self, h: dict) -> bool:
        auth = h.get("authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        return any(hmac.compare_digest(token, k) for k in self.api_keys)

    # ------------------------------------------------------------------ adapters
    def wsgi(self, environ, start_response):
        n = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(n) if n else b""
        headers = {k[5:].replace("_", "-"): v for k, v in environ.items() if k.startswith("HTTP_")}
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = environ["CONTENT_TYPE"]
        path = environ.get("PATH_INFO", "/") + (("?" + environ["QUERY_STRING"]) if environ.get("QUERY_STRING") else "")
        r = self.handle(environ["REQUEST_METHOD"], path, headers, body)
        reason = {200: "OK", 201: "Created", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
                  413: "Payload Too Large", 422: "Unprocessable Entity", 502: "Bad Gateway", 503: "Service Unavailable"}.get(r.status, "")
        start_response(f"{r.status} {reason}", [("Content-Type", r.content_type), ("Content-Length", str(len(r.body))),
                                                *r.headers.items()])
        return [r.body]

    def http_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Canon/0.1"

            def log_message(self, fmt, *args):
                if os.environ.get("CANON_LOG"):
                    super().log_message(fmt, *args)

            def _do(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n and n <= MAX_BODY else (b"x" * (MAX_BODY + 1) if n else b"")
                r = app.handle(method, self.path, dict(self.headers.items()), body)
                self.send_response(r.status)
                self.send_header("Content-Type", r.content_type)
                self.send_header("Content-Length", str(len(r.body)))
                for k, v in r.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(r.body)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_DELETE(self):
                self._do("DELETE")

        return Handler


def serve(host: str = "127.0.0.1", port: int = 8080, db: str = "canon.db") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), App(Store(db)).http_handler())


def main() -> None:
    ap = argparse.ArgumentParser(description="Canon clinical data normalization API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=os.environ.get("CANON_DB", "canon.db"))
    a = ap.parse_args()
    httpd = serve(a.host, a.port, a.db)
    print(f"Canon listening on http://{a.host}:{a.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
