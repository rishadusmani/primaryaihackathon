import base64
import glob
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from canon import terminology as T
from canon.mcp_server import handle
from canon.parsers import detect, llm, pdf, text
from canon.service import Canon, CanonError
from canon.tools import call_tool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = sorted(glob.glob(os.path.join(ROOT, "samples", "maria_chen", "*")))


def load_all(canon: Canon) -> str:
    pid = None
    for f in SAMPLES:
        with open(f, "rb") as fh:
            pid = canon.ingest(fh.read(), filename=os.path.basename(f),
                               patient_id=pid if f.endswith(".csv") else None)["patient_id"]
    return pid


class TerminologyTest(unittest.TestCase):
    def test_condition_synonyms_and_codes(self):
        self.assertEqual(T.lookup_condition(text="T2DM")["icd10"], "E11.9")
        self.assertEqual(T.lookup_condition(code="E1165", system="icd10")["icd10"], "E11.65")
        self.assertEqual(T.lookup_condition(code="38341003", system="2.16.840.1.113883.6.96")["icd10"], "I10")

    def test_medication_brand_to_ingredient(self):
        self.assertEqual(T.lookup_medication(text="Ozempic 0.5 mg pen")["rxnorm"], "1991302")
        self.assertEqual(T.lookup_medication(code="6809")["ingredient"], "metformin")

    def test_unit_conversion(self):
        self.assertEqual(T.convert_unit("4548-4", 60, "mmol/mol")[0], 7.64)       # IFCC -> NGSP %
        self.assertEqual(T.convert_unit("2160-0", 79.6, "umol/L")[0], 0.9)        # creatinine
        self.assertEqual(T.convert_unit("2345-7", 7.0, "mmol/L")[0], 126.11)      # glucose
        with self.assertRaises(ValueError):
            T.convert_unit("2345-7", 7.0, "furlongs")

    def test_frequency(self):
        self.assertEqual(T.parse_frequency("1 tab PO BID")["per_day"], 2)
        self.assertEqual(T.parse_frequency("every 2 weeks")["code"], "Q2WK")
        self.assertTrue(T.parse_frequency("q6h prn pain")["prn"])


class TextExtractionTest(unittest.TestCase):
    def test_ocr_negation_family_history(self):
        note = ("Patient: Jo Smith  DOB: 01/02/1970\nDate: 05/06/2026\n"
                "HPI: Denies diabetes. No history of hypertension.\n"
                "Family History: Mother had hyperlipidemia.\n"
                "Medications:\n- Metformin 5OO mg PO BID\n- Lisinopril 10 mg daily - discontinued\n"
                "Allergies: NKDA\nLabs: A1c 6.l %\n")
        facts = text.parse(note)
        conds = [f for f in facts if f["kind"] == "condition"]
        self.assertEqual(conds, [], "negated and family-history conditions must be dropped")
        meds = {f["text"]: f for f in facts if f["kind"] == "medication"}
        self.assertEqual(meds["metformin"]["dose_text"], "500 mg")
        self.assertEqual(meds["lisinopril"]["status"], "stopped")
        a1c = [f for f in facts if f["kind"] == "observation"][0]
        self.assertEqual(a1c["value"], "6.1")
        self.assertLess(a1c["confidence"], 0.8)  # OCR repair lowers confidence
        self.assertTrue(any(f.get("no_known_allergies") for f in facts))


