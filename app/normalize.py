"""Normalize messy clinical records into canonical FHIR R4 resources.

Input is a list of loosely structured dicts (as an EHR export, a CSV row or an
LLM extraction might produce). Output is a FHIR R4 collection Bundle coded to
standard vocabularies (LOINC, SNOMED CT, ICD-10-CM, RxNorm, UCUM), plus a list
of issues for anything that could not be mapped. Unmappable records are
reported, never guessed.

The vocabularies below are a small hand-curated starter set. Extend the tables
(or swap in a terminology server) to cover more codes.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
UCUM = "http://unitsofmeasure.org"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
CONDITION_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
PATIENT_ID_SYSTEM = "urn:clinical-normalizer:patient-id"

# Deterministic ids: the same input always yields the same resource ids.
_NS = uuid.UUID("6f1c5a2e-8d0b-4c1e-9a57-3b1f0e6d2c44")


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# ---------------------------------------------------------------- observations

@dataclass(frozen=True)
class ObsDef:
    loinc: str
    display: str
    unit: str  # canonical UCUM unit
    category: str  # "laboratory" | "vital-signs"
    aliases: tuple[str, ...]
    # Conversions from other UCUM units into the canonical unit.
    convert: dict[str, Callable[[float], float]] | None = None


OBSERVATIONS: list[ObsDef] = [
    ObsDef("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", "laboratory",
           ("hba1c", "a1c", "hemoglobina1c", "haemoglobina1c", "glycatedhemoglobin", "glycohemoglobin"),
           {"mmol/mol": lambda v: 0.09148 * v + 2.152}),
    ObsDef("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL", "laboratory",
           ("glucose", "glu", "bloodglucose", "bloodsugar", "serumglucose"),
           {"mmol/L": lambda v: v * 18.016}),
    ObsDef("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL", "laboratory",
           ("creatinine", "creat", "cr", "scr", "serumcreatinine"),
           {"umol/L": lambda v: v / 88.42}),
    ObsDef("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "mg/dL", "laboratory",
           ("cholesterol", "totalcholesterol", "chol", "tc"),
           {"mmol/L": lambda v: v * 38.67}),
    ObsDef("2823-3", "Potassium [Moles/volume] in Serum or Plasma", "mmol/L", "laboratory",
           ("potassium", "k", "serumpotassium"),
           {"meq/L": lambda v: v}),
    ObsDef("2951-2", "Sodium [Moles/volume] in Serum or Plasma", "mmol/L", "laboratory",
           ("sodium", "na", "serumsodium"),
           {"meq/L": lambda v: v}),
    ObsDef("29463-7", "Body weight", "kg", "vital-signs",
           ("weight", "bodyweight", "wt"),
           {"[lb_av]": lambda v: v * 0.45359237, "g": lambda v: v / 1000}),
    ObsDef("8310-5", "Body temperature", "Cel", "vital-signs",
           ("temperature", "temp", "bodytemperature"),
           {"[degF]": lambda v: (v - 32) * 5 / 9}),
    ObsDef("8867-4", "Heart rate", "/min", "vital-signs",
           ("heartrate", "hr", "pulse", "pulserate")),
]
BLOOD_PRESSURE_ALIASES = {"bloodpressure", "bp", "85354-9"}

_OBS_BY_KEY: dict[str, ObsDef] = {}
for _d in OBSERVATIONS:
    _OBS_BY_KEY[_key(_d.loinc)] = _d
    for _a in _d.aliases:
        _OBS_BY_KEY[_a] = _d

# Free-text unit spellings -> UCUM
UNIT_ALIASES = {
    "%": "%", "percent": "%",
    "mg/dl": "mg/dL", "mgdl": "mg/dL",
    "mmol/l": "mmol/L", "mmoll": "mmol/L",
    "mmol/mol": "mmol/mol",
    "umol/l": "umol/L", "µmol/l": "umol/L", "μmol/l": "umol/L",
    "meq/l": "meq/L",
    "kg": "kg", "kgs": "kg", "kilograms": "kg",
    "g": "g", "grams": "g",
    "lb": "[lb_av]", "lbs": "[lb_av]", "pounds": "[lb_av]",
    "c": "Cel", "°c": "Cel", "degc": "Cel", "celsius": "Cel", "cel": "Cel",
    "f": "[degF]", "°f": "[degF]", "degf": "[degF]", "fahrenheit": "[degF]",
    "bpm": "/min", "/min": "/min", "beats/min": "/min", "beatsperminute": "/min",
    "mmhg": "mm[Hg]", "mm[hg]": "mm[Hg]",
}


def _ucum(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.strip().lower().replace(" ", "")
    return UNIT_ALIASES.get(u)


_VALUE_RE = re.compile(r"^\s*(<=|>=|<|>)?\s*(-?\d+(?:\.\d+)?)\s*(.*?)\s*$")


# ------------------------------------------------------------------ conditions

@dataclass(frozen=True)
class CondDef:
    snomed: str
    display: str
    icd10: str
    icd10_display: str
    aliases: tuple[str, ...]


CONDITIONS: list[CondDef] = [
    CondDef("44054006", "Diabetes mellitus type 2", "E11.9", "Type 2 diabetes mellitus without complications",
            ("type2diabetes", "type2diabetesmellitus", "diabetesmellitustype2", "t2dm", "dm2", "dmii", "niddm", "e119")),
    CondDef("46635009", "Diabetes mellitus type 1", "E10.9", "Type 1 diabetes mellitus without complications",
            ("type1diabetes", "type1diabetesmellitus", "diabetesmellitustype1", "t1dm", "dm1", "iddm", "e109")),
    CondDef("59621000", "Essential hypertension", "I10", "Essential (primary) hypertension",
            ("hypertension", "essentialhypertension", "htn", "highbloodpressure", "i10")),
    CondDef("55822004", "Hyperlipidemia", "E78.5", "Hyperlipidemia, unspecified",
            ("hyperlipidemia", "hyperlipidaemia", "hld", "highcholesterol", "e785")),
    CondDef("195967001", "Asthma", "J45.909", "Unspecified asthma, uncomplicated",
            ("asthma", "j45909")),
    CondDef("709044004", "Chronic kidney disease", "N18.9", "Chronic kidney disease, unspecified",
            ("chronickidneydisease", "ckd", "n189")),
]
_COND_BY_KEY = {a: d for d in CONDITIONS for a in (*d.aliases, d.snomed)}

# ----------------------------------------------------------------- medications

# RxNorm ingredient concepts, with common brand / international names.
MEDICATIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "metformin": ("6809", ("glucophage",)),
    "lisinopril": ("29046", ("zestril", "prinivil")),
    "atorvastatin": ("83367", ("lipitor",)),
    "amlodipine": ("17767", ("norvasc",)),
    "insulin glargine": ("274783", ("lantus", "basaglar", "toujeo", "glargine")),
    "albuterol": ("435", ("salbutamol", "ventolin", "proair")),
    "aspirin": ("1191", ("asa", "acetylsalicylicacid")),
}
_MED_BY_KEY: dict[str, str] = {}
for _name, (_code, _aliases) in MEDICATIONS.items():
    _MED_BY_KEY[_key(_name)] = _name
    _MED_BY_KEY[_code] = _name
    for _a in _aliases:
        _MED_BY_KEY[_key(_a)] = _name

_STRENGTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|units?|iu|ml)\b", re.I)
_STRENGTH_UCUM = {"mg": "mg", "mcg": "ug", "g": "g", "unit": "[iU]", "units": "[iU]", "iu": "[iU]", "ml": "mL"}

# ----------------------------------------------------------------------- dates

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
    "%Y%m%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y",
)


def normalize_date(value: Any) -> str | None:
    """Parse a date in common formats to ISO 8601. Slashed dates are read as US (MM/DD)."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        has_time = "%H" in fmt
        return parsed.strftime("%Y-%m-%dT%H:%M:%S") if has_time else parsed.date().isoformat()
    raise ValueError(f"unrecognized date {text!r}")


