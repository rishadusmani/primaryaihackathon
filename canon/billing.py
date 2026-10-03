"""Accounts, API keys and Stripe usage-based billing.

Model
-----
* Each customer is an **account** with one or more **API keys** (stored only as
  SHA-256 hashes; the plaintext is shown once at creation).
* Every newly normalized document is a billable **usage event**
  (`document.normalized`, quantity 1). Re-uploading an identical document is free.
* New accounts get CANON_FREE_DOCUMENTS free documents (default 25). After that
  they need an active Stripe subscription, otherwise ingest returns
  402 payment_required with a checkout link. Reads are never blocked.
* Usage is sent to a Stripe **Billing Meter** right away (idempotent by usage
  event id). Anything that fails to send is retried by the /v1/billing/sync cron.
* Stripe webhooks activate, suspend or cancel accounts.

Environment
-----------
STRIPE_SECRET_KEY       sk_live_... / sk_test_...  (billing disabled if unset)
STRIPE_WEBHOOK_SECRET   whsec_...  (from the Stripe webhook endpoint)
STRIPE_PRICE_ID         price_...  (metered price; create with `python -m canon.billing setup`)
STRIPE_METER_EVENT      meter event name (default canon_document_normalized)
CANON_PUBLIC_URL        e.g. https://canon.example.com (Checkout/Portal return URLs)
CANON_FREE_DOCUMENTS    free documents per account before a subscription is required (default 25)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from .store import Store, new_id

METER_EVENT = os.environ.get("STRIPE_METER_EVENT", "canon_document_normalized")
ACTIVE_STATES = {"active"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00Z")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class BillingError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


# ---------------------------------------------------------------------------- Stripe client
def _form(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's nested form encoding: a[b][0][c]=v."""
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _form(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += _form(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", str(item))]
        elif v is not None:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


class Stripe:
    """Minimal Stripe REST client (stdlib only). `transport` is injectable for tests."""

    def __init__(self, secret_key: str | None = None, transport: Callable | None = None):
        self.secret_key = secret_key if secret_key is not None else os.environ.get("STRIPE_SECRET_KEY", "")
        self.transport = transport or self._http

    @property
    def enabled(self) -> bool:
        return bool(self.secret_key)

    def request(self, method: str, path: str, params: dict | None = None, idempotency_key: str | None = None) -> dict:
        if not self.enabled:
            raise BillingError("billing_not_configured", "Stripe is not configured on this server.", 503)
        return self.transport(method, path, params or {}, idempotency_key)

    def _http(self, method: str, path: str, params: dict, idempotency_key: str | None) -> dict:
        data = urllib.parse.urlencode(_form(params)).encode() if method == "POST" else None
        url = "https://api.stripe.com" + path
        if method == "GET" and params:
            url += "?" + urllib.parse.urlencode(_form(params))
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.secret_key}")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            msg = body.get("error", {}).get("message", str(e))
            raise BillingError("stripe_error", f"Stripe: {msg}", 502) from e


def verify_webhook(payload: bytes, signature_header: str, secret: str, tolerance: int = 300,
                   now: float | None = None) -> dict:
    """Verify a Stripe-Signature header (t=...,v1=...) and return the event."""
    if not secret:
        raise BillingError("webhook_not_configured", "STRIPE_WEBHOOK_SECRET is not set.", 503)
    parts: dict[str, list[str]] = {}
    for item in (signature_header or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts.setdefault(k.strip(), []).append(v.strip())
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError):
        raise BillingError("bad_signature", "Malformed Stripe-Signature header.", 400)
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in parts.get("v1", [])):
        raise BillingError("bad_signature", "Stripe signature verification failed.", 400)
    if abs((now or time.time()) - ts) > tolerance:
        raise BillingError("bad_signature", "Stripe webhook timestamp outside tolerance.", 400)
    return json.loads(payload)


