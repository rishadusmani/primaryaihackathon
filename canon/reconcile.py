"""Merge normalized items from every source into one canonical patient record.

Principles:
* One concept, one entry: same condition/drug/allergen/lab reading from N
  sources becomes one item listing N sources (confidence combines).
* Current state comes from the most recent clinical statement; claims never
  decide clinical status on their own. A statement with no date of its own takes
  its document's date: the clinical date, else when the document was generated
  (signed, printed, faxed, sent; `date_basis` on the source says which). With
  neither it ranks oldest, so an undated fax can't override a dated note, and the
  document is flagged so a human can date it.
* Disagreements are surfaced as `conflicts`, never silently resolved, so an
  agent can ask a human instead of acting on bad data.
"""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha1

LIST_KINDS = ("condition", "medication", "allergy", "observation", "procedure", "immunization", "encounter",
              "coverage")
CLINICAL_METHODS = ("structured", "rule_nlp", "llm")


def _src(item: dict) -> dict:
    s = item["_source"]
    return {"document_id": s["id"], "format": s["format"], "source": s.get("source_name"),
            "locator": item["provenance"]["locator"], "method": item["provenance"]["method"],
            **({"snippet": item["provenance"]["snippet"]} if item["provenance"].get("snippet") else {}),
            **({"evidence_verified": False} if item.get("evidence_verified") is False else {})}


def _combine(confs: list[float]) -> float:
    p = 1.0
    for c in confs:
        p *= (1 - c)
    return round(min(0.999, 1 - p), 3)


def _date_of(item: dict) -> str | None:
    """Clinical date of a statement: its own, else its document's. Upload time is not a clinical date."""
    return item.get("date") or item["_source"].get("document_date")


def _rank(item: dict) -> str:
    """Chronological sort key; undated statements sort first (oldest). Ties keep upload order."""
    return _date_of(item) or ""


def _time_of(item: dict) -> str:
    """Time of day the statement was made ("HH:MM", local as the source wrote it), or "" when unknown."""
    at = item.get("at") or ""
    return at[11:16] if at[:10] == _date_of(item) else ""


def _when(date: str | None) -> str:
    return f"dated {date}" if date else "with no date"


def _id(kind: str, key: str) -> str:
    return f"{kind[:3]}_{sha1(f'{kind}:{key}'.encode()).hexdigest()[:10]}"


def _clean(item: dict, drop=("key", "provenance", "confidence", "_source", "evidence_verified")) -> dict:
    return {k: v for k, v in item.items() if k not in drop and v not in (None, {}, [])}


def build_record(patient_id: str, items: list[tuple[str, dict]], sources: list[dict], unmapped: list[dict]) -> dict:
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for kind, item in items:
        by_kind[kind].append(item)
    conflicts: list[dict] = []
    record = {
        "object": "patient_record", "patient_id": patient_id,
        "patient": _patient(by_kind["patient"], conflicts),
        "conditions": _conditions(by_kind["condition"], conflicts),
        "medications": _medications(by_kind["medication"], conflicts),
        **_allergies(by_kind["allergy"], conflicts),
        "observations": _observations(by_kind["observation"], conflicts),
        "procedures": _simple("procedure", by_kind["procedure"], lambda i: (i["key"], i.get("date"))),
        "immunizations": _simple("immunization", by_kind["immunization"], lambda i: (i["key"], i.get("date"))),
        "encounters": _simple("encounter", by_kind["encounter"], lambda i: (i.get("date"), i.get("provider") or "")),
        "coverage": _simple("coverage", by_kind["coverage"], lambda i: (i.get("key") or "",)),
        "conflicts": conflicts + _undated(items),
        "unmapped": [{**{k: v for k, v in u.items() if k not in ("_source",)}, "source": _src(u)} for u in unmapped],
        "sources": sources,
    }
    record["stats"] = {k: len(record[k]) for k in ("conditions", "medications", "allergies", "observations",
                                                   "procedures", "immunizations", "encounters", "conflicts",
                                                   "unmapped", "sources")}
    return record


