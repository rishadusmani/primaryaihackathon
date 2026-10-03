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


def samples() -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(SAMPLE_DIR, "*"))):
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            raw = fh.read()
        fmt = parsers.detect(raw, name)
        binary = fmt == "pdf"
        out.append({"filename": name, "format": fmt, "source_name": SAMPLE_SOURCES.get(name), "size": len(raw),
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
