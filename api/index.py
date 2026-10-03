"""Vercel entry point: every route is rewritten here (see vercel.json).

Vercel's Python runtime serves the WSGI callable named `app`. The App instance
(and its Postgres connection) is created at import time and reused across warm
invocations.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canon.api import App  # noqa: E402

if not os.environ.get("DATABASE_URL") and os.environ.get("CANON_SANDBOX") != "1":
    raise RuntimeError("Set DATABASE_URL (Supabase Postgres) or CANON_SANDBOX=1 for a throwaway demo.")

_app = App()


def app(environ, start_response):
    return _app.wsgi(environ, start_response)