class DetectTest(unittest.TestCase):
    def test_detects_every_sample(self):
        got = {}
        for f in SAMPLES:
            with open(f, "rb") as fh:
                got[os.path.basename(f)] = detect(fh.read(), os.path.basename(f))
        self.assertEqual(sorted(got.values()), sorted(["text", "hl7v2", "ccda", "fhir", "x12_837", "csv", "pdf"]))


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canon = Canon()
        cls.pid = load_all(cls.canon)
        cls.rec = cls.canon.record(cls.pid)
        cls.summary = cls.canon.summary(cls.pid)

    def test_single_patient_matched_across_sources(self):
        self.assertEqual(len(self.canon.list_patients()), 1)
        self.assertEqual(self.rec["patient"]["birth_date"], "1988-04-12")
        self.assertEqual(len(self.rec["sources"]), 7)

    def test_nothing_unmapped(self):
        self.assertEqual(self.rec["unmapped"], [])

    def test_lab_deduplicated_across_formats_after_unit_conversion(self):
        cr = [o for o in self.rec["observations"] if o["codes"]["loinc"] == "2160-0"]
        self.assertEqual(len(cr), 1)
        self.assertEqual(cr[0]["value"], 0.9)
        self.assertEqual({s["format"] for s in cr[0]["sources"]}, {"text", "hl7v2"})
        a1c = [o for o in self.rec["observations"] if o["codes"]["loinc"] == "4548-4"]
        self.assertEqual([o["effective"] for o in a1c], ["2025-06-10", "2025-09-15", "2026-03-02"])

    def test_alternate_loinc_collapses(self):
        ldl = [o for o in self.rec["observations"] if o["codes"]["loinc"] == "13457-7"]
        self.assertTrue(any(s["format"] == "hl7v2" for o in ldl for s in o["sources"]))  # HL7 sent 2089-1

    def test_conditions(self):
        by = {c["codes"]["icd10"]: c for c in self.rec["conditions"]}
        self.assertEqual(by["E11.65"]["status"], "active")       # most specific code wins (from claim)
        self.assertEqual(by["J45.909"]["status"], "resolved")    # from hospital CCD
        self.assertEqual(by["F41.1"]["evidence"], "claims_only")  # never documented clinically
        self.assertEqual(by["F41.1"]["status"], "unknown")
        self.assertNotIn("C50.9", by)                             # family history excluded

    def test_medication_reconciliation(self):
        meds = {m["ingredient"]: m for m in self.rec["medications"]}
        self.assertEqual(meds["metformin"]["dose"], "1000 mg")    # plan change wins
        self.assertEqual(meds["triamcinolone"]["status"], "stopped")
        self.assertEqual(meds["albuterol"]["status"], "stopped")
        self.assertEqual(meds["dupilumab"]["frequency"]["code"], "Q2WK")
        self.assertEqual(meds["dupilumab"]["route"], "subcutaneous")

    def test_conflicts_surface(self):
        types = {c["type"] for c in self.rec["conflicts"]}
        self.assertIn("allergy_vs_nkda", types)
        self.assertIn("medication_discrepancy", types)
        self.assertEqual(self.rec["allergy_status"], "has_allergies")

    def test_summary_trends(self):
        a1c = next(l for l in self.summary["latest_labs"] if l["loinc"] == "4548-4")
        self.assertEqual((a1c["value"], a1c["trend"]), (7.8, "up"))

    def test_fhir_export(self):
        b = self.canon.fhir(self.pid)
        kinds = {e["resource"]["resourceType"] for e in b["entry"]}
        self.assertTrue({"Patient", "Condition", "MedicationStatement", "AllergyIntolerance",
                         "Observation"} <= kinds)

    def test_duplicate_upload_is_idempotent(self):
        with open(SAMPLES[1], "rb") as fh:
            r = self.canon.ingest(fh.read())
        self.assertTrue(r["document"]["duplicate"])

    def test_audit_chain(self):
        v = self.canon.store.verify_audit_chain()
        self.assertTrue(v["valid"])
        self.canon.store.execute("UPDATE audit SET actor='tampered' WHERE seq=1")
        self.assertFalse(self.canon.store.verify_audit_chain()["valid"])
        self.canon.store.execute("UPDATE audit SET actor='api' WHERE seq=1")


class MatchingTest(unittest.TestCase):
    def test_unidentified_requires_patient_id(self):
        with self.assertRaises(CanonError):
            Canon().ingest("Test,Result\nA1c,7.1\n", filename="x.csv")

    def test_misfiled_document_warning(self):
        c = Canon()
        pid = c.ingest("Patient: Ann Lee DOB: 01/01/1960\nAllergies: NKDA\n")["patient_id"]
        r = c.ingest("Patient: Bob Ray DOB: 02/02/1950\nAllergies: NKDA\n", patient_id=pid)
        self.assertIn("warning", r["match"])
        self.assertIn("demographic_mismatch", {x["type"] for x in c.record(pid)["conflicts"]})


class LLMMappingTest(unittest.TestCase):
    def test_unverified_evidence_is_downgraded(self):
        src = "Pt on metformin 500 mg BID. A1c 7.2%."
        facts = llm.to_facts({"patient": {"given_name": None, "family_name": None, "birth_date": None, "sex": None,
                                          "mrn": None},
                              "facts": [
                                  {"kind": "medication", "name": "metformin", "value": None, "unit": None, "date": None,
                                   "dose": "500 mg", "route": None, "frequency": "BID", "status": "active",
                                   "reaction": None, "evidence": "metformin 500 mg BID"},
                                  {"kind": "medication", "name": "warfarin", "value": None, "unit": None, "date": None,
                                   "dose": "5 mg", "route": None, "frequency": "daily", "status": "active",
                                   "reaction": None, "evidence": "warfarin 5 mg daily"}]}, source_text=src)
        self.assertEqual(facts[0]["confidence"], 0.85)
        self.assertFalse(facts[1]["evidence_verified"])
        self.assertLess(facts[1]["confidence"], 0.5)


class PdfTest(unittest.TestCase):
    def test_roundtrip(self):
        t, info = pdf.extract_text(pdf.make_text_pdf("Allergies: Latex (hives)\nBP 120/80"))
        self.assertIn("Latex (hives)", t)
        self.assertFalse(info["needs_ocr"])


