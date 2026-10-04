"""Public, stateless playground behind the web demo page.

Documents are normalized in a throwaway in-memory database for the duration of
one request: nothing is stored and nothing is billed. Models are only called for
the unmodified built-in samples (assertion review, answers cached per server
instance), never for text a visitor supplies, so the endpoint can't be used to
run up model costs.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import os
from collections import Counter

from . import parsers
from .service import Canon, CanonError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(ROOT, "samples", "maria_chen")
MAX_DOCS = 12
MAX_TOTAL_BYTES = 3 * 1024 * 1024
SAMPLE_SOURCES = {
    "01_pcp_referral_fax.txt": "PCP referral fax (OCR)",
    "02_quest_labs.hl7": "Quest lab feed",
    "03_hospital_ccd.xml": "Hospital record (TEFCA)",
    "04_portal_app_fhir.json": "Patient portal app",
    "05_payer_claim_837p.edi": "Insurance claim",
    "06_portal_lab_export.csv": "Lab portal export",
    "07_dermatology_letter.pdf": "Dermatology letter",
}


TRICKY_DIR = os.path.join(ROOT, "samples", "tricky_cardiology")
TRICKY_SOURCES = {
    "01_cardiology_clinic_note.txt": "Cardiology clinic note",
    "02_heart_failure_clinic_fhir.json": "Heart failure clinic (FHIR)",
}

# The "tricky note" walkthrough shown next to the record. Each entry is a phrase from
# the sample, how a context-free keyword matcher would read it, and what Canon does.
# `expect` is checked by tests/test_live_terminology.py so the demo can't drift from
# the engine: ("absent"|"present", section, code system, code[, field, value]).
# Rows with `model` are decided by assertion review: `expect` holds without a model,
# `expect_model` with one (tests/test_assertion.py checks it against canned answers).
WALKTHROUGH = [
    {"quote": "Influenza vaccine given today", "naive": "Influenza (J11.1) added as a diagnosis",
     "canon": "Recorded as an immunization (CVX 88), not a diagnosis",
     "expect": [("absent", "conditions", "icd10", "J11.1"), ("present", "immunizations", "cvx", "88")]},
    {"quote": "Low sodium diet advised", "naive": "Hyponatremia (E87.1) added as a diagnosis",
     "canon": "Ignored: it's a diet instruction",
     "expect": [("absent", "conditions", "icd10", "E87.1")]},
    {"quote": "presented with dyspnea and fever, now resolved", "naive": "Dyspnea and fever added to the problem list",
     "canon": "Symptoms in a narrative don't become problems",
     "expect": [("absent", "conditions", "icd10", "R06.00"), ("absent", "conditions", "icd10", "R50.9")]},
    {"quote": "Potassium chloride 20 mEq PO daily", "naive": "A chloride lab result of 20",
     "canon": "A medication: potassium chloride (RxNorm 8591)",
     "expect": [("present", "medications", "rxnorm", "8591"), ("absent", "observations", "loinc", "2075-0")]},
    {"quote": "Calcium 600 mg PO daily", "naive": "A calcium lab result of 600 (mg/dL)",
     "canon": "Not a lab result",
     "expect": [("absent", "observations", "loinc", "17861-6")]},
    {"quote": "Vitamin D 2000 IU PO daily", "naive": "A vitamin D level of 2000",
     "canon": "Not a lab result",
     "expect": [("absent", "observations", "loinc", "62292-8")]},
    {"quote": "Troponin I 15 ng/L", "naive": "Troponin 15, read in the usual ng/mL: wildly abnormal",
     "canon": "Converted to 0.015 ng/mL; normal against the ABIM range (≤0.04)",
     "expect": [("present", "observations", "loinc", "10839-9", "value", 0.015),
                ("present", "observations", "loinc", "10839-9", "interpretation", "normal")]},
    {"quote": "Ferritin 22 ng/mL", "naive": "A number with no context",
     "canon": "Flagged low against the ABIM adult range (24–336 ng/mL)",
     "expect": [("present", "observations", "loinc", "2276-4", "interpretation", "low")]},
    {"quote": "Entresto 49-51 mg tablet", "naive": "Valsartan alone, or an unknown drug",
     "canon": "A combination product: sacubitril / valsartan (RxNorm 1656339), one entry with the dose kept whole",
     "expect": [("present", "medications", "rxnorm", "1656339", "dose", "49-51 mg"),
                ("absent", "medications", "rxnorm", "69749")]},
    {"quote": "I50.22 (FHIR condition code)", "naive": "Kept as an unverified code",
     "canon": "Verified live against ICD-10-CM: chronic systolic (congestive) heart failure, merged with the note's heart failure",
     "live": True,
     "expect": [("present", "conditions", "icd10", "I50.22", "terminology", "live_lookup"),
                ("absent", "conditions", "icd10", "I50.9")]},
]


WALKTHROUGH += [
    {"quote": "Mother had type 2 diabetes", "naive": "Type 2 diabetes (E11.9) added to his problems",
     "canon": "His mother's diagnosis: kept out of his record", "model": True,
     "expect": [("absent", "conditions", "icd10", "E11.9")],
     "expect_model": [("absent", "conditions", "icd10", "E11.9")]},
    {"quote": "Depression screen negative", "naive": "Depression (F32.A) added as a diagnosis",
     "canon": "A negative screen: not a diagnosis", "model": True,
     "expect": [("absent", "conditions", "icd10", "F32.A")],
     "expect_model": [("absent", "conditions", "icd10", "F32.A")]},
    {"quote": "Pneumonia in 2019, resolved", "naive": "Active pneumonia (J18.9) added",
     "canon": "Past and resolved: recorded as history, not an active problem", "model": True,
     "expect": [("absent", "conditions", "icd10", "J18.9")],
     "expect_model": [("present", "conditions", "icd10", "J18.9", "status", "resolved")]},
    {"quote": "Possible pneumonia on last chest x-ray", "naive": "Active pneumonia (J18.9) added",
     "canon": "Suspected, not diagnosed: not recorded as active", "model": True,
     "expect": [("absent", "conditions", "icd10", "J18.9")],
     "expect_model": [("present", "conditions", "icd10", "J18.9", "status", "resolved")]},
    {"quote": "His wife reports he was diagnosed with COPD", "naive": "COPD added, but only by luck: the same matcher "
     "also gave him his mother's diabetes", "canon": "The rules hold it back (a relative is mentioned); the model reads "
     "that it's his diagnosis and restores COPD (J44.9)", "model": True,
     "expect": [("absent", "conditions", "icd10", "J44.9")],
     "expect_model": [("present", "conditions", "icd10", "J44.9", "status", "active")]},
]


def walkthrough() -> list[dict]:
    return [{k: v for k, v in w.items() if k not in ("expect", "expect_model")} for w in WALKTHROUGH]


def samples(sample_set: str = "maria_chen") -> list[dict]:
    directory, sources = (TRICKY_DIR, TRICKY_SOURCES) if sample_set == "tricky" else (SAMPLE_DIR, SAMPLE_SOURCES)
    out = []
    for path in sorted(glob.glob(os.path.join(directory, "*"))):
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            raw = fh.read()
        fmt = parsers.detect(raw, name)
        binary = fmt == "pdf"
        out.append({"filename": name, "format": fmt, "source_name": sources.get(name), "size": len(raw),
                    "encoding": "base64" if binary else "text",
                    "content": base64.b64encode(raw).decode() if binary else raw.decode("utf-8", "replace")})
    return out


def _bundled_digests() -> set[str]:
    """SHA-256 of every built-in sample file: the only documents the playground may send to a model."""
    out = set()
    for path in glob.glob(os.path.join(SAMPLE_DIR, "*")) + glob.glob(os.path.join(TRICKY_DIR, "*")):
        with open(path, "rb") as fh:
            out.add(hashlib.sha256(fh.read()).hexdigest())
    return out


def normalize(documents: list[dict]) -> dict:
    if not isinstance(documents, list) or not documents:
        raise CanonError("invalid_request", "Send {\"documents\": [{\"filename\", \"content\", \"encoding\"}]}.")
    if len(documents) > MAX_DOCS:
        raise CanonError("too_many_documents", f"The playground accepts up to {MAX_DOCS} documents.", 413)
    decoded = []
    total = 0
    for d in documents:
        content = d.get("content") or ""
        data = base64.b64decode(content) if d.get("encoding") == "base64" else content.encode("utf-8")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise CanonError("too_large", "The playground accepts up to 3 MB per request.", 413)
        decoded.append((d.get("filename") or "document", d.get("source_name"), data))

    canon = Canon(":memory:")
    bundled = _bundled_digests()
    results = []
    first_pid = None
    # Documents with demographics first, so files without them (e.g. a lab CSV) can attach to that patient.
    order = sorted(range(len(decoded)), key=lambda i: decoded[i][0].lower().endswith(".csv"))
    for i in order:
        name, source, data = decoded[i]
        entry = {"filename": name}
        # Unmodified built-in samples may use model review (answers cached per instance); anything a visitor
        # types or uploads stays rules-only, so the public playground can't spend the operator's model key.
        use_llm = None if hashlib.sha256(data).hexdigest() in bundled else False
        try:
            try:
                r = canon.ingest(data, filename=name, source_name=source, use_llm=use_llm, actor="playground")
            except CanonError as e:
                if e.code != "patient_unidentified" or not first_pid:
                    raise
                r = canon.ingest(data, filename=name, source_name=source, patient_id=first_pid, use_llm=use_llm,
                                 actor="playground")
                entry["attached_to_patient"] = True
            first_pid = first_pid or r["patient_id"]
            doc = r["document"]
            entry.update(format=doc["format"], patient_id=r["patient_id"], match=r["match"]["method"],
                         counts=doc["extraction"]["counts"], warnings=doc["extraction"].get("warnings", []),
                         document_id=doc["id"])
            review = doc["extraction"].get("assertion_review")
            if review:
                entry["model_review"] = {k: review.get(k) for k in ("model", "decisions", "cached")}
        except CanonError as e:
            entry.update(error={"code": e.code, "message": e.message})
        results.append(entry)

    patients = []
    for p in canon.list_patients():
        pid = p["patient_id"]
        rec = canon.record(pid, actor=None)
        facts = sum(n for d in results if d.get("patient_id") == pid
                    for k, n in d.get("counts", {}).items() if k not in ("patient",))
        items = sum(rec["stats"][k] for k in ("conditions", "medications", "allergies", "observations",
                                               "procedures", "immunizations", "encounters"))
        patients.append({"patient_id": pid, "summary": canon.summary(pid, actor=None), "record": rec,
                         "stats": {"documents": len(rec["sources"]), "facts_extracted": facts,
                                   "canonical_items": items, "conflicts": len(rec["conflicts"]),
                                   "formats": dict(Counter(s["format"] for s in rec["sources"]))}})
    return {"documents": results, "patients": patients}
