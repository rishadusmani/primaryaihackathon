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
from .reconcile import build_record
from .store import Store, new_id



def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


class Canon:
    def __init__(self, db_path: str = ":memory:"):
        self.store = Store(db_path)

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
        for f in facts:
            items.append(normalize(f))

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
        existing = self.store.one("SELECT id FROM documents WHERE patient_id=? AND sha256=?", (pid, digest))
        if existing:
            doc = self.get_document(existing["id"])
            doc["duplicate"] = True
            return {"document": doc, "patient_id": pid, "match": match}

        dates = [it.get("date") or it.get("effective") for k, it in items if k in ("encounter", "observation",
                                                                                     "condition", "medication")]
        dates = [d for d in dates if d]
        doc_date = Counter(dates).most_common(1)[0][0] if dates else None
        doc_id = new_id("doc")
        received = _now()
        self.store.execute(
            "INSERT INTO documents (id, patient_id, format, filename, source_name, received_at, document_date, "
            "sha256, size, raw, info, items) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, pid, fmt, filename, source_name, received, doc_date, digest, len(raw), raw, json.dumps(info),
             json.dumps(items)),
        )
        if match.get("created"):
            self.store.execute("INSERT INTO patients (id, created_at) VALUES (?,?)", (pid, received))
        for k in _match_keys(demo):
            self.store.execute("INSERT OR IGNORE INTO patient_keys (key, patient_id) VALUES (?,?)", (k, pid))
        self.store.audit(ts=received, event="document.ingested", actor=actor, patient_id=pid, document_id=doc_id,
                         detail={"format": fmt, "sha256": digest, "facts": len(facts), "match": match["method"]})
        return {"document": self.get_document(doc_id), "patient_id": pid, "match": match}

    def _match_patient(self, patient_id: str | None, demo: dict) -> dict:
        keys = _match_keys(demo)
        if patient_id:
            exists = self.store.one("SELECT id FROM patients WHERE id=?", (patient_id,))
            out = {"patient_id": patient_id, "method": "explicit", "confidence": 1.0, "created": not exists}
            if exists and keys:
                known = {r["key"] for r in self.store.all("SELECT key FROM patient_keys WHERE patient_id=?",
                                                          (patient_id,))}
                fam_dob = [k for k in keys if k.startswith("fam_dob:")]
                known_fd = [k for k in known if k.startswith("fam_dob:")]
                if fam_dob and known_fd and not set(fam_dob) & set(known_fd):
                    out["warning"] = ("Document demographics do not match this patient "
                                      f"({fam_dob[0][8:]} vs {known_fd[0][8:]}). Check for a misfiled document.")
            return out
        for k in keys:
            rows = self.store.all("SELECT DISTINCT patient_id FROM patient_keys WHERE key=?", (k,))
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
        r = self.store.one("SELECT * FROM documents WHERE id=?", (doc_id,))
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
        for r in self.store.all("SELECT id FROM patients ORDER BY created_at"):
            rec = self.record(r["id"], actor=None)
            out.append({"patient_id": r["id"], "name": (rec["patient"].get("names") or [None])[0],
                        "birth_date": rec["patient"].get("birth_date"), "documents": len(rec["sources"])})
        return out

    def record(self, patient_id: str, actor: str | None = "api") -> dict:
        rows = self.store.all("SELECT * FROM documents WHERE patient_id=? ORDER BY received_at", (patient_id,))
        if not rows:
            raise CanonError("not_found", f"No patient {patient_id}", 404)
        items: list[tuple[str, dict]] = []
        unmapped: list[dict] = []
        sources = []
        for r in rows:
            src = {"id": r["id"], "format": r["format"], "source_name": r["source_name"],
                   "received_at": r["received_at"], "document_date": r["document_date"]}
            sources.append({**src, "filename": r["filename"]})
            for kind, it in json.loads(r["items"]):
                it["_source"] = src
                (unmapped.append(it) if kind == "unmapped" else items.append((kind, it)))
        rec = build_record(patient_id, items, sources, unmapped)
        if actor:
            self.store.audit(ts=_now(), event="record.read", actor=actor, patient_id=patient_id,
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
            "allergies": [{"substance": a["substance"], "reactions": a.get("reactions", [])} for a in rec["allergies"]],
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
        return self.store.audit_entries([patient_id] if patient_id else None, limit)


def _med_line(m: dict) -> dict:
    return {k: v for k, v in {"ingredient": m["ingredient"], "rxnorm": m["codes"]["rxnorm"], "dose": m.get("dose"),
                              "frequency": (m.get("frequency") or {}).get("display"), "route": m.get("route"),
                              "since": m.get("first_seen"), "last_changed": m.get("last_changed")}.items() if v}
