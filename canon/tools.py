"""Agent-facing tools. One definition, exported as Anthropic tools, OpenAI
functions, or MCP tools, and dispatched by `call_tool`."""

from __future__ import annotations

import base64

from .service import Canon, CanonError

_PID = {"type": "string", "description": "Canon patient id (pat_...)"}

TOOLS = [
    {
        "name": "ingest_document",
        "description": "Add a clinical document for normalization: fax/OCR text, PDF, HL7 v2, C-CDA XML, FHIR JSON, "
                       "X12 837 claim or portal CSV. Format is auto-detected. The patient is matched by demographics "
                       "unless patient_id is given. Returns the document id, matched patient and extraction counts.",
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["content"],
            "properties": {
                "content": {"type": "string", "description": "Document content; base64 when encoding=base64"},
                "encoding": {"type": "string", "enum": ["text", "base64"], "description": "Default text"},
                "filename": {"type": "string"},
                "format": {"type": "string", "enum": ["fhir", "hl7v2", "ccda", "x12_837", "csv", "pdf", "text"]},
                "patient_id": _PID,
                "source_name": {"type": "string", "description": "Who sent it, e.g. 'Quest Diagnostics'"},
            },
        },
    },
    {
        "name": "list_patients",
        "description": "List patients with normalized records (id, name, birth date, number of source documents).",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "get_patient_summary",
        "description": "Compact, current view of a patient across all sources: active problems, active and "
                       "stopped medications, allergies, latest labs/vitals with trends, immunizations and "
                       "cross-source conflicts. Start here.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["patient_id"],
                         "properties": {"patient_id": _PID}},
    },
    {
        "name": "get_patient_record",
        "description": "Full canonical record with standard codes (ICD-10, SNOMED, LOINC, RxNorm, CVX, CPT), "
                       "canonical units, confidence and per-item source citations. Use `sections` to limit size.",
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["patient_id"],
            "properties": {
                "patient_id": _PID,
                "sections": {"type": "array", "items": {"type": "string", "enum": [
                    "patient", "conditions", "medications", "allergies", "observations", "procedures",
                    "immunizations", "encounters", "coverage", "conflicts", "unmapped", "sources"]}},
            },
        },
    },
    {
        "name": "get_observations",
        "description": "Lab and vital results in canonical units, oldest to newest. Filter by names "
                       "(e.g. ['a1c', 'ldl', 'blood pressure']) or LOINC codes, and by date.",
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["patient_id"],
            "properties": {
                "patient_id": _PID,
                "names": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "get_medications",
        "description": "Reconciled medication list with dose, route, frequency, status and change history.",
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["patient_id"],
            "properties": {"patient_id": _PID,
                           "status": {"type": "string", "enum": ["active", "stopped", "all"]}},
        },
    },
    {
        "name": "get_conflicts",
        "description": "Disagreements between sources (medication doses, allergies vs 'NKDA', demographics, lab "
                       "values). Check before acting on any medication, allergy or identity fact.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["patient_id"],
                         "properties": {"patient_id": _PID}},
    },
    {
        "name": "get_provenance",
        "description": "Where a fact came from: source documents, formats, locations and verbatim snippets. "
                       "Use to cite evidence or let a human verify an item by id.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["patient_id", "item_id"],
                         "properties": {"patient_id": _PID, "item_id": {"type": "string"}}},
    },
    {
        "name": "export_fhir",
        "description": "Export the canonical record as a FHIR R4 Bundle.",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["patient_id"],
                         "properties": {"patient_id": _PID}},
    },
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def anthropic_tools() -> list[dict]:
    return [dict(t) for t in TOOLS]


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in TOOLS]


def mcp_tools() -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in TOOLS]


def call_tool(canon: Canon, name: str, args: dict, actor: str = "agent") -> dict:
    """Run a tool; errors come back as {"error": {...}} so agents can recover."""
    try:
        return _dispatch(canon, name, args or {}, actor)
    except CanonError as e:
        return {"error": {"code": e.code, "message": e.message}}


def _dispatch(canon: Canon, name: str, a: dict, actor: str) -> dict:
    if name == "ingest_document":
        content = a["content"]
        data = base64.b64decode(content) if a.get("encoding") == "base64" else content.encode("utf-8")
        r = canon.ingest(data, filename=a.get("filename"), fmt=a.get("format"), patient_id=a.get("patient_id"),
                         source_name=a.get("source_name"), actor=actor)
        return r
    if name == "list_patients":
        return {"patients": canon.list_patients()}
    if name == "get_patient_summary":
        return canon.summary(a["patient_id"], actor=actor)
    if name == "get_patient_record":
        rec = canon.record(a["patient_id"], actor=actor)
        if a.get("sections"):
            keep = set(a["sections"]) | {"object", "patient_id"}
            if "allergies" in keep:
                keep.add("allergy_status")
            rec = {k: v for k, v in rec.items() if k in keep}
        return rec
    if name == "get_observations":
        return {"observations": canon.observations(a["patient_id"], a.get("names"), a.get("since"))}
    if name == "get_medications":
        meds = canon.record(a["patient_id"], actor=actor)["medications"]
        st = a.get("status", "all")
        return {"medications": [m for m in meds if st == "all" or m["status"] == st]}
    if name == "get_conflicts":
        return {"conflicts": canon.record(a["patient_id"], actor=actor)["conflicts"]}
    if name == "get_provenance":
        rec = canon.record(a["patient_id"], actor=actor)
        for section in ("conditions", "medications", "allergies", "observations", "procedures", "immunizations",
                        "encounters", "coverage"):
            for item in rec[section]:
                if item.get("id") == a["item_id"]:
                    return {"item_id": a["item_id"], "section": section, "sources": item.get("sources", []),
                            "confidence": item.get("confidence")}
        raise CanonError("not_found", f"No item {a['item_id']} in this patient's record.")
    if name == "export_fhir":
        return canon.fhir(a["patient_id"])
    raise CanonError("unknown_tool", f"No tool named {name}")
