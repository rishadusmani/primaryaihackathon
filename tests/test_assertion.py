import json
import os
import unittest
from unittest import mock

from canon.parsers import assertion, parse, text


def conditions(note: str, review: list | None = None) -> dict[str, str]:
    """{condition text: status} the rules assert for `note` under an Assessment heading."""
    facts = text.parse("Assessment: " + note, review=review)
    return {f["text"]: f["status"] for f in facts if f["kind"] == "condition" and "text" in f}


class RuleCueTest(unittest.TestCase):
    """What the rules do on their own (no model): assert plain mentions, hold back anything uncertain."""

    CASES = [
        # sentence, expected asserted conditions, expected review reasons
        ("Hypertension, not well controlled.", {"hypertension": "active"}, []),
        ("No improvement in hypertension, increase lisinopril to 20 mg daily.", {"hypertension": "active"}, []),
        ("Without change in asthma symptoms.", {"asthma": "active"}, []),
        ("Hypertension not at goal.", {"hypertension": "active"}, []),
        ("Denies chest pain but has hypertension.", {"hypertension": "active"}, []),
        ("Denies asthma.", {}, ["negated"]),
        ("Negative for pneumonia.", {}, ["negated"]),
        ("No history of hypertension.", {}, ["negated"]),
        ("Mother had type 2 diabetes.", {}, ["relative"]),
        ("Father with atrial fibrillation.", {}, ["relative"]),
        ("Family history of hypertension.", {}, ["relative"]),
        ("Possible pneumonia, will get chest x-ray.", {}, ["hedged"]),
        ("Suspected asthma.", {}, ["hedged"]),
        ("Screen for depression.", {}, ["hedged"]),
        ("At risk for heart failure.", {}, ["hedged"]),
        ("Mother at bedside; patient has asthma.", {"asthma": "active"}, []),
        ("Type 2 diabetes, A1c rising.", {"type 2 diabetes": "active"}, []),
        # cues after the condition
        ("Depression screen negative.", {}, ["negated"]),
        ("History of pneumonia in 2019, resolved.", {}, ["negated"]),
        ("Diabetes runs in the family.", {}, ["relative"]),
        ("Asthma vs COPD, PFTs pending.", {}, ["hedged", "hedged"]),
        ("Anxiety likely situational.", {}, ["hedged"]),
        ("Asthma exacerbation, no wheezing today.", {"asthma": "active"}, ["negated"]),  # wheezing, not asthma
        ("HTN, DM2, HLD - stable.", {"htn": "active", "dm2": "active", "hld": "active"}, []),
    ]

    def test_cases(self):
        for sentence, asserted, reasons in self.CASES:
            with self.subTest(sentence=sentence):
                review: list = []
                self.assertEqual(conditions(sentence, review), asserted)
                self.assertEqual([c["reason"] for c in review], reasons)

    def test_candidate_carries_what_is_needed_to_restore(self):
        review: list = []
        conditions("Mother had type 2 diabetes.", review)
        c = review[0]
        self.assertEqual(c["term"], "type 2 diabetes")
        self.assertIn("Mother had type 2 diabetes", c["sentence"])
        self.assertTrue(c["fact"]["mapped_code"].startswith("E11"))


