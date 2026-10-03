import pytest

from app.normalize import CONDITIONS, MEDICATIONS, OBSERVATIONS, UNIT_ALIASES, normalize_records


def only(records):
    result = normalize_records(records)
    assert result["issues"] == [], result["issues"]
    return [e["resource"] for e in result["bundle"]["entry"] if e["resource"]["resourceType"] != "Patient"][0]


def test_at_least_100_of_each():
    assert len(OBSERVATIONS) >= 100
    assert len(CONDITIONS) >= 100
    assert len(MEDICATIONS) >= 100


def test_every_canonical_and_convertible_unit_is_parseable():
    reachable = set(UNIT_ALIASES.values())
    for obs in OBSERVATIONS:
        assert obs.unit in reachable, (obs.loinc, obs.unit)
        for unit in obs.convert:
            assert unit in reachable, (obs.loinc, unit)


@pytest.mark.parametrize("test,value,unit,loinc,expected", [
    ("Hgb", "135", "g/L", "718-7", 13.5),
    ("WBC", "7.2", "x10^9/L", "6690-2", 7.2),
    ("PLT", "250", "K/uL", "777-3", 250),
    ("Hct", "0.42", "L/L", "4544-3", 42),
    ("Triglycerides", "1.7", "mmol/L", "2571-8", 150.57),
    ("LDL-C", "2.6", "mmol/L", "13457-7", 100.54),
    ("25-OH Vitamin D", "75", "nmol/L", "62292-8", 30.05),
    ("TSH", "2.1", "uIU/mL", "3016-3", 2.1),
    ("Troponin I", "15", "ng/L", "10839-9", 0.015),
    ("D-dimer", "0.5", "ug/mL FEU", "48065-7", 500),
    ("PaCO2", "5.3", "kPa", "2019-8", 39.75),
    ("Height", "70", "in", "8302-2", 177.8),
    ("Calcium", "2.4", "mmol/L", "17861-6", 9.62),
    ("Total bilirubin", "17.1", "umol/L", "1975-2", 1.0),
    ("Vitamin B12", "300", "pmol/L", "2132-9", 406.5),
    ("eGFR", "58", "mL/min/1.73m2", "62238-1", 58),
    ("INR", "2.5", None, "6301-6", 2.5),
    ("SpO2", "97%", None, "59408-5", 97),
    ("Resp rate", "18", "breaths/min", "9279-1", 18),
])
def test_new_observations_and_conversions(test, value, unit, loinc, expected):
    rec = {"patient_id": "p1", "type": "lab", "test": test, "value": value}
    if unit:
        rec["unit"] = unit
    obs = only([rec])
    assert obs["code"]["coding"][0]["code"] == loinc
    assert obs["valueQuantity"]["value"] == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("text,snomed,icd10", [
    ("CHF", "84114007", "I50.9"),
    ("AFib", "49436004", "I48.91"),
    ("Parkinson's disease", "49049000", "G20.A1"),
    ("COVID-19", "840539006", "U07.1"),
    ("GERD", "235595009", "K21.9"),
    ("MDD", "370143000", "F32.9"),
    ("PCOS", "237055002", "E28.2"),
])
def test_new_conditions(text, snomed, icd10):
    codes = [c["code"] for c in only([{"patient_id": "p1", "type": "dx", "diagnosis": text}])["code"]["coding"]]
    assert codes == [snomed, icd10]


@pytest.mark.parametrize("text,rxcui", [
    ("Eliquis 5mg BID", "1364430"),
    ("Insulin Aspart 100 unit/mL pen", "51428"),
    ("Heparin 5000 units SC q8h", "5224"),
    ("Vitamin D3 1000 IU daily", "2418"),
    ("cyanocobalamin 1000 mcg", "11248"),
    ("Isosorbide mononitrate ER 30 mg", "28004"),
    ("Isosorbide dinitrate 10 mg", "6058"),
    ("Metformin 596 mg", "6809"),  # numbers in free text never match an RxCUI
    ("Ozempic 0.5 mg weekly", "1991302"),
    ("paracetamol 500mg", "161"),
    ("1364430", "1364430"),  # a bare RxCUI is accepted
])
def test_new_medications(text, rxcui):
    med = only([{"patient_id": "p1", "type": "rx", "drug": text}])
    assert med["medicationCodeableConcept"]["coding"][0]["code"] == rxcui
