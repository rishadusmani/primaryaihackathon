"""Stripe usage-based billing: one meter event per successful API call."""

from __future__ import annotations

import logging

import stripe

from .config import Settings

log = logging.getLogger(__name__)


class Billing:
    def __init__(self, settings: Settings, client: stripe.StripeClient | None = None):
        self.settings = settings
        self.client = client or (
            stripe.StripeClient(settings.stripe_secret_key) if settings.billing_enabled else None
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def report_call(self, customer_id: str, identifier: str) -> bool:
        """Send one meter event. `identifier` makes retries idempotent on Stripe's side.

        Failures are logged, not raised: the caller already got their result.
        Returns True if the event was accepted (or billing is off).
        """
        if not self.enabled:
            log.info("billing disabled; would bill 1 call to %s (%s)", customer_id, identifier)
            return True
        try:
            self.client.v1.billing.meter_events.create({
                "event_name": self.settings.meter_event_name,
                "payload": {"stripe_customer_id": customer_id, "value": "1"},
                "identifier": identifier,
            })
            return True
        except stripe.StripeError:
            log.exception("failed to report meter event %s for %s", identifier, customer_id)
            return False

    def create_checkout_session(self) -> str:
        session = self.client.v1.checkout.sessions.create({
            "mode": "subscription",
            "line_items": [{"price": self.settings.stripe_price_id}],
            "success_url": f"{self.settings.public_base_url}/signup/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{self.settings.public_base_url}/",
        })
        return session.url

    def retrieve_checkout_session(self, session_id: str):
        return self.client.v1.checkout.sessions.retrieve(session_id)

    def parse_webhook(self, payload: bytes, signature: str | None):
        return self.client.construct_event(payload, signature, self.settings.stripe_webhook_secret)
