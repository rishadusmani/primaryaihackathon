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

## Try it in the browser

Open **https://primaryaihackathon.vercel.app** (or `python -m canon serve` → http://localhost:8080), click
**Load sample patient**, and explore conflicts, coded problems, reconciled medications and lab trends. Click any
item to see the exact source line it came from. The page uses the public playground API
(`POST /v1/playground/normalize`), which processes documents in memory only: nothing is stored or billed, and
LLM extraction is off.

Then click **Load tricky note**: a cardiology note and a FHIR export written to fool keyword matchers. It's full of
phrases like "influenza vaccine given", "low sodium diet", "potassium chloride 20 mEq" and "Troponin I 15 ng/L",
plus codes that aren't in Canon's tables (Entresto, `I50.22`). A panel lists each phrase, what a context-free
keyword matcher would code, and what Canon recorded. `tests/test_live_terminology.py` checks every one of those
claims against the engine, so the demo can't drift from the code.

## Quickstart (Python 3.10+, no dependencies)

```bash
# Normalize the 7 sample documents for one patient and print the agent summary
python -m canon normalize samples/maria_chen/*

# Full record or FHIR R4 Bundle
python -m canon normalize --view record samples/maria_chen/*
python -m canon normalize --view fhir   samples/maria_chen/*

# Tests (add CANON_TEST_DATABASE_URL=postgresql://... to also run them on Postgres)
python -m unittest discover -s tests -v
```

### HTTP API

```bash
python -m canon serve --port 8080          # sandbox: no auth, no billing

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
| `GET /v1/audit`, `GET /v1/audit/verify` | Hash-chained log of every ingest and record read (append-only in Postgres) |
| `POST /v1/signup`, `GET /v1/account` | Create an account + API key; status and usage |
| `POST /v1/billing/checkout`, `POST /v1/billing/portal` | Stripe Checkout / Billing Portal links |
| `POST /v1/stripe/webhook`, `GET /v1/billing/sync` | Stripe events; cron retry for usage reporting |
| `GET /v1/usage?days=30` | This account's agent usage: requests, errors, latency, documents, patients, LLM tokens |
| `GET /dashboard` | Customer usage dashboard (HTML) |

### Usage dashboard

Open `/dashboard` (e.g. `http://localhost:8080/dashboard`) and sign in with an
API key. Customers see only their own account's usage over the last 7, 30 or 90 days:

- requests, error rate, p50/p95 latency, documents ingested, distinct patients
  accessed, and LLM tokens spent reading scanned documents
- requests per day (successful vs. errors), with a table view
- a breakdown by endpoint and agent tool (`patients.summary`, `tool.get_conflicts`, ...)
- HTTP API vs. MCP traffic, and the 50 most recent requests

Every authenticated agent request is one row in `api_requests`, keyed by account.
Account, billing and `/v1/usage` calls are not counted. This is observability,
separate from the billable `usage_events`. MCP tool calls are metered to the MCP
server's account in its own database. On Postgres, apply
`migrations/002_api_requests.sql` after `001_init.sql`, and (when the API connects as `canon_app`)
`migrations/003_app_role_api_requests.sql` after `002_app_role.sql`.

### Hosting on Vercel

`app.py` exposes the API as a WSGI `app` for Vercel's Python runtime (`vercel.json`
selects the `python` preset). Deploy with `vercel deploy --prod`. It runs in one of
two modes:

