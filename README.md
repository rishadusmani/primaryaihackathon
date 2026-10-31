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
| Allergist note: "penicillin allergy delabeled after negative amoxicillin challenge" | Penicillin `status: resolved` with its date; a later copied-forward list re-adding it raises `allergy_resolution_disputed` |
| A fax with no visit date, only "Electronically signed 04/01/2026" | Dated by when it was generated (signature, fax header, PDF/CCD/FHIR/HL7/X12 timestamp), with `date_basis: generated` |
| Admission med list at 08:05 says metformin 500 mg; discharge list at 16:40 the same day says 1000 mg | Current dose 1000 mg with a timed history, not a same-day conflict (an untimed same-day source still raises one) |
| A document with no date anywhere lists an older dose | Ranked oldest, so it can't override dated notes, plus a low-severity `undated_source` flag |
| Claim bills F41.1 (anxiety) with no clinical note | `evidence: claims_only`, `status: unknown` |
| "Father had type 2 diabetes", "Denies chest pain" | Excluded (family history and negation handling) |
| OCR noise: `Metformin 5OO mg`, `A1c 7.l` | Repaired, with lower confidence |
| A misfiled document with another patient's DOB | `demographic_mismatch` conflict + ingest warning |
| A claim the LLM can't quote from the source | Kept, but `evidence_verified: false` and low confidence |

Every item carries `sources[]` (document, format, locator such as `OBX[2]` or
`line 31`, the verbatim snippet) and a `confidence` score. Nothing that can't
be mapped is dropped: it lands in `unmapped[]` with a reason.

## Try it in the browser

Run `python -m canon serve`, open http://localhost:8080, click
**Load sample patient**, and explore conflicts, coded problems, reconciled medications and lab trends. Click any
item to see the exact source line it came from. The page uses the public playground API
(`POST /v1/playground/normalize`), which processes documents in memory only: nothing is stored, and
LLM extraction is off.

Then click **Load tricky note**: a cardiology note and a FHIR export written to fool keyword matchers. It's full of
phrases like "influenza vaccine given", "low sodium diet", "potassium chloride 20 mEq" and "Troponin I 15 ng/L",
plus a code that isn't in Canon's tables (`I50.22`). A panel lists each phrase, what a context-free
keyword matcher would code, and what Canon recorded. `tests/test_live_terminology.py` checks every one of those
claims against the engine, so the demo can't drift from the code.

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
python -m canon serve --port 8080          # open on localhost; set CANON_API_KEYS to require a key

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
| `POST /mcp` | MCP server (Streamable HTTP) exposing the same tools |
| `GET /v1/audit`, `GET /v1/audit/verify` | Hash-chained log of every ingest and record read |

**Authentication.** Without configuration the API is open, so it binds to
`127.0.0.1` by default. To expose it, set `CANON_API_KEYS="key1,key2"`; every API
and MCP call then needs `Authorization: Bearer <key>`. The web demo, `/about`,
the playground and `/healthz` stay public.

**Deploying.** `App.wsgi` is a standard WSGI app, so any WSGI server works, for example
`gunicorn 'canon.api:App().wsgi'`. Data lives in one SQLite file (`--db`, or `CANON_DB`).

### For agents

**Remote MCP.** The HTTP API serves MCP over Streamable HTTP at `POST /mcp`, with
the same data as the REST API. The server is stateless and answers each POST with JSON.

```bash
claude mcp add --transport http canon http://127.0.0.1:8080/mcp
# with CANON_API_KEYS set:
claude mcp add --transport http canon https://<your-host>/mcp --header "Authorization: Bearer <key>"
```

**Local MCP** (stdio, your own SQLite file):

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

### Scanned faxes (OCR and LLM extraction)

Text-layer PDFs and OCR text go through the deterministic rule engine, which also
repairs common OCR errors. Scanned (image-only) PDFs are handled the cheapest way available:

- **On the device.** The web demo OCRs scanned PDFs in the browser with Tesseract.js
  and sends the text, so the scan never reaches a model. Agents can do the same: send
  OCR text instead of the PDF.
- **On the server.** A scanned PDF uploaded to the API is sent to Claude when `anthropic`
  is installed and credentials are set (`use_llm=1` forces it for any text/PDF). The default
  model is Claude Haiku 4.5, the cheapest (`CANON_LLM_MODEL` overrides it). There is no
  server-side OCR engine, to keep the install free of dependencies. The model must
