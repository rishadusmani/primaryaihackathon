import json
import os
import unittest

from canon import terminology as T
from canon.parsers import text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOCAB = os.path.join(ROOT, "canon", "vocab")

# A note full of phrases that look like facts but are not (vaccines, diets, supplements, salts).
TRAP_NOTE = """PROGRESS NOTE
Patient: Test Patient          DOB: 01/02/1959        MRN: TST-1
Sex: M
Date of Service: 03/04/2026
HPI: 67yo with CHF and AFib presents with dyspnea and fever. Influenza vaccine given today.
Hepatitis B vaccine series complete. Low sodium diet advised.
Problem List:
- Heart failure
- Atrial fibrillation
Medications:
- Eliquis 5 mg BID
- Lasix 40 mg daily
- Potassium chloride 20 mEq daily
- Calcium 600 mg daily
- Vitamin D 2000 IU daily
- Testosterone cypionate 200 mg IM q2wk
- Thyroxine 100 mcg daily
Labs:
Troponin I 15 ng/L
NT-proBNP 2150 pg/mL
Ferritin 22 ng/mL
"""


def load(name):
    with open(os.path.join(VOCAB, name), encoding="utf-8") as fh:
        return json.load(fh)


class VocabCoverageTest(unittest.TestCase):
    def test_at_least_100_of_each(self):
        self.assertGreaterEqual(len(T.CONDITIONS), 100)
        self.assertGreaterEqual(len(T.MEDICATIONS), 100)
        self.assertGreaterEqual(len(T.OBSERVATIONS), 100)

    def test_every_vocab_entry_is_reachable(self):
        for c in load("conditions.json"):
            self.assertEqual(T.lookup_condition(code=c["icd10"], system="icd10")["icd10"], c["icd10"])
            self.assertIsNotNone(T.lookup_condition(code=c["snomed"], system="snomed"), c["snomed"])
        for m in load("medications.json"):
            self.assertEqual(T.lookup_medication(code=m["rxnorm"])["rxnorm"], m["rxnorm"], m["ingredient"])
            self.assertTrue(T.MEDICATIONS[m["ingredient"]][2], f"{m['ingredient']} has no drug class")
        for o in load("observations.json"):
            self.assertIsNotNone(T.lookup_observation(code=o["loinc"]), o["loinc"])

    def test_new_loinc_codes_do_not_shadow_collapsed_aliases(self):
        new = {o["loinc"] for o in load("observations.json") if not o.get("extends")}
        self.assertEqual(new & set(T.LOINC_ALIASES), set())

    def test_every_conversion_unit_is_recognised_in_free_text(self):
        for o in load("observations.json"):
            for unit in o.get("convert", {}):
                self.assertTrue(T.is_unit(unit), (o["loinc"], unit))


class MappingFixesTest(unittest.TestCase):
    def test_unstaged_ckd_is_not_stage_3(self):
        self.assertEqual(T.lookup_condition(text="CKD")["icd10"], "N18.9")
        self.assertEqual(T.lookup_condition(text="CKD stage 3")["icd10"], "N18.30")

    def test_depression_vs_major_depressive_disorder(self):
        self.assertEqual(T.lookup_condition(text="depression")["icd10"], "F32.A")
        mdd = T.lookup_condition(text="MDD")
        self.assertEqual((mdd["icd10"], mdd["snomed"]), ("F32.9", "370143000"))
        self.assertEqual(T.lookup_condition(code="35489007", system="snomed")["icd10"], "F32.A")

    def test_anxiety_vs_gad(self):
        self.assertEqual(T.lookup_condition(text="anxiety")["icd10"], "F41.9")
        self.assertEqual(T.lookup_condition(text="GAD")["icd10"], "F41.1")

    def test_hypercholesterolemia(self):
        self.assertEqual(T.lookup_condition(text="hypercholesterolemia")["icd10"], "E78.00")
        self.assertEqual(T.lookup_condition(text="hyperlipidemia")["icd10"], "E78.5")


