"""Clinical timestamps: one ISO 8601 value whose precision is what the source said.

    "2026-03-02"              that day; the time is unknown
    "2026-03-02T16:40"        a clock time with no zone (read as local time)
    "2026-03-02T16:40-05:00"  an instant

A missing time is never filled in (00:00 or 12:00 would invent an order no source gave).
Two statements are put in order by time only when both have one and the times are
comparable: both with a zone (compared as instants) or both without (both local). A
zoned and an unzoned time on the same day can't be ordered, so a disagreement between
them stays a conflict.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?")
_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(?:\d{2}(?:\.\d+)?)?\s*(Z|[+-]\d{4})?)?")


def stamp(y: str, mo: str, d: str, hh: str | None = None, mm: str | None = None, zone: str | None = None) -> str:
    out = f"{y}-{mo}-{d}"
    if hh is None or mm is None:
        return out
    out += f"T{hh}:{mm}"
    if zone:
        out += "+00:00" if zone == "Z" else f"{zone[:3]}:{zone[-2:]}"
    return out


def from_iso(s: str | None) -> str | None:
    """FHIR / ISO 8601 date or dateTime -> stamp ("2026-03-02T16:40:00Z" -> "2026-03-02T16:40+00:00")."""
    m = _ISO.match((s or "").strip())
    return stamp(*m.groups()) if m else None


def from_compact(s: str | None) -> str | None:
    """HL7 v2 / C-CDA TS ("202603021640-0500") -> stamp ("2026-03-02T16:40-05:00")."""
    m = _COMPACT.match((s or "").strip())
    return stamp(*m.groups()) if m else None


def day(s: str | None) -> str | None:
    """The calendar day, as the source wrote it."""
    return s[:10] if s and _ISO.match(s) else None


def has_time(s: str | None) -> bool:
    return bool(s and len(s) > 10)


def _zoned(s: str) -> bool:
    return len(s) > 16


def comparable(a: str | None, b: str | None) -> bool:
    """Both carry a time of day and the two can be ordered (both zoned, or both local)."""
    return has_time(a) and has_time(b) and _zoned(a) == _zoned(b)


def moment(s: str | None) -> str:
    """Sort key within a day: the UTC instant for a zoned time, the clock time for a local one, "" for day-only.
    Only meaningful between comparable stamps."""
    if not has_time(s):
        return ""
    if not _zoned(s):
        return s[:16]
    sign = -1 if s[16] == "-" else 1
    offset = timedelta(hours=int(s[17:19]), minutes=int(s[20:22])) * sign
    t = datetime.strptime(s[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone(offset))
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def before(a: str | None, b: str | None) -> bool:
    """`a` is known to come before `b`."""
    if day(a) and day(b) and day(a) != day(b):
        return day(a) < day(b)
    return comparable(a, b) and moment(a) < moment(b)