class InterfacesTest(unittest.TestCase):
    def test_tools_and_mcp(self):
        c = Canon()
        with open(SAMPLES[1], "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        r = call_tool(c, "ingest_document", {"content": content, "encoding": "base64"})
        pid = r["patient_id"]
        s = call_tool(c, "get_patient_summary", {"patient_id": pid})
        self.assertTrue(s["latest_labs"])
        obs = call_tool(c, "get_observations", {"patient_id": pid, "names": ["a1c"]})["observations"]
        item = obs[0]["id"]
        prov = call_tool(c, "get_provenance", {"patient_id": pid, "item_id": item})
        self.assertEqual(prov["sources"][0]["locator"], "OBX[1]")
        self.assertIn("error", call_tool(c, "get_patient_summary", {"patient_id": "pat_nope"}))
        init = handle(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "canon")
        lst = handle(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertGreaterEqual(len(lst["result"]["tools"]), 9)
        call = handle(c, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "get_conflicts", "arguments": {"patient_id": pid}}})
        self.assertFalse(call["result"]["isError"])

    def test_http_api(self):
        from canon.api import make_handler
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Canon()))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            with open(SAMPLES[1], "rb") as fh:
                req = urllib.request.Request(base + "/v1/documents?filename=labs.hl7", data=fh.read(), method="POST")
            r = json.loads(urllib.request.urlopen(req).read())
            pid = r["patient_id"]
            s = json.loads(urllib.request.urlopen(f"{base}/v1/patients/{pid}/summary").read())
            self.assertEqual(s["patient"]["name"], "Maria Chen")
            tools = json.loads(urllib.request.urlopen(base + "/v1/tools?format=openai").read())
            self.assertEqual(tools["tools"][0]["type"], "function")
            v = json.loads(urllib.request.urlopen(base + "/v1/audit/verify").read())
            self.assertTrue(v["valid"])
        finally:
            srv.shutdown()



class UsageTest(unittest.TestCase):
    def _serve(self, canon):
        from canon.api import make_handler
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(canon))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}"

    @staticmethod
    def _get(url, key=None, data=None):
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_usage_is_metered_per_api_key(self):
        os.environ["CANON_API_KEYS"] = "ka:acme,kb:globex"
        self.addCleanup(os.environ.pop, "CANON_API_KEYS")
        base = self._serve(Canon())
        with open(SAMPLES[1], "rb") as fh:
            st, body = self._get(base + "/v1/documents?filename=labs.hl7", "ka", fh.read())
        self.assertEqual(st, 201)
        pid = json.loads(body)["patient_id"]
        self._get(f"{base}/v1/patients/{pid}/summary", "ka")
        self._get(f"{base}/v1/patients/nope/summary", "ka")
        self._get(base + "/v1/tools/get_conflicts", "ka", json.dumps({"patient_id": pid}).encode())
        self._get(base + "/v1/patients", "kb")
        self.assertEqual(self._get(base + "/v1/usage", "bad")[0], 401)

        u = json.loads(self._get(base + "/v1/usage?days=7", "ka")[1])
        self.assertEqual(u["client_id"], "acme")
        self.assertEqual(u["totals"]["requests"], 4)          # /v1/usage itself is not metered
        self.assertEqual(u["totals"]["errors"], 1)
        self.assertEqual(u["totals"]["documents_ingested"], 1)
        self.assertEqual(u["totals"]["patients_accessed"], 1)
        ops = {o["operation"]: o["requests"] for o in u["operations"]}
        self.assertEqual(ops, {"documents.ingest": 1, "patients.summary": 2, "tool.get_conflicts": 1})
        self.assertEqual(len(u["daily"]), 7)
        self.assertEqual(sum(d["requests"] for d in u["daily"]), 4)
        self.assertNotIn("clients", u)
        self.assertEqual(u["recent"][0]["patient_id"], pid)   # tool call attributed via its arguments

        g = json.loads(self._get(base + "/v1/usage", "kb")[1])
        self.assertEqual(g["totals"]["requests"], 1)          # tenants never see each other's usage

    def test_dashboard_served_without_key(self):
        os.environ["CANON_API_KEYS"] = "ka:acme"
        self.addCleanup(os.environ.pop, "CANON_API_KEYS")
        st, body = self._get(self._serve(Canon()) + "/dashboard")
        self.assertEqual(st, 200)
        self.assertIn(b"/v1/usage", body)

    def test_mcp_tool_calls_and_llm_tokens_are_metered(self):
        from canon import usage
        c = Canon()
        pid = load_all(c)
        handle(c, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "get_patient_summary", "arguments": {"patient_id": pid}}})
        usage.record(c.store, client_id="local", channel="api", operation="documents.ingest", status=201,
                     latency_ms=900, body={"document": {"extraction": {"llm_usage": {"input_tokens": 1200,
                                                                                     "output_tokens": 300}}}})
        u = usage.summarize(c.store, None)
        self.assertEqual(u["channels"], {"mcp": 1, "api": 1})
        self.assertEqual(u["totals"]["llm_input_tokens"], 1200)
        self.assertEqual(u["daily"][-1]["llm_tokens"], 1500)
        self.assertEqual(u["recent"][-1]["operation"], "tool.get_patient_summary")


if __name__ == "__main__":
    unittest.main()
