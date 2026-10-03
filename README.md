# primaryaihackathon: Clinical Normalizer

Messy clinical records in, canonical **FHIR R4** out, for AI agents to work with. Exposed as an HTTP API and an MCP server, and **billed per successful call** with Stripe usage-based billing.

- **Labs and vitals** → `Observation` with LOINC codes and UCUM units. Values are converted to a canonical unit: glucose mmol/L → mg/dL, creatinine µmol/L → mg/dL, A1c mmol/mol → %, lb → kg, °F → °C. Blood pressure `120/80` is split into systolic and diastolic components.
- **Diagnoses** → `Condition` coded with SNOMED CT and ICD-10-CM (`T2DM`, `HTN`, `E11.9`, ...).
- **Medications** → `MedicationStatement` with the RxNorm ingredient (brand names resolve: `Glucophage 500mg tab` → metformin 6809), plus strength and sig.
- **Unmappable input is reported, not guessed.** Each failed record comes back in `issues` with a reason.
- **Deterministic output.** The same input always gives the same resource ids.
- **Dates:** slashed dates are read as US (MM/DD).

The vocabularies in `app/normalize.py` are a small hand-curated starter set. Extend the tables, or swap in a terminology server, to cover more codes. Use synthetic data only (e.g. [Synthea](https://synthetichealth.github.io/synthea/)); don't send real patient data.

## Quick start (no Stripe)

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python scripts/create_key.py            # prints a cn_... key
.venv/bin/uvicorn app.main:app --reload
curl -X POST localhost:8000/v1/normalize -H "Authorization: Bearer cn_..." \
  -H 'Content-Type: application/json' -d @samples/messy_records.json
.venv/bin/pytest
```

With `STRIPE_SECRET_KEY` unset, billing is off and meter events are only logged.

## Per-call billing with Stripe

How it works: a customer subscribes through Stripe Checkout and gets an API key. After each call that normalizes at least one record, the API sends one Stripe **meter event**. Stripe adds up the calls and invoices monthly at the metered price. Calls that normalize nothing are free.

1. `cp .env.example .env` and set `STRIPE_SECRET_KEY` to a **test-mode or sandbox** key (`stripe sandbox create` works).
2. `set -a; . ./.env; set +a; .venv/bin/python scripts/setup_stripe.py` creates the meter and a `$0.005/call` metered price. Put the printed `STRIPE_PRICE_ID` in `.env`.
3. Forward webhooks locally with `stripe listen --forward-to localhost:8000/stripe/webhook`, and put the `whsec_...` it prints in `.env` as `STRIPE_WEBHOOK_SECRET`.
4. `.venv/bin/uvicorn app.main:app --env-file .env`, open http://localhost:8000, click **Get an API key**, and pay with test card `4242 4242 4242 4242`. The success page shows the key once.
5. Call the API. Usage appears under **Billing → Meters** in the Stripe dashboard, and `GET /v1/usage` shows the running count and cost.

Details:
- Send an `Idempotency-Key` header and a retried request reuses the same meter event identifier, so Stripe bills it once.
- A Stripe outage never fails the caller's request; the meter event error is logged.
- When a subscription is cancelled (`customer.subscription.deleted`), that customer's keys are revoked.
- Keys are stored hashed in SQLite (`DB_PATH`).

## Endpoints

| Method | Path | |
|---|---|---|
| POST | `/v1/normalize` | `{"records": [...]}` (up to 1000). Auth: `Authorization: Bearer cn_...` or `X-API-Key`. Billed if any record normalizes. |
| GET | `/v1/usage` | Billed calls and estimated cost for this key |
| GET | `/signup` | Redirects to Stripe Checkout |
| GET | `/signup/success` | Issues the API key after checkout |
| POST | `/stripe/webhook` | Stripe webhooks (signature-verified) |
| GET | `/docs` | OpenAPI UI |

## MCP server for agents

`mcp_server.py` exposes `normalize_clinical_records` and `get_usage`. It calls the HTTP API with your key, so agent usage is billed like any other call.

```json
{
  "mcpServers": {
    "clinical-normalizer": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": ["/path/to/repo/mcp_server.py"],
      "env": {"NORMALIZER_API_URL": "http://localhost:8000", "NORMALIZER_API_KEY": "cn_..."}
    }
  }
}
```

Or with Claude Code: `claude mcp add clinical-normalizer -e NORMALIZER_API_KEY=cn_... -- .venv/bin/python mcp_server.py`

## Layout

```
app/normalize.py     normalization + vocabularies
app/main.py          FastAPI app (auth, metering, signup, webhooks)
app/billing.py       Stripe meter events / Checkout / webhook verification
app/store.py         SQLite API keys + local usage counter
mcp_server.py        MCP server for agents
scripts/             setup_stripe.py, create_key.py
samples/             messy example records
tests/               pytest suite (Stripe is faked)
```
