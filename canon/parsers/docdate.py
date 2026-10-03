"""When a document was generated: the fallback date for a document whose contents carry no clinical date.

A generation timestamp is an upper bound (a list printed today may be years old), so it only stands in for
the clinical date when the document has none. Every format has somewhere to look:
C-CDA header effectiveTime, FHIR Bundle.timestamp / Composition.date / meta.lastUpdated, HL7 MSH-7,
X12 BHT-04 / GS-04 / ISA-09, PDF /CreationDate, and for faxes and letters an e-signature, report, print or
fax-header date.
"""

from __future__ import annotations

import json
import re
from datetime import date

from .text import DATE_RX, _date

# Strongest first: a signature dates the content, a fax header only dates the transmission.
TEXT_LABELS = (
    r"(?:electronically |e-?)?signed|dictated|transcribed|authenticated",
    r"report(?:ed)? date|date of report|generated|created|letter date",
    r"printed|print date|date printed|fax(?:ed)?|sent|received",
)


def generated_date(fmt: str, data: bytes, text: str | None = None) -> str | None:
    try:
        d = {"ccda": _ccda, "fhir": _fhir, "hl7v2": _hl7, "x12_837": _x12, "pdf": _pdf}.get(fmt, _none)(data)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        d = None
    if not d and text and fmt in ("pdf", "text"):
        d = _text(text)
    return d if _plausible(d) else None


def _plausible(d: str | None) -> bool:
    if not d:
        return False
    try:
        return 1900 <= date.fromisoformat(d).year <= date.today().year + 1
    except ValueError:
        return False


def _ymd(s: str | None) -> str | None:
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", (s or "").strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _none(data: bytes) -> None:
    return None


def _ccda(data: bytes) -> str | None:
    header = data.decode("utf-8", "replace").split("<component", 1)[0]
    m = re.search(r"<effectiveTime\b[^>]*\bvalue=\"(\d{8})", header)
    return _ymd(m.group(1)) if m else None


def _fhir(data: bytes) -> str | None:
    doc = json.loads(data.decode("utf-8"))
    comp = next((e.get("resource") for e in doc.get("entry") or []
                 if (e.get("resource") or {}).get("resourceType") == "Composition"), None)
    for v in (doc.get("timestamp"), (comp or {}).get("date"), (doc.get("meta") or {}).get("lastUpdated")):
        if _ymd(v):
            return _ymd(v)
    return None


def _hl7(data: bytes) -> str | None:
    msh = data.decode("utf-8", "replace").replace("\r", "\n").split("\n", 1)[0]
    return _ymd(msh.split(msh[3])[6]) if msh.startswith("MSH") and len(msh) > 3 else None


def _x12(data: bytes) -> str | None:
    text = data.decode("utf-8", "replace").strip()
    elem = text[3] if text.startswith("ISA") else "*"
    segs = {}
    for s in re.split(r"~|\n", text):
        parts = s.strip().split(elem)
        segs.setdefault(parts[0], parts)
    if len(segs.get("BHT", [])) > 4 and _ymd(segs["BHT"][4]):
        return _ymd(segs["BHT"][4])
    if len(segs.get("GS", [])) > 4 and _ymd(segs["GS"][4]):
        return _ymd(segs["GS"][4])
    isa = segs.get("ISA", [])
    if len(isa) > 9 and re.fullmatch(r"\d{6}", isa[9].strip()):
        return _ymd("20" + isa[9].strip())
    return None


def _pdf(data: bytes) -> str | None:
    m = re.search(rb"/CreationDate\s*\(D:(\d{8})", data) or re.search(rb"/ModDate\s*\(D:(\d{8})", data)
    return _ymd(m.group(1).decode()) if m else None


def _text(text: str) -> str | None:
    lines = text.replace("\r", "\n").split("\n")
    for labels in TEXT_LABELS:
        rx = re.compile(rf"\b(?:{labels})\b", re.I)
        for line in lines:
            lm = rx.search(line)
            dm = lm and DATE_RX.search(line, lm.end())
            if dm and _date(dm):
                return _date(dm)
    return None