return schema-valid JSON with a verbatim evidence quote for each fact. Quotes
are checked against the source, and codes are assigned by Canon's terminology
layer, never trusted from the model.

### Uncertain conditions (assertion review)

The rule engine holds back a condition when a cue in the same clause makes it unsure the
condition applies to the patient now. The cue can come before or after the term:

| Cue | Examples |
|---|---|
| Negation | "denies asthma", "no history of HTN", "depression screen negative", "resolved" |
| Someone else | "mother had type 2 diabetes", "diabetes runs in the family" |
| Hedge | "possible pneumonia", "screen for depression", "asthma vs COPD" |

Phrases like "no improvement in", "not well controlled" and "not at goal" are not negation, so
"No improvement in hypertension" keeps hypertension.

Without a model, held-back conditions stay out of the record. When `OPENAI_API_KEY` is set,
those sentences (and only those, at most 25 per document, in one request) go to GPT-5.6 Luna
(`CANON_ASSERTION_MODEL` overrides it). It labels each one present, historical, absent,
hypothetical or other_person, quoting the deciding words. Only present or historical puts the
condition back, and a quote that isn't in the sentence discards the label, so the model can
restore what the rules dropped but never adds a condition the rules didn't find. If the call
fails, the rules' result stands and the document gets a warning. Tokens are counted in
`llm_usage`. `CANON_ASSERTION=0` turns it off.

## Patient data

Use synthetic data like `samples/` for testing. Before you process real patient
data, make sure your deployment meets your legal duties (for example HIPAA). If
LLM extraction or assertion review is on, documents go to Anthropic or OpenAI.

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

## Vocabulary

| | Codes | Systems |
|---|---|---|
| Conditions | 757 | ICD-10-CM + SNOMED CT |
| Medications | 925 | RxNorm ingredients and 113 combination products (Augmentin, Entresto, Advair...), with brand names and drug classes |
| Labs and vitals | 204 | LOINC, with a canonical UCUM unit and SI ↔ conventional conversions |
| Vaccines | 40 | CVX |

- **Where it lives:** the hand-written tables in `terminology.py` cover the core primary-care concepts and win on any conflict. `canon/vocab/*.json` extends them.
- **How it's built:** `scripts/build_vocab.py` generates the JSON and checks every code online before writing:
  - LOINC and ICD-10-CM against NLM Clinical Tables (ICD-10-CM must be a current billable code);
  - SNOMED CT against tx.fhir.org (must be active);
  - RxNorm against RxNav (brand names must map to the ingredient);
  - drug classes from RxClass (FDA Established Pharmacologic Class).
- **Display names** come from those sources, not from hand-typed text.
- **To add codes:** edit the lists in the script and run `python scripts/build_vocab.py`. `--check` verifies without writing.
- **Exact-only phrases:** Canon's text parser scans documents for synonym phrases, so phrases that are ambiguous in prose map only when they are a whole field. Examples:
  - "influenza", so "influenza vaccine given" isn't a diagnosis;
  - "low sodium", so "low sodium diet" isn't hyponatremia;
  - "calcium" and "chloride", so "Calcium 600 mg" and "potassium chloride 20 mEq" aren't lab results;
  - symptoms like "fever", so a mention in the HPI doesn't fill the problem list.

### Reference ranges

89 of the 204 labs and vitals carry an adult reference range, so values are flagged `low`/`high`/`normal`.
A flag sent by the source (e.g. HL7 `OBX-8`) always wins.

