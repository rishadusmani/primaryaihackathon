"""Public, stateless playground behind the web demo page.

Documents are normalized in a throwaway in-memory database for the duration of
one request: nothing is stored, nothing is billed, and LLM extraction is never
called (so the endpoint can't be used to run up model costs).
"""

from __future__ import annotations

import base64
import glob
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
# `expect` is checked by tests/test_demo_walkthrough.py so the demo can't drift from
# the engine: ("absent"|"present", section, code system, code[, field, value]).
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
    {"quote": "Entresto 49-51 mg tablet", "naive": "Unknown drug: not in a built-in table",
     "canon": "Looked up live in NLM RxNav: sacubitril / valsartan (RxNorm 1656339)", "live": True,
     "expect": [("present", "medications", "rxnorm", "1656339", "terminology", "nlm_live")]},
    {"quote": "I50.22 (FHIR condition code)", "naive": "Kept as an unverified code",
     "canon": "Verified live against ICD-10-CM: chronic systolic (congestive) heart failure, merged with the note's heart failure",
     "live": True,
     "expect": [("present", "conditions", "icd10", "I50.22", "terminology", "nlm_live"),
                ("absent", "conditions", "icd10", "I50.9")]},
]


def walkthrough() -> list[dict]:
    return [{k: v for k, v in w.items() if k != "expect"} for w in WALKTHROUGH]


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
    results = []
    first_pid = None
    # Documents with demographics first, so files without them (e.g. a lab CSV) can attach to that patient.
    order = sorted(range(len(decoded)), key=lambda i: decoded[i][0].lower().endswith(".csv"))
    for i in order:
        name, source, data = decoded[i]
        entry = {"filename": name}
        try:
            try:
                r = canon.ingest(data, filename=name, source_name=source, use_llm=False, actor="playground")
            except CanonError as e:
                if e.code != "patient_unidentified" or not first_pid:
                    raise
                r = canon.ingest(data, filename=name, source_name=source, patient_id=first_pid, use_llm=False,
                                 actor="playground")
                entry["attached_to_patient"] = True
            first_pid = first_pid or r["patient_id"]
            doc = r["document"]
            entry.update(format=doc["format"], patient_id=r["patient_id"], match=r["match"]["method"],
                         counts=doc["extraction"]["counts"], warnings=doc["extraction"].get("warnings", []),
                         document_id=doc["id"])
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
