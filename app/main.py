"""HTTP API: canonical clinical normalization, billed per successful call."""

from __future__ import annotations

import hashlib
import html
import logging
import uuid
from typing import Any

import stripe
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .billing import Billing
from .config import Settings, get_settings
from .normalize import normalize_records
from .store import KeyStore

log = logging.getLogger(__name__)

MAX_RECORDS_PER_CALL = 1000


class NormalizeRequest(BaseModel):
    records: list[dict[str, Any]] = Field(..., max_length=MAX_RECORDS_PER_CALL)


def create_app(settings: Settings | None = None, billing: Billing | None = None) -> FastAPI:
    settings = settings or get_settings()
    billing = billing or Billing(settings)
    store = KeyStore(settings.db_path)

    app = FastAPI(
        title="Clinical Normalizer",
        description="Messy clinical records in, canonical FHIR R4 out. Billed per successful call.",
    )
    app.state.store = store
    app.state.billing = billing

    def api_key(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> tuple[str, str]:
        key = x_api_key
        if authorization and authorization.lower().startswith("bearer "):
            key = authorization[7:].strip()
        row = store.lookup(key) if key else None
        if row is None:
            raise HTTPException(401, "missing or invalid API key")
        return key, row["customer_id"]

    @app.post("/v1/normalize")
    def normalize(
        body: NormalizeRequest,
        auth: tuple[str, str] = Depends(api_key),
        idempotency_key: str | None = Header(default=None),
    ) -> dict:
        key, customer_id = auth
        result = normalize_records(body.records)

        # Only bill calls that produced at least one canonical resource.
        billed = result["normalized"] > 0
        event_id = None
        if billed:
            # Scope client idempotency keys to the customer so they can't collide.
            raw = f"{customer_id}:{idempotency_key}" if idempotency_key else uuid.uuid4().hex
            event_id = hashlib.sha256(raw.encode()).hexdigest()[:40]
            billing.report_call(customer_id, event_id)
            calls = store.record_call(key)
        else:
            calls = store.lookup(key)["calls"]

        result["billing"] = {
            "billed": billed,
            "meter_event_id": event_id,
            "price_per_call_usd": settings.price_per_call_usd,
            "calls_this_key": calls,
        }
        return result

    @app.get("/v1/usage")
    def usage(auth: tuple[str, str] = Depends(api_key)) -> dict:
        key, customer_id = auth
        calls = store.lookup(key)["calls"]
        return {
            "customer_id": customer_id,
            "billed_calls": calls,
            "price_per_call_usd": settings.price_per_call_usd,
            "estimated_cost_usd": round(calls * settings.price_per_call_usd, 4),
        }

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        cta = ('<a href="/signup">Get an API key</a>' if billing.enabled
               else "<p>Billing is off (dev mode). Mint a key with <code>python scripts/create_key.py</code>.</p>")
        return _page("Clinical Normalizer", f"""
            <p>Messy clinical records in, canonical FHIR R4 out (LOINC, SNOMED CT, ICD-10-CM, RxNorm, UCUM).</p>
            <p><b>${settings.price_per_call_usd} per successful call.</b> Calls that normalize nothing are free.</p>
            {cta}
            <p><a href="/docs">API docs</a></p>""")

    @app.get("/signup")
    def signup() -> RedirectResponse:
        if not billing.enabled:
            raise HTTPException(503, "billing is not configured")
        return RedirectResponse(billing.create_checkout_session(), status_code=303)

    @app.get("/signup/success", response_class=HTMLResponse)
    def signup_success(session_id: str) -> str:
        if not billing.enabled:
            raise HTTPException(503, "billing is not configured")
        try:
            session = billing.retrieve_checkout_session(session_id)
        except stripe.InvalidRequestError:
            raise HTTPException(404, "unknown checkout session")
        if session.status != "complete" or not session.customer:
            raise HTTPException(402, "checkout not completed")
        customer_id = session.customer if isinstance(session.customer, str) else session.customer.id
        key = store.create_key(customer_id, checkout_session_id=session_id)
        if key is None:
            return _page("Already issued", "<p>An API key was already shown for this checkout.</p>")
        return _page("Your API key", f"""
            <p>Copy it now; it won't be shown again.</p>
            <pre>{html.escape(key)}</pre>
            <pre>curl -X POST {html.escape(settings.public_base_url)}/v1/normalize \\
  -H "Authorization: Bearer {html.escape(key)}" -H "Content-Type: application/json" \\
  -d '{{"records":[{{"patient_id":"p1","type":"lab","test":"HbA1c","value":"7.2 %"}}]}}'</pre>""")

    @app.post("/stripe/webhook")
    async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None)) -> dict:
        if not billing.enabled:
            raise HTTPException(503, "billing is not configured")
        try:
            event = billing.parse_webhook(await request.body(), stripe_signature)
        except (ValueError, stripe.SignatureVerificationError):
            raise HTTPException(400, "invalid webhook signature")
        if event.type == "customer.subscription.deleted":
            customer = event.data.object.customer
            revoked = store.revoke_customer(customer)
            log.info("subscription ended for %s; revoked %d key(s)", customer, revoked)
        return {"received": True}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "billing": billing.enabled}

    return app


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:680px;margin:3rem auto;padding:0 1rem;line-height:1.5}}
pre{{background:#f4f4f4;padding:1rem;overflow-x:auto}}</style></head>
<body><h1>{title}</h1>{body}</body></html>"""


app = create_app()
