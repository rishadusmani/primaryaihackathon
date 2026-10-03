"""Re-run the pipeline and hosting suites against real Postgres.

    CANON_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres python -m unittest tests.test_postgres

The database must have migrations/001_init.sql and 002_api_requests.sql applied. Tables are truncated.
"""

import os
import unittest

from canon.billing import Billing, Stripe
from canon.service import Canon
from canon.store import Store

URL = os.environ.get("CANON_TEST_DATABASE_URL")


def fresh_store() -> Store:
    s = Store(URL)
    s.execute("TRUNCATE canon.accounts, canon.api_keys, canon.usage_events, canon.api_requests, canon.stripe_events, "
              "canon.patients, "
              "canon.patient_keys, canon.documents, canon.audit CASCADE")
    s.execute("INSERT INTO accounts (id, name, status, created_at) VALUES ('acct_sandbox','Sandbox','sandbox','x')")
    return s


if URL:
    from tests import test_canon, test_hosting

    class PgPipelineTest(test_canon.PipelineTest):
        @classmethod
        def setUpClass(cls):
            cls.canon = Canon(fresh_store())
            cls.pid = test_canon.load_all(cls.canon)
            cls.rec = cls.canon.record(cls.pid)
            cls.summary = cls.canon.summary(cls.pid)

        def test_audit_chain(self):
            self.assertTrue(self.canon.store.verify_audit_chain()["valid"])
            with self.assertRaises(Exception):  # append-only trigger
                self.canon.store.execute("UPDATE audit SET actor='tampered'")

        def test_dialect(self):
            self.assertEqual(self.canon.store.dialect, "postgres")

    class PgHostedTest(test_hosting.HostedTest):
        def setUp(self):
            super().setUp()
            from canon.api import App
            self.store = fresh_store()
            self.app = App(self.store, Billing(self.store, Stripe("sk_test_x", transport=self.fake)), sandbox=False)

    class PgUsageTest(test_canon.UsageTest):
        make_store = staticmethod(fresh_store)
else:
    class PgSkipped(unittest.TestCase):
        @unittest.skip("CANON_TEST_DATABASE_URL not set")
        def test_postgres(self):
            pass


if __name__ == "__main__":
    unittest.main()
