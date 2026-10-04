"""HL7 v2.x messages (ADT, ORU, RDE, VXU, ...) -> facts."""

from __future__ import annotations

import re

from .. import dates
from ..model import fact


def _date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s.strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


_stamp = dates.from_compact  # a clinical timestamp, kept to the precision (and zone) it was sent with


class _Msg:
    def __init__(self, raw: str):
        raw = raw.strip().replace("\r\n", "\r").replace("\n", "\r")
        lines = [l for l in raw.split("\r") if l.strip()]
        if not lines or not lines[0].startswith("MSH"):
            raise ValueError("HL7 v2 message must start with an MSH segment")
        self.fs = lines[0][3]
        enc = lines[0][4:8]
        self.cs, self.rs = enc[0], enc[1]
        self.segments = []
        for line in lines:
            parts = line.split(self.fs)
            if parts[0] == "MSH":
                parts = ["MSH", self.fs] + parts[1:]  # make MSH-n == parts[n]
            self.segments.append(parts)

    def field(self, seg: list[str], n: int) -> str:
        return seg[n] if n < len(seg) else ""

    def comp(self, value: str, n: int) -> str:
        """1-based component of the first repetition."""
        first = value.split(self.rs)[0]
        c = first.split(self.cs)
        return c[n - 1].strip() if n - 1 < len(c) else ""

    def reps(self, value: str) -> list[str]:
        return [v for v in value.split(self.rs) if v]


def parse(content: str) -> list[dict]:
    msg = _Msg(content)
    facts: list[dict] = []
    counts: dict[str, int] = {}
    obr_date = order_at = None
    m = "structured"
    for seg in msg.segments:
        name = seg[0]
        counts[name] = counts.get(name, 0) + 1
        loc = f"{name}[{counts[name]}]"
        f = lambda n: msg.field(seg, n)  # noqa: E731
        c = msg.comp
        snippet = msg.fs.join(seg[:9])
        if name == "MSH" and _date(f(7)):
            facts.append(fact("document", locator="MSH-7", method=m, generated=_date(f(7))))
        elif name == "PID":
            ids = [{"system": c(r, 4) or c(r, 5) or None, "value": c(r, 1)} for r in msg.reps(f(3))]
            addr = f(11)
            facts.append(fact("patient", locator=loc, method=m, snippet=snippet,
                              name_family=c(f(5), 1).title() or None, name_given=c(f(5), 2).title() or None,
                              dob=_date(f(7)), sex={"F": "female", "M": "male"}.get(f(8).upper(), f(8).lower() or None),
                              identifiers=[i for i in ids if i["value"]],
                              address=", ".join(filter(None, [c(addr, 1), c(addr, 3), c(addr, 4), c(addr, 5)])) or None,
                              phone=c(f(13), 1) or None))
        elif name == "PV1":
            facts.append(fact("encounter", locator=loc, method=m, snippet=snippet,
                              type={"O": "outpatient", "I": "inpatient", "E": "emergency"}.get(f(2), f(2) or None),
                              date=_stamp(f(44)),
                              provider=" ".join(filter(None, [c(f(7), 3), c(f(7), 2)])) or None,
                              facility=c(f(3), 4) or c(f(3), 1) or None))
        elif name == "DG1":
            facts.append(fact("condition", locator=loc, method=m, snippet=snippet, code=c(f(3), 1),
                              text=c(f(3), 2) or f(4) or None, system=c(f(3), 3) or None, recorded=_stamp(f(5)),
                              status="active"))
        elif name == "PRB":
            facts.append(fact("condition", locator=loc, method=m, snippet=snippet, code=c(f(3), 1),
                              text=c(f(3), 2) or None, system=c(f(3), 3) or None, recorded=_stamp(f(2)),
                              onset=_date(f(16)), status="active"))
        elif name == "AL1":
            facts.append(fact("allergy", locator=loc, method=m, snippet=snippet, code=c(f(3), 1) or None,
                              text=c(f(3), 2) or c(f(3), 1), severity={"SV": "severe", "MO": "moderate",
                                                                       "MI": "mild"}.get(c(f(4), 1), None),
                              reaction=f(5) or None))
        elif name == "OBR":
            obr_date = _stamp(f(7))
            facts.append(fact("procedure", locator=loc, method=m, snippet=snippet, code=c(f(4), 1),
                              text=c(f(4), 2) or None, system=c(f(4), 3) or None, date=obr_date))
        elif name == "OBX":
            val = f(5)
            if not val:
                continue
            facts.append(fact("observation", locator=loc, method=m, snippet=snippet, code=c(f(3), 1),
                              text=c(f(3), 2) or None, system=c(f(3), 3) or None, value=c(val, 1) if f(2) == "CE"
                              else val, unit=c(f(6), 1) or None, ref_range=f(7) or None, flag=f(8) or None,
                              effective=_stamp(f(14)) or obr_date))
        elif name == "ORC":  # the order the following RX segments belong to, and when it was placed
            order_at = _stamp(f(9))
        elif name in ("RXE", "RXO", "RXD"):
            code_field = {"RXE": 2, "RXO": 1, "RXD": 2}[name]
            g = f(code_field)
            amt, units = (f(3), c(f(5), 1)) if name == "RXE" else (f(2), c(f(4), 1))
            facts.append(fact("medication", locator=loc, method=m, snippet=snippet, code=c(g, 1),
                              text=c(g, 2) or None, system=c(g, 3) or None,
                              dose_text=f"{amt} {units}".strip() or None,
                              frequency_text=c(f(1), 2) if name == "RXE" else None, status="active",
                              as_of=order_at))
        elif name == "RXR":
            if facts and facts[-1]["kind"] == "medication":
                facts[-1]["route_text"] = c(f(1), 2) or c(f(1), 1)
        elif name == "TQ1" and facts and facts[-1]["kind"] == "medication":
            facts[-1]["frequency_text"] = c(f(3), 1) or facts[-1].get("frequency_text")
        elif name == "RXA":
            facts.append(fact("immunization", locator=loc, method=m, snippet=snippet, code=c(f(5), 1),
                              text=c(f(5), 2) or None, date=_date(f(3))))
        elif name == "IN1":
            facts.append(fact("coverage", locator=loc, method=m, snippet=snippet, payer=c(f(4), 1) or None,
                              member_id=f(36) or f(49) or None, group=f(8) or None))
    return facts