class NewCodesTest(unittest.TestCase):
    def test_conditions(self):
        for text_, icd in [("CHF", "I50.9"), ("Parkinson's disease", "G20.A1"), ("COVID-19", "U07.1"),
                           ("PCOS", "E28.2"), ("Crohn's disease", "K50.90"), ("Atrial flutter", "I48.92")]:
            self.assertEqual(T.lookup_condition(text=text_)["icd10"], icd, text_)

    def test_medications(self):
        for text_, rx in [("Lasix 40 mg", "4603"), ("Insulin aspart 10 units", "51428"), ("KCl 20 mEq", "8591"),
                          ("Isosorbide mononitrate ER 30 mg", "28004"), ("Mounjaro 5 mg weekly", "2601723"),
                          ("Vitamin B12 1000 mcg", "11248"), ("Heparin 5000 units SC", "5224")]:
            self.assertEqual(T.lookup_medication(text=text_)["rxnorm"], rx, text_)

    def test_conversions(self):
        cases = [("troponin i", 15, "ng/L", 0.015), ("d dimer", 0.5, "ug/mL FEU", 500), ("vitamin d level", 75, "nmol/L", 30.05),
                 ("serum calcium", 2.4, "mmol/L", 9.62), ("b12 level", 300, "pmol/L", 406.5), ("paco2", 5.3, "kPa", 39.75),
                 ("hematocrit", 0.42, "L/L", 42), ("rbc", 4.5, "x10^12/L", 4.5), ("bun", 5, "mmol/L", 14.01)]
        for name, value, unit, expected in cases:
            loinc = T.lookup_observation(text=name)["loinc"]
            self.assertAlmostEqual(T.convert_unit(loinc, value, unit)[0], expected, places=2, msg=name)

    def test_existing_rounding_unchanged(self):
        self.assertEqual(T.convert_unit("2160-0", 79.6, "umol/L")[0], 0.9)
        self.assertEqual(T.convert_unit("2345-7", 7.0, "mmol/L")[0], 126.11)


class FreeTextSafetyTest(unittest.TestCase):
    def test_exact_only_phrases_are_never_scanned(self):
        conds = {p for p, _ in T.condition_synonyms()}
        meds = {p for p, _ in T.medication_synonyms()}
        obs = {p for p, _ in T.observation_synonyms()}
        for phrase in ("influenza", "covid 19", "hepatitis b", "low sodium", "fever", "cough", "cancer", "pe", "add"):
            self.assertNotIn(phrase, conds)
            self.assertIsNotNone(T.lookup_condition(text=phrase), phrase)  # still maps as a whole field
        for phrase in ("calcium", "magnesium", "chloride", "insulin", "vitamin d", "testosterone", "thyroxine", "iron"):
            self.assertNotIn(phrase, obs)
        self.assertIn("kcl", meds)

    def test_trap_note(self):
        facts = text.parse(TRAP_NOTE)
        conditions = {T.lookup_condition(code=f.get("mapped_code") or f.get("code"), system="icd10")["icd10"]
                      for f in facts if f["kind"] == "condition"}
        observations = {(f["code"], f["value"]) for f in facts if f["kind"] == "observation"}
        medications = {T.lookup_medication(text=f["text"])["ingredient"] for f in facts if f["kind"] == "medication"}

        self.assertTrue({"I50.9", "I48.91"} <= conditions)
        for wrong in ("J11.1", "B18.1", "E87.1", "R50.9", "R06.00"):
            self.assertNotIn(wrong, conditions)

        self.assertTrue({("10839-9", "15"), ("33762-6", "2150"), ("2276-4", "22")} <= observations)
        codes = {c for c, _ in observations}
        for wrong in ("2075-0", "17861-6", "62292-8", "2986-8", "3026-2", "20448-7"):
            self.assertNotIn(wrong, codes)

        self.assertTrue({"apixaban", "furosemide", "potassium chloride"} <= medications)


class ExpansionTest(unittest.TestCase):
    """The October expansion: broader drugs, conditions and labs, all verified by scripts/build_vocab.py."""

    def test_coverage_grew(self):
        self.assertGreaterEqual(len(T.CONDITIONS), 200)
        self.assertGreaterEqual(len(T.MEDICATIONS), 300)
        self.assertGreaterEqual(len(T.OBSERVATIONS), 130)

    def test_conditions(self):
        for text_, icd in [("angina", "I20.9"), ("shingles", "B02.9"), ("multiple sclerosis", "G35.D"),
                           ("CKD stage 4", "N18.4"), ("ESRD", "N18.6"), ("diverticulitis", "K57.92"),
                           ("hepatocellular carcinoma", "C22.0"), ("restless legs syndrome", "G25.81")]:
            self.assertEqual(T.lookup_condition(text=text_)["icd10"], icd, text_)

    def test_medications_and_brands(self):
        for text_, ingredient in [("Altace 5 mg", "ramipril"), ("Pradaxa 150 mg", "dabigatran etexilate"),
                                  ("dabigatran 150 mg", "dabigatran etexilate"), ("Humira 40 mg", "adalimumab"),
                                  ("torsemide 20 mg", "torsemide"), ("Nexium 40 mg", "esomeprazole")]:
            self.assertEqual(T.lookup_medication(text=text_)["ingredient"], ingredient, text_)

    def test_punctuated_lab_names_in_free_text(self):
        note = "Labs:\nCA-125 35 U/mL\nCA 19-9 22 U/mL\nLp(a) 80 mg/dL\nIGF-1 150 ng/mL\nhs-CRP 2.1 mg/L\nCD4 count 420 cells/uL\n"
        got = {f["code"]: (f["value"], f["unit"]) for f in text.parse(note) if f["kind"] == "observation"}
        self.assertEqual(got, {"10334-1": ("35", "U/mL"), "24108-3": ("22", "U/mL"), "10835-7": ("80", "mg/dL"),
                               "2484-4": ("150", "ng/mL"), "30522-7": ("2.1", "mg/L"), "24467-3": ("420", "cells/uL")})

    def test_new_terms_respect_negation_and_relatives(self):
        review = []
        facts = text.parse("Assessment: Pregnancy test negative. Mother has multiple sclerosis. Denies tinnitus.",
                           review=review)
        self.assertEqual([f for f in facts if f["kind"] == "condition"], [])
        self.assertEqual(sorted(r["reason"] for r in review), ["negated", "negated", "relative"])

    def test_short_abbreviations_stay_exact_only(self):
        facts = text.parse("HPI: Seen in the ED last week. MS contin restarted.")
        self.assertEqual([f for f in facts if f["kind"] == "condition"], [])


