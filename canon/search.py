"""Patient-scoped search of source passages, with a lazily built SQLite FTS5 index."""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache

from . import terminology
from .parsers import FORMATS, pdf
from .service import CanonError

STOP_WORDS = set("a an the is are was were be been being do does did has have had i my me we our you your "
                 "patient patients chart charts what when where why how which who this that these those "
                 "in on at to of for from with about and or can could would should please show find tell "
                 "document documented say said".split())


def source_text(row) -> dict:
    raw = bytes(row["raw"])
    if row["format"] == "pdf":
        text, info = pdf.extract_text(raw)
        warnings = ["This PDF needs OCR and has no searchable text."] if info["needs_ocr"] else []
        return {"text": text, "text_basis": "Extracted PDF text", "warnings": warnings}
    return {"text": raw.decode("utf-8-sig", "replace"), "text_basis": "Original source text", "warnings": []}


def passages(text: str):
    start = 0
    while start < len(text):
        end = min(start + 1000, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + 400, end)
            if boundary != -1:
                end = boundary
        if text[start:end].strip():
            yield start, end, text[start:end]
        if end == len(text):
            break
        next_start = max(start + 1, end - 160)
        boundary = text.find("\n", next_start, end)
        if boundary == -1:
            boundary = text.find(" ", next_start, end)
        start = boundary + 1 if boundary != -1 else next_start


@lru_cache(maxsize=1)
def synonym_groups() -> tuple:
    groups = []
    for synonyms in (terminology.medication_synonyms(), terminology.condition_synonyms(),
                     terminology.observation_synonyms(), terminology.allergen_synonyms()):
        by_code = {}
        for phrase, code in synonyms:
            by_code.setdefault(code, []).append(phrase)
        groups.extend(by_code.values())
    return tuple(tuple(group) for group in groups)


def query_terms(query: str) -> list[str]:
    words = re.findall(r"[^\W_]+", query.lower(), re.UNICODE)
    terms = list(dict.fromkeys(word for word in words if word not in STOP_WORDS))
    normalized = " " + " ".join(words) + " "
    for group in synonym_groups():
        if any(f" {phrase} " in normalized for phrase in group):
            terms.extend(group)
    return list(dict.fromkeys(terms))[:80]


def excerpt_window(text: str, start: int, end: int, terms: list[str]) -> tuple[int, int]:
    """Center an exact, bounded excerpt on the densest set of matching words."""
    expression = r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b"
    matches = list(re.finditer(expression, text[start:end], re.IGNORECASE))
    if not matches:
        return start, min(end, start + 560)
    focus = max(matches, key=lambda match: sum(abs(other.start() - match.start()) < 200 for other in matches))
    begin = max(start, start + focus.start() - 160)
    boundary = text.rfind("\n", max(start, begin - 80), begin)
    if boundary != -1:
        begin = boundary + 1
    return begin, min(end, begin + 560)


def _index(store, rows) -> list[dict]:
    """Backfill old documents too. Index replacements and their hash commit together."""
    coverage = []
    with store.serialized("chart_search"):
        store.execute("CREATE TABLE IF NOT EXISTS chart_search_documents "
                      "(document_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, warnings TEXT NOT NULL)")
        store.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chart_search_passages USING fts5("
                      "document_id UNINDEXED, start UNINDEXED, end UNINDEXED, text, tokenize='porter unicode61')")
        for row in rows:
            indexed = store.one("SELECT sha256, warnings FROM chart_search_documents WHERE document_id=?", (row["id"],))
            if indexed and indexed["sha256"] == row["sha256"]:
                warnings = json.loads(indexed["warnings"])
            else:
                source = source_text(row)
                warnings = source["warnings"]
                store.execute("DELETE FROM chart_search_passages WHERE document_id=?", (row["id"],))
                for start, end, text in passages(source["text"]):
                    store.execute("INSERT INTO chart_search_passages(document_id,start,end,text) VALUES (?,?,?,?)",
                                  (row["id"], start, end, text))
                store.execute("INSERT INTO chart_search_documents VALUES (?,?,?) ON CONFLICT(document_id) "
                              "DO UPDATE SET sha256=excluded.sha256,warnings=excluded.warnings",
                              (row["id"], row["sha256"], json.dumps(warnings)))
            coverage.append({"document_id": row["id"], "warnings": warnings})
    return coverage


