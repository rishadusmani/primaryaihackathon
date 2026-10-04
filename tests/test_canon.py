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
from canon.parsers import detect, hl7v2, llm, pdf, text
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
        # a multi-dose phrase wins over the bare "daily"/"weekly" inside it
        for phrase, code in (("twice daily", "BID"), ("1000 mg twice daily", "BID"), ("two times daily", "BID"),
                             ("2x daily", "BID"), ("three times daily", "TID"), ("thrice daily", "TID"),
                             ("4 times daily", "QID"), ("twice weekly", "BIW"), ("3 times weekly", "TIW"),
                             ("daily", "QD"), ("once daily", "QD"), ("weekly", "QWK"),
                             ("daily and at bedtime", "QD")):
            self.assertEqual(T.parse_frequency(phrase)["code"], code, phrase)


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
        self.assertEqual(meds["metformin"]["frequency"]["code"], "BID")

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


HEAD = "Patient: Ann Lee DOB: 01/01/1960\n"


class ChronologyTest(unittest.TestCase):
    def notes(self, *docs: str) -> tuple[Canon, str]:
        c, pid = Canon(), None
        self.doc_ids = []
        for d in docs:
            r = c.ingest(d, patient_id=pid)
            pid = r["patient_id"]
            self.doc_ids.append(r["document"]["id"])
        return c, pid

    def test_undated_document_cannot_override_dated_one(self):
        c, pid = self.notes(HEAD + "Date of service: 2026-01-10\nMedications:\nMetformin 1000 mg twice daily\n",
                            HEAD + "Medications:\nMetformin 500 mg daily\n")   # uploaded later, but undated
        rec = c.record(pid)
        met = next(m for m in rec["medications"] if m["ingredient"] == "metformin")
        self.assertEqual((met["dose"], met["last_changed"]), ("1000 mg", "2026-01-10"))
        flags = [x for x in rec["conflicts"] if x["type"] == "undated_source"]
        self.assertEqual([(f["severity"], f["document_id"]) for f in flags], [("low", self.doc_ids[1])])
        self.assertNotIn("medication_discrepancy", {x["type"] for x in rec["conflicts"]})

    def test_generated_date_stands_in_for_a_missing_clinical_date(self):
        c, pid = self.notes(HEAD + "Date of service: 2026-01-10\nMedications:\nMetformin 1000 mg twice daily\n",
                            HEAD + "Medications:\nMetformin 500 mg daily\n"
                                   "Electronically signed by Dr. Ruiz on 04/01/2026 09:12\n")
        rec = c.record(pid)
        src = next(s for s in rec["sources"] if s["id"] == self.doc_ids[1])
        self.assertEqual((src["document_date"], src["date_basis"]), ("2026-04-01", "generated"))
        met = next(m for m in rec["medications"] if m["ingredient"] == "metformin")
        self.assertEqual((met["dose"], met["last_changed"]), ("500 mg", "2026-04-01"))
        self.assertNotIn("undated_source", {x["type"] for x in rec["conflicts"]})

    def test_each_parser_reports_when_the_document_was_produced(self):
        from canon import parsers

        def produced(fmt, data):
            facts, _ = parsers.parse(fmt, data, use_llm=False)
            doc = next((f for f in facts if f["kind"] == "document"), None)
            return doc and (doc["generated"], doc["provenance"]["locator"])

        self.assertEqual(produced("ccda", b'<ClinicalDocument xmlns="urn:hl7-org:v3"><title>CCD</title>'
                                          b'<effectiveTime value="20260215103000"/></ClinicalDocument>'),
                         ("2026-02-15", "ClinicalDocument/effectiveTime"))
        self.assertEqual(produced("fhir", json.dumps({"resourceType": "Bundle", "timestamp": "2026-02-20T10:00:00Z",
                                                      "entry": []}).encode()), ("2026-02-20", "Bundle.timestamp"))
        self.assertEqual(produced("hl7v2", b"MSH|^~\\&|LAB|QUEST|||20260302101500||ORU^R01|1|P|2.5.1\r"),
                         ("2026-03-02", "MSH-7"))
        self.assertEqual(produced("x12_837", b"ISA*00*~GS*HC*S*R*20260304*1200*1*X*005010X222A1~"
                                             b"BHT*0019*00*1*20260305*1200*CH~"), ("2026-03-05", "BHT"))
        pdf_bytes = pdf.make_text_pdf("Medications: metformin 500 mg daily")
        pdf_bytes = pdf_bytes.replace(b"%%EOF", b"<< /CreationDate (D:20260320083000Z) >>\n%%EOF")
        self.assertEqual(produced("pdf", pdf_bytes), ("2026-03-20", "PDF /CreationDate"))
        self.assertEqual(produced("text", b"FAX 03/01/2026 10:14 From: Clinic\nSigned: 02/27/2026\n"),
                         ("2026-02-27", "line 2"))   # a signature dates the content better than the fax header
        self.assertEqual(produced("text", b"Fax sent 03/01/2026 10:14\nMeds: metformin\n"), ("2026-03-01", "line 1"))
        self.assertIsNone(produced("csv", b"Test,Result\nA1c,7.1\n"))
        self.assertIsNone(produced("text", b"Medications: metformin 500 mg\n"))

    def test_multi_visit_note_dates_each_line_by_its_visit(self):
        c, pid = self.notes(HEAD + "Date of service: 2026-01-10\nMedications:\nMetformin 500 mg daily\n\n"
                                   "2026-03-02 Follow-up visit\nPlan: increase metformin to 1000 mg twice daily\n"
                                   "Allergies: NKDA\n")
        rec = c.record(pid)
        met = next(m for m in rec["medications"] if m["ingredient"] == "metformin")
        self.assertEqual([(h["date"], h["dose"]) for h in met["history"]],
                         [("2026-01-10", "500 mg"), ("2026-03-02", "1000 mg")])
        self.assertEqual(sorted(e["date"] for e in rec["encounters"]), ["2026-01-10", "2026-03-02"])

    def test_ccda_entry_author_time_dates_the_statement(self):
        ccd = (b'<ClinicalDocument xmlns="urn:hl7-org:v3"><effectiveTime value="20260401"/><component><structuredBody>'
               b'<component><section><code code="10160-0"/><entry><substanceAdministration>'
               b'<author><time value="20260215"/></author><effectiveTime><low value="20190301"/></effectiveTime>'
               b'<doseQuantity value="500" unit="mg"/><consumable><manufacturedProduct><manufacturedMaterial>'
               b'<code code="860975" displayName="metformin 500 MG Oral Tablet"/></manufacturedMaterial>'
               b'</manufacturedProduct></consumable></substanceAdministration></entry></section></component>'
               b'</structuredBody></component></ClinicalDocument>')
        c, pid = self.notes(HEAD + "Date of service: 2026-01-10\nMedications:\nMetformin 1000 mg twice daily\n")
        c.ingest(ccd, filename="ccd.xml", patient_id=pid)
        met = next(m for m in c.record(pid)["medications"] if m["ingredient"] == "metformin")
        # stated 2026-02-15 (author time), not 2019 (when the drug was started): the CCD is the latest word
        self.assertEqual((met["dose"], met["last_changed"]), ("500 mg", "2026-02-15"))

    def test_fhir_resource_dated_by_its_encounter(self):
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
            {"fullUrl": "urn:uuid:enc-1", "resource": {"resourceType": "Encounter", "id": "e1",
                                                        "period": {"start": "2026-02-20"}}},
            {"resource": {"resourceType": "AllergyIntolerance", "id": "a1", "code": {"text": "Penicillin"},
                          "encounter": {"reference": "urn:uuid:enc-1"}}}]}
        c, pid = self.notes(HEAD + "Date of service: 2025-05-01\nAllergies: NKDA\n")
        c.ingest(json.dumps(bundle), filename="portal.json", patient_id=pid)
        rec = c.record(pid)
        self.assertEqual([h["date"] for h in rec["allergies"][0]["history"]], ["2026-02-20"])  # the encounter's date
        self.assertEqual(rec["allergy_status"], "has_allergies")

    def test_same_day_statements_ordered_by_time(self):
        c, pid = self.notes(HEAD + "Admission 03/02/2026 08:05\nMedications:\nMetformin 500 mg daily\n",
                            HEAD + "Discharge 03/02/2026 4:40 PM\nMedications:\nMetformin 1000 mg twice daily\n")
        rec = c.record(pid)
        met = next(m for m in rec["medications"] if m["ingredient"] == "metformin")
        self.assertEqual(met["dose"], "1000 mg")
        self.assertEqual([h.get("at") for h in met["history"]], ["2026-03-02T08:05", "2026-03-02T16:40"])
        self.assertNotIn("medication_discrepancy", {x["type"] for x in rec["conflicts"]})  # a change, not a clash

    def test_same_day_without_times_is_still_a_conflict(self):
        c, pid = self.notes(HEAD + "Admission 03/02/2026 08:05\nMedications:\nMetformin 500 mg daily\n",
                            HEAD + "Date of service: 03/02/2026\nMedications:\nMetformin 1000 mg twice daily\n")
        self.assertIn("medication_discrepancy", {x["type"] for x in c.record(pid)["conflicts"]})

    def test_later_list_contradicting_an_explicit_change_is_flagged(self):
        c, pid = self.notes(HEAD + "Visit 03/02/2026 08:00\nPlan: increase metformin to 1000 mg twice daily\n",
                            HEAD + "Visit 03/02/2026 16:00\nMedications:\nMetformin 500 mg daily\n")
        rec = c.record(pid)
        met = next(m for m in rec["medications"] if m["ingredient"] == "metformin")
        self.assertEqual(met["dose"], "1000 mg")   # the explicit change still wins the day
        self.assertIn("medication_discrepancy", {x["type"] for x in rec["conflicts"]})

    def test_hl7_order_time_dates_medications(self):
        msg = ("MSH|^~\\&|EHR|HOSP|||20260302170000||RDE^O11|1|P|2.5.1\r"
               "ORC|NW|1|||||||20260302164000\r"  # ORC-9: when the order was placed
               "RXE|^QD^daily|860975^metformin 500 MG Oral Tablet^RXNORM|500||mg\r")
        med = next(f for f in hl7v2.parse(msg) if f["kind"] == "medication")
        self.assertEqual(med["as_of"], "2026-03-02T16:40")

    def test_readings_at_different_times_are_a_series_not_a_mismatch(self):
        c, pid = self.notes(HEAD + "Date of service: 2026-03-02\nAllergies: NKDA\n")
        c.ingest("Test,Result,Unit,Date\nGlucose,98,mg/dL,2026-03-02 08:00\n", filename="am.csv", patient_id=pid)
        c.ingest("Test,Result,Unit,Date\nGlucose,180,mg/dL,2026-03-02 16:00\n", filename="pm.csv", patient_id=pid)
        self.assertNotIn("observation_mismatch", {x["type"] for x in c.record(pid)["conflicts"]})
        glucose = next(lab for lab in c.summary(pid)["latest_labs"] if lab["name"].lower().startswith("glucose"))
        self.assertEqual((glucose["value"], glucose["at"], glucose["trend"]), (180, "2026-03-02T16:00", "up"))
        c.ingest("Test,Result,Unit,Date\nGlucose,120,mg/dL,2026-03-02\n", filename="untimed.csv", patient_id=pid)
        self.assertIn("observation_mismatch", {x["type"] for x in c.record(pid)["conflicts"]})

    def test_allergy_resolved_by_negative_challenge(self):
        c, pid = self.notes(
            HEAD + "Date of service: 2025-05-01\nAllergies: Penicillin (hives)\n",
            HEAD + "Date of service: 2026-03-02\nAllergies: NKDA\nAssessment:\n"
                   "Penicillin allergy delabeled after negative oral amoxicillin challenge.\n")
        rec, summary = c.record(pid), c.summary(pid)
        pcn = next(a for a in rec["allergies"] if a["substance"] == "penicillin")
        self.assertEqual((pcn["status"], pcn["resolved_on"]), ("resolved", "2026-03-02"))
        self.assertEqual([h["status"] for h in pcn["history"]], ["active", "resolved"])
        self.assertEqual(rec["allergy_status"], "no_known_allergies")
        self.assertNotIn("allergy_vs_nkda", {x["type"] for x in rec["conflicts"]})
        self.assertEqual(summary["allergies"], [])
        self.assertEqual(summary["resolved_allergies"][0]["substance"], "penicillin")
        self.assertNotIn("amoxicillin", {m["ingredient"] for m in rec["medications"]})  # the challenge drug
        fhir = next(e["resource"] for e in c.fhir(pid)["entry"] if e["resource"]["id"] == pcn["id"])
        self.assertEqual(fhir["clinicalStatus"]["coding"][0]["code"], "resolved")

    def test_allergy_listed_again_after_resolution_stays_active_and_is_flagged(self):
        c, pid = self.notes(
            HEAD + "Date of service: 2026-03-02\nPenicillin allergy removed after negative amoxicillin challenge.\n",
            HEAD + "Date of service: 2026-06-15\nAllergies: Penicillin (hives)\n")   # copied-forward list
        rec = c.record(pid)
        self.assertEqual(rec["allergies"][0]["status"], "active")
        self.assertEqual(rec["allergy_status"], "has_allergies")
        self.assertIn("allergy_resolution_disputed", {x["type"] for x in rec["conflicts"]})

    def test_undated_resolution_does_not_clear_a_dated_allergy(self):
        c, pid = self.notes(HEAD + "Date of service: 2026-01-10\nAllergies: Penicillin (hives)\n",
                            HEAD + "Penicillin allergy delabeled, tolerated amoxicillin challenge.\n")
        rec = c.record(pid)
        self.assertEqual(rec["allergies"][0]["status"], "active")
        self.assertTrue({"allergy_resolution_disputed", "undated_source"} <= {x["type"] for x in rec["conflicts"]})

    def test_failed_or_planned_challenge_resolves_nothing(self):
        for line in ("Failed amoxicillin challenge, developed hives.",
                     "Consider penicillin allergy delabeling with amoxicillin challenge."):
            c, pid = self.notes(HEAD + "Date of service: 2026-01-10\nAllergies: Penicillin (hives)\n",
                                HEAD + "Date of service: 2026-02-10\n" + line + "\n")
            rec = c.record(pid)
            self.assertEqual([a["status"] for a in rec["allergies"]], ["active"], line)

    def test_fhir_refuted_allergy(self):
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": {
            "resourceType": "AllergyIntolerance", "code": {"text": "Penicillin"}, "recordedDate": "2026-04-01",
            "verificationStatus": {"coding": [{"code": "refuted"}]}}}]}
        c, pid = self.notes(HEAD + "Date of service: 2025-05-01\nAllergies: Penicillin (hives)\n")
        c.ingest(json.dumps(bundle), filename="portal.json", patient_id=pid)
        rec = c.record(pid)
        self.assertEqual((rec["allergies"][0]["status"], rec["allergy_status"]), ("refuted", "unknown"))
        fhir = next(e["resource"] for e in c.fhir(pid)["entry"]
                    if e["resource"]["resourceType"] == "AllergyIntolerance")
        self.assertNotIn("clinicalStatus", fhir)
        self.assertEqual(fhir["verificationStatus"]["coding"][0]["code"], "refuted")