class ExpansionRound2Test(unittest.TestCase):
    def test_coverage(self):
        self.assertGreaterEqual(len(T.CONDITIONS), 290)
        self.assertGreaterEqual(len(T.MEDICATIONS), 400)
        self.assertGreaterEqual(len(T.OBSERVATIONS), 150)

    def test_conditions(self):
        for text_, icd in [("scabies", "B86"), ("bulimia nervosa", "F50.20"), ("binge eating disorder", "F50.819"),
                           ("HFrEF", "I50.20"), ("Cushing's syndrome", "E24.9"), ("gestational diabetes", "O24.419"),
                           ("primary biliary cholangitis", "K74.3"), ("supraventricular tachycardia", "I47.10")]:
            self.assertEqual(T.lookup_condition(text=text_)["icd10"], icd, text_)

    def test_medications(self):
        for text_, ingredient in [("Keytruda 200 mg IV", "pembrolizumab"), ("Narcan 4 mg nasal", "naloxone"),
                                  ("Ocrevus 600 mg", "ocrelizumab"), ("Lokelma 10 g", "sodium zirconium cyclosilicate")]:
            self.assertEqual(T.lookup_medication(text=text_)["ingredient"], ingredient, text_)

    def test_labs(self):
        got = {f["code"] for f in text.parse("Labs:\nhs-TnT 12 ng/L\nCK-MB 3.1 ng/mL\nACTH 25 pg/mL\nUrine sodium 40 mmol/L\n")
               if f["kind"] == "observation"}
        self.assertEqual(got, {"67151-1", "13969-1", "2141-0", "2955-3"})

    def test_vaccine_mentions_are_not_diagnoses(self):
        for note in ["HPI: Measles vaccine given today.", "HPI: Varicella vaccine given.",
                     "HPI: Hepatitis A vaccine series complete.", "HPI: Tdap booster for pertussis given."]:
            with self.subTest(note=note):
                self.assertEqual([f for f in text.parse(note) if f["kind"] == "condition"], [])
        conds = [f["text"] for f in text.parse("Assessment: Measles, isolate patient. Shingles vaccine recommended.")
                 if f["kind"] == "condition"]
        self.assertEqual(conds, ["measles"])

    def test_childhood_history_is_resolved(self):
        f = [x for x in text.parse("HPI: Had chickenpox as a child.") if x["kind"] == "condition"]
        self.assertEqual([(x["text"], x["status"]) for x in f], [("chickenpox", "resolved")])


class VaccineTest(unittest.TestCase):
    def imms(self, note):
        return [(f["text"], f["code"]) for f in text.parse(note) if f["kind"] == "immunization"]

    def test_vaccines_are_verified_and_loaded(self):
        self.assertGreaterEqual(len(T.VACCINES), 29)
        for v in load("vaccines.json"):
            self.assertEqual(T.lookup_vaccine(cvx=v["cvx"])["cvx"], v["cvx"], v["vaccine"])

    def test_every_vaccine_in_a_line_is_recorded(self):
        self.assertEqual(self.imms("HPI: MMR and varicella vaccines given today."), [("mmr", "03"), ("varicella", "21")])
        self.assertEqual(self.imms("Immunizations: Pediarix, IPV, Hib, rotavirus given at 4 month visit."),
                         [("dtap-hepb-ipv", "110"), ("ipv", "10"), ("hib", "17"), ("rotavirus", "122")])

    def test_specific_products_win_over_generic_names(self):
        self.assertEqual(self.imms("Immunizations:\n- MMRV (ProQuad) 06/2020"), [("mmrv", "94")])
        self.assertEqual(self.imms("HPI: Beyfortus given for RSV prevention."), [("rsv monoclonal antibody", "315")])

    def test_declined_or_recommended_is_not_given(self):
        for note in ["HPI: Patient declined flu vaccine.", "HPI: Flu vaccine declined.",
                     "HPI: Shingles vaccine recommended after recovery."]:
            with self.subTest(note=note):
                self.assertEqual(self.imms(note), [])
        self.assertEqual(self.imms("HPI: Flu vaccine declined. Shingrix given."), [("zoster", "187")])

    def test_products_given_without_the_word_vaccine(self):
        self.assertEqual(self.imms("HPI: Tdap and Menveo given today."), [("tdap", "115"), ("meningococcal acwy", "108")])
        self.assertEqual(self.imms("HPI: Patient received MMR at 12 months."), [("mmr", "03")])

    def test_disease_names_need_a_vaccine_context(self):
        self.assertEqual(self.imms("HPI: Given his RSV infection last month, start albuterol."), [])
        self.assertEqual(self.imms("HPI: Polio as a child, now with post-polio syndrome."), [])
        self.assertEqual(self.imms("HPI: Rabies exposure from dog bite, rabies vaccine given."), [("rabies", "90")])


