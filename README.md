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
| `GET /v1/audit`, `GET /v1/audit/verify` | Hash-chained log of every ingest and record read (append-only in Postgres) |
| `POST /v1/signup`, `GET /v1/account` | Create an account + API key; status and usage |
| `POST /v1/billing/checkout`, `POST /v1/billing/portal` | Stripe Checkout / Billing Portal links |
| `POST /v1/stripe/webhook`, `GET /v1/billing/sync` | Stripe events; cron retry for usage reporting |

### Hosting on Vercel

`app.py` exposes the API as a WSGI `app` for Vercel's Python runtime (`vercel.json`
selects the `python` preset). Deploy with `vercel deploy --prod`. It runs in one of
two modes:

- **Production** (`DATABASE_URL` set): Supabase Postgres, per-customer API keys
  and Stripe billing. See [Hosting and billing](#hosting-vercel--supabase-and-billing-stripe).
- **Demo** (no `DATABASE_URL`): SQLite in `/tmp`, which is per instance and wiped
  on cold starts. Each new instance reloads `samples/maria_chen`, so patient IDs
  change between instances and uploads are not kept. Set `CANON_API_KEYS` to
  require a static bearer key (otherwise the demo is open). Set
  `CANON_SEED_SAMPLES=0` to skip the sample load.

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

## Hosting (Vercel + Supabase) and billing (Stripe)

The hosted API runs as one Python serverless function on Vercel (`app.py`,
WSGI) in production mode. Data lives in Supabase Postgres in a private `canon` schema
(`migrations/001_init.sql`, already applied to the `primaryaihackathon`
Supabase project). Customers pay per normalized document through Stripe
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
* The first `CANON_FREE_DOCUMENTS` (default 25) are free and never billed.
* After the free tier, ingest returns `402 payment_required` until the customer finishes Stripe Checkout.
  Reads are never blocked.
* Each new document (duplicates are free) sends one Stripe meter event, idempotent by usage id.
  Failed sends are retried by a daily Vercel cron (`/v1/billing/sync`).
* Stripe webhooks activate accounts (`checkout.session.completed`) and handle `past_due` / `canceled`.

**Deploy checklist** (one time)

1. **Stripe**: create the meter and metered price (default $0.10 per document):
   `STRIPE_SECRET_KEY=sk_test_... python -m canon.billing setup --price-cents 10` → note `price_id`.
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
  store.py         SQLite / Postgres + hash-chained audit log
  billing.py       accounts, API keys, quota, Stripe metering + webhooks
app.py             Vercel entry point         migrations/   Postgres schema
samples/maria_chen/  one patient across 7 messy sources
tests/               end-to-end and unit tests
```

## Status and next steps

This is a hackathon prototype. The terminology tables are a curated starter set
covering common primary-care concepts. Production would load full
UMLS/RxNorm/LOINC/SNOMED vocabularies behind the same functions. Next steps:
a probabilistic patient-matching index, 835/NCPDP claims, a review UI for
conflicts and unmapped facts, and per-agent delegated access grants on reads.
