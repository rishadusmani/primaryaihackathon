"""Vercel entry point: the WSGI `app` for Vercel's Python runtime (see vercel.json).

Two modes, chosen by environment:

* Production (DATABASE_URL set): Supabase Postgres, per-customer API keys and
  Stripe usage billing. See "Hosting" in the README and .env.example.
* Demo (no DATABASE_URL): SQLite in /tmp, which is per instance and wiped on
  cold starts. Each new instance reloads samples/maria_chen so the demo is
  usable (CANON_SEED_SAMPLES=0 skips it). Set CANON_API_KEYS to require
  `Authorization: Bearer <key>`; without it the demo API is open.
"""

from __future__ import annotations

import glob
import os
import sys

from canon.api import App
from canon.service import SANDBOX_ACCOUNT, Canon, CanonError
from canon.store import Store

ROOT = os.path.dirname(os.path.abspath(__file__))


def _seed(canon: Canon) -> None:
    pid = None
    for path in sorted(glob.glob(os.path.join(ROOT, "samples", "maria_chen", "*"))):
        with open(path, "rb") as fh:
            data = fh.read()
        name = os.path.basename(path)
        try:
            r = canon.ingest(data, filename=name, actor="seed")
        except CanonError as e:
            if e.code != "patient_unidentified" or not pid:
                print(f"seed: skipped {name}: {e.message}", file=sys.stderr)
                continue
            r = canon.ingest(data, filename=name, patient_id=pid, actor="seed")
        pid = pid or r["patient_id"]


if os.environ.get("DATABASE_URL"):
    _app = App(Store(), sandbox=False)
else:
    _app = App(Store(os.environ.get("CANON_DB", "/tmp/canon.db")), sandbox=True)
    _demo = Canon(_app.store, SANDBOX_ACCOUNT)
    if os.environ.get("CANON_SEED_SAMPLES", "1") != "0" and not _demo.list_patients():
        _seed(_demo)


def app(environ, start_response):
    return _app.wsgi(environ, start_response)
