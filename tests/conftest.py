import os
import tempfile

# Keep the module-level app in app.main off the real DB and off Stripe.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "import.db")
os.environ.pop("STRIPE_SECRET_KEY", None)
