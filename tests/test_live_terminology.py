"""Live NLM fallback, tested against a fake NLM (no network)."""

import glob
import os
import unittest
from unittest import mock

from canon import live_terminology as L
from canon import playground
from canon.normalize import normalize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_GET = L._get

CONCEPTS = {  # rxcui -> (name, tty)
    "1656340": ("Entresto", "BN"), "1656339": ("sacubitril / valsartan", "MIN"),
    "1656328": ("sacubitril", "IN"), "69749": ("valsartan", "IN"),
    "2562811": ("finerenone", "IN"), "2562812": ("Kerendia", "BN"),
    "6809": ("metformin", "IN"), "4603": ("furosemide", "IN"),
}
NAMES = {"entresto": ["1656340"], "kerendia": ["2562812"]}
RELATED = {("1656340", "IN"): ["1656328", "69749"], ("1656340", "MIN"): ["1656339"], ("2562812", "IN"): ["2562811"]}
APPROX = {"metfromin": ["4603"]}  # a fuzzy hit on the wrong drug: must be rejected
ICD10 = {"I50.22": "Chronic systolic (congestive) heart failure", "N18.4": "Chronic kidney disease, stage 4 (severe)"}
LOINC = {"2947-0": "Sodium [Moles/volume] in Blood"}
EPC = {"2562811": ["Nonsteroidal Mineralocorticoid-Receptor Antagonist"]}


class FakeNLM:
    def __init__(self):
        self.calls = []

    def __call__(self, url, **params):
        self.calls.append((url, params))
        path = url.split("nih.gov", 1)[1]
        if path.endswith("/icd10cm/v3/search"):
            t = params["terms"]
            hits = [[c, n] for c, n in ICD10.items() if c == t or n.lower() == t.lower()]
            return [len(hits), [h[0] for h in hits], None, hits]
        if path.endswith("/loinc_items/v3/search"):
            hits = [[c, n] for c, n in LOINC.items() if c == params["terms"]]
            return [len(hits), [h[0] for h in hits], None, hits]
        if path.endswith("/rxcui.json"):
            return {"idGroup": {"rxnormId": NAMES.get(params["name"], [])}}
        if path.endswith("/approximateTerm.json"):
            return {"approximateGroup": {"candidate": [{"rxcui": c} for c in APPROX.get(params["term"], [])]}}
        if "/rxclass/" in path:
            names = EPC.get(params["rxcui"], [])
            return {"rxclassDrugInfoList": {"rxclassDrugInfo": [{"rxclassMinConceptItem": {"className": n}} for n in names]}}
        if path.endswith("/properties.json"):
            cui = path.split("/")[-2]
            name, tty = CONCEPTS.get(cui, (None, None))
            return {"properties": {"rxcui": cui, "name": name, "tty": tty}} if name else {}
        if path.endswith("/related.json"):
            cui = path.split("/")[-2]
            got = [{"rxcui": c, "name": CONCEPTS[c][0]} for c in RELATED.get((cui, params["tty"]), [])]
            return {"relatedGroup": {"conceptGroup": [{"conceptProperties": got}] if got else []}}
        raise AssertionError(f"unexpected NLM call {url} {params}")


class LiveTestCase(unittest.TestCase):
    def setUp(self):
        L._cache.clear()
        L._failures, L._open_until = 0, 0.0
        self.fake = FakeNLM()
        patcher = mock.patch.object(L, "_get", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"CANON_LIVE_TERMINOLOGY": "1"})
        env.start()
        self.addCleanup(env.stop)


class LiveLookupTest(LiveTestCase):
    def test_brand_resolves_to_combination(self):
        m = L.medication(text="Entresto 49-51 mg tablet")
        self.assertEqual((m["ingredient"], m["rxnorm"], m["terminology"]), ("sacubitril / valsartan", "1656339", "live_lookup"))
        self.assertNotIn("_ingredients", m)

    def test_brand_resolves_to_ingredient_with_class(self):
        m = L.medication(text="Kerendia 10 mg PO daily")
        self.assertEqual((m["ingredient"], m["drug_class"]), ("finerenone", "Nonsteroidal Mineralocorticoid-Receptor Antagonist"))

    def test_fuzzy_match_to_a_different_drug_is_rejected(self):
        self.assertIsNone(L.medication(text="metfromin 500 mg"))

    def test_icd10_code_verified_and_normalised(self):
        c = L.condition(code="I5022")
        self.assertEqual((c["icd10"], c["display"], c["verified"]), ("I50.22", ICD10["I50.22"], True))
        self.assertIsNone(L.condition(code="Q99.9"))  # not a (fake) billable code

    def test_condition_text_needs_exact_official_title(self):
        self.assertEqual(L.condition(text="chronic kidney disease, stage 4 (severe)")["icd10"], "N18.4")
        self.assertIsNone(L.condition(text="kidney disease"))

    def test_results_are_cached(self):
        L.medication(text="Entresto")
        n = len(self.fake.calls)
        L.medication(text="Entresto")
        self.assertEqual(len(self.fake.calls), n)

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"CANON_LIVE_TERMINOLOGY": "0"}):
            self.assertIsNone(L.medication(text="Entresto"))
        self.assertEqual(self.fake.calls, [])

    def test_spent_budget_skips_lookups_without_caching_the_miss(self):
        with L.budget(0):
            self.assertIsNone(L.medication(text="Entresto"))
        self.assertIsNotNone(L.medication(text="Entresto"))

    def test_circuit_breaker_opens_after_repeated_failures(self):
        # the real _get against a dead network
        with mock.patch.object(L, "_get", REAL_GET), mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            for text in ("alphadrug", "betadrug", "gammadrug"):
                L.medication(text=text)
            self.assertGreater(L._open_until, 0)
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("should not be called")):
                self.assertIsNone(L.medication(text="deltadrug"))


