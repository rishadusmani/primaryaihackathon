"""Second opinion on conditions the rule engine was unsure about.

text.parse drops a condition when a cue makes the rules unsure whether it applies to the patient right
now: a negation ("no", "denies"), a relative ("mother had..."), or a hedge ("possible", "rule out"). Those
sentences are the only thing sent to a model. It labels each one, and only "present" or "historical"
puts the condition back into the record, so the model can restore a fact the rules dropped but can never
add one the rules didn't find. Codes still come from the rule match.

Guardrails:
* Strict JSON schema output, one label per candidate.
* Evidence: the model must quote the sentence; a quote that isn't in the sentence discards the label.
* At most MAX_CANDIDATES sentences per document, in one request.
* Identical requests are answered from an in-memory cache (per process, CACHE_SIZE entries), so the
  same sentences cost one call per server instance, e.g. the public demo's built-in sample.

Uses OpenAI's Responses API over plain HTTPS (no SDK dependency). Enabled when OPENAI_API_KEY is set and
CANON_ASSERTION is not "0", except for ingests with use_llm=False (the public playground), which never call
a model. Model: CANON_ASSERTION_MODEL (default gpt-5.6-luna).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
import re
import urllib.error
import urllib.request

from ..model import fact

MODEL = os.environ.get("CANON_ASSERTION_MODEL", "gpt-5.6-luna")
API_URL = os.environ.get("CANON_ASSERTION_URL", "https://api.openai.com/v1/responses")
MAX_CANDIDATES = 25
TIMEOUT_S = 30
CACHE_SIZE = 256
_cache: OrderedDict[str, dict] = OrderedDict()

LABELS = ("present", "historical", "absent", "hypothetical", "other_person")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "assertion", "evidence"],
                "properties": {
                    "id": {"type": "integer"},
                    "assertion": {"type": "string", "enum": list(LABELS)},
                    "evidence": {"type": "string",
                                 "description": "The exact words from the sentence that justify the label"},
                },
            },
        },
    },
}

INSTRUCTIONS = (
    "You check clinical sentences for a medical-record pipeline. For each item, decide what the sentence says "
    "about the named condition for THIS patient:\n"
    "- present: the patient has it now (including worsening, uncontrolled, or 'no improvement in' it)\n"
    "- historical: the patient had it in the past and it is resolved\n"
    "- absent: the sentence says the patient does not have it (denied, negative, ruled out)\n"
    "- hypothetical: possible, suspected, being evaluated, a risk, or a plan to screen; not established\n"
    "- other_person: it belongs to someone else, such as a family member\n"
    "Judge only from the sentence. Quote the exact words that decide the label as evidence."
)


def enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY")) and os.environ.get("CANON_ASSERTION", "1") != "0"


def request_body(candidates: list[dict]) -> dict:
    lines = [{"id": i, "condition": c["term"], "sentence": c["sentence"]} for i, c in enumerate(candidates)]
    return {
        "model": MODEL,
        "store": False,  # sentences come from patient records: don't let OpenAI retain the request or response
        "instructions": INSTRUCTIONS,
        "input": json.dumps({"items": lines}),
        "reasoning": {"effort": "low"},
        "text": {"format": {"type": "json_schema", "name": "condition_assertions", "schema": SCHEMA,
                            "strict": True}},
    }


def _post(body: dict) -> dict:
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"assertion model returned HTTP {e.code}: {detail}") from e


def _output_text(response: dict) -> str:
    """Concatenate output_text parts of message items, as the OpenAI SDK's `output_text` does."""
    return "".join(part.get("text") or "" for item in response.get("output") or [] if item.get("type") == "message"
                   for part in item.get("content") or [] if part.get("type") == "output_text")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def resolve(candidates: list[dict], usage: dict | None = None, post=None) -> tuple[list[dict], dict]:
    """Ask the model about `candidates` (from text.parse's `review`). Returns (facts to add, report).

    `post` is the HTTP call, injectable for tests. `usage` accumulates input/output tokens."""
    todo = candidates[:MAX_CANDIDATES]
    report = {"model": MODEL, "candidates": len(candidates), "sent": len(todo), "labels": {}, "rejected": 0}
    if not todo:
        return [], report
    body = request_body(todo)
    key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    cached = key in _cache and post is None
    if cached:
        _cache.move_to_end(key)
        response = _cache[key]
    else:
        response = (post or _post)(body)
        if post is None:
            _cache[key] = response
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    report["cached"] = cached
    report["decisions"] = []
    if usage is not None and not cached:
        u = response.get("usage") or {}
        for k in ("input_tokens", "output_tokens"):
            usage[k] = usage.get(k, 0) + int(u.get(k) or 0)
    items = json.loads(_output_text(response) or "{}").get("items") or []
    added: list[dict] = []
    for item in items:
        i = item.get("id")
        if not isinstance(i, int) or not 0 <= i < len(todo) or item.get("assertion") not in LABELS:
            report["rejected"] += 1
            continue
        c = todo[i]
        evidence = item.get("evidence") or ""
        if not evidence or _norm(evidence) not in _norm(c["sentence"]):
            report["rejected"] += 1  # unverifiable label: keep the rules' decision
            continue
        label = item["assertion"]
        report["labels"][label] = report["labels"].get(label, 0) + 1
        report["decisions"].append({"term": c["term"], "sentence": c["sentence"], "reason": c["reason"],
                                    "label": label, "evidence": evidence})
        if label in ("present", "historical"):
            f = c["fact"]
            added.append(fact("condition", locator=c["locator"], method="llm", snippet=f["snippet"], confidence=0.75,
                              text=f["text"], mapped_code=f["mapped_code"], recorded=f.get("recorded"),
                              status="active" if label == "present" else "resolved",
                              assertion={"label": label, "reason": c["reason"], "evidence": evidence,
                                         "model": MODEL}))
    return added, report
