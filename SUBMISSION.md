# Supabase Select 2026 Hackathon — submission draft (Primary AI)

> Paste-ready answers for the submission form. Every field is filled in; nothing has been submitted.

## Project name
Canon

## Tagline (one line)
Turns messy faxes, PDFs, HL7, FHIR, claims and portal exports into one clean, coded, cited patient record that AI agents can safely act on.

## Short description (~460 characters)
Healthcare data arrives as faxes, PDFs, HL7 feeds, C-CDA, FHIR, claims and CSVs that contradict each other. Canon normalizes all of it into one patient record: standard codes, canonical units, duplicates merged, conflicts flagged, every fact cited. Rules handle what's provable; when a sentence is ambiguous (a relative's diagnosis, a negative screen, a "possible" finding), a model gives a second opinion that must quote its evidence. Agents use it via API or MCP.

## Full description

**The problem.** AI agents are being asked to do real work in healthcare: book visits, prep referrals, check meds. But the data they get is a mess. Much of it, especially referrals, still arrives by fax or PDF. Lab feeds, hospital summaries, patient portals and insurance claims each describe the same patient differently. Even "standard" data disagrees: two LOINC codes for the same test, A1c in mmol/mol versus %, a medication dose changed in one system but not another. An agent reading seven raw documents will miss things or make them up.

**What Canon does.** You send Canon any clinical document, in any of 7 formats, and it returns one canonical patient record:

- **Every format in.** Fax/OCR text, PDF, HL7 v2, C-CDA, FHIR R4, X12 837 claims and portal CSVs. The format is detected automatically, and documents are matched to the right patient by demographics.
- **Standard codes out.** ICD-10, SNOMED CT, LOINC, RxNorm, CVX and CPT, with units converted to UCUM (e.g. A1c 60 mmol/mol → 7.64 %).
- **Reconciled across sources.** The same A1c arriving by fax, HL7 and the portal becomes *one* result with three sources. Medication changes ("increase metformin to 1000 mg") update the current dose and keep the history.
- **Conflicts surfaced, never hidden.** Examples: "Hospital says no known allergies, the fax says penicillin." "The portal still shows 500 mg." A diagnosis that appears only on a billing claim is flagged as such. Agents are told to check before acting.
- **Every fact is cited.** Each item links to the source document, the exact location (`OBX[2]`, `line 31`) and a verbatim snippet, with a confidence score. Nothing is silently dropped: anything that can't be mapped is listed with a reason.
- **Rules first, a model only where the rules are unsure.** The rule engine handles negation ("denies chest pain", "depression screen negative"), someone else's history ("mother had diabetes", "runs in the family"), hedges ("possible pneumonia", "asthma vs COPD") and pseudo-negation ("no improvement in hypertension" keeps hypertension), whether the cue comes before or after the condition, plus OCR repair ("5OO mg" → 500 mg) and medication start/stop/increase intent. When a cue makes the rules unsure, they hold the condition back and send only that sentence to GPT-5.6 Luna for a second opinion. It can restore a condition ("His wife reports he was diagnosed with COPD") but never invent one, and every label must quote the sentence. That costs about $0.0004 per note.
- **See it in the browser.** The live demo opens on the 7-document sample patient (or drop in your own files). Conflicts come first, then coded problems, reconciled medications with change history, and lab trends. Click any row to see the exact line it came from. Customers get an Agent usage dashboard showing their agents' requests, errors, latency and LLM tokens.
- **Built for agents.** A token-efficient patient summary, 9 tools in Anthropic/OpenAI/MCP formats, a hosted MCP server (plus a local stdio one), FHIR R4 export, and a Claude agent example. Scanned faxes can go through Claude with structured outputs, and every LLM-extracted fact must quote its evidence. Unverifiable quotes are downgraded.

**How it's different from FHIR/HL7/SNOMED.** Those standards define the *envelope* and the *dictionary*. They don't make the data inside correct, de-duplicated or consistent. Canon consumes those standards and outputs them (FHIR R4 export). Its job is to make the data trustworthy enough for an agent to act on.

**A business from day one.** Canon is multi-tenant: each customer's data is isolated, API keys are stored hashed, and there's a tamper-evident hash-chained audit log of every read and write. It has Stripe usage-based billing at $0.05 per normalized page: signup, Checkout, metered usage, webhooks and a billing portal.

