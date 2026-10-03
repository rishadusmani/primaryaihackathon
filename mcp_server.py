"""MCP server exposing the normalizer to agents (Claude Desktop, Claude Code, etc.).

It calls the HTTP API with NORMALIZER_API_KEY, so every tool call is metered
and billed like any other API call.

    NORMALIZER_API_URL=http://localhost:8000 NORMALIZER_API_KEY=cn_... python mcp_server.py
"""

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

API_URL = os.getenv("NORMALIZER_API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("NORMALIZER_API_KEY", "")

server = MCPServer(
    name="clinical-normalizer",
    instructions=(
        "Use normalize_clinical_records before reasoning over clinical data: it turns messy "
        "labs, vitals, diagnoses and medications into canonical FHIR R4 coded with LOINC, "
        "SNOMED CT, ICD-10-CM, RxNorm and UCUM units. Each successful call is billed."
    ),
)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


@server.tool()
def normalize_clinical_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize raw clinical records into a FHIR R4 Bundle.

    Each record is a loose object with a patient_id plus one of:
    - lab/vital: {"type": "lab", "test": "HbA1c", "value": "7.2 %", "date": "03/02/2024"}
    - condition: {"type": "condition", "diagnosis": "T2DM", "onset": "2019-05-01"}
    - medication: {"type": "medication", "drug": "Glucophage 500mg tab", "sig": "BID"}
    Values in non-canonical units (mmol/L glucose, lb, °F, ...) are converted.
    Returns the bundle, per-record issues for anything unmappable, and billing info.
    """
    resp = httpx.post(
        f"{API_URL}/v1/normalize",
        json={"records": records},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


@server.tool()
def get_usage() -> dict[str, Any]:
    """Return how many billed calls this API key has made and the estimated cost."""
    resp = httpx.get(f"{API_URL}/v1/usage", headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    server.run()
