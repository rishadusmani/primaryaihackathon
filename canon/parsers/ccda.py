"""HL7 C-CDA XML (CCD, discharge summaries, portal/TEFCA exports) -> facts."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..model import fact

SECTIONS = {
    "11450-4": "problems", "10160-0": "medications", "48765-2": "allergies", "30954-2": "results",
    "8716-3": "vitals", "11369-6": "immunizations", "47519-4": "procedures", "46240-8": "encounters",
}


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for k in list(el.attrib):
            if "}" in k:
                el.attrib[k.split("}", 1)[1]] = el.attrib.pop(k)
    return root


def _date(s: str | None) -> str | None:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _eff(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    et = el.find("effectiveTime")
    if et is None:
        return None
    return _date(et.get("value")) or _date((et.find("low") if et.find("low") is not None else et).get("value"))


def _code(el: ET.Element | None) -> dict:
    if el is None:
        return {}
    text = el.get("displayName")
    ot = el.find("originalText")
    if not text and ot is not None and (ot.text or "").strip():
        text = ot.text.strip()
    return {"code": el.get("code"), "system": el.get("codeSystem"), "text": text}


def parse(content: str) -> list[dict]:
    root = _strip_ns(ET.fromstring(content.encode() if isinstance(content, str) else content))
    facts: list[dict] = []
    m = "structured"

    pr = root.find("./recordTarget/patientRole")
    if pr is not None:
        pt = pr.find("patient")
        name = pt.find("name") if pt is not None else None
        addr = pr.find("addr")
        facts.append(fact(
            "patient", locator="recordTarget", method=m,
            name_given=" ".join((g.text or "").strip() for g in name.findall("given")) if name is not None else None,
            name_family=(name.findtext("family") or "").strip() if name is not None else None,
            dob=_date(pt.find("birthTime").get("value")) if pt is not None and pt.find("birthTime") is not None
            else None,
            sex={"F": "female", "M": "male"}.get(pt.find("administrativeGenderCode").get("code"))
            if pt is not None and pt.find("administrativeGenderCode") is not None else None,
            identifiers=[{"system": i.get("root"), "value": i.get("extension")} for i in pr.findall("id")
                         if i.get("extension")],
            address=", ".join(filter(None, [addr.findtext("streetAddressLine"), addr.findtext("city"),
                                            addr.findtext("state"), addr.findtext("postalCode")]))
            if addr is not None else None,
            phone=(pr.find("telecom").get("value", "").replace("tel:", "") if pr.find("telecom") is not None
                   else None)))

    for si, section in enumerate(root.iter("section")):
        code_el = section.find("code")
        kind = SECTIONS.get(code_el.get("code") if code_el is not None else "")
        if not kind:
            continue
        for ei, entry in enumerate(section.findall("entry")):
            loc = f"section[{kind}]/entry[{ei + 1}]"
            if kind == "problems":
                for obs in entry.iter("observation"):
                    v = obs.find("value")
                    if v is None or not v.get("code"):
                        continue
                    status_obs = None
                    for er in obs.findall("entryRelationship/observation"):
                        if (er.find("code") is not None and er.find("code").get("code") == "33999-4"):
                            status_obs = er.find("value")
                    status = (status_obs.get("displayName") or "").lower() if status_obs is not None else None
                    facts.append(fact("condition", locator=loc, method=m, **_code(v),
                                      onset=_eff(obs), status=status or None))
                    break
            elif kind == "medications":
                sa = entry.find(".//substanceAdministration")
                if sa is None:
                    continue
                mat = sa.find("./consumable/manufacturedProduct/manufacturedMaterial/code")
                dq = sa.find("doseQuantity")
                route = sa.find("routeCode")
                freq = None
                for et in sa.findall("effectiveTime"):
                    p = et.find("period")
                    if p is not None and p.get("unit") == "h":
                        hrs = float(p.get("value"))
                        freq = {24: "daily", 12: "BID", 8: "TID", 6: "QID"}.get(int(hrs), f"every {hrs:g} hours")
                    elif p is not None and p.get("unit") in ("wk", "w"):
                        freq = "weekly"
                status = (sa.find("statusCode").get("code") if sa.find("statusCode") is not None else None)
                facts.append(fact("medication", locator=loc, method=m, **_code(mat),
                                  dose_text=f"{dq.get('value')} {dq.get('unit', '')}".strip() if dq is not None
                                  else None, route_text=route.get("displayName") if route is not None else None,
                                  frequency_text=freq, start=_eff(sa),
                                  status={"completed": "stopped", "active": "active"}.get(status, status)))
            elif kind == "allergies":
                obs = entry.find(".//entryRelationship/observation")
                if obs is None:
                    obs = entry.find(".//observation")
                if obs is None:
                    continue
                if obs.get("negationInd") == "true":
                    facts.append(fact("allergy", locator=loc, method=m, no_known_allergies=True))
                    continue
                pe = obs.find(".//participant/participantRole/playingEntity/code")
                reaction = status = None
                for er in obs.findall("entryRelationship/observation"):
                    v = er.find("value")
                    if v is None or not v.get("displayName"):
                        continue
                    if er.find("code") is not None and er.find("code").get("code") == "33999-4":
                        status = v.get("displayName").lower()
                    else:
                        reaction = v.get("displayName")
                facts.append(fact("allergy", locator=loc, method=m, **_code(pe), reaction=reaction, status=status))
            elif kind in ("results", "vitals"):
                for oi, obs in enumerate(entry.iter("observation")):
                    v = obs.find("value")
                    if v is None or v.get("value") is None:
                        continue
                    interp = obs.find("interpretationCode")
                    rr = obs.find(".//referenceRange/observationRange/text")
                    facts.append(fact("observation", locator=f"{loc}/observation[{oi + 1}]", method=m,
                                      **_code(obs.find("code")), value=v.get("value"), unit=v.get("unit"),
                                      effective=_eff(obs), flag=interp.get("code") if interp is not None else None,
                                      ref_range=rr.text if rr is not None else None))
            elif kind == "immunizations":
                sa = entry.find(".//substanceAdministration")
                if sa is None:
                    continue
                mat = sa.find("./consumable/manufacturedProduct/manufacturedMaterial/code")
                c = _code(mat)
                facts.append(fact("immunization", locator=loc, method=m, text=c.get("text"), code=c.get("code"),
                                  date=_eff(sa)))
            elif kind == "procedures":
                p = entry.find(".//procedure")
                if p is not None:
                    facts.append(fact("procedure", locator=loc, method=m, **_code(p.find("code")), date=_eff(p)))
            elif kind == "encounters":
                e = entry.find(".//encounter")
                if e is not None:
                    c = _code(e.find("code"))
                    facts.append(fact("encounter", locator=loc, method=m, type=c.get("text"), date=_eff(e)))
    return facts