# ---------------------------------------------------------------------------- accounts + billing
class Billing:
    def __init__(self, store: Store, stripe: Stripe | None = None):
        self.store = store
        self.stripe = stripe or Stripe()
        self.price_id = os.environ.get("STRIPE_PRICE_ID", "")
        self.public_url = os.environ.get("CANON_PUBLIC_URL", "http://localhost:8080").rstrip("/")
        self.free_documents = int(os.environ.get("CANON_FREE_DOCUMENTS", "25"))
        self.webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # -- accounts / keys -------------------------------------------------------------
    def create_account(self, name: str, email: str | None, status: str = "trial") -> tuple[dict, str]:
        if not name or len(name) > 200:
            raise BillingError("invalid_request", "name is required (max 200 chars).")
        if email is not None and ("@" not in email or len(email) > 320):
            raise BillingError("invalid_request", "email is invalid.")
        aid = new_id("acct")
        self.store.execute("INSERT INTO accounts (id, name, email, status, created_at) VALUES (?,?,?,?,?)",
                           (aid, name, email, status, _now()))
        if self.stripe.enabled:
            cust = self.stripe.request("POST", "/v1/customers",
                                       {"name": name, "email": email, "metadata": {"canon_account_id": aid}},
                                       idempotency_key=f"customer-{aid}")
            self.store.execute("UPDATE accounts SET stripe_customer_id=? WHERE id=?", (cust["id"], aid))
        key = self.create_key(aid)
        self.store.audit(ts=_now(), event="account.created", actor="signup", account_id=aid,
                         detail={"status": status})
        return self.account(aid), key

    def create_key(self, account_id: str) -> str:
        key = "cn_live_" + secrets.token_urlsafe(24)
        self.store.execute("INSERT INTO api_keys (id, account_id, key_hash, prefix, created_at) VALUES (?,?,?,?,?)",
                           (new_id("key"), account_id, hash_key(key), key[:12], _now()))
        return key

    def authenticate(self, key: str | None) -> dict | None:
        if not key:
            return None
        r = self.store.one("SELECT a.* FROM api_keys k JOIN accounts a ON a.id = k.account_id "
                           "WHERE k.key_hash=? AND k.revoked_at IS NULL", (hash_key(key),))
        return dict(r) if r else None

    def account(self, account_id: str) -> dict:
        r = self.store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not r:
            raise BillingError("not_found", "Account not found.", 404)
        a = dict(r)
        used_total = self._count(account_id)
        return {"id": a["id"], "name": a["name"], "email": a["email"], "status": a["status"],
                "billing_enabled": self.stripe.enabled,
                "has_subscription": a["status"] in ACTIVE_STATES,
                "usage": {"documents_this_month": self._count(account_id, since=_month_start()),
                          "documents_total": used_total,
                          "free_documents_remaining": max(0, self.free_documents - used_total)},
                "created_at": a["created_at"]}

    def _count(self, account_id: str, since: str | None = None) -> int:
        if since:
            r = self.store.one("SELECT COALESCE(SUM(quantity),0) AS n FROM usage_events WHERE account_id=? "
                               "AND created_at>=?", (account_id, since))
        else:
            r = self.store.one("SELECT COALESCE(SUM(quantity),0) AS n FROM usage_events WHERE account_id=?",
                               (account_id,))
        return int(r["n"])

    # -- entitlement + metering -------------------------------------------------------
    def check_can_ingest(self, account: dict) -> None:
        if account["status"] in ACTIVE_STATES or account["status"] == "sandbox":
            return
        if account["status"] in ("past_due", "canceled"):
            raise BillingError("payment_required", f"Subscription is {account['status']}. Update billing to "
                               "continue normalizing documents.", 402, portal="/v1/billing/portal")
        if self._count(account["id"]) >= self.free_documents:
            raise BillingError("payment_required",
                               f"Free tier used ({self.free_documents} documents). Start a subscription via "
                               "POST /v1/billing/checkout.", 402, checkout="/v1/billing/checkout")

    def record_usage(self, account_id: str, document_id: str) -> None:
        acct = self.store.one("SELECT status, stripe_customer_id FROM accounts WHERE id=?", (account_id,))
        if acct and acct["status"] == "sandbox":
            return
        uid = new_id("use")
        billable = bool(acct and acct["status"] in ACTIVE_STATES)  # free-tier usage is never billed
        self.store.execute("INSERT INTO usage_events (id, account_id, kind, quantity, billable, document_id, "
                           "created_at) VALUES (?,?,?,?,?,?,?)",
                           (uid, account_id, "document.normalized", 1, int(billable), document_id, _now()))
        if billable and acct["stripe_customer_id"]:
            self._report(uid, acct["stripe_customer_id"], 1)

    def _report(self, usage_id: str, customer_id: str, quantity: int) -> bool:
        try:
            self.stripe.request("POST", "/v1/billing/meter_events", {
                "event_name": METER_EVENT, "identifier": usage_id, "timestamp": int(time.time()),
                "payload": {"stripe_customer_id": customer_id, "value": quantity}}, idempotency_key=usage_id)
        except BillingError as e:
            self.store.execute("UPDATE usage_events SET report_error=? WHERE id=?", (e.message[:500], usage_id))
            return False
        self.store.execute("UPDATE usage_events SET reported_at=?, report_error=NULL WHERE id=?", (_now(), usage_id))
        return True

    def sync_unreported(self, limit: int = 500) -> dict:
        """Retry billable meter events that failed to send (current period only)."""
        rows = self.store.all(
            "SELECT u.id, u.quantity, a.stripe_customer_id FROM usage_events u JOIN accounts a ON a.id=u.account_id "
            "WHERE u.reported_at IS NULL AND u.billable=1 AND a.status='active' AND a.stripe_customer_id IS NOT NULL "
            "AND u.created_at>=? ORDER BY u.created_at LIMIT ?", (_month_start(), limit))
        sent = sum(1 for r in rows if self._report(r["id"], r["stripe_customer_id"], r["quantity"]))
        return {"attempted": len(rows), "reported": sent}

    # -- Stripe hosted pages -----------------------------------------------------------
    def checkout_url(self, account: dict) -> str:
        if not self.price_id:
            raise BillingError("billing_not_configured", "STRIPE_PRICE_ID is not set on this server.", 503)
        params = {"mode": "subscription", "client_reference_id": account["id"],
                  "line_items": [{"price": self.price_id}],
                  "success_url": f"{self.public_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
                  "cancel_url": f"{self.public_url}/billing/cancel",
                  "subscription_data": {"metadata": {"canon_account_id": account["id"]}}}
        if account.get("stripe_customer_id"):
            params["customer"] = account["stripe_customer_id"]
        else:
            params["customer_email"] = account.get("email")
        return self.stripe.request("POST", "/v1/checkout/sessions", params)["url"]

    def portal_url(self, account: dict) -> str:
        if not account.get("stripe_customer_id"):
            raise BillingError("no_customer", "No Stripe customer yet; start with POST /v1/billing/checkout.", 409)
        return self.stripe.request("POST", "/v1/billing_portal/sessions", {
            "customer": account["stripe_customer_id"], "return_url": f"{self.public_url}/billing/return"})["url"]

    # -- webhooks ------------------------------------------------------------------------
    def handle_webhook(self, payload: bytes, signature: str) -> dict:
        event = verify_webhook(payload, signature, self.webhook_secret)
        if self.store.execute("INSERT INTO stripe_events (id, type, received_at) VALUES (?,?,?) "
                              "ON CONFLICT DO NOTHING", (event["id"], event["type"], _now())) == 0:
            return {"received": True, "duplicate": True}
        obj = event["data"]["object"]
        etype = event["type"]
        account_id = None
        if etype == "checkout.session.completed":
            account_id = obj.get("client_reference_id")
            if account_id:
                self.store.execute("UPDATE accounts SET status='active', stripe_customer_id=COALESCE(?, "
                                   "stripe_customer_id), stripe_subscription_id=? WHERE id=?",
                                   (obj.get("customer"), obj.get("subscription"), account_id))
                self.sync_unreported()
        elif etype in ("customer.subscription.created", "customer.subscription.updated",
                       "customer.subscription.deleted"):
            status = {"active": "active", "trialing": "active", "past_due": "past_due", "unpaid": "past_due",
                      "canceled": "canceled", "incomplete_expired": "canceled", "paused": "past_due"}.get(
                obj.get("status"), None)
            if etype == "customer.subscription.deleted":
                status = "canceled"
            if status:
                row = self.store.one("SELECT id FROM accounts WHERE stripe_customer_id=?", (obj.get("customer"),))
                if row:
                    account_id = row["id"]
                    self.store.execute("UPDATE accounts SET status=?, stripe_subscription_id=? WHERE id=?",
                                       (status, obj.get("id"), account_id))
        elif etype == "invoice.payment_failed":
            row = self.store.one("SELECT id FROM accounts WHERE stripe_customer_id=?", (obj.get("customer"),))
            if row:
                account_id = row["id"]
                self.store.execute("UPDATE accounts SET status='past_due' WHERE id=?", (account_id,))
        self.store.audit(ts=_now(), event=f"stripe.{etype}", actor="stripe", account_id=account_id,
                         detail={"event_id": event["id"]})
        return {"received": True, "type": etype, "account_id": account_id}