class NormalizeIntegrationTest(LiveTestCase):
    def fact(self, kind, **fields):
        return {"kind": kind, "confidence": 0.9, "provenance": {"locator": "x", "method": "structured"}, **fields}

    def test_unverified_icd10_code_becomes_verified(self):
        kind, item = normalize(self.fact("condition", code="I50.22", system="icd10", status="active"))
        self.assertEqual(kind, "condition")
        self.assertEqual((item["codes"]["icd10"], item["code_verified"], item["terminology"]), ("I50.22", True, "live_lookup"))

    def test_table_hits_never_call_nlm(self):
        normalize(self.fact("medication", text="Metformin 500 mg tablet"))
        normalize(self.fact("condition", text="T2DM"))
        self.assertEqual(self.fake.calls, [])

    def test_live_observation_keeps_unit_as_sent(self):
        kind, item = normalize(self.fact("observation", code="2947-0", system="loinc", value="134", unit="mmol/L"))
        self.assertEqual((kind, item["value"], item["unit"], item["unit_converted"]), ("observation", 134.0, "mmol/L", False))

    def test_unknown_stays_unmapped(self):
        kind, item = normalize(self.fact("medication", text="metfromin 500 mg"))
        self.assertEqual(kind, "unmapped")


class DemoWalkthroughTest(LiveTestCase):
    """Every claim in the tricky-note walkthrough must hold for the real engine output."""

    def run_demo(self):
        docs = [{"filename": os.path.basename(f), "encoding": "text", "content": open(f, encoding="utf-8").read()}
                for f in sorted(glob.glob(os.path.join(ROOT, "samples", "tricky_cardiology", "*")))]
        result = playground.normalize(docs)
        self.assertEqual(len(result["patients"]), 1, "both documents should merge into one patient")
        return result["patients"][0]["record"]

    @staticmethod
    def find(rec, section, system, code):
        return [i for i in rec.get(section, []) if i["codes"].get(system) == code]

    def check(self, rec, rows):
        for w in rows:
            for exp in w["expect"]:
                state, section, system, code, *field = exp
                hits = self.find(rec, section, system, code)
                with self.subTest(quote=w["quote"], expect=exp):
                    if state == "absent":
                        self.assertEqual(hits, [])
                    else:
                        self.assertTrue(hits)
                        if field:
                            self.assertEqual(hits[0].get(field[0]), field[1])

    def test_walkthrough_claims_hold(self):
        rec = self.run_demo()
        self.check(rec, playground.WALKTHROUGH)
        self.assertEqual(rec.get("unmapped", []), [])

    def test_offline_demo_degrades_to_unmapped_not_wrong(self):
        with mock.patch.dict(os.environ, {"CANON_LIVE_TERMINOLOGY": "0"}):
            rec = self.run_demo()
        self.check(rec, [w for w in playground.WALKTHROUGH if not w.get("live")])
        self.assertTrue(self.find(rec, "medications", "rxnorm", "1656339"))  # a combination in Canon's own table
        self.assertEqual(rec.get("unmapped", []), [])

    def test_combination_dose_kept_whole(self):
        rec = self.run_demo()
        entresto = self.find(rec, "medications", "rxnorm", "1656339")[0]
        self.assertEqual(entresto["dose"], "49-51 mg")

    def test_walkthrough_endpoint_hides_expectations(self):
        self.assertTrue(all("expect" not in w and "expect_model" not in w for w in playground.walkthrough()))
        self.assertEqual(len(playground.samples("tricky")), 2)


if __name__ == "__main__":
    unittest.main()
