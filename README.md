# Canon: clinical data normalization for AI agents

Healthcare data arrives as faxes, PDFs, HL7 v2 feeds, C-CDA documents, FHIR
bundles, claims and portal exports. **Canon turns all of it into one canonical,
coded, cited patient record that an agent can reason over safely.**

```
fax / PDF / HL7 v2 / C-CDA / FHIR / X12 837 / portal CSV
        │  detect format → parse → extract (rules, + Claude for scans)
        ▼
   facts with provenance  (what was seen, where, how, how confident)
        │  terminology: ICD-10, SNOMED, LOINC, RxNorm, CVX, CPT + UCUM unit conversion
        ▼
   reconcile across sources  (dedupe, current state, conflicts, claims-only flags)
        ▼
   canonical patient record → summary · tools · MCP · FHIR R4 export
```

## Why agents need this, not just "the records"

An agent that reads seven raw documents will miss things or invent them. Canon
gives it:

| Problem in raw data | What Canon returns |
|---|---|
| Same A1c arrives by fax (`7.8 %`), HL7 (`4548-4`) and portal CSV | **One** observation listing all three sources |
| Hospital reports A1c as `60 mmol/mol`, the lab reports `%` | Canonical units (`7.64 %`) plus the original value |
| HL7 uses LOINC `2089-1`, the fax says "LDL" | Both collapse to `13457-7` |
| Fax lists metformin 500 mg; the plan says "increase to 1000 mg"; the portal still says 500 mg | Current dose 1000 mg, the full change history, **and a `medication_discrepancy` conflict** |
| Hospital CCD says NKDA; fax says penicillin (hives); derm letter says sulfa | `has_allergies` **plus an `allergy_vs_nkda` conflict** |
| Claim bills F41.1 (anxiety) with no clinical note | `evidence: claims_only`, `status: unknown` |
| "Father had type 2 diabetes", "Denies chest pain" | Excluded (family history and negation handling) |
| OCR noise: `Metformin 5OO mg`, `A1c 7.l` | Repaired, with lower confidence |
| A misfiled document with another patient's DOB | `demographic_mismatch` conflict + ingest warning |
| A claim the LLM can't quote from the source | Kept, but `evidence_verified: false` and low confidence |

Every item carries `sources[]` (document, format, locator such as `OBX[2]` or
`line 31`, the verbatim snippet) and a `confidence` score. Nothing that can't
be mapped is dropped: it lands in `unmapped[]` with a reason.

## Quickstart (Python 3.10+, no dependencies)

```bash
# Normalize the 7 sample documents for one patient and print the agent summary
python -m canon normalize samples/maria_chen/*

# Full record or FHIR R4 Bundle
python -m canon normalize --view record samples/maria_chen/*
python -m canon normalize --view fhir   samples/maria_chen/*

# Tests
python -m unittest discover -s tests -v
```

### HTTP API

```bash
python -m canon serve --port 8080          # set CANON_API_KEYS="key:client" to require auth

curl -X POST 'localhost:8080/v1/documents?filename=labs.hl7' --data-binary @samples/maria_chen/02_quest_labs.hl7
curl localhost:8080/v1/patients/<patient_id>/summary
curl 'localhost:8080/v1/patients/<patient_id>/observations?names=a1c,ldl'
curl localhost:8080/v1/patients/<patient_id>/fhir
curl 'localhost:8080/v1/tools?format=openai'      # or anthropic | mcp
curl localhost:8080/v1/audit/verify               # tamper-evident access log
```

| Endpoint | Purpose |
|---|---|
| `POST /v1/documents` | Raw body in any format (auto-detected), or JSON `{content, encoding, filename, patient_id, source_name, use_llm}` |
| `GET /v1/documents/{id}?items=1` | Extraction details for one document |
| `GET /v1/patients` | Patients with normalized records |
| `GET /v1/patients/{id}/summary` | Compact, agent-ready view: active problems, meds, allergies, latest labs with trends, conflicts |
| `GET /v1/patients/{id}/record` | Full canonical record with codes, provenance and confidence |
| `GET /v1/patients/{id}/observations` | Labs/vitals in canonical units (`names=`, `since=`) |
| `GET /v1/patients/{id}/fhir` | FHIR R4 Bundle export |
| `GET /v1/tools`, `POST /v1/tools/{name}` | Agent tool schemas and execution |
| `GET /v1/audit`, `GET /v1/audit/verify` | Hash-chained log of every ingest and record read |

