"""A separate, in-memory chart search demo containing only bundled synthetic records."""

from __future__ import annotations

import base64
import hashlib

from . import playground, live_terminology
from .service import Canon


class ChartDemo:
    def __init__(self):
        self.canon = Canon()
        self.patients = []
        for sample_set, questions in (
            ("maria_chen", ["Why was metformin increased?", "Penicillin allergy", "HbA1c"]),
            ("tricky", ["What did cardiology say about pneumonia?", "Apixaban", "Troponin"]),
        ):
            patient_id = "demo_" + sample_set
            # The demo stays deterministic and never sends chart text to a model or terminology service.
            with live_terminology.budget(0):
                for document in playground.samples(sample_set):
                    raw = (base64.b64decode(document["content"]) if document["encoding"] == "base64"
                           else document["content"].encode())
                    self.canon.ingest(raw, filename=document["filename"], source_name=document["source_name"],
                                      patient_id=patient_id, use_llm=False, actor="synthetic_demo",
                                      document_id="demo_doc_" + hashlib.sha256(patient_id.encode() + raw).hexdigest()[:24])
            record = self.canon.record(patient_id, actor=None)
            self.patients.append({"patient_id": patient_id, "patient": record["patient"],
                                  "documents": record["sources"], "conflicts": record["conflicts"],
                                  "questions": questions})
