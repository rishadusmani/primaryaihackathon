"""X12 837P professional claims -> facts.

Claims are billing artifacts, not clinical documentation: a diagnosis on a
claim means "billed for", so these facts get lower confidence and never set a
condition's status on their own.
"""

from __future__ import annotations

import re

from ..model import fact


def _date(s: str | None) -> str | None:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def parse(content: str) -> list[dict]:
    text = content.strip()
    seg_term = text[105] if text.startswith("ISA") and len(text) > 106 else "~"
    elem = text[3] if text.startswith("ISA") else "*"
    comp = text[104] if text.startswith("ISA") and len(text) > 105 else ":"
    segments = [s.strip().split(elem) for s in text.split(seg_term) if s.strip()]

    facts: list[dict] = []
    m = "claims"
    claim_id = None
    patient: dict = {}
    payer = member_id = group = None
    rendering = None
    claim_date = None
    dx: list[tuple[str, str]] = []
    lines: list[tuple[str, str | None, str]] = []
    nm1_ctx = None
    line_no = 0
    for i, s in enumerate(segments):
        tag = s[0]
        g = lambda n: s[n] if n < len(s) else ""  # noqa: E731
        if tag == "SBR":
            group = g(3) or group
        elif tag == "NM1":
            nm1_ctx = g(1)
            if nm1_ctx == "IL":
                patient.update(name_family=g(3).title() or None, name_given=g(4).title() or None)
                member_id = g(9) or member_id
            elif nm1_ctx == "PR":
                payer = g(3) or payer
            elif nm1_ctx == "82":
                rendering = " ".join(filter(None, [g(4).title(), g(3).title()])) or g(3)
        elif tag == "N3" and nm1_ctx == "IL":
            patient["address"] = g(1)
        elif tag == "N4" and nm1_ctx == "IL":
            patient["address"] = ", ".join(filter(None, [patient.get("address"), g(1), g(2), g(3)]))
        elif tag == "DMG" and nm1_ctx == "IL":
            patient.update(dob=_date(g(2)), sex={"F": "female", "M": "male"}.get(g(3)))
        elif tag == "CLM":
            claim_id = g(1)
        elif tag == "HI":
            for el in s[1:]:
                parts = el.split(comp)
                if len(parts) >= 2 and parts[0] in ("ABK", "ABF", "BK", "BF"):
                    dx.append((parts[1], f"HI[{i}]"))
        elif tag == "SV1":
            line_no += 1
            proc = g(1).split(comp)
            lines.append((proc[1] if len(proc) > 1 else proc[0], None, f"SV1[{line_no}]"))
        elif tag == "DTP" and g(1) in ("472", "431", "434"):
            d = _date(g(3).split("-")[0])
            if g(1) == "472" and lines:
                code, _, loc = lines[-1]
                lines[-1] = (code, d, loc)
            claim_date = claim_date or d

    if patient:
        facts.append(fact("patient", locator="NM1*IL", method=m,
                          identifiers=[{"system": f"member:{payer or 'payer'}", "value": member_id}] if member_id
                          else None, **patient))
    if payer or member_id:
        facts.append(fact("coverage", locator="NM1*PR", method=m, payer=payer, member_id=member_id, group=group))
    for code, loc in dx:
        facts.append(fact("condition", locator=f"CLM[{claim_id}]/{loc}", method=m, code=code, system="icd10",
                          recorded=claim_date, billed_only=True))
    for code, d, loc in lines:
        facts.append(fact("procedure", locator=f"CLM[{claim_id}]/{loc}", method=m, code=code, system="cpt",
                          date=d or claim_date))
    if claim_date:
        facts.append(fact("encounter", locator=f"CLM[{claim_id}]", method=m, type="professional claim",
                          date=claim_date, provider=rendering, claim_id=claim_id))
    return facts
