"""Live terminology fallback: when a code or name isn't in Canon's tables, ask the
National Library of Medicine before giving up.

- ICD-10-CM codes and exact condition names: NLM Clinical Tables
- Medications (codes, generics, brands, combinations): NLM RxNav, classes via RxClass
- LOINC codes: NLM Clinical Tables

Only exact or verifiably-equivalent matches are accepted. A fuzzy RxNav match is
kept only if every ingredient it resolves to is named in the original text, so
"Entresto" or "sacubitril/valsartan" resolve, but a near-miss spelling of some
other drug never does. Everything returned carries `terminology: "nlm_live"` so
an agent can tell a table hit from a live one.

Calls are bounded: short timeouts, an in-process cache (misses included), and a
circuit breaker that stops calling NLM for a while after repeated failures, so a
slow upstream can never hang an ingest; service.ingest also caps the total time per
document (CANON_LIVE_BUDGET, default 10 s). Set CANON_LIVE_TERMINOLOGY=0 to disable.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CLINICAL_TABLES = "https://clinicaltables.nlm.nih.gov/api"
RXNAV = "https://rxnav.nlm.nih.gov/REST"
SOURCE = "nlm_live"
log = logging.getLogger(__name__)

_cache: dict[tuple, dict | None] = {}
_lock = threading.Lock()
_failures = 0
_open_until = 0.0
CACHE_MAX = 5000
BREAKER_THRESHOLD = 3
BREAKER_SECONDS = 120


_local = threading.local()


class _Unavailable(Exception):
    pass


@contextlib.contextmanager
def budget(seconds: float | None = None):
    """Cap the wall time spent on uncached lookups inside the block (one ingest)."""
    seconds = float(os.environ.get("CANON_LIVE_BUDGET", "10")) if seconds is None else seconds
    prev = getattr(_local, "deadline", None)
    _local.deadline = time.monotonic() + seconds
    try:
        yield
    finally:
        _local.deadline = prev


def enabled() -> bool:
    return os.environ.get("CANON_LIVE_TERMINOLOGY", "1").lower() not in ("0", "false", "off", "no")


def _timeout() -> float:
    return float(os.environ.get("CANON_LIVE_TIMEOUT", "2.5"))


def _check_available() -> None:
    now = time.monotonic()
    if now < _open_until:
        raise _Unavailable("circuit open")
    deadline = getattr(_local, "deadline", None)
    if deadline is not None and now >= deadline:
        raise _Unavailable("lookup budget spent")


def _get(url: str, **params) -> dict | list:
    global _failures, _open_until
    _check_available()
    now = time.monotonic()
    deadline = getattr(_local, "deadline", None)
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(full, headers={"Accept": "application/json", "User-Agent": "canon-terminology"})
    try:
        timeout = _timeout() if deadline is None else max(0.2, min(_timeout(), deadline - now))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        with _lock:
            _failures += 1
            if _failures >= BREAKER_THRESHOLD:
                _open_until = time.monotonic() + BREAKER_SECONDS
                _failures = 0
        raise _Unavailable(str(e)) from e
    with _lock:
        _failures = 0
    return data


def _cached(key: tuple, fn):
    if not enabled():
        return None
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        _check_available()
        value = fn()
    except _Unavailable:
        return None  # don't cache outages; try again next time
    except Exception:  # an unexpected NLM response shape must never fail an ingest
        log.warning("live terminology lookup %s failed", key, exc_info=True)
        return None
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = value
    return value


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# --------------------------------------------------------------------------- conditions
ICD10_RX = re.compile(r"^[A-TV-Z]\d{2}(?:\.?[0-9A-Z]{1,4})?$")


def condition(text: str | None = None, code: str | None = None, system: str | None = None) -> dict | None:
    """ICD-10-CM by exact (billable) code, or by a name that exactly matches the official title."""
    if code and system in (None, "http://hl7.org/fhir/sid/icd-10-cm"):
        c = code.strip().upper()
        if ICD10_RX.match(c):
            c = c if "." in c or len(c) <= 3 else c[:3] + "." + c[3:]
            hit = _cached(("icd10", c), lambda: _icd10_by_code(c))
            if hit:
                return hit
    if text and len(text) <= 200:
        return _cached(("icd10_text", _norm(text)), lambda: _icd10_by_name(text))
    return None


def _icd10_result(code: str, name: str) -> dict:
    return {"icd10": code, "snomed": None, "display": name, "verified": True, "terminology": SOURCE}


def _icd10_by_code(code: str) -> dict | None:
    data = _get(f"{CLINICAL_TABLES}/icd10cm/v3/search", terms=code, sf="code", maxList=20)
    return next((_icd10_result(c, n) for c, n in data[3] if c == code), None)


def _icd10_by_name(text: str) -> dict | None:
    data = _get(f"{CLINICAL_TABLES}/icd10cm/v3/search", terms=text, sf="code,name", maxList=50)
    want = _norm(text)
    return next((_icd10_result(c, n) for c, n in data[3] if _norm(n) == want), None)


# --------------------------------------------------------------------------- medications
FORM_WORDS = {"tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules", "er", "xr", "xl", "sr", "dr",
              "ec", "la", "cr", "ir", "oral", "po", "solution", "soln", "susp", "suspension", "inj", "injection",
              "cream", "ointment", "pen", "inhaler", "patch", "drops", "spray", "hfa", "mg", "mcg", "ml", "units",
              "daily", "bid", "tid", "qid", "prn", "qhs", "qd", "hcl", "extended", "release", "delayed"}


def medication(text: str | None = None, code: str | None = None) -> dict | None:
    """RxNorm ingredient (or multi-ingredient MIN) for an RxCUI, a generic or brand name."""
    if code and code.strip().isdigit():
        hit = _cached(("rxcui", code.strip()), lambda: _resolve(code.strip()))
        if hit:
            return _public(hit)
    if not text:
        return None
    words = [w for w in _norm(re.split(r"\d", text, maxsplit=1)[0]).split() if w not in FORM_WORDS]
    query = " ".join(words)
    if len(query) < 3 or len(query) > 100:
        return None
    return _cached(("rx_text", query), lambda: _medication_by_name(query))


def _props(rxcui: str) -> dict:
    return _get(f"{RXNAV}/rxcui/{rxcui}/properties.json").get("properties") or {}


def _related(rxcui: str, tty: str) -> list[dict]:
    data = _get(f"{RXNAV}/rxcui/{rxcui}/related.json", tty=tty)
    return [c for g in (data.get("relatedGroup", {}).get("conceptGroup") or [])
            for c in (g.get("conceptProperties") or [])]


def _resolve(rxcui: str) -> dict | None:
    props = _props(rxcui)
    tty = props.get("tty")
    if not tty:
        return None
    if tty in ("IN", "PIN", "MIN"):
        target = (rxcui, props["name"], tty)
    else:
        ins = _related(rxcui, "IN")
        if len(ins) == 1:
            target = (ins[0]["rxcui"], ins[0]["name"], "IN")
        elif len(ins) > 1:
            mins = _related(rxcui, "MIN")
            if len(mins) != 1:
                return None
            target = (mins[0]["rxcui"], mins[0]["name"], "MIN")
        else:
            return None
    cui, name, tty = target
    ingredients = [name] if tty != "MIN" else [n.strip() for n in name.split("/")]
    return {"ingredient": name.lower(), "rxnorm": cui, "drug_class": _drug_class(cui) if tty != "MIN" else None,
            "terminology": SOURCE, "_ingredients": [i.lower() for i in ingredients],
            "_names": {_norm(props.get("name", "")), _norm(name)}}


def _drug_class(rxcui: str) -> str | None:
    try:
        data = _get(f"{RXNAV}/rxclass/class/byRxcui.json", rxcui=rxcui, relaSource="DAILYMED", relas="has_epc")
    except _Unavailable:
        return None
    names = sorted({c["rxclassMinConceptItem"]["className"]
                    for c in (data.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo", [])
                    if "diagnostic" not in c["rxclassMinConceptItem"]["className"].lower()})
    return "; ".join(names) or None


def _acceptable(hit: dict | None, query: str) -> bool:
    if not hit:
        return False
    if query in hit["_names"]:
        return True  # exact generic, brand or combination name
    return all(re.search(rf"(?<![a-z0-9]){re.escape(_norm(i))}(?![a-z0-9])", query) for i in hit["_ingredients"])


def _public(hit: dict) -> dict:
    return {k: v for k, v in hit.items() if not k.startswith("_")}


def _medication_by_name(query: str) -> dict | None:
    data = _get(f"{RXNAV}/rxcui.json", name=query, search=2)
    for rxcui in (data.get("idGroup", {}).get("rxnormId") or [])[:3]:
        hit = _resolve(rxcui)
        if _acceptable(hit, query):
            return _public(hit)
    data = _get(f"{RXNAV}/approximateTerm.json", term=query, maxEntries=5, option=1)
    seen = set()
    for cand in (data.get("approximateGroup", {}).get("candidate") or []):
        rxcui = cand.get("rxcui")
        if not rxcui or rxcui in seen:
            continue
        seen.add(rxcui)
        hit = _resolve(rxcui)
        if _acceptable(hit, query):
            return _public(hit)
        if len(seen) >= 3:
            break
    return None


# --------------------------------------------------------------------------- observations
LOINC_RX = re.compile(r"^\d{1,7}-\d$")


def observation(code: str | None = None) -> dict | None:
    """LOINC by exact code. No unit conversion: the value keeps the unit it arrived with."""
    if not code or not LOINC_RX.match(code.strip()):
        return None
    c = code.strip()
    return _cached(("loinc", c), lambda: _loinc_by_code(c))


def _loinc_by_code(code: str) -> dict | None:
    data = _get(f"{CLINICAL_TABLES}/loinc_items/v3/search", terms=code, sf="LOINC_NUM",
                df="LOINC_NUM,LONG_COMMON_NAME", maxList=20)
    name = next((n for num, n in data[3] if num == code), None)
    if not name:
        return None
    return {"loinc": code, "display": name, "unit": None, "category": "lab", "reference_range": (None, None),
            "terminology": SOURCE}