def _undated(items: list[tuple[str, dict]]) -> list[dict]:
    """One low-severity flag per document whose clinical statements have no date to rank them by."""
    docs: dict[str, list[str]] = defaultdict(list)
    for kind, i in items:
        if kind in ("condition", "medication", "allergy") and not _date_of(i) \
                and i["provenance"]["method"] in CLINICAL_METHODS:
            docs[i["_source"]["id"]].append(kind)
    out = []
    for doc_id, kinds in docs.items():
        src = next(i["_source"] for _, i in items if i["_source"]["id"] == doc_id)
        name = src.get("source_name") or src.get("filename") or doc_id
        out.append({"type": "undated_source", "severity": "low", "document_id": doc_id,
                    "values": sorted(set(kinds)),
                    "message": f"{name} has no clinical or generation date, so its {len(kinds)} statement(s) are "
                               "treated as the oldest and can't override dated sources. Confirm the document date."})
    return out


# --------------------------------------------------------------------------- patient
def _patient(items: list[dict], conflicts: list) -> dict:
    out: dict = {"names": [], "identifiers": [], "addresses": [], "phones": []}
    votes: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for i in items:
        for field in ("dob", "sex", "name_given", "name_family"):
            if i.get(field):
                votes[field][i[field].strip()] += i["confidence"]
        for ident in i.get("identifiers") or []:
            if ident not in out["identifiers"]:
                out["identifiers"].append(ident)
        if i.get("address") and i["address"] not in out["addresses"]:
            out["addresses"].append(i["address"])
        if i.get("phone") and i["phone"] not in out["phones"]:
            out["phones"].append(i["phone"])
        nm = " ".join(filter(None, [i.get("name_given"), i.get("name_family")]))
        if nm and nm not in out["names"]:
            out["names"].append(nm)
    for field, v in votes.items():
        best = max(v.items(), key=lambda kv: kv[1])[0]
        key = {"dob": "birth_date"}.get(field, field)
        out[key] = best
        distinct = {x.lower() for x in v}
        if field in ("dob", "sex") and len(distinct) > 1:
            conflicts.append({"type": "demographic_mismatch", "field": key, "values": sorted(v),
                              "severity": "high",
                              "message": f"Sources disagree on {key}: {sorted(v)}. Possible wrong-patient document."})
    return {k: v for k, v in out.items() if v not in ([], None)}


# --------------------------------------------------------------------------- timelines
def _timeline(items: list[dict], explicit=lambda i: False) -> list[dict]:
    """One concept's statements, oldest to newest; the last one is the current state. On the same date an
    explicit statement (a dose change, a stop, a resolution) outranks a list entry that may be copied forward,
    then the later time of day wins (an admission list at 08:00, the discharge list at 16:00). A statement
    with a known time sorts after a same-day one without."""
    return sorted(items, key=lambda i: (_rank(i), bool(explicit(i)), _time_of(i), i["confidence"]))


def _latest(timeline: list[dict]) -> tuple[str | None, list[dict]]:
    """The newest date and the statements that day not known to come before the current one. Disagreements
    among these are conflicts; statements timed earlier that day are history. (A list timed after an explicit
    change still counts: the change wins the tie, but the later list contradicting it must be surfaced.)"""
    newest = timeline[-1]
    day, t = _date_of(newest), _time_of(newest)
    return day, [i for i in timeline if _date_of(i) == day and not (t and _time_of(i) and _time_of(i) < t)]


def _span(timeline: list[dict]) -> tuple[str | None, str | None]:
    dates = [d for d in map(_date_of, timeline) if d]
    return (dates[0], dates[-1]) if dates else (None, None)


def _history(timeline: list[dict], **fields) -> list[dict]:
    return [{"date": _date_of(i), **({"at": i["at"]} if _time_of(i) else {}),
             **{k: get(i) for k, get in fields.items()}, "document_id": i["_source"]["id"]} for i in timeline]


