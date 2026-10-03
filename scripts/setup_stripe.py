"""One-time Stripe setup: create the usage meter and a per-call metered price.

    STRIPE_SECRET_KEY=sk_test_... python scripts/setup_stripe.py

Prints the STRIPE_PRICE_ID to put in .env. Safe to re-run: reuses an existing
active meter with the same event name.
"""

import os
import sys
from decimal import Decimal

import stripe

event_name = os.getenv("STRIPE_METER_EVENT_NAME", "normalize_call")
price_usd = Decimal(os.getenv("PRICE_PER_CALL_USD", "0.005"))

if not os.getenv("STRIPE_SECRET_KEY"):
    sys.exit("Set STRIPE_SECRET_KEY (use a test-mode or sandbox key).")
client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])

meter = next(
    (m for m in client.v1.billing.meters.list({"status": "active", "limit": 100}).auto_paging_iter()
     if m.event_name == event_name),
    None,
)
if meter:
    print(f"Reusing meter {meter.id} ({event_name})")
else:
    meter = client.v1.billing.meters.create({
        "display_name": "Normalization calls",
        "event_name": event_name,
        "default_aggregation": {"formula": "sum"},
        "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
        "value_settings": {"event_payload_key": "value"},
    })
    print(f"Created meter {meter.id} ({event_name})")

price = client.v1.prices.create({
    "currency": "usd",
    "unit_amount_decimal": str(price_usd * 100),  # in cents
    "recurring": {"interval": "month", "usage_type": "metered", "meter": meter.id},
    "product_data": {"name": "Clinical Normalizer API"},
    "nickname": f"${price_usd} per call",
})
print(f"Created price {price.id} at ${price_usd}/call\n")
print(f"STRIPE_PRICE_ID={price.id}")
