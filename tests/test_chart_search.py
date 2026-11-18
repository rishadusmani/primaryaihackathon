import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from canon.api import App
from canon.chart_demo import ChartDemo
from canon.parsers.pdf import make_text_pdf
from canon.service import Canon, CanonError
from canon.store import Store


class ChartSearchTest(unittest.TestCase):
    def setUp(self):
        self.canon = Canon()

    def note(self, patient="one", text="", **kwargs):
        with patch.dict(os.environ, {"CANON_LIVE_TERMINOLOGY": "0"}):
            return self.canon.ingest(text or "Metformin increased because A1c remained above goal.",
                                     patient_id=patient, use_llm=False, **kwargs)["document"]

    def test_reason_in_source_survives_without_an_extracted_fact(self):
        self.note(text="Referral reason: persistent itching after topical treatment.\nReview in six weeks.")
        result = self.canon.search("one", "Why was itching referred?")
        self.assertEqual(result["total"], 1)
        passage = result["results"][0]
        source = self.canon.document_text("one", passage["document_id"])
        self.assertEqual(passage["excerpt"], source["text"][passage["start"]:passage["end"]])
        self.assertIn("persistent itching", passage["excerpt"])

    def test_patient_and_account_scope_on_search_and_source_reads(self):
        self.note(patient="one", text="Metformin. Private sentinel for patient one.")
        other = self.note(patient="two", text="Metformin. Private sentinel for patient two.")
        other_account = Canon(self.canon.store, account_id="other")
        with patch.dict(os.environ, {"CANON_LIVE_TERMINOLOGY": "0"}):
            other_account.ingest("Metformin. Different account sentinel.", patient_id="one", use_llm=False)
        result = self.canon.search("one", "metformin")
        self.assertEqual(result["total"], 1)
        self.assertNotIn("patient two", json.dumps(result))
        self.assertNotIn("Different account sentinel", json.dumps(result))
        with self.assertRaises(CanonError):
            self.canon.document_text("one", other["id"])
        self.assertIn("Different account sentinel", json.dumps(other_account.search("one", "metformin")))

    def test_term_expansion_uses_canon_vocabulary(self):
        self.note(text="Eliquis 5 mg PO BID. Continue at follow-up.")
        result = self.canon.search("one", "apixaban")
        self.assertEqual(result["total"], 1)
        self.assertIn("Eliquis", result["results"][0]["excerpt"])

    def test_pdf_search_preserves_extracted_source(self):
        with patch.dict(os.environ, {"CANON_LIVE_TERMINOLOGY": "0"}):
            self.canon.ingest(make_text_pdf("Referral reason: itching.\nPrior treatment was unsuccessful."),
                              patient_id="one", filename="letter.pdf", use_llm=False)
        result = self.canon.search("one", "itching", fmt="pdf")
        self.assertEqual(result["total"], 1)
        source = self.canon.document_text("one", result["results"][0]["document_id"])
        self.assertEqual(source["text_basis"], "Extracted PDF text")
        self.assertIn("Prior treatment", source["text"])

    def test_filters_and_blank_query_return_the_same_document_scope(self):
        self.note(text="Date: 03/02/2026\nMetformin 500 mg BID", filename="note.txt")
        self.note(text="Metformin mentioned in an undated note.", filename="undated.txt")
        result = self.canon.search("one", "metformin", since="2026-03-01", until="2026-03-31", fmt="text")
        self.assertEqual(result["coverage"]["searched_documents"], 1)
        self.assertEqual(result["total"], 1)
        blank = self.canon.search("one", "", since="2026-03-01")
        self.assertEqual([d["filename"] for d in blank["documents"]], ["note.txt"])
        self.assertEqual(self.canon.search("one", "metformin", fmt="pdf")["total"], 0)

    def test_old_database_backfill_deduplication_and_concurrent_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.db")
            self.canon = Canon(path)
            self.note()
            self.note()  # Existing ingest deduplicates identical content.
            restored = Canon(Store(path))
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: restored.search("one", "metformin"), range(4)))
            self.assertTrue(all(result["total"] == 1 for result in results))
            count = restored.store.one("SELECT count(*) AS n FROM chart_search_documents")["n"]
            self.assertEqual(count, 1)

    def test_invalid_queries_and_fts_syntax_are_handled(self):
        self.note()
        for filters in ({"since": "bad"}, {"since": "2026-04-01", "until": "2026-03-01"},
                        {"fmt": "bad"}, {"limit": 0}):
            with self.assertRaises(CanonError):
                self.canon.search("one", "metformin", **filters)
        with self.assertRaises(CanonError):
            self.canon.search("one", "x" * 301)
        self.assertEqual(self.canon.search("one", '"metformin" OR * -- (')["total"], 1)
        self.assertEqual(self.canon.search("one", "???")["total"], 0)

    def test_unreadable_pdf_is_reported_within_the_selected_scope(self):
        self.note()
        document = self.note(text="Temporary text before simulating a scanned source.")
        scan = b"%PDF-1.4\n<< /Type /Page >>\n%%EOF"
        self.canon.store.execute("UPDATE documents SET raw=?,format='pdf',sha256='scan' WHERE id=?",
                                 (scan, document["id"]))
        result = self.canon.search("one", "metformin")
        self.assertEqual(result["coverage"]["selected_documents"], 2)
        self.assertEqual(result["coverage"]["searched_documents"], 1)
        self.assertEqual(result["coverage"]["unsearchable_documents"], 1)
        filtered = self.canon.search("one", "metformin", fmt="text")
        self.assertEqual(filtered["coverage"]["unsearchable_documents"], 0)

    def test_unicode_offsets_and_long_notes(self):
        text = "Résumé 🩺\n" + "Unrelated source material.\n" * 100 + "Medication withheld because of nausea."
        self.note(text=text)
        result = self.canon.search("one", "nausea")
        passage = result["results"][0]
        self.assertEqual(passage["excerpt"], text[passage["start"]:passage["end"]])
        self.assertLessEqual(len(passage["excerpt"]), 1000)


