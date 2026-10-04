"""CSV exports from patient portals / lab websites -> observation facts.

Column names vary wildly between portals, so headers are matched by synonym.
"""

from __future__ import annotations

import csv
import io
import re

from .. import dates
from ..model import fact
from .text import time_after

COLUMNS = {
    "text": ["test", "test name", "component", "name", "analyte", "result name", "lab", "description"],
    "value": ["value", "result", "result value", "your value", "measurement"],
    "unit": ["unit", "units", "uom"],
    "ref_range": ["reference range", "ref range", "range", "normal range", "standard range"],
    "flag": ["flag", "abnormal", "abnormal flag", "status flag"],
    "effective": ["date", "collected", "collection date", "result date", "date collected", "observation date"],
    "code": ["loinc", "loinc code", "code"],
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z ]", "", h.lower()).strip()


def _date(s: str | None) -> str | None:
    """A date cell, with its time ("2026-03-02 08:15", "3/2/26 4:30 PM") and zone ("...T08:15Z") when given."""
    if not s:
        return None
    s = s.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return dates.from_iso(s)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if not m:
        return None
    y = m.group(3)
    y = ("20" + y) if len(y) == 2 else y
    date = f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    t = time_after(s[m.end():])
    return f"{date}T{t}" if t else date



def parse(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content.strip()))
    mapping = {}
    for h in reader.fieldnames or []:
        for target, syns in COLUMNS.items():
            if _norm(h) in syns and target not in mapping:
                mapping[target] = h
    if "text" not in mapping or "value" not in mapping:
        raise ValueError(f"CSV needs a test-name and value column; got {reader.fieldnames}")
    facts = []
    for i, row in enumerate(reader, start=2):
        get = lambda k: (row.get(mapping[k]) or "").strip() if k in mapping else None  # noqa: E731
        val = get("value")
        if not val:
            continue
        unit = get("unit")
        if not unit:  # "7.8 %" in a single column
            mm = re.match(r"^\s*([<>]?\d+(?:\.\d+)?)\s*(\S.*)?$", val)
            if mm:
                val, unit = mm.group(1), (mm.group(2) or None)
        facts.append(fact("observation", locator=f"row {i}", method="structured", snippet=",".join(row.values()),
                          confidence=0.95, text=get("text"), code=get("code"), value=val, unit=unit,
                          ref_range=get("ref_range"), flag=get("flag"), effective=_date(get("effective"))))
    return facts