# ---------------------------------------------------------------------------- one-time Stripe setup
def setup(price_cents: str, currency: str = "usd") -> dict:
    """Create the Billing Meter + metered Price in Stripe. Run once per Stripe account/mode."""
    s = Stripe()
    meter = s.request("POST", "/v1/billing/meters", {
        "display_name": "Documents normalized", "event_name": METER_EVENT,
        "default_aggregation": {"formula": "sum"},
        "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
        "value_settings": {"event_payload_key": "value"}})
    price = s.request("POST", "/v1/prices", {
        "currency": currency, "unit_amount_decimal": price_cents,
        "recurring": {"interval": "month", "usage_type": "metered", "meter": meter["id"]},
        "product_data": {"name": "Canon clinical data normalization"},
        "nickname": f"Per document ({price_cents}c)"})
    return {"meter_id": meter["id"], "price_id": price["id"]}


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m canon.billing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("setup", help="Create the Stripe meter and metered price")
    sp.add_argument("--price-cents", default="10", help="Price per normalized document in cents (decimal ok)")
    sp.add_argument("--currency", default="usd")
    a = ap.parse_args()
    if a.cmd == "setup":
        out = setup(a.price_cents, a.currency)
        print(json.dumps(out, indent=2))
        print(f"\nSet on your host:  STRIPE_PRICE_ID={out['price_id']}")


if __name__ == "__main__":
    main()
