"""HTTP API. One framework-free `App`, served by http.server locally and as a
WSGI app on Vercel (app.py).

Public
    GET  /healthz
    POST /v1/signup                      {name, email} -> account + API key (shown once) + checkout link
    POST /v1/stripe/webhook              Stripe events (signature verified)
    GET  /v1/billing/sync                cron: retry unsent usage (Authorization: Bearer $CRON_SECRET)

Authenticated (Authorization: Bearer cn_live_...)
    POST /v1/documents                   raw body (any format) or JSON {content, encoding, ...}
    GET  /v1/documents/{id}[?items=1]
    GET  /v1/patients
    GET  /v1/patients/{id}/record | summary | observations | fhir
    GET  /v1/tools?format=anthropic|openai|mcp      POST /v1/tools/{name}
    GET  /v1/audit[?patient_id=]
    GET  /v1/account                     status + usage
    POST /v1/billing/checkout            -> Stripe Checkout URL
    POST /v1/billing/portal              -> Stripe Billing Portal URL

CANON_SANDBOX=1 disables per-customer auth and billing (local development and the
ephemeral demo deployment). In sandbox mode, CANON_API_KEYS="key1:label,key2:label"
optionally requires one of those static keys.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .billing import Billing, BillingError
from .service import SANDBOX_ACCOUNT, Canon, CanonError
from .store import Store
from .tools import TOOLS_BY_NAME, anthropic_tools, call_tool, mcp_tools, openai_tools

MAX_BODY = 25 * 1024 * 1024
INGEST_TOOLS = {"ingest_document"}


class Response:
    def __init__(self, status: int, body: dict | str, content_type: str | None = None):
        self.status = status
        if isinstance(body, dict):
            self.body = json.dumps(body, indent=2).encode()
            self.content_type = "application/json"
        else:
            self.body = body.encode()
            self.content_type = content_type or "text/html; charset=utf-8"


def _err(status: int, code: str, message: str, **extra) -> Response:
    return Response(status, {"error": {"code": code, "message": message, **extra}})


def _page(title: str, msg: str) -> Response:
    return Response(200, f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                         f"<title>{html.escape(title)}</title><body style='font:16px system-ui;max-width:40em;"
                         f"margin:4em auto;padding:0 1em'><h1>{html.escape(title)}</h1><p>{html.escape(msg)}</p>")


class App:
    def __init__(self, store: Store | None = None, billing: Billing | None = None, sandbox: bool | None = None):
        self.store = store or Store()
        self.billing = billing or Billing(self.store)
        self.sandbox = (os.environ.get("CANON_SANDBOX") == "1") if sandbox is None else sandbox
        self.lock = threading.Lock()
        raw = os.environ.get("CANON_API_KEYS", "")
        self.static_keys = {k.strip() for k, _, _ in (p.partition(":") for p in raw.split(",")) if k.strip()}
        if self.sandbox:
            self.store.execute("INSERT INTO accounts (id, name, status, created_at) VALUES (?,?,?,?) "
                               "ON CONFLICT DO NOTHING", (SANDBOX_ACCOUNT, "Sandbox", "sandbox", "1970-01-01T00:00:00Z"))

    # ------------------------------------------------------------------ entry point
    def handle(self, method: str, raw_path: str, headers: dict[str, str], body: bytes) -> Response:
        u = urlparse(raw_path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"
        h = {k.lower(): v for k, v in headers.items()}
        try:
            if len(body) > MAX_BODY:
                return _err(413, "too_large", "Request exceeds 25 MB.")
            with self.lock:
                return self._route(method, path, q, h, body)
        except (CanonError, BillingError) as e:
            return _err(e.status, e.code, e.message, **getattr(e, "extra", {}))
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return _err(400, "bad_request", str(e))

    # ------------------------------------------------------------------ routing
    def _route(self, method: str, path: str, q: dict, h: dict, body: bytes) -> Response:
        # --- public
        if method == "GET" and path in ("/", "/healthz"):
            return Response(200, {"ok": True, "service": "canon", "sandbox": self.sandbox,
                                  "billing_enabled": self.billing.stripe.enabled,
                                  "docs": "https://github.com/rishadusmani/primaryaihackathon"})
        if method == "POST" and path == "/v1/signup":
            if self.sandbox:
                return _err(400, "sandbox", "Signup is disabled in sandbox mode.")
            j = json.loads(body or b"{}")
            account, key = self.billing.create_account(j.get("name", "").strip(), (j.get("email") or None))
            out = {"account": self.billing.account(account["id"]), "api_key": key,
                   "note": "Store this key now; it is not shown again."}
            if self.billing.stripe.enabled and self.billing.price_id:
                row = dict(self.store.one("SELECT * FROM accounts WHERE id=?", (account["id"],)))
                out["checkout_url"] = self.billing.checkout_url(row)
            return Response(201, out)
        if method == "POST" and path == "/v1/stripe/webhook":
            return Response(200, self.billing.handle_webhook(body, h.get("stripe-signature", "")))
        if method == "GET" and path == "/v1/billing/sync":
            secret = os.environ.get("CRON_SECRET")
            if not secret or h.get("authorization") != f"Bearer {secret}":
                return _err(401, "unauthorized", "Cron secret required.")
            return Response(200, self.billing.sync_unreported())
        if method == "GET" and path == "/billing/success":
            return _page("Subscription started", "Your Canon subscription is active. You can close this tab.")
        if method == "GET" and path == "/billing/cancel":
            return _page("Checkout canceled", "No charge was made. You can restart checkout from the API.")
        if method == "GET" and path == "/billing/return":
            return _page("Billing updated", "You can close this tab.")

        # --- authenticated
        account = self._authenticate(h)
        if account is None:
            return _err(401, "unauthorized", "Missing or invalid API key (Authorization: Bearer cn_live_...).")
        canon = Canon(self.store, account["id"])
        canon.on_document_ingested = self.billing.record_usage
        actor = "sandbox" if self.sandbox else f"key:{account['id']}"

        if method == "GET" and path == "/v1/account":
            return Response(200, self.billing.account(account["id"]))
        if method == "POST" and path == "/v1/billing/checkout":
            return Response(200, {"checkout_url": self.billing.checkout_url(account)})
        if method == "POST" and path == "/v1/billing/portal":
            return Response(200, {"portal_url": self.billing.portal_url(account)})

        if method == "POST" and path == "/v1/documents":
            self.billing.check_can_ingest(account)
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
            if name in INGEST_TOOLS:
                self.billing.check_can_ingest(account)
            out = call_tool(canon, name, json.loads(body or b"{}"), actor=actor)
            return Response(200 if "error" not in out else 400, out)
        if method == "GET" and path == "/v1/audit":
            return Response(200, {"entries": canon.audit_log(q.get("patient_id"), min(int(q.get("limit", 100)), 1000))})
        if method == "GET" and path == "/v1/audit/verify":
            v = self.store.verify_audit_chain()  # the chain spans all tenants; expose only the verdict
            return Response(200, {"valid": v["valid"], "entries_checked": v["entries_checked"]})
        return _err(404, "not_found", f"No route {method} {path}")

    def _authenticate(self, h: dict) -> dict | None:
        auth = h.get("authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else None
        if self.sandbox:
            if self.static_keys and token not in self.static_keys:
                return None
            return dict(self.store.one("SELECT * FROM accounts WHERE id=?", (SANDBOX_ACCOUNT,)))
        return self.billing.authenticate(token)

    # ------------------------------------------------------------------ adapters
    def wsgi(self, environ, start_response):
        n = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(n) if n else b""
        headers = {k[5:].replace("_", "-"): v for k, v in environ.items() if k.startswith("HTTP_")}
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = environ["CONTENT_TYPE"]
        path = environ.get("PATH_INFO", "/") + (("?" + environ["QUERY_STRING"]) if environ.get("QUERY_STRING") else "")
        r = self.handle(environ["REQUEST_METHOD"], path, headers, body)
        reason = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized", 402: "Payment Required",
                  404: "Not Found", 409: "Conflict", 413: "Payload Too Large", 422: "Unprocessable Entity",
                  502: "Bad Gateway", 503: "Service Unavailable"}.get(r.status, "")
        start_response(f"{r.status} {reason}", [("Content-Type", r.content_type), ("Content-Length", str(len(r.body)))])
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
                self.end_headers()
                self.wfile.write(r.body)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

        return Handler


def serve(host: str = "127.0.0.1", port: int = 8080, db: str | None = "canon.db",
          sandbox: bool | None = None) -> ThreadingHTTPServer:
    app = App(Store(db), sandbox=sandbox)
    return ThreadingHTTPServer((host, port), app.http_handler())


def main() -> None:
    ap = argparse.ArgumentParser(description="Canon clinical data normalization API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=os.environ.get("CANON_DB", "canon.db"))
    ap.add_argument("--require-auth", action="store_true", help="Require API keys (default: sandbox, no auth)")
    a = ap.parse_args()
    sandbox = not a.require_auth and os.environ.get("CANON_SANDBOX") != "0"
    httpd = serve(a.host, a.port, None if os.environ.get("DATABASE_URL") else a.db, sandbox=sandbox)
    print(f"Canon listening on http://{a.host}:{a.port}  auth={'sandbox (none)' if sandbox else 'API keys'}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