def fake_response(items: list[dict], input_tokens: int = 120, output_tokens: int = 40) -> dict:
    return {"output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"items": items})}]}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


def candidates(note: str) -> list[dict]:
    review: list = []
    conditions(note, review)
    return review


class AssertionModelTest(unittest.TestCase):
    """The model path, with the HTTP call replaced by canned responses."""

    def test_request_is_strict_json_schema_on_luna(self):
        body = assertion.request_body(candidates("Mother had asthma."))
        self.assertEqual(body["model"], assertion.MODEL)
        fmt = body["text"]["format"]
        self.assertEqual((fmt["type"], fmt["strict"]), ("json_schema", True))
        sent = json.loads(body["input"])["items"]
        self.assertEqual(sent, [{"id": 0, "condition": "asthma", "sentence": "Mother had asthma."}])
        if "CANON_ASSERTION_MODEL" not in os.environ:
            self.assertEqual(assertion.MODEL, "gpt-5.6-luna")

    def test_present_restores_and_others_stay_dropped(self):
        cands = candidates("Patient's mother says she has asthma.") + candidates("Suspected pneumonia.") + \
            candidates("Mother had type 2 diabetes.")
        self.assertEqual(len(cands), 3)
        usage: dict = {}
        added, report = assertion.resolve(cands, usage, post=lambda body: fake_response([
            {"id": 0, "assertion": "present", "evidence": "she has asthma"},
            {"id": 1, "assertion": "hypothetical", "evidence": "Suspected pneumonia"},
            {"id": 2, "assertion": "other_person", "evidence": "Mother had type 2 diabetes"},
        ]))
        self.assertEqual([(f["text"], f["status"], f["provenance"]["method"]) for f in added],
                         [("asthma", "active", "llm")])
        self.assertEqual(added[0]["assertion"]["label"], "present")
        self.assertEqual(report["labels"], {"present": 1, "hypothetical": 1, "other_person": 1})
        self.assertEqual(usage, {"input_tokens": 120, "output_tokens": 40})

    def test_historical_restores_as_resolved(self):
        added, _ = assertion.resolve(candidates("No longer has asthma, resolved in 2019."), post=lambda b: fake_response(
            [{"id": 0, "assertion": "historical", "evidence": "resolved in 2019"}]))
        self.assertEqual([(f["text"], f["status"]) for f in added], [("asthma", "resolved")])

    def test_unverifiable_or_malformed_labels_are_ignored(self):
        cands = candidates("Denies asthma.") + candidates("Possible pneumonia.")
        added, report = assertion.resolve(cands, post=lambda b: fake_response([
            {"id": 0, "assertion": "present", "evidence": "patient has severe asthma"},  # not in the sentence
            {"id": 7, "assertion": "present", "evidence": "pneumonia"},  # no such candidate
            {"id": 1, "assertion": "certain", "evidence": "pneumonia"},  # not a label
        ]))
        self.assertEqual(added, [])
        self.assertEqual(report["rejected"], 3)

    def test_only_capped_number_of_candidates_sent(self):
        many = candidates("Denies asthma.") * (assertion.MAX_CANDIDATES + 5)
        seen = {}

        def post(body):
            seen["n"] = len(json.loads(body["input"])["items"])
            return fake_response([])
        _, report = assertion.resolve(many, post=post)
        self.assertEqual(seen["n"], assertion.MAX_CANDIDATES)
        self.assertEqual((report["candidates"], report["sent"]), (assertion.MAX_CANDIDATES + 5,
                                                                   assertion.MAX_CANDIDATES))

    def test_no_candidates_no_call(self):
        added, report = assertion.resolve([], post=lambda b: self.fail("must not call the model"))
        self.assertEqual((added, report["sent"]), ([], 0))


class PipelineWiringTest(unittest.TestCase):
    NOTE = b"Assessment: Patient's mother says she has asthma. Hypertension, not well controlled.\n"

    def setUp(self):
        assertion._cache.clear()

    def test_disabled_without_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            facts, info = parse("text", self.NOTE)
        self.assertNotIn("assertion_review", info)
        self.assertEqual(sorted(f["text"] for f in facts if f["kind"] == "condition"), ["hypertension"])

    def test_enabled_restores_and_meters_tokens(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(assertion, "_post", lambda body: fake_response(
                    [{"id": 0, "assertion": "present", "evidence": "she has asthma"}])):
            facts, info = parse("text", self.NOTE)
        self.assertEqual(sorted(f["text"] for f in facts if f["kind"] == "condition"), ["asthma", "hypertension"])
        self.assertIn(f"assertion:{assertion.MODEL}", info["extractors"])
        self.assertEqual(info["llm_usage"], {"input_tokens": 120, "output_tokens": 40})

    def test_model_failure_keeps_rules_result(self):
        def boom(body):
            raise RuntimeError("assertion model returned HTTP 503: unavailable")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), mock.patch.object(assertion, "_post", boom):
            facts, info = parse("text", self.NOTE)
        self.assertEqual(sorted(f["text"] for f in facts if f["kind"] == "condition"), ["hypertension"])
        self.assertTrue(any("Assertion review skipped" in w for w in info["warnings"]))

    def test_use_llm_false_never_calls_the_model(self):
        """The public playground ingests with use_llm=False; it must not spend the operator's OpenAI key."""
        calls = []
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(assertion, "_post", lambda body: calls.append(body) or fake_response([])):
            facts, info = parse("text", self.NOTE, use_llm=False)
        self.assertEqual(calls, [])
        self.assertNotIn("assertion_review", info)
        self.assertEqual(sorted(f["text"] for f in facts if f["kind"] == "condition"), ["hypertension"])

    def test_playground_never_calls_the_model(self):
        from canon import playground

        calls = []
        doc = {"filename": "note.txt", "content": self.NOTE.decode()}
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                mock.patch.object(assertion, "_post", lambda body: calls.append(body) or fake_response([])):
            playground.normalize([doc])
        self.assertEqual(calls, [])

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "CANON_ASSERTION": "0"}):
            self.assertFalse(assertion.enabled())


