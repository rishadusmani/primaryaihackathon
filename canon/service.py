"""Canon service: ingest anything, get one canonical, cited patient record."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone

from . import parsers
from .fhir_export import to_fhir_bundle
from .normalize import normalize
from . import live_terminology
from .reconcile import build_record
from .store import Store, new_id



def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TEXT_CHARS_PER_PAGE = 3000  # a typical printed page; used to count pages in text/OCR uploads


def _pages(fmt: str, info: dict) -> int:
    """Billable pages: real pages for PDFs, ~3,000-character pages for text, 1 for structured formats
    (HL7, FHIR, C-CDA, X12, CSV), which have no pages."""
    if fmt == "pdf":
        return max(1, int((info.get("pdf") or {}).get("pages") or 1))
    if fmt == "text":
        return max(1, -(-int(info.get("text_chars") or 0) // TEXT_CHARS_PER_PAGE))
    return 1


class CanonError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _match_keys(demo: dict) -> list[str]:
    keys = []
    for ident in demo.get("identifiers") or []:
        if ident.get("value"):
            keys.append(f"id:{(ident.get('system') or '').lower()}|{ident['value'].strip().lower()}")
    fam = re.sub(r"[^a-z]", "", (demo.get("name_family") or "").lower())
    if fam and demo.get("dob"):
        keys.append(f"fam_dob:{fam}|{demo['dob']}")
    return keys


SANDBOX_ACCOUNT = "acct_sandbox"


class Canon:
    """All reads and writes are scoped to one account (tenant)."""

    def __init__(self, db: str | Store | None = ":memory:", account_id: str = SANDBOX_ACCOUNT):
        self.store = db if isinstance(db, Store) else Store(db)
        self.account_id = account_id
        self.on_document_ingested = None  # hook: fn(account_id, document_id, info) for usage metering

    # ------------------------------------------------------------------ ingest
    def ingest(self, data: bytes | str, *, filename: str | None = None, content_type: str | None = None,
               fmt: str | None = None, patient_id: str | None = None, source_name: str | None = None,
               use_llm: bool | None = None, actor: str = "api") -> dict:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if not raw.strip():
            raise CanonError("empty_document", "Document is empty.")
        fmt = fmt or parsers.detect(raw, filename, content_type)
        if fmt not in parsers.FORMATS:
            raise CanonError("unsupported_format", f"Unsupported format '{fmt}'. Use one of {parsers.FORMATS}.")
        try:
            facts, info = parsers.parse(fmt, raw, use_llm=use_llm)
        except Exception as e:  # parser errors are user-facing input problems
            raise CanonError("parse_error", f"Could not parse as {fmt}: {e}", 422) from e

        items: list[tuple[str, dict]] = []
        with live_terminology.budget():  # cap time spent on live NLM lookups for this document
            for f in facts:
                items.append(normalize(f))
        produced = next((it for k, it in items if k == "document" and it.get("generated")), None)
        items = [(k, it) for k, it in items if k != "document"]
        if not any(k not in ("patient", "encounter") for k, _ in items):
            info.setdefault("warnings", []).append(
                f"No clinical facts found in this document (parsed as {fmt}). Check the file or format.")

        demo = {}
        for kind, it in items:
            if kind == "patient":
                for k, v in it.items():
                    if k == "identifiers":
                        demo.setdefault("identifiers", []).extend(v)
                    elif k not in ("confidence", "provenance"):
                        demo.setdefault(k, v)
        match = self._match_patient(patient_id, demo)
        pid = match["patient_id"]

        digest = hashlib.sha256(raw).hexdigest()
        existing = self.store.one("SELECT id FROM documents WHERE account_id=? AND patient_id=? AND sha256=?",
                                  (self.account_id, pid, digest))
        if existing:
            doc = self.get_document(existing["id"])
            doc["duplicate"] = True
            return {"document": doc, "patient_id": pid, "match": match}

        dates = [it.get("date") or it.get("effective") for k, it in items if k in ("encounter", "observation",
                                                                                     "condition", "medication")]
        dates = [d for d in dates if d]
        # Clinical date first; when nothing in the document is dated, when it was produced (signed, exported...).
        doc_date = Counter(dates).most_common(1)[0][0] if dates else (produced or {}).get("generated")
        if produced:
            info["generated"] = {"date": produced["generated"], "locator": produced["provenance"]["locator"]}
        if doc_date:
            info["date_basis"] = "clinical" if dates else "generated"
        info["pages"] = _pages(fmt, info)
        doc_id = new_id("doc")
        received = _now()
        self.store.execute("INSERT INTO patients (account_id, id, created_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                           (self.account_id, pid, received))
        self.store.execute(
            "INSERT INTO documents (id, account_id, patient_id, format, filename, source_name, received_at, "
            "document_date, sha256, size, raw, info, items) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, self.account_id, pid, fmt, filename, source_name, received, doc_date, digest, len(raw), raw, json.dumps(info),
             json.dumps(items)),
        )
        for k in _match_keys(demo):
            self.store.execute("INSERT INTO patient_keys (account_id, key, patient_id) VALUES (?,?,?) "
                               "ON CONFLICT DO NOTHING", (self.account_id, k, pid))
        self.store.audit(ts=received, event="document.ingested", actor=actor, account_id=self.account_id,
                         patient_id=pid, document_id=doc_id,
                         detail={"format": fmt, "sha256": digest, "facts": len(facts), "match": match["method"]})
        if self.on_document_ingested:
            self.on_document_ingested(self.account_id, doc_id, info)
        return {"document": self.get_document(doc_id), "patient_id": pid, "match": match}

    def _match_patient(self, patient_id: str | None, demo: dict) -> dict:
        keys = _match_keys(demo)
        if patient_id:
            exists = self.store.one("SELECT id FROM patients WHERE id=? AND account_id=?", (patient_id, self.account_id))
            out = {"patient_id": patient_id, "method": "explicit", "confidence": 1.0, "created": not exists}
            if exists and keys:
                known = {r["key"] for r in self.store.all(
                    "SELECT key FROM patient_keys WHERE account_id=? AND patient_id=?", (self.account_id, patient_id))}
                fam_dob = [k for k in keys if k.startswith("fam_dob:")]
                known_fd = [k for k in known if k.startswith("fam_dob:")]
                if fam_dob and known_fd and not set(fam_dob) & set(known_fd):
                    out["warning"] = ("Document demographics do not match this patient "
                                      f"({fam_dob[0][8:]} vs {known_fd[0][8:]}). Check for a misfiled document.")
            return out
        for k in keys:
            rows = self.store.all("SELECT DISTINCT patient_id FROM patient_keys WHERE account_id=? AND key=?",
                                  (self.account_id, k))
            if len(rows) == 1:
                return {"patient_id": rows[0]["patient_id"], "created": False,
                        "method": "identifier" if k.startswith("id:") else "name_dob",
                        "confidence": 0.99 if k.startswith("id:") else 0.95}
        if not keys:
            raise CanonError("patient_unidentified",
                             "No patient demographics found in the document; pass patient_id explicitly.")
        return {"patient_id": new_id("pat"), "method": "new_patient", "confidence": 1.0, "created": True}

    # ------------------------------------------------------------------ reads
    def get_document(self, doc_id: str, include_items: bool = False) -> dict:
        r = self.store.one("SELECT * FROM documents WHERE id=? AND account_id=?", (doc_id, self.account_id))
        if not r:
            raise CanonError("not_found", f"No document {doc_id}", 404)
        items = json.loads(r["items"])
        counts = Counter(k for k, _ in items)
        out = {"id": r["id"], "object": "document", "patient_id": r["patient_id"], "format": r["format"],
               "filename": r["filename"], "source_name": r["source_name"], "received_at": r["received_at"],
               "document_date": r["document_date"], "sha256": r["sha256"], "size": r["size"],
               "extraction": {**json.loads(r["info"]), "counts": dict(counts)}}
        if include_items:
            out["items"] = [{"kind": k, **it} for k, it in items]
        return out

    def list_patients(self) -> list[dict]:
        out = []
        for r in self.store.all("SELECT id FROM patients WHERE account_id=? ORDER BY created_at", (self.account_id,)):
            rec = self.record(r["id"], actor=None)
            out.append({"patient_id": r["id"], "name": (rec["patient"].get("names") or [None])[0],
                        "birth_date": rec["patient"].get("birth_date"), "documents": len(rec["sources"])})
        return out

    def record(self, patient_id: str, actor: str | None = "api") -> dict:
        rows = self.store.all("SELECT * FROM documents WHERE account_id=? AND patient_id=? ORDER BY received_at",
                              (self.account_id, patient_id))
        if not rows:
            raise CanonError("not_found", f"No patient {patient_id}", 404)
        items: list[tuple[str, dict]] = []
        unmapped: list[dict] = []
        sources = []
        for r in rows:
            src = {"id": r["id"], "format": r["format"], "source_name": r["source_name"], "filename": r["filename"],
                   "received_at": r["received_at"], "document_date": r["document_date"],
                   "date_basis": json.loads(r["info"] or "{}").get("date_basis")}
            sources.append(dict(src))
            for kind, it in json.loads(r["items"]):
                it["_source"] = src
                (unmapped.append(it) if kind == "unmapped" else items.append((kind, it)))
        rec = build_record(patient_id, items, sources, unmapped)
        if actor:
            self.store.audit(ts=_now(), event="record.read", actor=actor, account_id=self.account_id,
                             patient_id=patient_id,
                             detail={"documents": len(rows)})
        return rec

    def summary(self, patient_id: str, actor: str | None = "api") -> dict:
        """Token-efficient view for agents: what's true now, what changed, what's contradictory."""
        rec = self.record(patient_id, actor=actor)
        latest: dict[str, dict] = {}
        series: dict[str, list[dict]] = {}
        for o in rec["observations"]:
            if o.get("value") is None or not o.get("effective"):
                continue
            series.setdefault(o["display"], []).append(o)
        labs, vitals = [], []
        for name, obs in series.items():
            obs = sorted(obs, key=lambda o: o["effective"])
            cur = obs[-1]
            entry = {"name": name, "loinc": cur["codes"]["loinc"], "value": cur["value"], "unit": cur["unit"],
                     "date": cur["effective"], "interpretation": cur.get("interpretation")}
            if len(obs) > 1:
                prev = obs[-2]
                delta = round(cur["value"] - prev["value"], 2)
                entry["previous"] = {"value": prev["value"], "date": prev["effective"]}
                entry["trend"] = "up" if delta > 0 else "down" if delta < 0 else "flat"
            (labs if cur["category"] == "lab" else vitals).append(entry)
            latest[name] = entry
        p = rec["patient"]
        return {
            "object": "patient_summary", "patient_id": patient_id,
            "patient": {"name": (p.get("names") or [None])[0], "birth_date": p.get("birth_date"), "sex": p.get("sex")},
            "active_problems": [{"display": c["display"], "icd10": c["codes"].get("icd10"), "since": c.get("onset")
                                 or c.get("first_seen")} for c in rec["conditions"] if c["status"] == "active"],
            "other_problems": [{"display": c["display"], "status": c["status"], "evidence": c["evidence"]}
                               for c in rec["conditions"] if c["status"] != "active"],
            "active_medications": [_med_line(m) for m in rec["medications"] if m["status"] == "active"],
            "stopped_medications": [{"ingredient": m["ingredient"], "stopped": m["last_changed"]}
                                    for m in rec["medications"] if m["status"] == "stopped"],
            "allergy_status": rec["allergy_status"],
            "allergies": [{"substance": a["substance"], "reactions": a.get("reactions", [])}
                          for a in rec["allergies"] if a["status"] == "active"],
            "resolved_allergies": [{"substance": a["substance"], "status": a["status"], "since": a.get("resolved_on")}
                                   for a in rec["allergies"] if a["status"] != "active"],
            "latest_labs": sorted(labs, key=lambda x: x["name"]),
            "latest_vitals": sorted(vitals, key=lambda x: x["name"]),
            "immunizations": [{"vaccine": i.get("vaccine"), "date": i.get("date")} for i in rec["immunizations"]],
            "conflicts": [{"type": c["type"], "severity": c["severity"], "message": c["message"]}
                          for c in rec["conflicts"]],
            "data_quality": {"sources": len(rec["sources"]), "unmapped_facts": len(rec["unmapped"]),
                             "formats": sorted({s["format"] for s in rec["sources"]})},
        }

    def observations(self, patient_id: str, names: list[str] | None = None, since: str | None = None) -> list[dict]:
        from .terminology import lookup_observation
        rec = self.record(patient_id)
        wanted = None
        if names:
            wanted = set()
            for n in names:
                o = lookup_observation(text=n) or lookup_observation(code=n)
                if not o:
                    raise CanonError("unknown_observation", f"Unknown lab/vital '{n}'.")
                wanted.add(o["loinc"])
        out = [o for o in rec["observations"] if (wanted is None or o["codes"]["loinc"] in wanted)
               and (since is None or (o.get("effective") or "") >= since)]
        return sorted(out, key=lambda o: (o["display"], o.get("effective") or ""))

    def fhir(self, patient_id: str) -> dict:
        return to_fhir_bundle(self.record(patient_id))

    def audit_log(self, patient_id: str | None = None, limit: int = 100) -> list[dict]:
        return self.store.audit_entries(self.account_id, patient_id, limit)


def _med_line(m: dict) -> dict:
    return {k: v for k, v in {"ingredient": m["ingredient"], "rxnorm": m["codes"]["rxnorm"], "dose": m.get("dose"),
                              "frequency": (m.get("frequency") or {}).get("display"), "route": m.get("route"),
                              "since": m.get("first_seen"), "last_changed": m.get("last_changed")}.items() if v}
