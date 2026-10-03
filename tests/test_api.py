from types import SimpleNamespace

import pytest
import stripe
from fastapi.testclient import TestClient

from app.billing import Billing
from app.config import Settings
from app.main import create_app


class FakeStripe:
    """Stands in for stripe.StripeClient; records meter events."""

    def __init__(self):
        self.meter_events = []
        self.sessions = {}
        self.fail = False
        fake = self

        class MeterEvents:
            def create(self, params):
                if fake.fail:
                    raise stripe.APIConnectionError("down")
                fake.meter_events.append(params)

        class Sessions:
            def retrieve(self, sid):
                if sid not in fake.sessions:
                    raise stripe.InvalidRequestError("no such session", "id")
                return fake.sessions[sid]

            def create(self, params):
                return SimpleNamespace(url="https://checkout.stripe.test/s")

        self.v1 = SimpleNamespace(
            billing=SimpleNamespace(meter_events=MeterEvents()),
            checkout=SimpleNamespace(sessions=Sessions()),
        )
        self.event = None

    def construct_event(self, payload, sig, secret):
        if sig != "good":
            raise stripe.SignatureVerificationError("bad", sig)
        return self.event


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        stripe_secret_key="sk_test_fake", stripe_webhook_secret="whsec", stripe_price_id="price_1",
        meter_event_name="normalize_call", price_per_call_usd=0.005,
        public_base_url="http://test", db_path=str(tmp_path / "t.db"),
    )
    fake = FakeStripe()
    app = create_app(settings, Billing(settings, client=fake))
    key = app.state.store.create_key("cus_123")
    return SimpleNamespace(client=TestClient(app), fake=fake, key=key, app=app)


GOOD = {"records": [{"patient_id": "p1", "type": "lab", "test": "HbA1c", "value": "7.2 %"}]}
BAD = {"records": [{"patient_id": "p1", "type": "lab", "test": "Vitamin Q", "value": "1"}]}


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_rejects_missing_or_bad_key(env):
    assert env.client.post("/v1/normalize", json=GOOD).status_code == 401
    assert env.client.post("/v1/normalize", json=GOOD, headers=auth("cn_nope")).status_code == 401


def test_successful_call_is_metered_once(env):
    r = env.client.post("/v1/normalize", json=GOOD, headers=auth(env.key))
    assert r.status_code == 200
    body = r.json()
    assert body["normalized"] == 1
    assert body["billing"]["billed"] is True
    assert body["billing"]["calls_this_key"] == 1
    [event] = env.fake.meter_events
    assert event["event_name"] == "normalize_call"
    assert event["payload"] == {"stripe_customer_id": "cus_123", "value": "1"}
    assert event["identifier"] == body["billing"]["meter_event_id"]


def test_call_that_normalizes_nothing_is_free(env):
    body = env.client.post("/v1/normalize", json=BAD, headers={"X-API-Key": env.key}).json()
    assert body["billing"]["billed"] is False
    assert env.fake.meter_events == []


def test_idempotency_key_gives_stable_meter_identifier(env):
    h = {**auth(env.key), "Idempotency-Key": "abc"}
    a = env.client.post("/v1/normalize", json=GOOD, headers=h).json()["billing"]["meter_event_id"]
    b = env.client.post("/v1/normalize", json=GOOD, headers=h).json()["billing"]["meter_event_id"]
    assert a == b


def test_stripe_outage_does_not_fail_the_call(env):
    env.fake.fail = True
    assert env.client.post("/v1/normalize", json=GOOD, headers=auth(env.key)).status_code == 200


def test_usage(env):
    env.client.post("/v1/normalize", json=GOOD, headers=auth(env.key))
    env.client.post("/v1/normalize", json=GOOD, headers=auth(env.key))
    u = env.client.get("/v1/usage", headers=auth(env.key)).json()
    assert u["billed_calls"] == 2 and u["estimated_cost_usd"] == 0.01


def test_signup_success_issues_key_once(env):
    env.fake.sessions["cs_1"] = SimpleNamespace(status="complete", customer="cus_new")
    first = env.client.get("/signup/success", params={"session_id": "cs_1"})
    assert first.status_code == 200 and "cn_" in first.text
    second = env.client.get("/signup/success", params={"session_id": "cs_1"})
    assert "already" in second.text and "cn_" not in second.text


def test_signup_success_requires_completed_checkout(env):
    env.fake.sessions["cs_2"] = SimpleNamespace(status="open", customer=None)
    assert env.client.get("/signup/success", params={"session_id": "cs_2"}).status_code == 402
    assert env.client.get("/signup/success", params={"session_id": "cs_x"}).status_code == 404


def test_signup_redirects_to_checkout(env):
    r = env.client.get("/signup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.test")


def test_webhook_subscription_deleted_revokes_keys(env):
    assert env.client.post("/stripe/webhook", content=b"{}", headers={"Stripe-Signature": "bad"}).status_code == 400
    env.fake.event = SimpleNamespace(
        type="customer.subscription.deleted",
        data=SimpleNamespace(object=SimpleNamespace(customer="cus_123")),
    )
    assert env.client.post("/stripe/webhook", content=b"{}", headers={"Stripe-Signature": "good"}).status_code == 200
    assert env.client.post("/v1/normalize", json=GOOD, headers=auth(env.key)).status_code == 401
