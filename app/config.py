import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_id: str
    meter_event_name: str
    price_per_call_usd: float
    public_base_url: str
    db_path: str

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key)


def get_settings() -> Settings:
    """Read settings from the environment (call at startup, not import time)."""
    return Settings(
        stripe_secret_key=os.getenv("STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET", ""),
        stripe_price_id=os.getenv("STRIPE_PRICE_ID", ""),
        meter_event_name=os.getenv("STRIPE_METER_EVENT_NAME", "normalize_call"),
        price_per_call_usd=float(os.getenv("PRICE_PER_CALL_USD", "0.005")),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        db_path=os.getenv("DB_PATH", "normalizer.db"),
    )