class ChartDemoTest(unittest.TestCase):
    def test_demo_uses_bundled_charts_without_external_calls(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("External call")):
            demo = ChartDemo()
            self.assertEqual(len(demo.patients), 2)
            maria, james = demo.patients
            self.assertEqual(len(maria["documents"]), 7)
            self.assertEqual(len(james["documents"]), 2)
            self.assertTrue(maria["conflicts"])
            for patient in demo.patients:
                for question in patient["questions"]:
                    self.assertGreater(demo.canon.search(patient["patient_id"], question)["total"], 0, question)

    def test_public_demo_is_isolated_from_private_store(self):
        app = App(api_keys={"test-key"})
        with patch.dict(os.environ, {"CANON_LIVE_TERMINOLOGY": "0"}):
            secret = app.canon.ingest("Secret medication sentinel", patient_id="private", use_llm=False)
        listing = app.handle("GET", "/v1/playground/charts", {}, b"")
        self.assertEqual(listing.status, 200)
        self.assertNotIn("private", listing.body.decode())
        response = app.handle("GET", "/v1/playground/charts/private/search?q=secret", {}, b"")
        self.assertEqual(response.status, 404)
        response = app.handle("GET", f"/v1/playground/charts/private/documents/{secret['document']['id']}/text", {}, b"")
        self.assertEqual(response.status, 404)
        self.assertEqual(app.handle("GET", "/v1/patients/private/search?q=secret", {}, b"").status, 401)
        response = app.handle("GET", "/v1/patients/private/search?q=secret", {"Authorization": "Bearer test-key"}, b"")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.data["total"], 1)
        self.assertEqual(app.handle("GET", "/chart-search", {}, b"").status, 200)


if __name__ == "__main__":
    unittest.main()