- **Production** (`DATABASE_URL` set): Supabase Postgres, per-customer API keys
  and Stripe billing. See [Hosting and billing](#hosting-vercel--supabase-and-billing-stripe).
- **Demo** (no `DATABASE_URL`): SQLite in `/tmp`, which is per instance and wiped
  on cold starts (usage history and the dashboard reset with it). Each new instance reloads `samples/maria_chen`, so patient IDs
  change between instances and uploads are not kept. Set `CANON_API_KEYS` to
  require a static bearer key (otherwise the demo is open). Set
  `CANON_SEED_SAMPLES=0` to skip the sample load.

### For agents

**Remote MCP** (hosted, nothing to install). The API serves MCP over Streamable HTTP
at `POST /mcp`, using the same API key, data, billing and usage metering as the REST API:

```bash
claude mcp add --transport http canon https://primaryaihackathon.vercel.app/mcp \
  --header "Authorization: Bearer cn_live_..."
```

Any client that speaks Streamable HTTP and can send an `Authorization` header works
(Claude Code, the Agent SDK, the MCP SDKs). The server is stateless and answers each
POST with JSON, with no sessions or SSE stream, so it runs fine on serverless.

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
  model is Claude Haiku 4.5, the cheapest (`CANON_LLM_MODEL` overrides it). A full OCR engine
  is too large for a Vercel function, so there is no server-side OCR. The model must
return schema-valid JSON with a verbatim evidence quote for each fact. Quotes
are checked against the source, and codes are assigned by Canon's terminology
layer, never trusted from the model.

## Hosting (Vercel + Supabase) and billing (Stripe)

The hosted API runs as one Python serverless function on Vercel (`app.py`,
WSGI) in production mode. Data lives in Supabase Postgres in a private `canon` schema
(`migrations/001_init.sql`, already applied to the `primaryaihackathon`
Supabase project). Customers pay per normalized page through Stripe
usage-based billing.

**How customers use it**

```bash
curl -X POST https://<app>/v1/signup -d '{"name":"Acme Clinic","email":"ops@acme.com"}'
# -> {"api_key": "cn_live_...", "checkout_url": "https://checkout.stripe.com/...", ...}

curl -X POST https://<app>/v1/documents -H "Authorization: Bearer cn_live_..." --data-binary @labs.hl7
curl https://<app>/v1/account -H "Authorization: Bearer cn_live_..."            # status + usage
curl -X POST https://<app>/v1/billing/portal -H "Authorization: Bearer cn_live_..." # invoices, card, cancel
```

* Every account's patients, documents and audit entries are isolated from every other account.
* API keys are stored only as SHA-256 hashes.
* Billing is per page: real pages for PDFs, ~3,000-character pages for text/OCR uploads, and 1 per
  structured document (HL7, FHIR, C-CDA, X12, CSV), which have no pages. `CANON_LLM_PAGE_UNITS` (default 1)
  can make pages read by Claude (scanned faxes) count as more than one unit.
* There is no free tier by default (`CANON_FREE_PAGES=0`): ingest returns `402 payment_required` until the
  customer finishes Stripe Checkout. While Stripe isn't configured, ingest isn't gated.
* Accounts with status `sandbox` (the demo, and deliberately exempted accounts such as the hackathon judges)
  are never gated or billed; their pages are still recorded for the dashboard.
  Reads are never blocked.
* Each new document (duplicates are free) sends one Stripe meter event with its page count, idempotent by usage id.
  Failed sends are retried by a daily Vercel cron (`/v1/billing/sync`).
* Stripe webhooks activate accounts (`checkout.session.completed`) and handle `past_due` / `canceled`.

**Deploy checklist** (one time)

1. **Stripe**: create the meter and metered price (default $0.05 per page):
   `STRIPE_SECRET_KEY=sk_test_... python -m canon.billing setup --price-cents 5` → note `price_id`.
   To create them by hand instead: a meter with event name `canon_document_normalized`, Sum aggregation,
   customer key `stripe_customer_id` and value key `value`; then a monthly usage-based price on it per unit (page).
   Then add a webhook endpoint `https://<app>/v1/stripe/webhook` for `checkout.session.completed`,
   `customer.subscription.created|updated|deleted` and `invoice.payment_failed` → note its `whsec_` secret.
   Finally, enable the Customer Portal (Settings → Billing → Customer portal).
2. **Supabase**: copy the *Transaction pooler* connection string
   (Project Settings → Database → Connect, port 6543) for `DATABASE_URL`.
3. **Vercel**: import this repo (framework preset *Other*) and set the variables in
   `.env.example` (`DATABASE_URL`, `STRIPE_*`, `CANON_PUBLIC_URL`, `CRON_SECRET`), then deploy.
4. Smoke test: `curl https://<app>/healthz` should show `"billing_enabled": true`. Then sign up and complete a test Checkout with card `4242 4242 4242 4242`.

Use Stripe test keys until you're ready, then swap in live keys and re-run `setup` in live mode.

> **PHI warning:** Don't send real patient data to the hosted service until BAAs are in place with
> every processor in the path (Vercel Enterprise, Supabase HIPAA add-on, Anthropic if LLM extraction
> is on). Until then use synthetic data like `samples/`.

Local development stays dependency-free: `python -m canon serve` runs in sandbox mode (no auth, no
billing, SQLite). `python -m canon serve --require-auth` turns on keys and billing locally.

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
| Conditions | 120 | ICD-10-CM + SNOMED CT |
| Medications | 132 | RxNorm ingredients, with brand names and drug classes |
| Labs and vitals | 105 | LOINC, with a canonical UCUM unit and SI ↔ conventional conversions |

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

89 of the 105 labs and vitals carry an adult reference range, so values are flagged `low`/`high`/`normal`.
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
  store.py         SQLite / Postgres + hash-chained audit log
  billing.py       accounts, API keys, quota, Stripe metering + webhooks
  usage.py         per-request API metering + aggregates for /v1/usage
  dashboard.html   customer usage dashboard served at /dashboard
app.py             Vercel entry point         migrations/   Postgres schema
scripts/build_vocab.py  regenerates and verifies canon/vocab/ online
samples/maria_chen/  one patient across 7 messy sources
samples/tricky_cardiology/  the tricky-note demo (traps for keyword matchers)
tests/               end-to-end and unit tests
```

## Status and next steps

This is a hackathon prototype. Production would load full
UMLS/RxNorm/LOINC/SNOMED vocabularies behind the same functions. Next steps:
a probabilistic patient-matching index, 835/NCPDP claims, a review UI for
conflicts and unmapped facts, and per-agent delegated access grants on reads.
