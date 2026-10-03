"""Hosting concerns: auth, tenant isolation, Stripe billing, webhooks, WSGI."""

import hashlib
import hmac
import io
import json
import os
import time
import unittest

from canon.api import App
from canon.billing import Billing, BillingError, Stripe, _form, verify_webhook
from canon.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name: str) -> bytes:
    with open(os.path.join(ROOT, "samples", "maria_chen", name), "rb") as fh:
        return fh.read()


HL7 = _read("02_quest_labs.hl7")
FAX = _read("01_pcp_referral_fax.txt")


class FakeStripe:
    """Records calls and returns plausible Stripe objects."""

    def __init__(self, fail_meter=False):
        self.calls = []
        self.fail_meter = fail_meter

    def __call__(self, method, path, params, idem):
        self.calls.append((method, path, params, idem))
        if path == "/v1/customers":
            return {"id": f"cus_{len(self.calls)}"}
        if path == "/v1/checkout/sessions":
            return {"url": "https://checkout.stripe.com/c/pay/cs_test_1"}
        if path == "/v1/billing_portal/sessions":
            return {"url": "https://billing.stripe.com/p/session/1"}
        if path == "/v1/billing/meter_events":
            if self.fail_meter:
                raise BillingError("stripe_error", "Stripe: boom", 502)
            return {"object": "billing.meter_event"}
        raise AssertionError(path)

    def meter_events(self):
        return [c for c in self.calls if c[1] == "/v1/billing/meter_events"]


def signed(payload: dict, secret: str, ts: int | None = None):
    body = json.dumps(payload).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


class HostedTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(STRIPE_PRICE_ID="price_123", STRIPE_WEBHOOK_SECRET="whsec_test", CANON_FREE_DOCUMENTS="2",
                          CRON_SECRET="cron123", CANON_PUBLIC_URL="https://canon.test")
        self.fake = FakeStripe()
        self.store = Store(":memory:")
        self.app = App(self.store, Billing(self.store, Stripe("sk_test_x", transport=self.fake)), sandbox=False)

    def tearDown(self):
        for k in ("STRIPE_PRICE_ID", "STRIPE_WEBHOOK_SECRET", "CANON_FREE_DOCUMENTS", "CRON_SECRET",
                  "CANON_PUBLIC_URL"):
            os.environ.pop(k, None)

    def call(self, method, path, body=b"", key=None, headers=None):
        h = dict(headers or {})
        if key:
            h["Authorization"] = f"Bearer {key}"
        r = self.app.handle(method, path, h, body if isinstance(body, bytes) else json.dumps(body).encode())
        return r.status, (json.loads(r.body) if r.content_type == "application/json" else r.body.decode())

    def signup(self, name="Acme Clinic"):
        st, out = self.call("POST", "/v1/signup", {"name": name, "email": "ops@acme.test"})
        self.assertEqual(st, 201, out)
        return out

    def activate(self, account_id, event_id="evt_1"):
        body, sig = signed({"id": event_id, "type": "checkout.session.completed",
                            "data": {"object": {"client_reference_id": account_id, "customer": None,
                                                "subscription": "sub_1"}}}, "whsec_test")
        return self.call("POST", "/v1/stripe/webhook", body, headers={"Stripe-Signature": sig})

    def test_auth_required(self):
        st, out = self.call("GET", "/v1/patients")
        self.assertEqual(st, 401)
        self.assertEqual(self.call("GET", "/v1/patients", key="cn_live_wrong")[0], 401)
        self.assertEqual(self.call("GET", "/healthz")[0], 200)

    def test_signup_creates_customer_and_checkout(self):
        out = self.signup()
        self.assertTrue(out["api_key"].startswith("cn_live_"))
        self.assertEqual(out["checkout_url"], "https://checkout.stripe.com/c/pay/cs_test_1")
        self.assertEqual(out["account"]["usage"]["free_documents_remaining"], 2)
        # key is stored hashed only
        self.assertIsNone(self.store.one("SELECT * FROM api_keys WHERE key_hash=?", (out["api_key"],)))
        checkout = next(c for c in self.fake.calls if c[1] == "/v1/checkout/sessions")[2]
        self.assertEqual(checkout["line_items"][0]["price"], "price_123")
        self.assertEqual(checkout["client_reference_id"], out["account"]["id"])

    def test_tenant_isolation(self):
        a, b = self.signup("A"), self.signup("B")
        st, r = self.call("POST", "/v1/documents", HL7, key=a["api_key"])
        self.assertEqual(st, 201)
        pid = r["patient_id"]
        self.assertEqual(self.call("GET", f"/v1/patients/{pid}/summary", key=b["api_key"])[0], 404)
        self.assertEqual(self.call("GET", "/v1/patients", key=b["api_key"])[1]["patients"], [])
        self.assertEqual(self.call("GET", f"/v1/documents/{r['document']['id']}", key=b["api_key"])[0], 404)
        # same document uploaded by B creates B's own patient, not a link to A's
        st, rb = self.call("POST", "/v1/documents", HL7, key=b["api_key"])
        self.assertEqual(st, 201)
        self.assertNotEqual(rb["patient_id"], pid)
        self.assertEqual(self.call("GET", "/v1/audit", key=b["api_key"])[1]["entries"][0]["patient_id"],
                         rb["patient_id"])

    def test_free_tier_then_payment_required_then_metered(self):
        a = self.signup()
        key, acct = a["api_key"], a["account"]["id"]
        self.assertEqual(self.call("POST", "/v1/documents", HL7, key=key)[0], 201)
        self.assertEqual(self.call("POST", "/v1/documents", HL7, key=key)[0], 200)  # duplicate: free, not counted
        self.assertEqual(self.call("POST", "/v1/documents", FAX, key=key)[0], 201)
        st, out = self.call("POST", "/v1/documents", b"Patient: Ann Lee DOB: 01/01/1960\nAllergies: NKDA", key=key)
        self.assertEqual(st, 402)
        self.assertEqual(out["error"]["code"], "payment_required")
        # tool ingest is gated too; reads are not
        self.assertEqual(self.call("POST", "/v1/tools/ingest_document", {"content": "x"}, key=key)[0], 402)
        self.assertEqual(self.call("GET", "/v1/patients", key=key)[0], 200)
        self.assertEqual(self.fake.meter_events(), [], "free-tier usage must never be billed")

        st, out = self.activate(acct)
        self.assertEqual((st, out["account_id"]), (200, acct))
        self.assertEqual(self.fake.meter_events(), [], "activation must not back-bill free usage")
        st, out = self.call("POST", "/v1/documents", b"Patient: Ann Lee DOB: 01/01/1960\nAllergies: NKDA", key=key)
        self.assertEqual(st, 201)
        ev = self.fake.meter_events()
        self.assertEqual(len(ev), 1)
        params, idem = ev[0][2], ev[0][3]
        self.assertEqual(params["payload"]["value"], 1)
        self.assertEqual(params["payload"]["stripe_customer_id"], self.store.one(
            "SELECT stripe_customer_id FROM accounts WHERE id=?", (acct,))["stripe_customer_id"])
        self.assertEqual(params["identifier"], idem)  # idempotent per usage event
        acct_view = self.call("GET", "/v1/account", key=key)[1]
        self.assertEqual(acct_view["status"], "active")
        self.assertEqual(acct_view["usage"]["documents_total"], 3)

    def test_failed_meter_event_is_retried_by_cron(self):
        a = self.signup()
        self.activate(a["account"]["id"])
        self.fake.fail_meter = True
        self.call("POST", "/v1/documents", HL7, key=a["api_key"])
        self.assertIsNotNone(self.store.one("SELECT report_error FROM usage_events")["report_error"])
        self.fake.fail_meter = False
        self.assertEqual(self.call("GET", "/v1/billing/sync")[0], 401)
        st, out = self.call("GET", "/v1/billing/sync", headers={"Authorization": "Bearer cron123"})
        self.assertEqual(out, {"attempted": 1, "reported": 1})
        self.assertEqual(self.call("GET", "/v1/billing/sync", headers={"Authorization": "Bearer cron123"})[1],
                         {"attempted": 0, "reported": 0})

    def test_webhook_signature_and_lifecycle(self):
        a = self.signup()
        acct = a["account"]["id"]
        cust = self.store.one("SELECT stripe_customer_id FROM accounts WHERE id=?", (acct,))["stripe_customer_id"]
        body, sig = signed({"id": "evt_x", "type": "checkout.session.completed", "data": {"object": {}}}, "wrong")
        self.assertEqual(self.call("POST", "/v1/stripe/webhook", body, headers={"Stripe-Signature": sig})[0], 400)
        self.activate(acct, "evt_a")
        self.assertTrue(self.activate(acct, "evt_a")[1]["duplicate"])  # replay ignored
        body, sig = signed({"id": "evt_b", "type": "invoice.payment_failed", "data": {"object": {"customer": cust}}},
                           "whsec_test")
        self.call("POST", "/v1/stripe/webhook", body, headers={"Stripe-Signature": sig})
        self.assertEqual(self.call("GET", "/v1/account", key=a["api_key"])[1]["status"], "past_due")
        self.assertEqual(self.call("POST", "/v1/documents", HL7, key=a["api_key"])[0], 402)
        body, sig = signed({"id": "evt_c", "type": "customer.subscription.deleted",
                            "data": {"object": {"customer": cust, "id": "sub_1", "status": "canceled"}}}, "whsec_test")
        self.call("POST", "/v1/stripe/webhook", body, headers={"Stripe-Signature": sig})
        self.assertEqual(self.call("GET", "/v1/account", key=a["api_key"])[1]["status"], "canceled")

    def test_old_webhook_rejected(self):
        body, sig = signed({"id": "e", "type": "x", "data": {"object": {}}}, "s", ts=int(time.time()) - 3600)
        with self.assertRaises(BillingError):
            verify_webhook(body, sig, "s")

    def test_portal(self):
        a = self.signup()
        st, out = self.call("POST", "/v1/billing/portal", key=a["api_key"])
        self.assertEqual(out["portal_url"], "https://billing.stripe.com/p/session/1")

    def test_wsgi_adapter(self):
        a = self.signup()
        environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/documents", "QUERY_STRING": "filename=l.hl7",
                   "CONTENT_LENGTH": str(len(HL7)), "wsgi.input": io.BytesIO(HL7),
                   "HTTP_AUTHORIZATION": f"Bearer {a['api_key']}"}
        status = {}
        body = b"".join(self.app.wsgi(environ, lambda s, h: status.update(s=s, h=dict(h))))
        self.assertEqual(status["s"], "201 Created")
        self.assertEqual(json.loads(body)["document"]["format"], "hl7v2")