def fake_luna(body):
    """Label each sentence the way a correct model would, quoting words from it."""
    rules = [("Mother had", "other_person", "Mother had"), ("screen negative", "absent", "screen negative"),
             ("resolved", "historical", "resolved"), ("Possible", "hypothetical", "Possible pneumonia"),
             ("wife reports", "present", "he was diagnosed with COPD")]
    items = []
    for it in json.loads(body["input"])["items"]:
        label, ev = next((lab, e) for cue, lab, e in rules if cue in it["sentence"])
        items.append({"id": it["id"], "assertion": label, "evidence": ev})
    return fake_response(items)


class DemoModelReviewTest(unittest.TestCase):
    """The public demo's tricky note with model review on: the walkthrough's `expect_model` claims hold."""

    def setUp(self):
        assertion._cache.clear()

    def run_demo(self, edit=None):
        import glob
        from canon import playground
        docs = []
        for f in sorted(glob.glob(os.path.join(playground.TRICKY_DIR, "*"))):
            with open(f, encoding="utf-8") as fh:
                content = fh.read()
            docs.append({"filename": os.path.basename(f), "encoding": "text", "content": edit(content) if edit else content})
        calls = []
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "CANON_LIVE_TERMINOLOGY": "0"}), \
                mock.patch.object(assertion, "_post", lambda body: calls.append(body) or fake_luna(body)):
            return playground.normalize(docs), calls

    def test_walkthrough_claims_hold_with_model(self):
        from canon import playground
        result, calls = self.run_demo()
        self.assertEqual(len(calls), 1, "one model call for the note")
        rec = result["patients"][0]["record"]
        for w in playground.WALKTHROUGH:
            for state, section, system, code, *field in w.get("expect_model", []):
                hits = [i for i in rec.get(section, []) if i["codes"].get(system) == code]
                with self.subTest(quote=w["quote"]):
                    if state == "absent":
                        self.assertEqual(hits, [])
                    else:
                        self.assertTrue(hits)
                        if field:
                            self.assertEqual(hits[0].get(field[0]), field[1])
        note = next(d for d in result["documents"] if d["filename"].endswith(".txt"))
        decisions = note["model_review"]["decisions"]
        self.assertEqual(len(decisions), 5)
        for w in playground.WALKTHROUGH:
            if w.get("model"):
                with self.subTest(quote=w["quote"]):
                    self.assertTrue(any(w["quote"].lower() in d["sentence"].lower() for d in decisions),
                                    "the page matches each model row to a decision by its quote")

    def test_repeat_demo_is_served_from_cache(self):
        _, first = self.run_demo()
        result, second = self.run_demo()
        self.assertEqual((len(first), len(second)), (1, 0))
        note = next(d for d in result["documents"] if d["filename"].endswith(".txt"))
        self.assertTrue(note["model_review"]["cached"])

    def test_edited_sample_never_reaches_the_model(self):
        result, calls = self.run_demo(edit=lambda c: c + "\nFather had asthma.\n")
        self.assertEqual(calls, [])
        note = next(d for d in result["documents"] if d["filename"].endswith(".txt"))
        self.assertNotIn("model_review", note)


if __name__ == "__main__":
    unittest.main()