# --------------------------------------------------------------------- helpers

class RecordError(ValueError):
    pass


def _get(record: dict, *names: str) -> Any:
    lowered = {str(k).lower(): v for k, v in record.items()}
    for n in names:
        if lowered.get(n) not in (None, ""):
            return lowered[n]
    return None


def _rid(*parts: Any) -> str:
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in parts)))


def _round(v: float) -> float:
    return round(v, 2)


def _infer_type(record: dict) -> str:
    explicit = _get(record, "type", "kind", "resource", "category")
    if explicit:
        t = _key(explicit)
        if t in {"lab", "labs", "laboratory", "vital", "vitals", "vitalsigns", "observation", "result"}:
            return "observation"
        if t in {"condition", "diagnosis", "dx", "problem"}:
            return "condition"
        if t in {"medication", "med", "meds", "drug", "prescription", "rx"}:
            return "medication"
        raise RecordError(f"unknown record type {explicit!r}")
    if _get(record, "value", "result") is not None:
        return "observation"
    if _get(record, "drug", "medication", "med") is not None:
        return "medication"
    if _get(record, "diagnosis", "condition", "problem") is not None:
        return "condition"
    raise RecordError("cannot infer record type; set 'type' to lab, condition or medication")


def _subject(record: dict) -> tuple[str, dict]:
    pid = _get(record, "patient_id", "patientid", "patient", "mrn", "subject")
    if pid is None:
        raise RecordError("missing patient_id")
    pid = str(pid).strip()
    return pid, {"reference": f"urn:uuid:{_rid('Patient', pid)}"}


