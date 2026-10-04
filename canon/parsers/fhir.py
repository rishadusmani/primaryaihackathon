"""FHIR R4 JSON (Bundle or single resource) -> facts."""

from __future__ import annotations

import json

from ..model import fact


def _codings(cc: dict | None) -> list[dict]:
    return (cc or {}).get("coding", []) or []


def _text(cc: dict | None) -> str | None:
    if not cc:
        return None
    return cc.get("text") or next((c.get("display") for c in _codings(cc) if c.get("display")), None)


def _code_facts(cc: dict | None) -> tuple[str | None, str | None]:
    cs = _codings(cc)
    return (cs[0].get("code"), cs[0].get("system")) if cs else (None, None)


def _all_codes(cc: dict | None) -> list[dict]:
    return [{"system": c.get("system"), "code": c.get("code")} for c in _codings(cc) if c.get("code")]


def _status(cc: dict | str | None) -> str | None:
    if isinstance(cc, str):
        return cc
    cs = _codings(cc)
    return cs[0].get("code") if cs else None


def _date(*vals) -> str | None:
    for v in vals:
        if isinstance(v, str) and v:
            return v[:10]
        if isinstance(v, dict):
            d = v.get("start") or v.get("end")
            if d:
                return d[:10]
    return None


def _dose(d: dict) -> tuple[str | None, str | None, str | None]:
    """Return (dose text, route text, frequency text) from a FHIR Dosage."""
    if not d:
        return None, None, None
    dose = None
    for dr in d.get("doseAndRate", []) or []:
        q = dr.get("doseQuantity")
        if q:
            dose = f"{q.get('value')} {q.get('unit', '')}".strip()
    route = _text(d.get("route"))
    rep = (d.get("timing") or {}).get("repeat") or {}
    freq = None
    if rep.get("frequency") and rep.get("periodUnit"):
        n, per, unit = rep["frequency"], rep.get("period", 1), rep["periodUnit"]
        if unit == "d" and per == 1:
            freq = {1: "daily", 2: "BID", 3: "TID", 4: "QID"}.get(n, f"{n}x per day")
        elif unit == "wk" and per == 1 and n == 1:
            freq = "weekly"
        else:
            freq = f"{n} per {per} {unit}"
    code = _text((d.get("timing") or {}).get("code"))
    freq = freq or code
    if d.get("asNeededBoolean"):
        freq = (freq or "") + " PRN"
    return dose, route, freq or d.get("text")