### Hosting on Vercel

`app.py` exposes the HTTP API as a WSGI `app` for Vercel's Python runtime
(`vercel.json` selects the `python` preset). Deploy with `vercel deploy --prod`.

- Set `CANON_API_KEYS` in the Vercel project; without it the API is open.
- The SQLite database lives in `/tmp`, which is per instance and wiped on cold
  starts. Each new instance reloads `samples/maria_chen`, so patient IDs change
  between instances and uploaded documents are not kept. Use a hosted database
  for anything durable. Set `CANON_SEED_SAMPLES=0` to skip the sample load.

### For agents

**MCP** (Claude Code, Claude Desktop, any MCP client):

```bash
claude mcp add canon -- python -m canon mcp --db canon.db
```

**Tools** (same definitions everywhere): `ingest_document`, `list_patients`,
`get_patient_summary`, `get_patient_record`, `get_observations`,
`get_medications`, `get_conflicts`, `get_provenance`, `export_fhir`.

**Claude agent example**: loads the samples and answers a clinical question
with tool calls:

```bash
pip install anthropic && export ANTHROPIC_API_KEY=...
python examples/claude_agent.py "Is it safe to prescribe amoxicillin? How is her diabetes trending?"
```

### Scanned faxes (LLM extraction)

Text-layer PDFs and OCR text go through the deterministic rule engine. Image-only
PDFs (true scanned faxes) are sent to Claude when `anthropic` is installed and
credentials are set (`use_llm=1` forces it for any text/PDF). The model must
return schema-valid JSON with a verbatim evidence quote for each fact. Quotes
are checked against the source, and codes are assigned by Canon's terminology
layer, never trusted from the model.

## Supported inputs

| Format | Parser | Notes |
|---|---|---|
| FHIR R4 JSON | `parsers/fhir.py` | Bundle or single resource; Patient, Condition, MedicationRequest/Statement, AllergyIntolerance, Observation (incl. components), Procedure, Immunization, Encounter, Coverage |
| HL7 v2.x | `parsers/hl7v2.py` | PID, PV1, DG1, PRB, AL1, OBR/OBX, RXE/RXO/RXD/RXR/TQ1, RXA, IN1 |
| C-CDA XML | `parsers/ccda.py` | Problems, medications, allergies (incl. negated NKA), results, vitals, immunizations, procedures, encounters |
| X12 837P | `parsers/x12.py` | Subscriber demographics, payer, HI diagnoses, SV1 lines, service dates (marked as claims evidence) |
| Portal CSV | `parsers/csv_portal.py` | Header synonyms; value+unit split |
| PDF | `parsers/pdf.py` | Stdlib text-layer extraction; scans go to the LLM |
| Fax / OCR / notes | `parsers/text.py` | Sections, negation, family history, sig parsing, OCR repair, lab/vital values |

## Layout

```
canon/
  parsers/         format detection + one parser per format (+ llm.py)
  terminology.py   code systems, synonyms, unit conversion, frequencies
  normalize.py     fact → canonical item (or unmapped with reason)
  reconcile.py     cross-source merge, current state, conflicts
  service.py       ingest, patient matching, record/summary queries
  fhir_export.py   canonical → FHIR R4
  tools.py         agent tool definitions + dispatcher
  api.py           HTTP API      mcp_server.py   MCP over stdio
  store.py         SQLite + hash-chained audit log
samples/maria_chen/  one patient across 7 messy sources
tests/               end-to-end and unit tests
```

## Status and next steps

This is a hackathon prototype. The terminology tables are a curated starter set
covering common primary-care concepts. Production would load full
UMLS/RxNorm/LOINC/SNOMED vocabularies behind the same functions. Next steps:
a probabilistic patient-matching index, 835/NCPDP claims, a review UI for
conflicts and unmapped facts, and per-agent delegated access grants on reads.