class LLMMappingTest(unittest.TestCase):
    def test_request_params_per_model(self):
        haiku = llm.request_params("claude-haiku-4-5", [{"type": "text", "text": "x"}])
        self.assertEqual(set(haiku["output_config"]), {"format"})  # no effort, no fallback beta on Haiku
        self.assertNotIn("betas", haiku)
        opus = llm.request_params("claude-opus-5-5", [])
        self.assertEqual(opus["output_config"]["effort"], "medium")
        self.assertEqual(opus["fallbacks"], "default")
        self.assertEqual(llm.MODEL if "CANON_LLM_MODEL" in os.environ else "claude-haiku-4-5", llm.MODEL)

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
        from canon.api import App
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), App(sandbox=True).http_handler())
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
            srv.server_close()



class UsageTest(unittest.TestCase):
    @staticmethod
    def make_store():
        from canon.store import Store
        return Store(":memory:")

    def setUp(self):
        from canon.api import App
        from canon.billing import Billing, Stripe
        self.store = self.make_store()
        self.app = App(self.store, Billing(self.store, Stripe("")), sandbox=False)
        self.ka = self.app.billing.create_account("Acme Clinic", None)[1]
        self.kb = self.app.billing.create_account("Globex Health", None)[1]

    def call(self, method, path, key=None, body=b""):
        r = self.app.handle(method, path, {"Authorization": f"Bearer {key}"} if key else {}, body)
        return r.status, (json.loads(r.body) if r.content_type == "application/json" else r.body.decode())

    def test_usage_is_metered_per_account(self):
        with open(SAMPLES[1], "rb") as fh:
            st, r = self.call("POST", "/v1/documents?filename=labs.hl7", self.ka, fh.read())
        self.assertEqual(st, 201)
        pid = r["patient_id"]
        self.call("GET", f"/v1/patients/{pid}/summary", self.ka)
        self.call("GET", "/v1/patients/nope/summary", self.ka)
        self.call("POST", "/v1/tools/get_conflicts", self.ka, json.dumps({"patient_id": pid}).encode())
        self.call("GET", "/v1/account", self.ka)                 # account calls are not agent work
        self.call("GET", "/v1/patients", self.kb)
        self.assertEqual(self.call("GET", "/v1/usage", "cn_live_wrong")[0], 401)

        st, u = self.call("GET", "/v1/usage?days=7", self.ka)
        self.assertEqual(st, 200)
        self.assertEqual(u["account"]["name"], "Acme Clinic")
        self.assertEqual(u["totals"]["requests"], 4)          # /v1/usage and /v1/account are not metered
        self.assertEqual(u["totals"]["errors"], 1)
        self.assertEqual(u["totals"]["documents_ingested"], 1)
        self.assertEqual(u["totals"]["patients_accessed"], 1)
        ops = {o["operation"]: o["requests"] for o in u["operations"]}
        self.assertEqual(ops, {"documents.ingest": 1, "patients.summary": 2, "tool.get_conflicts": 1})
        self.assertEqual(len(u["daily"]), 7)
        self.assertEqual(sum(d["requests"] for d in u["daily"]), 4)
        self.assertEqual(u["recent"][0]["patient_id"], pid)   # tool call attributed via its arguments

        self.assertEqual(self.call("GET", "/v1/usage", self.kb)[1]["totals"]["requests"], 1)  # tenants isolated

    def test_dashboard_served_without_key(self):
        st, body = self.call("GET", "/dashboard")
        self.assertEqual(st, 200)
        self.assertIn("/v1/usage", body)

    def test_about_page_served_without_key(self):
        st, body = self.call("GET", "/about")
        self.assertEqual(st, 200)
        self.assertIn("Why Canon exists", body)

    def test_connect_page_served_without_key(self):
        st, body = self.call("GET", "/connect")
        self.assertEqual(st, 200)
        self.assertIn("/v1/signup", body)
        self.assertIn('"/mcp"', body)
        st, r = self.call("POST", "/v1/signup", body=json.dumps({"name": "From the page"}).encode())
        self.assertEqual(st, 201)
        self.assertEqual(self.call("POST", "/mcp", r["api_key"],
                                   json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())[0], 200)

    def test_remote_mcp_over_http(self):
        def rpc(payload, key=self.ka):
            return self.call("POST", "/mcp", key, json.dumps(payload).encode())

        st, _ = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, key=None)
        self.assertEqual(st, 401)
        r = self.app.handle("POST", "/mcp", {}, b"{}")
        self.assertIn("WWW-Authenticate", r.headers)
        self.assertEqual(self.call("GET", "/mcp", self.ka)[0], 405)

        st, init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}}})
        self.assertEqual((st, init["result"]["serverInfo"]["name"]), (200, "canon"))
        self.assertEqual(rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        st, lst = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(lst["result"]["tools"]), 9)

        # a billing refusal reaches the agent as a readable tool error, not a transport failure
        from canon.billing import BillingError
        with open(SAMPLES[1], "rb") as fh:
            ingest = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ingest_document",
                      "arguments": {"content": base64.b64encode(fh.read()).decode(), "encoding": "base64"}}}
        real = self.app.billing.check_can_ingest
        self.app.billing.check_can_ingest = lambda a: (_ for _ in ()).throw(
            BillingError("payment_required", "A subscription is required.", 402))
        st, r = rpc(ingest)
        self.assertEqual(st, 200)
        self.assertTrue(r["result"]["isError"])
        self.assertIn("payment_required", r["result"]["content"][0]["text"])
        self.app.billing.check_can_ingest = real
        pid = json.loads(rpc(ingest)[1]["result"]["content"][0]["text"])["patient_id"]

        # the same account reads it over REST; other accounts can't see it over MCP
        self.assertEqual(self.call("GET", f"/v1/patients/{pid}/summary", self.ka)[0], 200)
        st, batch = rpc([{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "get_conflicts", "arguments": {"patient_id": pid}}},
                         {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
        self.assertEqual([m["id"] for m in batch], [4, 5])
        self.assertFalse(batch[0]["result"]["isError"])
        st, other = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                         "params": {"name": "get_patient_summary", "arguments": {"patient_id": pid}}}, self.kb)
        self.assertTrue(other["result"]["isError"])
        self.assertEqual(self.call("POST", "/mcp", self.ka, b"{nope")[1]["error"]["code"], -32700)

        u = self.call("GET", "/v1/usage", self.ka)[1]
        self.assertEqual(u["channels"].get("mcp"), 3)  # tool calls only: 2 ingests + get_conflicts
        self.assertEqual(u["totals"]["errors"], 1)      # the refused ingest

    def test_mcp_tool_calls_and_llm_tokens_are_metered(self):
        from canon import usage
        c = Canon(self.make_store())
        pid = load_all(c)
        handle(c, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "get_patient_summary", "arguments": {"patient_id": pid}}})
        usage.record(c.store, account_id=c.account_id, channel="api", operation="documents.ingest", status=201,
                     latency_ms=900, body={"document": {"extraction": {"llm_usage": {"input_tokens": 1200,
                                                                                     "output_tokens": 300}}}})
        u = usage.summarize(c.store, c.account_id)
        self.assertEqual(u["channels"], {"mcp": 1, "api": 1})
        self.assertEqual(u["totals"]["llm_input_tokens"], 1200)
        self.assertEqual(u["daily"][-1]["llm_tokens"], 1500)
        self.assertEqual(u["recent"][-1]["operation"], "tool.get_patient_summary")


if __name__ == "__main__":
    unittest.main()
