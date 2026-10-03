"""The two data shapes in Canon.

1. **Fact**: what a parser saw in one source, in raw form, plus where it saw it.
   Every parser (FHIR, HL7 v2, C-CDA, X12, CSV, fax/PDF text, LLM) emits facts.

       {"kind": "observation", "text": "HbA1c", "value": "7.8", "unit": "%",
        "effective": "2026-03-02", "provenance": {"locator": "OBX[2]", "snippet": "...",
        "method": "structured"}, "confidence": 0.98}

2. **Canonical record**: facts after terminology mapping, unit conversion,
   de-duplication and reconciliation across every source for one patient.
   This is the object agents reason over (see CANONICAL_RECORD_DOC).

Dates on a fact: a kind's own dates (`onset`, `start`, `effective`, `recorded`) say when
something happened; `as_of` says when the source asserted it (the visit a note line sits
under, a C-CDA entry's author time, the FHIR encounter a resource belongs to). A
`document` fact carries `generated`: when the document itself was produced (signed,
faxed, exported), the fallback when nothing in it is dated.
"""

from __future__ import annotations

KINDS = ("document", "patient", "condition", "medication", "allergy", "observation", "procedure", "immunization",
         "encounter", "coverage")

# Prior confidence by extraction method; parsers may lower it per fact.
METHOD_CONFIDENCE = {"structured": 0.98, "claims": 0.80, "rule_nlp": 0.80, "llm": 0.85}


def fact(kind: str, *, locator: str, method: str, snippet: str | None = None,
         confidence: float | None = None, **fields) -> dict:
    assert kind in KINDS, kind
    clean = {k: v for k, v in fields.items() if v not in (None, "", [], {})}
    return {
        "kind": kind,
        **clean,
        "provenance": {"locator": locator, "method": method, **({"snippet": snippet[:300]} if snippet else {})},
        "confidence": round(confidence if confidence is not None else METHOD_CONFIDENCE[method], 3),
    }


CANONICAL_RECORD_DOC = {
    "patient": "Demographics merged across sources: name, birth_date, sex, identifiers[], addresses[], phones[]",
    "conditions[]": "id, display, codes{icd10, snomed}, status (active|resolved|unknown), onset, first_seen, "
                    "last_seen, history[] (dated statements, oldest first), sources[], confidence",
    "medications[]": "id, ingredient, display, codes{rxnorm}, drug_class, strength, dose, route, "
                     "frequency{code, per_day, display}, status (active|stopped|unknown), first_seen, "
                     "last_changed, history[] (dated statements, oldest first), sources[], confidence",
    "allergies[]": "id, substance, codes{snomed}, reactions[], severity, status (active|resolved|refuted), "
                   "resolved_on, history[], sources[]. Only active allergies count toward allergy_status",
    "observations[]": "id, display, codes{loinc}, category (lab|vital), value, unit (canonical UCUM), "
                      "original{value, unit}, interpretation (low|normal|high), effective, sources[]",
    "procedures[]": "id, display, codes{cpt}, date, sources[]",
    "immunizations[]": "id, vaccine, codes{cvx}, date, sources[]",
    "encounters[]": "id, type, date, provider, facility, reason, sources[]",
    "coverage[]": "payer, member_id, group, sources[]",
    "conflicts[]": "Disagreements between sources the agent should surface rather than silently resolve, and "
                   "undated sources that can't be placed in time",
    "sources[]": "Every document that contributed: id, format, source name, received_at, document_date, "
                 "date_basis (clinical: dated by its contents | generated: by when it was signed/exported)",
    "unmapped[]": "Facts we extracted but could not map to a standard code (never silently dropped)",
    "terminology": "On conditions, medications and observations: \"live_lookup\" when the code came from a live "
                   "public-terminology lookup (RxNorm, ICD-10-CM, LOINC) rather than Canon's built-in tables",
}