def parse(content: str | dict) -> list[dict]:
    data = json.loads(content) if isinstance(content, str) else content
    if data.get("resourceType") == "Bundle":
        resources = [e.get("resource", {}) for e in data.get("entry", [])]
    else:
        resources = [data]
    facts: list[dict] = []
    created = (_date(data.get("timestamp")) or
               next((_date(r.get("date")) for r in resources if r.get("resourceType") == "Composition"), None) or
               _date((data.get("meta") or {}).get("lastUpdated")))
    if created:
        facts.append(fact("document", locator="Bundle.timestamp" if data.get("timestamp") else "Bundle",
                          method="structured", generated=created))
    for i, r in enumerate(resources):
        rt = r.get("resourceType")
        loc = f"{rt}/{r.get('id', i)}"
        m = "structured"
        if rt == "Patient":
            name = (r.get("name") or [{}])[0]
            addr = (r.get("address") or [{}])[0]
            facts.append(fact(
                "patient", locator=loc, method=m,
                name_given=" ".join(name.get("given", [])) or None, name_family=name.get("family"),
                dob=r.get("birthDate"), sex=r.get("gender"),
                identifiers=[{"system": x.get("system"), "value": x.get("value")} for x in r.get("identifier", [])],
                address=", ".join(filter(None, [*addr.get("line", []), addr.get("city"), addr.get("state"),
                                                addr.get("postalCode")])) or None,
                phone=next((t.get("value") for t in r.get("telecom", []) if t.get("system") == "phone"), None)))
        elif rt == "Condition":
            code, system = _code_facts(r.get("code"))
            facts.append(fact("condition", locator=loc, method=m, text=_text(r.get("code")), code=code,
                              system=system, alt_codes=_all_codes(r.get("code"))[1:],
                              status=_status(r.get("clinicalStatus")),
                              onset=_date(r.get("onsetDateTime"), r.get("onsetPeriod")),
                              recorded=_date(r.get("recordedDate"))))
        elif rt in ("MedicationRequest", "MedicationStatement"):
            cc = r.get("medicationCodeableConcept") or {}
            code, system = _code_facts(cc)
            dosage = (r.get("dosageInstruction") or r.get("dosage") or [{}])[0]
            dose, route, freq = _dose(dosage)
            facts.append(fact("medication", locator=loc, method=m, text=_text(cc), code=code, system=system,
                              dose_text=dose, route_text=route, frequency_text=freq, sig=dosage.get("text"),
                              status=r.get("status"),
                              start=_date(r.get("authoredOn"), r.get("effectiveDateTime"), r.get("effectivePeriod"),
                                          r.get("dateAsserted"))))
        elif rt == "AllergyIntolerance":
            code, system = _code_facts(r.get("code"))
            reaction = (r.get("reaction") or [{}])[0]
            text = _text(r.get("code"))
            if code in ("716186003", "409137002") or (text and "no known" in text.lower()):
                facts.append(fact("allergy", locator=loc, method=m, no_known_allergies=True,
                                  recorded=_date(r.get("recordedDate"))))
                continue
            status = ("refuted" if _status(r.get("verificationStatus")) in ("refuted", "entered-in-error")
                      else _status(r.get("clinicalStatus")))
            # An active entry is asserted as of the document; recordedDate is when it was first recorded, so
            # only a resolution carries its own date into chronology.
            facts.append(fact("allergy", locator=loc, method=m, text=text, code=code, system=system,
                              reaction=_text((reaction.get("manifestation") or [{}])[0]),
                              severity=reaction.get("severity") or r.get("criticality"), status=status,
                              recorded=_date(r.get("recordedDate")) if status not in (None, "active") else None))
        elif rt == "Observation":
            facts.extend(_observation(r, loc))
        elif rt == "Procedure":
            code, system = _code_facts(r.get("code"))
            facts.append(fact("procedure", locator=loc, method=m, text=_text(r.get("code")), code=code,
                              system=system, date=_date(r.get("performedDateTime"), r.get("performedPeriod"))))
        elif rt == "Immunization":
            code, system = _code_facts(r.get("vaccineCode"))
            facts.append(fact("immunization", locator=loc, method=m, text=_text(r.get("vaccineCode")), code=code,
                              date=_date(r.get("occurrenceDateTime"))))
        elif rt == "Encounter":
            facts.append(fact("encounter", locator=loc, method=m,
                              type=_text((r.get("type") or [None])[0]) or (r.get("class") or {}).get("code"),
                              date=_date(r.get("period")),
                              provider=next((p.get("individual", {}).get("display") for p in r.get("participant", [])),
                                            None),
                              facility=(r.get("serviceProvider") or {}).get("display"),
                              reason=_text((r.get("reasonCode") or [None])[0])))
        elif rt == "Coverage":
            facts.append(fact("coverage", locator=loc, method=m,
                              payer=next((p.get("display") for p in r.get("payor", [])), None),
                              member_id=r.get("subscriberId"),
                              group=next((c.get("value") for c in r.get("class", []) if
                                          _status(c.get("type")) == "group"), None)))
    _assert_dates(data, facts)
    return facts


def _assert_dates(data: dict, facts: list[dict]) -> None:
    """A resource recorded during an encounter was asserted on that encounter's date."""
    entries = (data.get("entry") or []) if data.get("resourceType") == "Bundle" else [{"resource": data}]
    visits: dict[str, str] = {}
    for e in entries:
        r = e.get("resource") or {}
        if r.get("resourceType") == "Encounter" and _date(r.get("period")):
            for key in (e.get("fullUrl"), f"Encounter/{r.get('id')}"):
                if key:
                    visits[key] = _date(r.get("period"))
    asserted = {}
    for i, e in enumerate(entries):
        r = e.get("resource") or {}
        ref = (r.get("encounter") or r.get("context") or {}).get("reference") or ""
        ref = ref.split("/_history")[0]
        date = visits.get(ref) or visits.get("Encounter/" + ref.rsplit("/", 1)[-1])
        if date:
            asserted[f"{r.get('resourceType')}/{r.get('id', i)}"] = date
    for f in facts:
        resource = "/".join(f["provenance"]["locator"].split("/")[:2])
        if resource in asserted:
            f["as_of"] = asserted[resource]


def _observation(r: dict, loc: str) -> list[dict]:
    date = _date(r.get("effectiveDateTime"), r.get("effectivePeriod"), r.get("issued"))
    out = []
    parts = [(r.get("code"), r)] + [(c.get("code"), c) for c in r.get("component", [])]
    for idx, (cc, holder) in enumerate(parts):
        q = holder.get("valueQuantity")
        val = q.get("value") if q else holder.get("valueString") or holder.get("valueInteger")
        if val is None:
            continue
        code, system = _code_facts(cc)
        flag = _status((holder.get("interpretation") or [None])[0])
        out.append(fact("observation", locator=loc + (f"/component[{idx - 1}]" if idx else ""), method="structured",
                        text=_text(cc), code=code, system=system, value=val,
                        unit=(q or {}).get("unit") or (q or {}).get("code"), effective=date, flag=flag))
    return out
