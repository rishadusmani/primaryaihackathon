import json
from pathlib import Path

import pytest

from app.normalize import normalize_date, normalize_records

SAMPLES = json.loads((Path(__file__).parent.parent / "samples" / "messy_records.json").read_text())["records"]


def resources(result, rtype):
    return [e["resource"] for e in result["bundle"]["entry"] if e["resource"]["resourceType"] == rtype]


def one(records):
    result = normalize_records(records)
    assert result["issues"] == [], result["issues"]
    return [e["resource"] for e in result["bundle"]["entry"] if e["resource"]["resourceType"] != "Patient"][0]


def test_sample_file_normalizes_with_only_the_unknown_lab_failing():
    result = normalize_records(SAMPLES)
    assert result["normalized"] == 20
    assert result["failed"] == 1
    assert "Vitamin Q" in result["issues"][0]["message"]
    assert len(resources(result, "Patient")) == 3


def test_hba1c_maps_to_loinc_and_us_date():
    obs = one([{"patient_id": "p1", "type": "lab", "test": "HbA1c", "value": "7.2 %", "date": "03/02/2024"}])
    assert obs["code"]["coding"][0]["code"] == "4548-4"
    assert obs["valueQuantity"] == {"value": 7.2, "unit": "%", "system": "http://unitsofmeasure.org", "code": "%"}
    assert obs["effectiveDateTime"] == "2024-03-02"
    assert obs["category"][0]["coding"][0]["code"] == "laboratory"


@pytest.mark.parametrize("test,value,unit,expected,canonical", [
    ("Glucose", "8.1", "mmol/L", 145.93, "mg/dL"),
    ("creat", "97", "umol/L", 1.1, "mg/dL"),
    ("Wt", "212 lbs", None, 96.16, "kg"),
    ("Temp", "101.3 F", None, 38.5, "Cel"),
    ("a1c", "53", "mmol/mol", 7.0, "%"),
])
def test_unit_conversion_to_canonical(test, value, unit, expected, canonical):
    rec = {"patient_id": "p1", "type": "lab", "test": test, "value": value}
    if unit:
        rec["unit"] = unit
    q = one([rec])["valueQuantity"]
    assert q["unit"] == canonical
    assert q["value"] == pytest.approx(expected, abs=0.01)


def test_comparator_is_preserved():
    q = one([{"patient_id": "p1", "test": "glucose", "value": "<70 mg/dL"}])["valueQuantity"]
    assert q["comparator"] == "<" and q["value"] == 70


def test_blood_pressure_split_into_components():
    obs = one([{"patient_id": "p1", "type": "vital", "name": "BP", "value": "138/88"}])
    assert obs["code"]["coding"][0]["code"] == "85354-9"
    assert [c["valueQuantity"]["value"] for c in obs["component"]] == [138, 88]


def test_condition_gets_snomed_and_icd10():
    cond = one([{"patient_id": "p1", "type": "dx", "diagnosis": "T2DM", "onset": "May 1, 2019"}])
    codes = {c["system"].rsplit("/", 1)[-1]: c["code"] for c in cond["code"]["coding"]}
    assert codes == {"sct": "44054006", "icd-10-cm": "E11.9"}
    assert cond["onsetDateTime"] == "2019-05-01"


def test_brand_medication_maps_to_rxnorm_ingredient_with_strength():
    med = one([{"patient_id": "p1", "type": "rx", "drug": "Glucophage 500mg tab", "sig": "BID"}])
    assert med["medicationCodeableConcept"]["coding"][0]["code"] == "6809"
    assert med["dosage"][0]["doseAndRate"][0]["doseQuantity"]["value"] == 500
    assert med["dosage"][0]["text"] == "BID"


def test_type_is_inferred_when_missing():
    assert one([{"patient_id": "p1", "drug": "lisinopril 10 mg"}])["resourceType"] == "MedicationStatement"


@pytest.mark.parametrize("record,fragment", [
    ({"type": "lab", "test": "HbA1c", "value": "7"}, "missing patient_id"),
    ({"patient_id": "p1", "type": "lab", "test": "glucose", "value": "5", "unit": "furlongs"}, "unrecognized unit"),
    ({"patient_id": "p1", "type": "lab", "test": "HbA1c", "value": "7", "unit": "mg/dL"}, "cannot convert"),
    ({"patient_id": "p1", "type": "rx", "drug": "unobtainium"}, "no RxNorm mapping"),
    ({"patient_id": "p1", "type": "lab", "test": "HbA1c", "value": "7", "date": "yesterday"}, "unrecognized date"),
])
def test_unmappable_records_are_reported_not_guessed(record, fragment):
    result = normalize_records([record])
    assert result["normalized"] == 0
    assert fragment in result["issues"][0]["message"]


def test_output_is_deterministic():
    assert normalize_records(SAMPLES) == normalize_records(SAMPLES)


def test_iso_datetime_kept():
    assert normalize_date("2024-03-02T08:30:00") == "2024-03-02T08:30:00"
