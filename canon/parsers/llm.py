"""LLM extraction pass with Claude, for scanned faxes (image-only PDFs) and
free text the rule engine can't fully map.

Guardrails that keep this safe to put under an agent:
* Structured outputs: the model must return JSON matching EXTRACTION_SCHEMA.
* Evidence required: every fact carries a verbatim quote. For text inputs we
  check the quote really occurs in the source; facts whose quote is missing
  are kept but marked `evidence_verified: false` with low confidence.
* Codes are not trusted from the model: terminology mapping happens in
  normalize.py against our vocabularies, same as for every other source.

Optional: only used when the `anthropic` package is installed and credentials
are available (ANTHROPIC_API_KEY or an `ant auth login` profile).
"""

from __future__ import annotations

import base64
import json
import os
import re

from ..model import fact

MODEL = os.environ.get("CANON_LLM_MODEL", "claude-opus-5-5")

_NULLABLE_STR = {"type": ["string", "null"]}
EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["patient", "facts"],
    "properties": {
        "patient": {
            "type": "object", "additionalProperties": False,
            "required": ["given_name", "family_name", "birth_date", "sex", "mrn"],
            "properties": {"given_name": _NULLABLE_STR, "family_name": _NULLABLE_STR,
                           "birth_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                           "sex": _NULLABLE_STR, "mrn": _NULLABLE_STR},
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "name", "value", "unit", "date", "dose", "route", "frequency", "status",
                             "reaction", "evidence"],
                "properties": {
                    "kind": {"type": "string", "enum": ["condition", "medication", "allergy", "no_known_allergies",
                                                        "observation", "procedure", "immunization"]},
                    "name": {"type": "string", "description": "Name as written (condition, drug, test, allergen)"},
                    "value": {"type": ["string", "null"], "description": "Observation result value"},
                    "unit": _NULLABLE_STR,
                    "date": {"type": ["string", "null"], "description": "YYYY-MM-DD if stated"},
                    "dose": _NULLABLE_STR, "route": _NULLABLE_STR, "frequency": _NULLABLE_STR,
                    "status": {"type": ["string", "null"], "enum": ["active", "stopped", "resolved", None]},
                    "reaction": _NULLABLE_STR,
                    "evidence": {"type": "string", "description": "Exact verbatim quote from the document"},
                },
            },
        },
    },
}

SYSTEM = (
    "You extract clinical facts about the patient from medical documents (faxes, letters, notes, lab reports) "
    "for a data-normalization pipeline. Extract only facts that apply to this patient: skip family history, "
    "negated findings (\"denies chest pain\"), and hypotheticals. Medications that are stopped or discontinued "
    "get status \"stopped\". Copy names, values and units exactly as written; do not convert units or invent "
    "codes. Every fact must include an exact verbatim quote from the document as evidence. If the document "
    "states no known allergies, emit one fact of kind no_known_allergies."
)


def available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("CANON_LLM_FORCE"))


def _call(content_blocks: list[dict]) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content_blocks}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Extraction declined by the model: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Extraction output was truncated (max_tokens); split the document and retry.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def extract(*, text: str | None = None, pdf: bytes | None = None) -> list[dict]:
    blocks: list[dict] = []
    if pdf is not None:
        blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                      "data": base64.standard_b64encode(pdf).decode()}})
    if text:
        blocks.append({"type": "text", "text": f"<document>\n{text}\n</document>"})
    blocks.append({"type": "text", "text": "Extract all patient clinical facts from the document above."})
    data = _call(blocks)
    return to_facts(data, source_text=text)


def to_facts(data: dict, source_text: str | None = None) -> list[dict]:
    """Convert the model's JSON into Canon facts (separate for testability)."""
    haystack = _norm_ws(source_text) if source_text else None
    out: list[dict] = []
    p = data.get("patient") or {}
    if any(p.values()):
        out.append(fact("patient", locator="llm:patient", method="llm",
                        name_given=p.get("given_name"), name_family=p.get("family_name"), dob=p.get("birth_date"),
                        sex=(p.get("sex") or "").lower() or None,
                        identifiers=[{"system": "MRN", "value": p["mrn"]}] if p.get("mrn") else None))
    for i, f in enumerate(data.get("facts", [])):
        ev = f.get("evidence") or ""
        verified = None if haystack is None else (_norm_ws(ev) in haystack if ev else False)
        conf = 0.85 if verified in (True, None) else 0.4
        common = dict(locator=f"llm:fact[{i}]", method="llm", snippet=ev, confidence=conf)
        extra = {"evidence_verified": verified} if verified is not None else {}
        k = f["kind"]
        if k == "no_known_allergies":
            out.append(fact("allergy", no_known_allergies=True, **common, **extra))
        elif k == "condition":
            out.append(fact("condition", text=f["name"], status=f.get("status") or "active", onset=f.get("date"),
                            **common, **extra))
        elif k == "medication":
            sig = " ".join(filter(None, [f.get("dose"), f.get("route"), f.get("frequency")]))
            out.append(fact("medication", text=f["name"], dose_text=f.get("dose"), route_text=f.get("route") or sig,
                            frequency_text=f.get("frequency"), status=f.get("status") or "active",
                            start=f.get("date"), **common, **extra))
        elif k == "allergy":
            out.append(fact("allergy", text=f["name"], reaction=f.get("reaction"), **common, **extra))
        elif k == "observation":
            out.append(fact("observation", text=f["name"], value=f.get("value"), unit=f.get("unit"),
                            effective=f.get("date"), **common, **extra))
        elif k == "procedure":
            out.append(fact("procedure", text=f["name"], date=f.get("date"), **common, **extra))
        elif k == "immunization":
            out.append(fact("immunization", text=f["name"], date=f.get("date"), **common, **extra))
    return out