- **Source:** ranges added by the vocabulary come from the
  [ABIM Laboratory Test Reference Ranges, January 2026](https://www.abim.org/media/e2wdwdqu/laboratory-reference-ranges.pdf).
  Each entry in `canon/vocab/observations.json` records the ABIM wording it came from.
- **Sex-specific ranges** use the outer bounds of both sexes, as the hand-written hemoglobin range does.
- **No single range, no flag:** PSA, cortisol, hCG, NT-proBNP, hs-CRP and testosterone get no range rather than a misleading one.

### Live terminology lookup

When a code or name isn't in Canon's tables, `canon/live_terminology.py` asks the National Library of Medicine
before marking a fact unmapped:

| Input | Service | Example |
|---|---|---|
| ICD-10-CM code, or the exact official title | NLM Clinical Tables (billable codes only) | `I50.22` → chronic systolic heart failure |
| RxCUI, generic, brand or combination | NLM RxNav, with FDA drug class from RxClass | Entresto → sacubitril / valsartan |
| LOINC code | NLM Clinical Tables | `2947-0` → sodium in blood (value kept in the unit sent) |

- **Only verifiable matches are accepted.** A fuzzy RxNav match counts only if every ingredient it resolves to is named in the original text. "metfromin" stays unmapped rather than becoming the wrong drug.
- **Live items are marked.** They carry `terminology: "live_lookup"`, and the demo shows a **live lookup** chip. NLM's terms don't allow its name in application labels, so the marker is generic.
- **Lookups are bounded:**
  - 2.5 s timeout per call;
  - an in-process cache;
  - a circuit breaker after repeated failures;
  - at most 10 s of lookups per document (`CANON_LIVE_BUDGET`).

  If NLM is slow or down, facts simply stay `unmapped` as before.
- **Turning it off:** set `CANON_LIVE_TERMINOLOGY=0`.

## Data sources and attributions

This product uses publicly available data from the U.S. National Library of Medicine (NLM), National Institutes of Health, Department of Health and Human Services; NLM is not responsible for the product and does not endorse or recommend this or any other product.

- **RxNorm, RxClass:** drug codes and classes, via NLM's RxNav APIs. Free to use, with a limit of 20 requests per second per IP address. Canon caches results (NLM recommends 12–24 hours).
- **ICD-10-CM:** published by the CDC (public domain), searched through NLM's Clinical Table Search Service.
- **LOINC:** This material contains content from LOINC® (https://loinc.org). LOINC is copyright © Regenstrief Institute, Inc. and the Logical Observation Identifiers Names and Codes (LOINC) Committee and is available at no cost under the license at https://loinc.org/terms-of-use. LOINC® is a registered United States trademark of Regenstrief Institute, Inc.
- **UCUM:** unit codes are subject to a license from Regenstrief Institute, Inc. and The UCUM Organization, available at https://unitsofmeasure.org. The UCUM table and UCUM codes are copyright © 1995-2009, Regenstrief Institute, Inc. and the Unified Codes for Units of Measures (UCUM) Organization.
- **SNOMED CT:** SNOMED CT® is a registered trademark of SNOMED International. Codes are built in only; they are checked when the vocabulary is built, never looked up live.
- **Reference ranges:** [ABIM Laboratory Test Reference Ranges, January 2026](https://www.abim.org/media/e2wdwdqu/laboratory-reference-ranges.pdf).

## Layout

```
canon/
  parsers/         format detection + one parser per format (+ llm.py)
  terminology.py   code systems, synonyms, unit conversion, frequencies
  vocab/*.json     verified LOINC / SNOMED CT / ICD-10-CM / RxNorm tables (generated)
  live_terminology.py  live RxNorm / ICD-10-CM / LOINC fallback for codes not in the tables
  normalize.py     fact → canonical item (or unmapped with reason)
  reconcile.py     cross-source merge, current state, conflicts
  service.py       ingest, patient matching, record/summary queries
  fhir_export.py   canonical → FHIR R4
  tools.py         agent tool definitions + dispatcher
  api.py           HTTP API      mcp_server.py   MCP over stdio
  store.py         SQLite + hash-chained audit log
  web/             browser demo and about page
scripts/build_vocab.py  regenerates and verifies canon/vocab/ online
samples/maria_chen/  one patient across 7 messy sources
samples/tricky_cardiology/  the tricky-note demo (traps for keyword matchers)
tests/               end-to-end and unit tests
```

## Status and next steps

Canon started as a hackathon prototype. Production use would load full
UMLS/RxNorm/LOINC/SNOMED vocabularies behind the same functions. Next steps:
a probabilistic patient-matching index, 835/NCPDP claims, a review UI for
conflicts and unmapped facts, and per-agent delegated access grants on reads.

Contributions are welcome. Run `python -m unittest discover -s tests` before you open a pull request.

## License

MIT. See [LICENSE](LICENSE). The vocabulary content keeps its own terms; see
[Data sources and attributions](#data-sources-and-attributions).
