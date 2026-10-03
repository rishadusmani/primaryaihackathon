"""Format detection + dispatch. Callers can pass a hint; otherwise we sniff."""

from __future__ import annotations

import json
import re

from . import ccda, csv_portal, fhir, hl7v2, llm, pdf, text, x12

FORMATS = ("fhir", "hl7v2", "ccda", "x12_837", "csv", "pdf", "text")


def detect(data: bytes, filename: str | None = None, content_type: str | None = None) -> str:
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    head = data[:2048].lstrip()
    if head.startswith(b"%PDF") or "pdf" in ct or name.endswith(".pdf"):
        return "pdf"
    s = head.decode("utf-8", "ignore")
    if s.startswith("MSH") and len(s) > 8:
        return "hl7v2"
    if s.startswith("ISA") or re.match(r"^(ST|GS)\*", s):
        return "x12_837"
    if s.startswith("{"):
        try:
            if "resourceType" in json.loads(data.decode("utf-8")):
                return "fhir"
        except (ValueError, UnicodeDecodeError):
            pass
    if s.startswith("<") and ("ClinicalDocument" in s or "urn:hl7-org:v3" in s):
        return "ccda"
    if name.endswith(".csv") or "csv" in ct:
        return "csv"
    first = s.splitlines()[0] if s.splitlines() else ""
    if first.count(",") >= 2 and re.search(r"(test|component|result|value|analyte)", first, re.I):
        return "csv"
    return "text"


def parse(fmt: str, data: bytes, *, use_llm: bool | None = None) -> tuple[list[dict], dict]:
    """Return (facts, info). `use_llm`: None = only when needed and available."""
    info: dict = {"format": fmt, "extractors": []}
    if fmt == "fhir":
        facts = fhir.parse(data.decode("utf-8"))
    elif fmt == "hl7v2":
        facts = hl7v2.parse(data.decode("utf-8", "replace"))
    elif fmt == "ccda":
        facts = ccda.parse(data.decode("utf-8"))
    elif fmt == "x12_837":
        facts = x12.parse(data.decode("utf-8", "replace"))
    elif fmt == "csv":
        facts = csv_portal.parse(data.decode("utf-8-sig"))
    elif fmt in ("pdf", "text"):
        return _unstructured(fmt, data, use_llm, info)
    else:
        raise ValueError(f"Unsupported format {fmt}; expected one of {FORMATS}")
    info["extractors"].append(f"{fmt}_parser")
    return facts, info


def _unstructured(fmt: str, data: bytes, use_llm: bool | None, info: dict) -> tuple[list[dict], dict]:
    raw_text = None
    if fmt == "pdf":
        raw_text, pinfo = pdf.extract_text(data)
        info["pdf"] = pinfo
    else:
        raw_text = data.decode("utf-8", "replace")
    facts: list[dict] = []
    if raw_text and raw_text.strip():
        facts = text.parse(raw_text)
        info["extractors"].append("rule_nlp")
        info["text_chars"] = len(raw_text)
    needs_ocr = fmt == "pdf" and info["pdf"]["needs_ocr"]
    want_llm = use_llm if use_llm is not None else (needs_ocr and llm.available())
    if want_llm:
        if not llm.available():
            info["warnings"] = ["LLM extraction requested but anthropic SDK/credentials are unavailable."]
        else:
            usage: dict = {}
            llm_facts = llm.extract(text=None if needs_ocr else raw_text, pdf=data if fmt == "pdf" else None,
                                    usage=usage)
            facts.extend(llm_facts)
            if usage:
                info["llm_usage"] = usage
            info["extractors"].append(f"llm:{llm.MODEL}")
    elif needs_ocr:
        info.setdefault("warnings", []).append(
            "PDF has no text layer (scanned fax). Enable LLM extraction (ANTHROPIC_API_KEY) to read it.")
    return facts, info
