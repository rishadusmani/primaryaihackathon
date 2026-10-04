"""Canonical record -> FHIR R4 Bundle, so the normalized data can flow back
into EHRs, payer APIs and any FHIR-native agent tooling."""

from __future__ import annotations

from . import terminology as T

UCUM = "http://unitsofmeasure.org"


def _cc(codes: dict, display: str | None) -> dict:
    coding = []
    for k, system in (("icd10", T.ICD10), ("snomed", T.SNOMED), ("loinc", T.LOINC), ("rxnorm", T.RXNORM),
                      ("cpt", T.CPT), ("cvx", T.CVX)):
        if codes.get(k):
            coding.append({"system": system, "code": codes[k]})
    return {"coding": coding, **({"text": display} if display else {})}


def _ext(item: dict) -> list[dict]:
    return [{"url": "https://canon.dev/fhir/StructureDefinition/confidence", "valueDecimal": item["confidence"]},
            *({"url": "https://canon.dev/fhir/StructureDefinition/source-document", "valueString": s["document_id"]}
              for s in item.get("sources", []))] if "confidence" in item else []


def to_fhir_bundle(rec: dict) -> dict:
    pid = rec["patient_id"]
    ref = {"reference": f"Patient/{pid}"}
    p = rec["patient"]
    entries = []
    name = (p.get("names") or [""])[0].split()
    entries.append({"resourceType": "Patient", "id": pid,
                    "name": [{"family": p.get("name_family") or (name[-1] if name else None),
                              "given": [p["name_given"]] if p.get("name_given") else name[:-1]}],
                    **({"birthDate": p["birth_date"]} if p.get("birth_date") else {}),
                    **({"gender": p["sex"]} if p.get("sex") else {}),
                    "identifier": [{"system": i.get("system"), "value": i.get("value")}
                                   for i in p.get("identifiers", [])]})
    for c in rec["conditions"]:
        status = {"active": "active", "resolved": "resolved"}.get(c["status"])
        entries.append({"resourceType": "Condition", "id": c["id"], "subject": ref, "code": _cc(c["codes"],
                                                                                              c["display"]),
                        **({"clinicalStatus": {"coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                            "code": status}]}} if status else {}),
                        **({"onsetDateTime": c["onset"]} if c.get("onset") else {}),
                        "extension": _ext(c)})
    for m in rec["medications"]:
        dosage = {}
        if m.get("dose") or m.get("frequency"):
            text = " ".join(filter(None, [m.get("dose"), m.get("route"), (m.get("frequency") or {}).get("display")]))
            dosage = {"dosage": [{"text": text}]}
        entries.append({"resourceType": "MedicationStatement", "id": m["id"], "subject": ref,
                        "status": {"active": "active", "stopped": "stopped", "on_hold": "on-hold"}.get(
                            m["status"], "unknown"),
                        "medicationCodeableConcept": _cc(m["codes"], m["ingredient"]), **dosage,
                        **({"effectiveDateTime": m["last_changed"]} if m.get("last_changed") else {}),
                        "extension": _ext(m)})
    for a in rec["allergies"]:
        entries.append({"resourceType": "AllergyIntolerance", "id": a["id"], "patient": ref,
                        "code": _cc({"snomed": a["codes"]["snomed_substance"]}, a["substance"]),
                        "reaction": [{"manifestation": [{"text": r}]} for r in a.get("reactions", [])],
                        # FHIR ait-2: a refuted allergy carries verificationStatus and no clinicalStatus
                        **({"verificationStatus": {"coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
                            "code": "refuted"}]}} if a["status"] == "refuted" else {"clinicalStatus": {"coding": [{
                                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                                "code": a["status"]}]}}),
                        "extension": _ext(a)})
    if rec["allergy_status"] == "no_known_allergies":
        entries.append({"resourceType": "AllergyIntolerance", "id": f"nka-{pid}", "patient": ref,
                        "code": {"coding": [{"system": T.SNOMED, "code": "716186003",
                                             "display": "No known allergy"}]}})
    for o in rec["observations"]:
        res = {"resourceType": "Observation", "id": o["id"], "status": "final", "subject": ref,
               "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                         "code": "laboratory" if o["category"] == "lab" else "vital-signs"}]}],
               "code": _cc(o["codes"], o["display"]), "extension": _ext(o)}
        if o.get("effective"):
            res["effectiveDateTime"] = o["effective"]
        if o.get("value") is not None:
            res["valueQuantity"] = {"value": o["value"], "unit": o["unit"], "system": UCUM, "code": o["unit"]}
            if o.get("qualifier"):
                res["valueQuantity"]["comparator"] = o["qualifier"]
        elif o.get("value_text"):
            res["valueString"] = o["value_text"]
        if o.get("interpretation"):
            res["interpretation"] = [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                "code": {"high": "H", "low": "L", "normal": "N", "critical_high": "HH", "critical_low": "LL",
                         "abnormal": "A"}[o["interpretation"]]}]}]
        entries.append(res)
    for pr in rec["procedures"]:
        entries.append({"resourceType": "Procedure", "id": pr["id"], "status": "completed", "subject": ref,
                        "code": _cc(pr.get("codes", {}), pr.get("display")),
                        **({"performedDateTime": pr["date"]} if pr.get("date") else {})})
    for im in rec["immunizations"]:
        entries.append({"resourceType": "Immunization", "id": im["id"], "status": "completed", "patient": ref,
                        "vaccineCode": _cc(im["codes"], im.get("vaccine")),
                        **({"occurrenceDateTime": im["date"]} if im.get("date") else {})})
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"fullUrl": f"urn:uuid:{e['id']}", "resource": e} for e in entries]}