# ----------------------------------------------------------------- normalizers

def _observation(record: dict, pid: str, subject: dict) -> dict:
    name = _get(record, "code", "test", "name", "observation", "lab", "vital")
    if name is None:
        raise RecordError("observation is missing a test name or code")
    raw_value = _get(record, "value", "result")
    if raw_value is None:
        raise RecordError(f"observation {name!r} has no value")
    when = normalize_date(_get(record, "date", "effective", "collected", "datetime", "time"))
    key = _key(name)

    resource: dict[str, Any] = {
        "resourceType": "Observation",
        "status": "final",
        "subject": subject,
    }
    if when:
        resource["effectiveDateTime"] = when

    if key in {_key(a) for a in BLOOD_PRESSURE_ALIASES}:
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", str(raw_value))
        if not m:
            raise RecordError(f"blood pressure value {raw_value!r} is not in SYS/DIA form")
        resource.update(
            category=_category("vital-signs"),
            code=_cc(LOINC, "85354-9", "Blood pressure panel with all children optional"),
            component=[
                {"code": _cc(LOINC, "8480-6", "Systolic blood pressure"),
                 "valueQuantity": _qty(float(m.group(1)), "mm[Hg]")},
                {"code": _cc(LOINC, "8462-4", "Diastolic blood pressure"),
                 "valueQuantity": _qty(float(m.group(2)), "mm[Hg]")},
            ],
        )
        resource["id"] = _rid("Observation", pid, "85354-9", when, raw_value)
        return resource

    obs = _OBS_BY_KEY.get(key)
    if obs is None:
        raise RecordError(f"no LOINC mapping for observation {name!r}")

    m = _VALUE_RE.match(str(raw_value))
    if not m:
        raise RecordError(f"non-numeric value {raw_value!r} for {name!r}")
    comparator, number, inline_unit = m.group(1), float(m.group(2)), m.group(3)
    raw_unit = _get(record, "unit", "units", "uom") or inline_unit or None
    unit = _ucum(raw_unit) if raw_unit else obs.unit
    if unit is None:
        raise RecordError(f"unrecognized unit {raw_unit!r} for {name!r}")
    if unit != obs.unit:
        conv = (obs.convert or {}).get(unit)
        if conv is None:
            raise RecordError(f"cannot convert {raw_unit!r} to {obs.unit} for {name!r}")
        number = conv(number)

    quantity = _qty(_round(number), obs.unit)
    if comparator:
        quantity["comparator"] = comparator
    resource.update(
        category=_category(obs.category),
        code=_cc(LOINC, obs.loinc, obs.display, text=str(name)),
        valueQuantity=quantity,
    )
    resource["id"] = _rid("Observation", pid, obs.loinc, when, raw_value, raw_unit)
    return resource


def _condition(record: dict, pid: str, subject: dict) -> dict:
    name = _get(record, "code", "diagnosis", "condition", "problem", "name", "description")
    if name is None:
        raise RecordError("condition is missing a name or code")
    cond = _COND_BY_KEY.get(_key(name))
    if cond is None:
        raise RecordError(f"no SNOMED CT mapping for condition {name!r}")
    onset = normalize_date(_get(record, "onset", "date", "diagnosed", "recorded"))
    status = _key(_get(record, "status", "clinical_status") or "active")
    if status not in {"active", "recurrence", "relapse", "inactive", "remission", "resolved"}:
        status = "active"
    resource = {
        "resourceType": "Condition",
        "id": _rid("Condition", pid, cond.snomed, onset),
        "clinicalStatus": _cc(CONDITION_CLINICAL, status),
        "code": {
            "coding": [
                {"system": SNOMED, "code": cond.snomed, "display": cond.display},
                {"system": ICD10, "code": cond.icd10, "display": cond.icd10_display},
            ],
            "text": str(name),
        },
        "subject": subject,
    }
    if onset:
        resource["onsetDateTime"] = onset
    return resource