class ExpansionRound3Test(unittest.TestCase):
    def conds(self, note):
        return [(f["text"], f["mapped_code"]) for f in text.parse(note) if f["kind"] == "condition"]

    def test_coverage(self):
        self.assertGreaterEqual(len(T.CONDITIONS), 410)
        self.assertGreaterEqual(len(T.MEDICATIONS), 600)
        self.assertGreaterEqual(len(T.OBSERVATIONS), 175)
        self.assertGreaterEqual(len(T.VACCINES), 38)

    def test_conditions(self):
        for text_, icd in [("hidradenitis suppurativa", "L73.2"), ("Barrett's esophagus", "K22.70"),
                           ("myasthenia gravis", "G70.00"), ("MGUS", "D47.2"), ("Graves' disease", "E05.00"),
                           ("plantar fasciitis", "M72.20"), ("overactive bladder", "N32.81"),
                           ("metabolic syndrome", "E88.810"), ("Meniere's disease", "H81.09")]:
            self.assertEqual(T.lookup_condition(text=text_)["icd10"], icd, text_)

    def test_specific_phrases_win_over_their_parts(self):
        for note, expected in [("Assessment: Aspiration pneumonia.", ("aspiration pneumonia", "J69.0")),
                               ("Assessment: Postpartum depression.", ("postpartum depression", "F53.0")),
                               ("Assessment: Psoriatic arthritis.", ("psoriatic arthritis", "L40.50")),
                               ("Assessment: Cluster headache.", ("cluster headache", "G44.009"))]:
            with self.subTest(note=note):
                self.assertEqual(self.conds(note), [expected])

    def test_new_terms_respect_negation_and_relatives(self):
        self.assertEqual(self.conds("ROS: No palpitations, no syncope, no hematuria."), [])
        self.assertEqual(self.conds("Family history: Father had myasthenia gravis."), [])
        self.assertEqual(self.conds("Assessment: Rule out appendicitis."), [])

    def test_medications(self):
        for text_, ingredient in [("Dupixent 300 mg SC q2wk", "dupilumab"), ("Rinvoq 15 mg daily", "upadacitinib"),
                                  ("Ingrezza 80 mg daily", "valbenazine"), ("Jakafi 10 mg BID", "ruxolitinib"),
                                  ("Uloric 40 mg daily", "febuxostat"), ("Gemtesa 75 mg daily", "vibegron")]:
            self.assertEqual(T.lookup_medication(text=text_)["ingredient"], ingredient, text_)

    def test_labs(self):
        got = {(f["code"], f["unit"]) for f in text.parse(
            "Labs:\nIgA level 210 mg/dL\nRheumatoid factor 12 IU/mL\nHIV viral load 40 copies/mL\n"
            "Urine osmolality 450 mOsm/kg\nBase excess -2 mmol/L\n") if f["kind"] == "observation"}
        self.assertEqual(got, {("2458-8", "mg/dL"), ("11572-5", "IU/mL"), ("20447-9", "copies/mL"),
                               ("2695-5", "mOsm/kg"), ("1925-7", "mmol/L")})

    def test_travel_vaccines(self):
        imms = [(f["text"], f["code"]) for f in text.parse(
            "Immunizations: Vaxchora and Ixchiq given before travel. Pentacel at 2 months.") if f["kind"] == "immunization"]
        self.assertEqual(sorted(imms), [("chikungunya live", "317"), ("cholera", "26"), ("dtap-ipv-hib", "120")])
        self.assertEqual([f for f in text.parse("HPI: Returned from Brazil, dengue last year.")
                          if f["kind"] == "immunization"], [])


if __name__ == "__main__":
    unittest.main()