## How we used Supabase
- **Supabase Postgres is the system of record** for production. Accounts, hashed API keys, usage events, Stripe event de-duplication, patients, patient-match keys, documents (raw bytes kept for re-processing) and the audit log all live in a dedicated `canon` schema (`migrations/001_init.sql`).
- **Least privilege by design.** The `canon` schema isn't exposed through the Data API, and `anon`/`authenticated` have no grants. The hosted API connects as a dedicated `canon_app` role, not `postgres`. That role gets only the grants it needs; the audit log is insert-only, and a trigger makes Postgres reject any UPDATE or DELETE on it. Every table has row-level security, with policies only for `canon_app`.
- **Usage metering in Postgres.** Every authenticated agent request becomes a row in `canon.api_requests` (operation, status, latency, patient, LLM tokens). That table powers the per-customer Agent usage dashboard, alongside the billable `usage_events` that feed Stripe.
- **Serverless-safe concurrency.** The API runs on Vercel serverless through Supabase's transaction pooler. Audit-log appends take a Postgres advisory lock inside a transaction, so the hash chain stays correct across concurrent instances.
- Schema in `migrations/` (001 init, 002 metering and app role, 003 metering grants). Checked with Supabase's security advisor.

## Tech stack
Python (standard library only for the core: zero runtime dependencies), Supabase Postgres (psycopg 3), Vercel serverless (WSGI), Stripe Billing Meters, Model Context Protocol, OpenAI GPT-5.6 Luna (second opinion on ambiguous sentences), Claude (optional scanned-fax extraction and the example agent), FHIR R4 / HL7 v2 / C-CDA / X12.

## Links
- Repository: https://github.com/rishadusmani/primaryaihackathon
- Live demo: https://primaryaihackathon.vercel.app (opens with the 7-document sample patient already normalized; no sign-in needed)
- **Judge API key:** `cn_live_primaryaisupabasehackathon`
  - Dashboard: https://primaryaihackathon.vercel.app/dashboard, sign in with the key above
  - Try the API: `curl -H "Authorization: Bearer cn_live_primaryaisupabasehackathon" https://primaryaihackathon.vercel.app/v1/patients`
  - Upload a document: `curl -X POST "https://primaryaihackathon.vercel.app/v1/documents?filename=note.txt" -H "Authorization: Bearer cn_live_primaryaisupabasehackathon" --data-binary @note.txt`
  - The key is unmetered: upload as much as you like, it's never billed.
  - Connect an agent (Claude, OpenAI, Gemini, Cursor): https://primaryaihackathon.vercel.app/connect, paste the key under "Already have a key?"
  - Use it from Claude over MCP: `claude mcp add --transport http canon https://primaryaihackathon.vercel.app/mcp --header "Authorization: Bearer cn_live_primaryaisupabasehackathon"`
- Your own key: `curl -X POST https://primaryaihackathon.vercel.app/v1/signup -H 'Content-Type: application/json' -d '{"name":"Judge","email":"you@example.com"}'`. New accounts have no free pages: reads work right away, and uploads need a subscription through the returned Checkout link (Stripe test card `4242 4242 4242 4242`).
- API health: https://primaryaihackathon.vercel.app/healthz
- Demo video with voice-over (2:27): https://github.com/rishadusmani/primaryaihackathon/blob/main/docs/canon-demo.mp4 (recorded on the live site: the web demo, connecting an agent over MCP, a REST call and an MCP tool call, and the usage dashboard; upload to YouTube or Loom if the form needs a streaming link)
- Product images (10): https://github.com/rishadusmani/primaryaihackathon/tree/main/docs/images

## How to run it
```bash
git clone https://github.com/rishadusmani/primaryaihackathon && cd primaryaihackathon
python -m canon normalize samples/maria_chen/*        # 7 messy documents → one record (no dependencies)
python -m unittest discover -s tests                  # 100 tests
python -m canon serve                                 # local API on :8080
claude mcp add canon -- python -m canon mcp           # local MCP (stdio) for Claude Code
```

## Demo script (for the video, ~2 minutes)
0. **Tip:** record the live demo at https://primaryaihackathon.vercel.app. It opens with steps 1–3 already on screen. For step 6, show /dashboard.
1. **Problem (15s).** Show the 7 sample files for one patient: a fax with OCR typos, an HL7 lab feed, a hospital C-CDA, a FHIR bundle, an insurance claim, a CSV and a PDF letter.
2. **Normalize (30s).** Run `python -m canon normalize samples/maria_chen/*`. Point out that all seven are matched to one patient.
3. **The catches (45s).** In the summary, show:
   - A1c 7.8 % trending up, de-duplicated across fax and HL7.
   - Metformin 1000 mg, from the plan change.
   - The **allergy conflict**: the hospital says NKDA, the fax says penicillin, the derm letter says sulfa.
   - The metformin dose discrepancy.
   - Anxiety flagged "claims only".
4. **The tricky note (20s).** Click "Load tricky note" and scroll to the last five walkthrough rows. A keyword matcher gives the patient his mother's diabetes and an active pneumonia from 2019; Canon keeps those out, and each row shows the model's live label. On the COPD row, the rules held it back because his wife is mentioned, and the model restored it as his diagnosis.
5. **Agent (20s).** Ask Claude (MCP or `examples/claude_agent.py`) "Is amoxicillin safe?" It checks conflicts and cites the penicillin allergy from the fax.
6. **Business (10s).** Supabase schema, signup → API key, Stripe billing.

## Team
Rishad U. (Primary AI): owner and builder.