def search_patient(canon, patient_id: str, query: str, *, fmt: str | None = None,
                   since: str | None = None, until: str | None = None, limit: int = 12) -> dict:
    if not isinstance(query, str) or len(query) > 300:
        raise CanonError("invalid_query", "Use a search query of up to 300 characters.")
    if fmt and fmt not in FORMATS:
        raise CanonError("invalid_format", "Choose a supported document format.")
    for value in (since, until):
        if value:
            try:
                date.fromisoformat(value)
                if len(value) != 10:
                    raise ValueError()
            except ValueError:
                raise CanonError("invalid_date", "Use dates in YYYY-MM-DD format.")
    if since and until and since > until:
        raise CanonError("invalid_date", "The start date must be before the end date.")
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise CanonError("invalid_limit", "Use a result limit between 1 and 50.")

    rows = canon.store.all("SELECT * FROM documents WHERE account_id=? AND patient_id=? ORDER BY id",
                           (canon.account_id, patient_id))
    if not rows:
        raise CanonError("not_found", "No chart found for this patient.", 404)
    coverage = _index(canon.store, rows)
    selected = [r for r in rows if (not fmt or r["format"] == fmt)
                and (not since or (r["document_date"] or "")[:10] >= since)
                and (not until or bool(r["document_date"]) and r["document_date"][:10] <= until)]
    terms = query_terms(query)
    results = []
    total = 0
    if terms and selected:
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        filters = "d.account_id=? AND d.patient_id=?"
        params = [expression, canon.account_id, patient_id]
        if fmt:
            filters += " AND d.format=?"
            params.append(fmt)
        if since:
            filters += " AND substr(d.document_date,1,10)>=?"
            params.append(since)
        if until:
            filters += " AND substr(d.document_date,1,10)<=?"
            params.append(until)
        found = canon.store.all(
            "SELECT p.document_id,p.start,p.end,p.text,d.filename,d.source_name,d.format,d.document_date,"
            "bm25(chart_search_passages) AS score FROM chart_search_passages AS p "
            "JOIN documents AS d ON d.id=p.document_id WHERE chart_search_passages MATCH ? AND " + filters +
            " ORDER BY score,d.document_date DESC,p.document_id,CAST(p.start AS INTEGER)", tuple(params))
        # Keep the best passage from each source. Repeated sources retain their own document IDs.
        seen = set()
        for row in found:
            if row["document_id"] in seen:
                continue
            seen.add(row["document_id"])
            total += 1
            if len(results) < limit:
                source = next(r for r in rows if r["id"] == row["document_id"])
                text = source_text(source)["text"]
                start, end = excerpt_window(text, int(row["start"]), int(row["end"]), terms)
                results.append({k: row[k] for k in ("document_id", "filename", "source_name", "format", "document_date")}
                               | {"excerpt": text[start:end], "start": start, "end": end,
                                  "locator": f"Lines {text.count(chr(10), 0, start) + 1}–{text.count(chr(10), 0, end) + 1}"})
    selected_ids = {row["id"] for row in selected}
    warnings = [item for item in coverage if item["warnings"] and item["document_id"] in selected_ids]
    return {"patient_id": patient_id, "query": query, "terms": terms, "results": results, "total": total,
            "documents": [{k: r[k] for k in ("id", "filename", "source_name", "format", "document_date")}
                          for r in selected],
            "coverage": {"documents": len(rows), "selected_documents": len(selected),
                         "searched_documents": len(selected) - len(warnings),
                         "unsearchable_documents": len(warnings), "warnings": warnings,
                         "first_date": min((r["document_date"] for r in rows if r["document_date"]), default=None),
                         "last_date": max((r["document_date"] for r in rows if r["document_date"]), default=None)}}