class StripeEncodingTest(unittest.TestCase):
    def test_nested_form(self):
        self.assertEqual(_form({"line_items": [{"price": "p"}], "payload": {"value": 1}, "x": None, "b": True}),
                         [("line_items[0][price]", "p"), ("payload[value]", "1"), ("b", "true")])

    def test_billing_disabled_without_key(self):
        b = Billing(Store(":memory:"), Stripe(""))
        with self.assertRaises(BillingError) as cm:
            b.stripe.request("POST", "/v1/customers")
        self.assertEqual(cm.exception.status, 503)


if __name__ == "__main__":
    unittest.main()


class VercelEntryTest(unittest.TestCase):
    """app.py demo mode (no DATABASE_URL): seeded /tmp SQLite, optional static keys."""

    def run_app(self, env: dict, path: str, auth: str | None = None):
        import subprocess
        import sys
        import tempfile
        code = ("import io, json, app\n"
                f"env={{'REQUEST_METHOD':'GET','PATH_INFO':{path!r},'QUERY_STRING':'','wsgi.input':io.BytesIO()}}\n"
                + (f"env['HTTP_AUTHORIZATION']={auth!r}\n" if auth else "")
                + "st={}\nbody=b''.join(app.app(env, lambda s,h: st.update(s=s)))\n"
                  "print(json.dumps({'status': st['s'], 'body': json.loads(body)}))\n")
        with tempfile.TemporaryDirectory() as d:
            e = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "CANON_API_KEYS", "CANON_SANDBOX")}
            e.update(CANON_DB=os.path.join(d, "demo.db"), **env)
            out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=e, capture_output=True, text=True,
                                 check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_demo_mode_seeds_sample_patient(self):
        r = self.run_app({}, "/v1/patients")
        self.assertTrue(r["status"].startswith("200"))
        self.assertEqual(r["body"]["patients"][0]["name"], "Maria Chen")
        self.assertEqual(r["body"]["patients"][0]["documents"], 7)

    def test_demo_mode_static_keys(self):
        self.assertTrue(self.run_app({"CANON_API_KEYS": "k1:demo"}, "/v1/patients")["status"].startswith("401"))
        r = self.run_app({"CANON_API_KEYS": "k1:demo", "CANON_SEED_SAMPLES": "0"}, "/v1/patients", "Bearer k1")
        self.assertEqual(r["body"]["patients"], [])


