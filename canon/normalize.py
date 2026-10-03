"""Fact -> canonical item: terminology mapping, unit conversion, status and
date normalization. Anything that can't be mapped is returned as `unmapped`
(with the reason) so it's never silently dropped."""

from __future__ import annotations

import re

from . import live_terminology as L
from . import terminology as T
from .parsers.text import DOSE_RX

CONDITION_STATUS = {"active": "active", "recurrence": "active", "relapse": "active", "chronic": "active",
                    "inactive": "resolved", "remission": "resolved", "resolved": "resolved", "completed": "resolved"}
MED_STATUS = {"active": "active", "intended": "active", "draft": "active", "on-hold": "on_hold", "suspended": "on_hold",
              "completed": "stopped", "stopped": "stopped", "cancelled": "stopped", "discontinued": "stopped",
              "not-taken": "stopped", "aborted": "stopped", "nullified": "stopped"}
FLAGS = {"h": "high", "hh": "critical_high", "l": "low", "ll": "critical_low", "n": "normal", "a": "abnormal",
         "high": "high", "low": "low", "normal": "normal", "abnormal": "abnormal", "hi": "high", "lo": "low"}


def _d(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else None


def normalize(f: dict) -> tuple[str, dict]:
    kind = f["kind"]
    base = {"confidence": f["confidence"], "provenance": f["provenance"]}
    if f.get("evidence_verified") is False:
        base["evidence_verified"] = False
    fn = globals()[f"_{kind}"]
    out = fn(f)
    if out is None:
        return "unmapped", {"kind": kind, "raw": {k: v for k, v in f.items() if k not in ("provenance",
                                                                                      "confidence")},
                            "reason": "no standard code match", **base}
    if isinstance(out, tuple):  # ("unmapped", reason)
        return "unmapped", {"kind": kind, "raw": {k: v for k, v in f.items() if k not in ("provenance", "confidence")},
                            "reason": out[1], **base}
    return kind, {**out, **base}


def _patient(f: dict) -> dict:
    return {k: f.get(k) for k in ("name_given", "name_family", "dob", "sex", "identifiers", "address", "phone")
            if f.get(k)}


def _condition(f: dict):
    c = None
    if f.get("mapped_code"):
        c = T.lookup_condition(code=f["mapped_code"], system="icd10")
    if not c and f.get("code"):
        c = T.lookup_condition(text=f.get("text"), code=f["code"], system=f.get("system"))
    for alt in f.get("alt_codes", []) or []:
        if c and c.get("verified"):
            break
        c = T.lookup_condition(code=alt["code"], system=alt.get("system")) or c
    if not c and f.get("text"):
        c = T.lookup_condition(text=f["text"])
    if not c or not c.get("verified"):  # not in our tables (or an unverified ICD-10 code): ask NLM
        sys_ = T.norm_system(f.get("system"))
        live = L.condition(text=f.get("text"), code=f.get("code") if sys_ in (None, T.ICD10) else None, system=sys_)
        for alt in f.get("alt_codes", []) or []:
            if live:
                break
            if T.norm_system(alt.get("system")) in (None, T.ICD10):
                live = L.condition(code=alt["code"], system=T.ICD10)
        c = live or c
    if not c:
        return None
    status = CONDITION_STATUS.get((f.get("status") or "").lower())
    if f.get("billed_only"):
        status = None
    return {"key": c["icd10"].split(".")[0], "display": c["display"],
            "codes": {"icd10": c["icd10"], **({"snomed": c["snomed"]} if c.get("snomed") else {})},
            "code_verified": c["verified"], "status": status, "onset": _d(f.get("onset")),
            "date": _d(f.get("recorded")) or _d(f.get("onset")), "billed_only": bool(f.get("billed_only")),
            "original_text": f.get("text") or f.get("code"), "terminology": c.get("terminology")}


def _medication(f: dict):
    sys = T.norm_system(f.get("system"))
    m = T.lookup_medication(text=f.get("text"), code=f.get("code") if sys in (None, T.RXNORM) else None)
    if not m and f.get("code"):
        m = T.lookup_medication(text=f.get("text"))
    if not m:
        m = L.medication(text=f.get("text"), code=f.get("code") if sys in (None, T.RXNORM) else None)
    if not m:
        return None
    dose = None
    if f.get("dose_text"):
        dm = DOSE_RX.search(f["dose_text"]) or DOSE_RX.search(f.get("text") or "")
        dose = re.sub(r"\s+", " ", dm.group(0)).strip() if dm else f["dose_text"]
    elif f.get("text"):
        dm = DOSE_RX.search(f["text"])
        dose = dm.group(0) if dm else None
    if dose:
        dose = re.sub(r"(\d)\s*(mg|mcg|g|ml|mL|units?)\b", r"\1 \2", dose, flags=re.I).replace("mL", "ml")
    freq = T.parse_frequency(f.get("frequency_text") or f.get("sig") or f.get("route_text"))
    route = T.parse_route(f.get("route_text") or f.get("sig") or "")
    status = MED_STATUS.get((f.get("status") or "active").lower(), "unknown")
    return {"key": m["ingredient"], "ingredient": m["ingredient"], "display": f.get("text") or m["ingredient"],
            "codes": {"rxnorm": m["rxnorm"]}, "drug_class": m["drug_class"], "dose": dose,
            "terminology": m.get("terminology"),
            "route": route, "frequency": freq, "status": status, "date": _d(f.get("start")),
            **({"change": f["change"]} if f.get("change") else {})}


def _allergy(f: dict):
    if f.get("no_known_allergies"):
        return {"key": "__nkda__", "no_known_allergies": True, "date": _d(f.get("recorded"))}
    a = T.lookup_allergen(f.get("text")) or (T.lookup_allergen(f.get("code")) if f.get("code") else None)
    if not a and f.get("code"):
        for name, (sct, sub, _) in T.ALLERGENS.items():
            if f["code"] in (sct, sub):
                a = T.lookup_allergen(name)
    if not a:
        return None
    return {"key": a["substance"], "substance": a["substance"],
            "codes": {"snomed": a["snomed_allergy"], "snomed_substance": a["snomed_substance"]},
            "reaction": (f.get("reaction") or "").lower() or None, "severity": f.get("severity"),
            "status": "active" if (f.get("status") or "active").lower() == "active" else f.get("status").lower(),
            "date": _d(f.get("recorded")), "original_text": f.get("text")}


def _observation(f: dict):
    sys = T.norm_system(f.get("system"))
    o = T.lookup_observation(text=f.get("text"), code=f.get("code") if sys in (None, T.LOINC) else None)
    if not o and f.get("text"):
        o = T.lookup_observation(text=f["text"])
    if not o:
        o = L.observation(code=f.get("code") if sys in (None, T.LOINC) else None)
    if not o:
        return None
    raw = str(f.get("value")).strip()
    m = re.match(r"^([<>]=?)?\s*(-?\d+(?:\.\d+)?)", raw)
    item = {"key": o["loinc"], "display": o["display"], "codes": {"loinc": o["loinc"]}, "category": o["category"],
            "effective": _d(f.get("effective")), "original": {"value": raw, "unit": f.get("unit")}}
    item["date"] = item["effective"]
    if not m:
        return {**item, "value": None, "value_text": raw, "unit": None,
                "interpretation": FLAGS.get((f.get("flag") or "").lower())}
    v = float(m.group(2))
    if o.get("terminology"):  # live LOINC: no canonical unit known, keep the value as sent
        return {**item, "value": v, "unit": f.get("unit"), "unit_converted": False, "terminology": o["terminology"],
                "interpretation": FLAGS.get((f.get("flag") or "").strip().lower()),
                **({"qualifier": m.group(1)} if m.group(1) else {})}
    try:
        cv, unit, converted = T.convert_unit(o["loinc"], v, f.get("unit"))
    except ValueError as e:
        return ("unmapped", str(e))
    if m.group(1):
        item["qualifier"] = m.group(1)
    interp = FLAGS.get((f.get("flag") or "").strip().lower()) or T.interpret(o["loinc"], cv)
    return {**item, "value": cv, "unit": unit, "unit_converted": converted, "interpretation": interp}


def _procedure(f: dict):
    code = (f.get("code") or "").strip() or None
    sys = T.norm_system(f.get("system"))
    display = T.PROCEDURES.get(code) if code else None
    if not display and not f.get("text"):
        return None
    codes = {"cpt": code} if code and (display or sys == T.CPT) else ({"code": code, "system": sys} if code else {})
    return {"key": code or f.get("text", "").lower(), "display": display or f.get("text"), "codes": codes,
            "date": _d(f.get("date"))}


def _immunization(f: dict):
    v = T.lookup_vaccine(text=f.get("text"), cvx=f.get("code"))
    if not v:
        return None
    return {"key": v["vaccine"], "vaccine": v["vaccine"], "codes": {"cvx": v["cvx"]}, "date": _d(f.get("date"))}


def _encounter(f: dict):
    return {"key": f"{_d(f.get('date'))}", "type": f.get("type"), "date": _d(f.get("date")),
            "provider": f.get("provider"), "facility": f.get("facility"), "reason": f.get("reason"),
            **({"claim_id": f["claim_id"]} if f.get("claim_id") else {})}


def _coverage(f: dict):
    return {"key": f.get("member_id") or f.get("payer"), "payer": f.get("payer"), "member_id": f.get("member_id"),
            "group": f.get("group")}
