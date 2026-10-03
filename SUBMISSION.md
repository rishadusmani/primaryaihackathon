# Supabase Select 2026 Hackathon — submission draft (Primary AI)

> Draft answers to paste into the form. Fields marked TODO need something only you have.

## Project name
Canon

## Tagline (one line)
Turns messy faxes, PDFs, HL7, FHIR, claims and portal exports into one clean, coded, cited patient record that AI agents can safely act on.

## Short description (~280 characters)
Healthcare data arrives as faxes, PDFs, HL7 feeds, C-CDA, FHIR, claims and CSVs that contradict each other. Canon normalizes all of it into one patient record: standard codes, canonical units, duplicates merged, conflicts flagged, every fact cited. Agents use it via API or MCP.

## Full description

**The problem.** AI agents are being asked to do real work in healthcare: book visits, prep referrals, check meds. But the data they get is a mess. Much of it, especially referrals, still arrives by fax or PDF. Lab feeds, hospital summaries, patient portals and insurance claims each describe the same patient differently. Even "standard" data disagrees: two LOINC codes for the same test, A1c in mmol/mol versus %, a medication dose changed in one system but not another. An agent reading seven raw documents will miss things or make them up.

**What Canon does.** You send Canon any clinical document, in any of 7 formats, and it returns one canonical patient record:

- **Every format in.** Fax/OCR text, PDF, HL7 v2, C-CDA, FHIR R4, X12 837 claims and portal CSVs. The format is detected automatically, and documents are matched to the right patient by demographics.
- **Standard codes out.** ICD-10, SNOMED CT, LOINC, RxNorm, CVX and CPT, with units converted to UCUM (e.g. A1c 60 mmol/mol → 7.64 %).
- **Reconciled across sources.** The same A1c arriving by fax, HL7 and the portal becomes *one* result with three sources. Medication changes ("increase metformin to 1000 mg") update the current dose and keep the history.
- **Conflicts surfaced, never hidden.** Examples: "Hospital says no known allergies, the fax says penicillin." "The portal still shows 500 mg." A diagnosis that appears only on a billing claim is flagged as such. Agents are told to check before acting.
- **Every fact is cited.** Each item links to the source document, the exact location (`OBX[2]`, `line 31`) and a verbatim snippet, with a confidence score. Nothing is silently dropped: anything that can't be mapped is listed with a reason.
- **Clinically careful text extraction.** Negation ("denies chest pain"), family history ("father had diabetes"), OCR repair ("5OO mg" → 500 mg) and medication start/stop/increase intent are all handled.
- **See it in the browser.** A one-page demo: load the 7-document sample patient (or drop in your own files), see conflicts first, then coded problems, reconciled medications with change history, and lab trends. Click any item to see the exact line it came from.
- **Built for agents.** A token-efficient patient summary, 9 tools in Anthropic/OpenAI/MCP formats, an MCP server, FHIR R4 export, and a Claude agent example. Scanned faxes can go through Claude with structured outputs, and every LLM-extracted fact must quote its evidence. Unverifiable quotes are downgraded.

**How it's different from FHIR/HL7/SNOMED.** Those standards define the *envelope* and the *dictionary*. They don't make the data inside correct, de-duplicated or consistent. Canon consumes those standards and outputs them (FHIR R4 export). Its job is to make the data trustworthy enough for an agent to act on.

**A business from day one.** Canon is multi-tenant: each customer's data is isolated, API keys are stored hashed, and there's a tamper-evident hash-chained audit log of every read and write. It has Stripe usage-based billing at a placeholder $0.10 per normalized document: signup, free tier, Checkout, metered usage, webhooks and a billing portal.

## How we used Supabase
- **Supabase Postgres is the system of record** for production. Accounts, hashed API keys, usage events, Stripe event de-duplication, patients, patient-match keys, documents (raw bytes kept for re-processing) and the audit log all live in a dedicated `canon` schema (`migrations/001_init.sql`).
- **Security by design.** The `canon` schema isn't exposed through the Data API: only the server role can reach it, and `anon`/`authenticated` have no grants. Every table has RLS enabled with no policies, so access is deny-all. A database trigger makes the audit log **append-only**: Postgres itself rejects any UPDATE or DELETE.
- **Serverless-safe concurrency.** The API runs on Vercel serverless through Supabase's transaction pooler. Audit-log appends take a Postgres advisory lock inside a transaction, so the hash chain stays correct across concurrent instances.
- Verified with Supabase's security advisor (only the expected "RLS enabled, no policy" notice).

## Tech stack
Python (standard library only for the core: zero runtime dependencies), Supabase Postgres (psycopg 3), Vercel serverless (WSGI), Stripe Billing Meters, Model Context Protocol, Claude (optional extraction and the example agent), FHIR R4 / HL7 v2 / C-CDA / X12.

## Links
- Repository: https://github.com/rishadusmani/primaryaihackathon
- Live demo (web): https://primaryaihackathon.vercel.app → "Load sample patient" (no key needed once the latest `main` is deployed)
- Live API: https://primaryaihackathon.vercel.app (demo deployment; requires `Authorization: Bearer <key>`. TODO: share a judge key, or remove `CANON_API_KEYS` so the demo is open)
- Demo video: TODO

## How to run it
```bash
git clone https://github.com/rishadusmani/primaryaihackathon && cd primaryaihackathon
python -m canon normalize samples/maria_chen/*        # 7 messy documents → one record (no dependencies)
python -m unittest discover -s tests                  # 41 tests
python -m canon serve                                 # local API on :8080
claude mcp add canon -- python -m canon mcp           # use it from Claude Code
```

## Demo script (for the video, ~2 minutes)
0. **Tip:** the web demo (https://primaryaihackathon.vercel.app) is the easiest thing to screen-record. Steps 1–3 work there with one click.
1. **Problem (15s).** Show the 7 sample files for one patient: a fax with OCR typos, an HL7 lab feed, a hospital C-CDA, a FHIR bundle, an insurance claim, a CSV and a PDF letter.
2. **Normalize (30s).** Run `python -m canon normalize samples/maria_chen/*`. Point out that all seven are matched to one patient.
3. **The catches (45s).** In the summary, show:
   - A1c 7.8 % trending up, de-duplicated across fax and HL7.
   - Metformin 1000 mg, from the plan change.
   - The **allergy conflict**: the hospital says NKDA, the fax says penicillin, the derm letter says sulfa.
   - The metformin dose discrepancy.
   - Anxiety flagged "claims only".
4. **Agent (20s).** Ask Claude (MCP or `examples/claude_agent.py`) "Is amoxicillin safe?" It checks conflicts and cites the penicillin allergy from the fax.
5. **Business (10s).** Supabase schema, signup → API key, Stripe billing.

## Team
Rishad U. (owner). TODO: add teammates if any.
