"""API metering: one row per agent request (HTTP API or MCP tool call), and the
aggregates behind GET /v1/usage and the customer dashboard at /dashboard.

This is observability, not billing: billable documents live in usage_events
(see billing.py)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .store import Store

MAX_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc)


def llm_tokens(body: dict | None) -> tuple[int, int]:
    """LLM tokens spent by an ingest, read from the response's extraction info."""
    u = ((body or {}).get("document") or {}).get("extraction", {}).get("llm_usage") or {}
    return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))


def record(store: Store, *, account_id: str, channel: str, operation: str, status: int, latency_ms: float,
           bytes_in: int = 0, bytes_out: int = 0, patient_id: str | None = None, body: dict | None = None) -> None:
    tin, tout = llm_tokens(body)
    store.execute(
        "INSERT INTO api_requests (ts, account_id, channel, operation, status, latency_ms, bytes_in, bytes_out, patient_id, "
        "llm_input_tokens, llm_output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_now().strftime("%Y-%m-%dT%H:%M:%SZ"), account_id, channel, operation, status, round(latency_ms, 2),
         bytes_in, bytes_out, patient_id, tin, tout),
    )


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(p * (len(v) - 1))))], 1)


def summarize(store: Store, account_id: str, days: int = 30, recent: int = 50) -> dict:
    """One account's API usage over the last `days` days (UTC)."""
    days = max(1, min(int(days), MAX_DAYS))
    end = _now().date()
    start = end - timedelta(days=days - 1)
    since = start.isoformat()
    where, params = "account_id = ? AND ts >= ?", (account_id, since)
    rows = store.all(f"SELECT * FROM api_requests WHERE {where} ORDER BY seq", params)

    daily = {(start + timedelta(days=i)).isoformat(): {"requests": 0, "errors": 0, "llm_tokens": 0}
             for i in range(days)}
    ops: dict[str, dict] = defaultdict(lambda: {"requests": 0, "errors": 0, "latencies": []})
    channels: dict[str, int] = defaultdict(int)
    latencies: list[float] = []
    patients: set[str] = set()
    errors = ingests = tin = tout = bytes_in = 0
    for r in rows:
        err = r["status"] >= 400
        d = daily.get(r["ts"][:10])
        if d is not None:
            d["requests"] += 1
            d["errors"] += err
            d["llm_tokens"] += r["llm_input_tokens"] + r["llm_output_tokens"]
        o = ops[r["operation"]]
        o["requests"] += 1
        o["errors"] += err
        o["latencies"].append(r["latency_ms"])
        channels[r["channel"]] += 1
        latencies.append(r["latency_ms"])
        errors += err
        if r["patient_id"] and not err:
            patients.add(r["patient_id"])
        if r["operation"] in ("documents.ingest", "tool.ingest_document") and not err:
            ingests += 1
        tin += r["llm_input_tokens"]
        tout += r["llm_output_tokens"]
        bytes_in += r["bytes_in"]

    n = len(rows)
    recent_rows = store.all(f"SELECT * FROM api_requests WHERE {where} ORDER BY seq DESC LIMIT ?", (*params, recent))
    out = {
        "object": "usage",
        "account_id": account_id,
        "period": {"start": since, "end": end.isoformat(), "days": days},
        "totals": {
            "requests": n, "errors": errors, "error_rate": round(errors / n, 4) if n else 0.0,
            "latency_p50_ms": _pct(latencies, 0.5), "latency_p95_ms": _pct(latencies, 0.95),
            "documents_ingested": ingests, "patients_accessed": len(patients), "bytes_ingested": bytes_in,
            "llm_input_tokens": tin, "llm_output_tokens": tout,
        },
        "daily": [{"date": k, **v} for k, v in daily.items()],
        "operations": sorted(({"operation": k, "requests": v["requests"], "errors": v["errors"],
                               "latency_p50_ms": _pct(v["latencies"], 0.5),
                               "latency_p95_ms": _pct(v["latencies"], 0.95)} for k, v in ops.items()),
                             key=lambda x: -x["requests"]),
        "channels": dict(channels),
        "recent": [{"ts": r["ts"], "channel": r["channel"], "operation": r["operation"], "status": r["status"],
                    "latency_ms": r["latency_ms"], "patient_id": r["patient_id"],
                    "llm_tokens": r["llm_input_tokens"] + r["llm_output_tokens"]} for r in recent_rows],
    }
    return out