def _medication(record: dict, pid: str, subject: dict) -> dict:
    text = _get(record, "code", "medication", "drug", "med", "name", "description")
    if text is None:
        raise RecordError("medication is missing a name")
    text = str(text)
    ingredient = _MED_BY_KEY.get(_key(text))
    if ingredient is None:
        # Try word windows: "Metformin HCl 500mg tab" -> "metformin"
        words = re.findall(r"[A-Za-z]+", text)
        for size in (2, 1):
            for i in range(len(words) - size + 1):
                ingredient = _MED_BY_KEY.get(_key("".join(words[i:i + size])))
                if ingredient:
                    break
            if ingredient:
                break
    if ingredient is None:
        raise RecordError(f"no RxNorm mapping for medication {text!r}")
    rxcui = MEDICATIONS[ingredient][0]
    when = normalize_date(_get(record, "date", "start", "started", "effective", "prescribed"))
    status = _key(_get(record, "status") or "active")
    status = {"onhold": "on-hold", "nottaken": "not-taken"}.get(status, status)
    if status not in {"active", "completed", "stopped", "on-hold", "intended", "not-taken"}:
        status = "active"

    resource: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "id": _rid("MedicationStatement", pid, rxcui, when, text),
        "status": status,
        "medicationCodeableConcept": _cc(RXNORM, rxcui, ingredient, text=text),
        "subject": subject,
    }
    if when:
        resource["effectiveDateTime"] = when
    dosage: dict[str, Any] = {}
    sig = _get(record, "sig", "frequency", "instructions", "dosage")
    if sig:
        dosage["text"] = str(sig)
    strength = _STRENGTH_RE.search(" ".join(str(x) for x in (text, _get(record, "dose", "strength") or "")))
    if strength:
        unit = _STRENGTH_UCUM[strength.group(2).lower()]
        dosage["doseAndRate"] = [{"doseQuantity": _qty(float(strength.group(1)), unit)}]
    if dosage:
        resource["dosage"] = [dosage]
    return resource


def _cc(system: str, code: str, display: str | None = None, text: str | None = None) -> dict:
    coding = {"system": system, "code": code}
    if display:
        coding["display"] = display
    cc: dict[str, Any] = {"coding": [coding]}
    if text:
        cc["text"] = text
    return cc


def _qty(value: float, unit: str) -> dict:
    return {"value": value, "unit": unit, "system": UCUM, "code": unit}


def _category(code: str) -> list[dict]:
    display = {"laboratory": "Laboratory", "vital-signs": "Vital Signs"}[code]
    return [_cc(OBS_CATEGORY, code, display)]


_NORMALIZERS = {"observation": _observation, "condition": _condition, "medication": _medication}


def normalize_records(records: list[dict]) -> dict:
    """Normalize raw records into a FHIR R4 collection Bundle.

    Returns {"bundle": Bundle, "issues": [...], "normalized": n, "failed": n}.
    """
    entries: list[dict] = []
    issues: list[dict] = []
    patients: dict[str, dict] = {}

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append({"index": index, "severity": "error", "message": "record must be an object"})
            continue
        try:
            pid, subject = _subject(record)
            resource = _NORMALIZERS[_infer_type(record)](record, pid, subject)
        except ValueError as exc:  # RecordError, or e.g. an unparseable date
            issues.append({"index": index, "severity": "error", "message": str(exc)})
            continue
        patients.setdefault(pid, {
            "resourceType": "Patient",
            "id": _rid("Patient", pid),
            "identifier": [{"system": PATIENT_ID_SYSTEM, "value": pid}],
        })
        entries.append({"fullUrl": f"urn:uuid:{resource['id']}", "resource": resource})

    patient_entries = [{"fullUrl": f"urn:uuid:{p['id']}", "resource": p} for p in patients.values()]
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": patient_entries + entries}
    return {"bundle": bundle, "issues": issues, "normalized": len(entries), "failed": len(issues)}
