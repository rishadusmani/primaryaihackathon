"""Normalize messy clinical records into canonical FHIR R4 resources.

Input is a list of loosely structured dicts (as an EHR export, a CSV row or an
LLM extraction might produce). Output is a FHIR R4 collection Bundle coded to
standard vocabularies (LOINC, SNOMED CT, ICD-10-CM, RxNorm, UCUM), plus a list
of issues for anything that could not be mapped. Unmappable records are
reported, never guessed.

Vocabularies live in app/vocab/*.json (100+ codes each for observations,
conditions and medications), generated and verified against NLM and SNOMED
sources by scripts/build_vocab.py. Add codes there, not here.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


# ----------------------------------------------------------------- vocabularies
# Built and verified against NLM / SNOMED sources by scripts/build_vocab.py.

_VOCAB = Path(__file__).parent / "vocab"


def _load(name: str) -> list[dict]:
    return json.loads((_VOCAB / name).read_text())


@dataclass(frozen=True)
class ObsDef:
    loinc: str
    display: str
    unit: str  # canonical UCUM unit
    category: str  # "laboratory" | "vital-signs"
    aliases: tuple[str, ...]
    # Other UCUM unit -> factor (v * f) or [scale, offset] into the canonical unit.
    convert: dict[str, float | list[float]]

    def to_canonical(self, value: float, unit: str) -> float | None:
        if unit == self.unit:
            return value
        rule = self.convert.get(unit)
        if rule is None:
            return None
        scale, offset = rule if isinstance(rule, list) else (rule, 0.0)
        return value * scale + offset


@dataclass(frozen=True)
class CondDef:
    snomed: str
    display: str
    icd10: str
    icd10_display: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class MedDef:
    rxcui: str
    name: str
    tty: str
    aliases: tuple[str, ...]


def _index(defs: list, code_attr: str, kind: str) -> dict:
    index: dict[str, Any] = {}
    for d in defs:
        for k in {_key(a) for a in (*d.aliases, getattr(d, code_attr))}:
            if k in index and index[k] is not d:
                raise ValueError(f"{kind} alias {k!r} is ambiguous")
            index[k] = d
    return index


OBSERVATIONS = [ObsDef(**{**o, "aliases": tuple(o["aliases"])}) for o in _load("observations.json")]
CONDITIONS = [CondDef(**{**c, "aliases": tuple(c["aliases"])}) for c in _load("conditions.json")]
MEDICATIONS = [MedDef(**{**m, "aliases": tuple(m["aliases"])}) for m in _load("medications.json")]
BLOOD_PRESSURE_ALIASES = {"bloodpressure", "bp", "853549"}

_OBS_BY_KEY: dict[str, ObsDef] = _index(OBSERVATIONS, "loinc", "observation")
_COND_BY_KEY: dict[str, CondDef] = _index(CONDITIONS, "snomed", "condition")
_MED_BY_KEY: dict[str, MedDef] = _index(MEDICATIONS, "name", "medication")
_MED_BY_RXCUI: dict[str, MedDef] = {m.rxcui: m for m in MEDICATIONS}
_MED_MAX_WORDS = max(len(re.findall(r"[a-z0-9]+", a.lower())) for m in MEDICATIONS for a in (*m.aliases, m.name))

# Free-text unit spellings (lowercased, spaces removed) -> UCUM
UNIT_ALIASES = {
    "%": "%", "percent": "%", "pct": "%",
    "mg/dl": "mg/dL", "mgdl": "mg/dL",
    "g/dl": "g/dL", "gm/dl": "g/dL", "g/l": "g/L",
    "mmol/l": "mmol/L", "mmoll": "mmol/L", "mmol/mol": "mmol/mol",
    "umol/l": "umol/L", "µmol/l": "umol/L", "μmol/l": "umol/L", "micromol/l": "umol/L",
    "nmol/l": "nmol/L", "pmol/l": "pmol/L",
    "meq/l": "meq/L",
    "u/l": "U/L", "iu/l": "U/L", "units/l": "U/L",
    "ng/ml": "ng/mL", "ng/dl": "ng/dL", "ng/l": "ng/L",
    "ug/l": "ug/L", "mcg/l": "ug/L", "µg/l": "ug/L",
    "ug/dl": "ug/dL", "mcg/dl": "ug/dL", "µg/dl": "ug/dL",
    "ug/ml": "ug/mL", "mcg/ml": "ug/mL", "µg/ml": "ug/mL",
    "pg/ml": "pg/mL", "mg/l": "mg/L", "mg/g": "mg/g", "mg/gcr": "mg/g", "mg/gcreat": "mg/g",
    "miu/l": "m[IU]/L", "uiu/ml": "m[IU]/L", "µiu/ml": "m[IU]/L", "μiu/ml": "m[IU]/L", "mu/l": "m[IU]/L",
    "miu/ml": "m[IU]/mL",
    "10*3/ul": "10*3/uL", "10^3/ul": "10*3/uL", "x10^3/ul": "10*3/uL", "x10e3/ul": "10*3/uL",
    "k/ul": "10*3/uL", "k/mcl": "10*3/uL", "thou/ul": "10*3/uL", "10*3/mm3": "10*3/uL", "/nl": "10*3/uL",
    "10*9/l": "10*9/L", "10^9/l": "10*9/L", "x10^9/l": "10*9/L", "x10e9/l": "10*9/L",
    "10*6/ul": "10*6/uL", "10^6/ul": "10*6/uL", "x10^6/ul": "10*6/uL", "x10e6/ul": "10*6/uL",
    "m/ul": "10*6/uL", "mil/ul": "10*6/uL", "/pl": "10*6/uL",
    "10*12/l": "10*12/L", "10^12/l": "10*12/L", "x10^12/l": "10*12/L", "x10e12/l": "10*12/L",
    "l/l": "L/L", "fl": "fL", "pg": "pg",
    "s": "s", "sec": "s", "secs": "s", "seconds": "s",
    "{inr}": "{INR}", "inr": "{INR}", "ratio": "{ratio}", "{ratio}": "{ratio}",
    "mm/h": "mm/h", "mm/hr": "mm/h", "mm/hour": "mm/h",
    "mosm/kg": "mosm/kg", "mosmol/kg": "mosm/kg", "mmol/kg": "mosm/kg",
    "ml/min/1.73m2": "mL/min/{1.73_m2}", "ml/min/1.73m^2": "mL/min/{1.73_m2}", "ml/min": "mL/min/{1.73_m2}",
    "ml/min/{1.73_m2}": "mL/min/{1.73_m2}",
    "kg": "kg", "kgs": "kg", "kilograms": "kg", "g": "g", "grams": "g",
    "lb": "[lb_av]", "lbs": "[lb_av]", "pounds": "[lb_av]", "[lb_av]": "[lb_av]",
    "cm": "cm", "m": "m", "in": "[in_i]", "inch": "[in_i]", "inches": "[in_i]", "[in_i]": "[in_i]", '"': "[in_i]",
    "kg/m2": "kg/m2", "kg/m^2": "kg/m2",
    "c": "Cel", "°c": "Cel", "degc": "Cel", "celsius": "Cel", "cel": "Cel",
    "f": "[degF]", "°f": "[degF]", "degf": "[degF]", "fahrenheit": "[degF]", "[degf]": "[degF]",
    "bpm": "/min", "/min": "/min", "beats/min": "/min", "breaths/min": "/min", "br/min": "/min", "perminute": "/min",
    "mmhg": "mm[Hg]", "mm[hg]": "mm[Hg]", "kpa": "kPa",
    "{score}": "{score}", "score": "{score}", "/10": "{score}",
    "[ph]": "[pH]", "ph": "[pH]", "1": "1",
}


def _ucum(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.strip().lower().replace(" ", "")
    if u.endswith("feu") or u.startswith("feu"):  # D-dimer "ng/mL FEU": FEU lives in the code
        u = u.replace("feu", "")
    return UNIT_ALIASES.get(u)


_VALUE_RE = re.compile(r"^\s*(<=|>=|<|>)?\s*(-?\d+(?:\.\d+)?)\s*(.*?)\s*$")

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
    converted = obs.to_canonical(number, unit)
    if converted is None:
        raise RecordError(f"cannot convert {raw_unit!r} to {obs.unit} for {name!r}")
    number = converted

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
    med = _MED_BY_KEY.get(_key(text)) or _MED_BY_RXCUI.get(text.strip())
    if med is None:
        # Longest word window first: "Insulin Aspart 100 unit/mL pen" -> insulin aspart,
        # "Glucophage XR 500mg tab" -> metformin
        words = re.findall(r"[A-Za-z0-9]+", text)
        for size in range(min(_MED_MAX_WORDS, len(words)), 0, -1):
            for i in range(len(words) - size + 1):
                med = _MED_BY_KEY.get(_key("".join(words[i:i + size])))
                if med:
                    break
            if med:
                break
    if med is None:
        raise RecordError(f"no RxNorm mapping for medication {text!r}")
    rxcui, ingredient = med.rxcui, med.name
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