class PlaygroundTest(unittest.TestCase):
    """The public demo page and its stateless playground API (works in production mode, no key)."""

    def setUp(self):
        self.store = Store(":memory:")
        self.app = App(self.store, Billing(self.store, Stripe("")), sandbox=False)

    def call(self, method, path, body=b""):
        r = self.app.handle(method, path, {}, body if isinstance(body, bytes) else json.dumps(body).encode())
        return r.status, r

    def test_page_is_public(self):
        st, r = self.call("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", r.content_type)
        self.assertIn(b"/v1/playground/normalize", r.body)

    def test_sample_patient_normalizes_without_storing(self):
        st, r = self.call("GET", "/v1/playground/samples")
        docs = json.loads(r.body)["documents"]
        self.assertEqual(len(docs), 7)
        st, r = self.call("POST", "/v1/playground/normalize", {"documents": docs})
        self.assertEqual(st, 200)
        out = json.loads(r.body)
        self.assertEqual(len(out["patients"]), 1)
        p = out["patients"][0]
        self.assertEqual(p["stats"]["documents"], 7)
        self.assertEqual({c["type"] for c in p["record"]["conflicts"]}, {"allergy_vs_nkda", "medication_discrepancy"})
        self.assertTrue(all("error" not in d for d in out["documents"]))
        # nothing persisted, nothing billed
        self.assertIsNone(self.store.one("SELECT id FROM documents"))
        self.assertIsNone(self.store.one("SELECT id FROM usage_events"))

    def test_limits(self):
        st, _ = self.call("POST", "/v1/playground/normalize", {"documents": [{"content": "x"}] * 13})
        self.assertEqual(st, 413)
        st, _ = self.call("POST", "/v1/playground/normalize", {"documents": []})
        self.assertEqual(st, 400)

    def test_bad_document_reported_not_fatal(self):
        st, r = self.call("POST", "/v1/playground/normalize", {"documents": [
            {"filename": "a.txt", "content": "Patient: Ann Lee DOB: 01/01/1960\nAllergies: Latex (hives)"},
            {"filename": "bad.json", "content": "{\"resourceType\": \"Bundle\", \"entry\": [ broken"}]})
        out = json.loads(r.body)
        self.assertEqual(st, 200)
        self.assertEqual(out["patients"][0]["record"]["allergies"][0]["substance"], "latex")
        bad = next(d for d in out["documents"] if d["filename"] == "bad.json")
        self.assertTrue(any("No clinical facts" in w for w in bad["warnings"]))
