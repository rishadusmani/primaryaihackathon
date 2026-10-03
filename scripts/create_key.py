"""Mint an API key directly (dev mode, or to give a teammate/agent a key).

    python scripts/create_key.py              # dev customer, no real billing
    python scripts/create_key.py cus_123      # bill an existing Stripe customer
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import get_settings  # noqa: E402
from app.store import KeyStore  # noqa: E402

customer = sys.argv[1] if len(sys.argv) > 1 else "cus_dev"
key = KeyStore(get_settings().db_path).create_key(customer)
print(f"API key for {customer}:\n{key}")