def _groups(items: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for i in items:
        groups[i["key"]].append(i)
    return groups


# --------------------------------------------------------------------------- conditions
def _conditions(items: list[dict], conflicts: list) -> list[dict]:
    out = []
    for key, g in _groups(items).items():
        best = max(g, key=lambda i: (i.get("code_verified", False), len(i["codes"].get("icd10", "")), i["confidence"]))
        g = _timeline(g)
        # claims never decide clinical status
        clinical = [i for i in g if i["provenance"]["method"] in CLINICAL_METHODS and i.get("status")]
        status = clinical[-1]["status"] if clinical else "unknown"
        if clinical:
            latest, same_day = _latest(clinical)
            statuses = {i["status"] for i in same_day}
            if len(statuses) > 1:
                status = "active"
                conflicts.append({"type": "condition_status", "item_id": _id("condition", key),
                                  "display": best["display"], "values": sorted(statuses), "severity": "medium",
                                  "message": f"{best['display']}: sources {_when(latest)} disagree on status."})
        onsets = [i["onset"] for i in g if i.get("onset")]
        first_seen, last_seen = _span(g)
        out.append({
            "id": _id("condition", key), "display": best["display"], "codes": best["codes"], "status": status,
            "terminology": best.get("terminology"),
            "onset": min(onsets) if onsets else None, "first_seen": first_seen, "last_seen": last_seen,
            "evidence": "claims_only" if all(i.get("billed_only") for i in g) else "clinical",
            "confidence": _combine([i["confidence"] for i in g]),
            "history": _history(g, status=lambda i: i.get("status") or ("billed" if i.get("billed_only") else None)),
            "sources": [_src(i) for i in g],
        })
    order = {"active": 0, "unknown": 1, "resolved": 2}
    return sorted([{k: v for k, v in c.items() if v is not None} for c in out],
                  key=lambda c: (order.get(c["status"], 3), c["display"]))


# --------------------------------------------------------------------------- medications
def _sig(i: dict) -> str:
    f = (i.get("frequency") or {}).get("code")
    return f"{i.get('dose') or '?'} {f or '?'}"


def _medications(items: list[dict], conflicts: list) -> list[dict]:
    out = []
    for key, g in _groups(items).items():
        g = _timeline(g, explicit=lambda i: i.get("change") or i["status"] == "stopped")
        current = g[-1]
        # fill missing sig fields from the most recent statement that has them
        dose = current.get("dose") or next(
            (i["dose"] for i in reversed(g) if i.get("dose") and i["status"] != "stopped"), None)
        freq = current.get("frequency") or next(
            (i["frequency"] for i in reversed(g) if i.get("frequency") and i["status"] != "stopped"), None)
        route = next((i["route"] for i in reversed(g) if i.get("route")), None)
        # discrepancies among statements from the latest date across different documents
        latest, same_day = _latest(g)
        docs = {i["_source"]["id"] for i in same_day}
        if len(docs) > 1:
            statuses = {i["status"] for i in same_day}
            sigs = {_sig(i) for i in same_day if i.get("dose") and i["status"] != "stopped"}
            if len(statuses) > 1 or len(sigs) > 1:
                values = sorted(statuses) if len(statuses) > 1 else sorted(sigs)
                what = "status" if len(statuses) > 1 else "dose/frequency"
                conflicts.append({
                    "type": "medication_discrepancy", "item_id": _id("medication", key), "display": key,
                    "values": values, "severity": "high",
                    "documents": sorted(docs),
                    "message": f"{key}: documents {_when(latest)} disagree on {what} ({' vs '.join(values)}). "
                               "Reconcile with the patient or prescriber before acting."})
        out.append({k: v for k, v in {
            "id": _id("medication", key), "ingredient": key, "display": current["display"],
            "codes": current["codes"], "drug_class": current["drug_class"], "status": current["status"],
            "terminology": current.get("terminology"),
            "dose": dose, "route": route, "frequency": freq, "last_changed": latest, "first_seen": _span(g)[0],
            "confidence": _combine([i["confidence"] for i in g]),
            "history": _history(g, status=lambda i: i["status"], dose=lambda i: i.get("dose"),
                                frequency=lambda i: (i.get("frequency") or {}).get("display")),
            "sources": [_src(i) for i in g],
        }.items() if v is not None})
    return sorted(out, key=lambda m: (m["status"] != "active", m["ingredient"]))


# --------------------------------------------------------------------------- allergies
def _allergies(items: list[dict], conflicts: list) -> dict:
    nkda = [i for i in items if i.get("no_known_allergies")]
    allergies = []
    for key, g in _groups([i for i in items if not i.get("no_known_allergies")]).items():
        g = _timeline(g, explicit=lambda i: i.get("status", "active") != "active")
        status = g[-1].get("status", "active")
        resolutions = [i for i in g if i.get("status", "active") != "active"]
        if status == "active" and resolutions:
            # Resolved, then listed again by a later source: often a copied-forward list, sometimes a real
            # reaction. Either way the safe reading is "allergic" until someone reconciles the list.
            r, later = resolutions[-1], g[-1]
            conflicts.append({
                "type": "allergy_resolution_disputed", "item_id": _id("allergy", key), "severity": "high",
                "values": [f"{r['status']} {_date_of(r) or 'undated'}", f"active {_date_of(later) or 'undated'}"],
                "documents": sorted({r["_source"]["id"], later["_source"]["id"]}),
                "message": f"{key} allergy was marked {r['status']} ({_when(_date_of(r))}) but a later source "
                           f"({_when(_date_of(later))}) still lists it. Treat as active until the allergy list "
                           "is reconciled."})
        reactions = sorted({i["reaction"] for i in g if i.get("reaction")})
        sev = next((i["severity"] for i in g if i.get("severity")), None)
        allergies.append({k: v for k, v in {
            "id": _id("allergy", key), "substance": key, "codes": g[0]["codes"],
            "reactions": reactions, "severity": sev, "status": status,
            "resolved_on": _date_of(g[-1]) if status != "active" else None,
            "history": _history(g, status=lambda i: i.get("status", "active")),
            "confidence": _combine([i["confidence"] for i in g]), "sources": [_src(i) for i in g],
        }.items() if v not in (None, [])})
    active = [a for a in allergies if a["status"] == "active"]
    if active and nkda:
        conflicts.append({
            "type": "allergy_vs_nkda", "severity": "high",
            "values": [a["substance"] for a in active] + ["NKDA"],
            "message": f"{len(nkda)} source(s) state no known allergies but others document "
                       f"{', '.join(a['substance'] for a in active)}. Treat allergies as present until confirmed.",
            "nkda_sources": [_src(i) for i in nkda]})
    status = "has_allergies" if active else ("no_known_allergies" if nkda else "unknown")
    return {"allergy_status": status,
            "allergies": sorted(allergies, key=lambda a: (a["status"] != "active", a["substance"]))}


# --------------------------------------------------------------------------- observations
def _observations(items: list[dict], conflicts: list) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for i in items:
        v = i.get("value")
        groups[(i["key"], i.get("effective"), round(v, 1) if isinstance(v, float) else i.get("value_text"))].append(i)
    out = []
    for (key, eff, _), g in groups.items():
        best = max(g, key=lambda i: i["confidence"])
        out.append({k: v for k, v in {
            "id": _id("observation", f"{key}|{eff}|{best.get('value')}"), "display": best["display"],
            "codes": best["codes"], "category": best["category"], "value": best.get("value"),
            "value_text": best.get("value_text"), "qualifier": best.get("qualifier"), "unit": best.get("unit"),
            "interpretation": best.get("interpretation"), "effective": eff,
            "at": next((i["at"] for i in g if i.get("at")), None), "terminology": best.get("terminology"),
            "original": best["original"] if best.get("unit_converted") else None,
            "confidence": _combine([i["confidence"] for i in g]), "sources": [_src(i) for i in g],
        }.items() if v is not None})
    # same test, same day, materially different values from different documents; readings taken at
    # different known times of day (a morning and an evening glucose) are a series, not a disagreement
    by_day: dict[tuple, list[dict]] = defaultdict(list)
    for o in out:
        if o.get("effective") and isinstance(o.get("value"), float):
            by_day[(o["codes"]["loinc"], o["effective"])].append(o)

    def differ(a: dict, b: dict) -> bool:
        apart = abs(a["value"] - b["value"]) / max(abs(a["value"]), abs(b["value"]), 1e-9) > 0.05
        return apart and (not a.get("at") or not b.get("at") or a["at"] == b["at"])

    for (loinc, day), obs in by_day.items():
        clash = [o for o in obs if any(differ(o, x) for x in obs if x is not o)]
        if clash:
            vals = sorted({o["value"] for o in clash})
            conflicts.append({"type": "observation_mismatch", "display": clash[0]["display"], "date": day,
                              "values": vals, "severity": "medium", "item_ids": [o["id"] for o in clash],
                              "message": f"{clash[0]['display']} on {day} reported as {vals} by different sources."})
    return sorted(out, key=lambda o: (o["category"], o["display"], o.get("effective") or "", o.get("at") or ""))


# --------------------------------------------------------------------------- generic
def _simple(kind: str, items: list[dict], keyfn) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for i in items:
        groups[keyfn(i)].append(i)
    out = []
    for key, g in groups.items():
        merged: dict = {}
        for i in sorted(g, key=lambda i: i["confidence"]):
            merged.update({k: v for k, v in _clean(i).items() if v})
        out.append({"id": _id(kind, "|".join(map(str, key))), **merged,
                    "sources": [_src(i) for i in g]})
    return sorted(out, key=lambda x: x.get("date") or "", reverse=True)
