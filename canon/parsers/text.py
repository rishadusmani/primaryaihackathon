"""Unstructured clinical text (OCR'd faxes, PDFs, scanned letters, portal
messages, dictated notes) -> facts, using deterministic rules:

* OCR clean-up of digit look-alikes ("7.l" -> "7.1", "5OO mg" -> "500 mg")
* section detection (Medications / Allergies / Assessment / Labs / Family history ...)
* dictionary matching against the terminology layer, longest phrase first
* negation and family-history suppression ("denies chest pain", "mother had diabetes")
* escalation: conditions dropped because a cue made the rules unsure (negation, a relative,
  a hedge) are reported to `review` so a model can confirm or restore them (see assertion.py)
* medication sig parsing (dose, route, frequency) and stop/start intent
* lab and vital value extraction with units and nearby dates

This catches the bulk of what matters in typical referral faxes. For anything
it can't map, the LLM extractor (parsers/llm.py) is used as a second pass.
"""

from __future__ import annotations

import re

from .. import terminology as T
from ..model import fact

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], start=1)}
DATE_RX = re.compile(
    r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b|\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)

SECTION_ALIASES = {
    "medications": ["medications", "current medications", "meds", "medication list", "home medications",
                    "active medications", "outpatient medications", "rx"],
    "allergies": ["allergies", "allergy", "drug allergies", "allergies/adverse reactions"],
    "problems": ["problem list", "active problems", "problems", "past medical history", "pmh", "medical history",
                 "diagnoses", "diagnosis", "dx", "chronic conditions"],
    "assessment": ["assessment", "assessment and plan", "assessment/plan", "a/p", "impression", "plan",
                   "impression/plan", "reason for referral", "referral reason"],
    "labs": ["labs", "lab results", "laboratory", "results", "recent labs", "pertinent labs", "laboratory data"],
    "vitals": ["vitals", "vital signs", "vs"],
    "family_history": ["family history", "fh", "fhx", "family hx"],
    "social_history": ["social history", "sh", "social hx"],
    "immunizations": ["immunizations", "vaccines", "immunization history", "vaccinations"],
    "hpi": ["hpi", "history of present illness", "subjective", "chief complaint", "cc"],
    "ros": ["ros", "review of systems"],
}
_SECTION_LOOKUP = {a: k for k, v in SECTION_ALIASES.items() for a in v}
HEADING_RX = re.compile(r"^\s*([A-Za-z][A-Za-z /&]{0,40}?)\s*:\s*(.*)$")

NEGATION_RX = re.compile(r"\b(no|denies|denied|negative for|without|not|never|rule out|r/o|ruled out|"
                         r"no history of|no hx of|free of|resolved)\b[^.;]*$", re.I)
# "No improvement in X" / "without change in X" are about X getting worse or staying put, not X being absent.
PSEUDO_NEG_RX = re.compile(r"\b(no|without) (significant |further )?(improvement|change|better|worse|response)\b|"
                           r"\bnot (yet )?(improv\w*|better|controlled|well[- ]controlled|at goal)\b", re.I)
# Someone other than the patient is the subject.
RELATIVE_RX = re.compile(r"\b(mother|father|mom|dad|parents?|sisters?|brothers?|siblings?|sons?|daughters?|aunts?|"
                         r"uncles?|grand(mother|father|parents?)|cousins?|wife|husband|spouse|partner|family history of|"
                         r"fhx? of|fh of)\b", re.I)
# Not (yet) established: possible, suspected, being ruled in or out.
HEDGE_RX = re.compile(r"\b(possible|possibly|probable|probably|suspected|suspect|suspicion of|concern(ing)? for|likely|"
                      r"questionable|cannot exclude|can't exclude|consider|evaluate for|screen(ing)? for|risk of|"
                      r"at risk for|vs\.?|versus)\b", re.I)
# Cues that follow the condition in the same clause ("depression screen negative", "asthma vs COPD").
# Narrower than the before-cues: a bare "no"/"not" after a term usually belongs to something else
# ("asthma exacerbation, no wheezing"; "hypertension, not well controlled").
POST_NEGATION_RX = re.compile(r"\b(negative|neg|ruled out|r/o'?d|unlikely|excluded|absent|not present|not found|"
                              r"not seen|resolved)\b", re.I)
POST_RELATIVE_RX = re.compile(r"\b(runs in (the|his|her) family|in (the|his|her) family|family history|"
                              r"in (his|her|the patient's) (mother|father|sister|brother|parents?|son|daughter))\b", re.I)
POST_HEDGE_RX = re.compile(r"(\?|\b(vs\.?|versus|suspected|possible|probable|likely|questionable|pending)\b)", re.I)
STOP_RX = re.compile(r"\b(stop|stopped|discontinue|discontinued|d/c|dc'd|hold|held|off)\b", re.I)
DOSE_RX = re.compile(r"(\d+(?:\.\d+)?(?:\s*[/-]\s*\d+(?:\.\d+)?)?)\s*(mg|mcg|µg|g|units?|u|ml|mL|puffs?|tabs?|"
                     r"tablets?|capsules?|caps?|drops?|%)\b", re.I)
NOISE_RX = re.compile(r"^\s*(page \d+ of \d+|fax|from:|to:|confidential|this fax|.*\bfax\b.*\d{3}.\d{4}).*$", re.I)


def _date(m: re.Match) -> str | None:
    try:
        if m.group(1):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        elif m.group(4):
            mo, d, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
            y = y + 2000 if y < 100 else y
        else:
            mo, d, y = MONTHS[m.group(7).lower()[:3]], int(m.group(8)), int(m.group(9))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except (TypeError, ValueError):
        pass
    return None


def ocr_clean(text: str) -> tuple[str, int]:
    """Fix common OCR confusions inside numbers. Returns (text, fixes)."""
    fixes = 0

    def num(m: re.Match) -> str:
        nonlocal fixes
        s = m.group(0)
        t = s.replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1").replace("S", "5")
        if t != s:
            fixes += 1
        return t

    # tokens that are mostly digits but contain look-alike letters, e.g. 5OO, 7.l, l0
    text = re.sub(r"(?<![A-Za-z])(?=[\dOolIS.]*\d)[\dOolIS]+(?:\.[\dOolIS]+)?(?![A-Za-z])", num, text)
    for bad, good in (("rng", "mg"), ("mgldL", "mg/dL"), ("mg/dl", "mg/dL")):
        if bad in text:
            fixes += text.count(bad) if bad != "mg/dl" else 0
            text = text.replace(bad, good)
    return text, fixes


def _sections(lines: list[str]) -> list[tuple[str, int, str]]:
    """Yield (section, line_no, text) for each content line."""
    out = []
    current = "body"
    for n, line in enumerate(lines, start=1):
        m = HEADING_RX.match(line)
        if m and m.group(1).strip().lower() in _SECTION_LOOKUP:
            current = _SECTION_LOOKUP[m.group(1).strip().lower()]
            rest = m.group(2).strip()
            if rest:
                out.append((current, n, rest))
            continue
        bare = line.strip().rstrip(":").lower()
        if bare in _SECTION_LOOKUP and len(bare) > 1:
            current = _SECTION_LOOKUP[bare]
            continue
        if line.strip():
            out.append((current, n, line.strip()))
    return out


def _header(text: str) -> dict:
    def grab(rx: str) -> str | None:
        m = re.search(rx, text, re.I | re.M)
        return m.group(1).strip() if m else None

    out: dict = {}
    name = grab(r"^\s*(?:patient(?: name)?|pt(?: name)?|name|re)\s*:\s*([A-Za-z' ,.-]+?)\s*(?:\bDOB\b|\bMRN\b|$)")
    if name:
        if "," in name:
            fam, giv = [p.strip() for p in name.split(",", 1)]
        else:
            parts = name.split()
            giv, fam = " ".join(parts[:-1]), parts[-1] if parts else None
        out.update(name_given=giv.title() or None, name_family=(fam or "").title() or None)
    dob = re.search(r"\b(?:DOB|date of birth|D\.O\.B\.)\s*:?\s*(\S+(?:\s+\d{1,2},?\s+\d{4})?)", text, re.I)
    if dob:
        dm = DATE_RX.search(dob.group(1))
        out["dob"] = _date(dm) if dm else None
    mrn = grab(r"\bMRN\s*[:#]?\s*([A-Z0-9-]+)")
    if mrn:
        out["identifiers"] = [{"system": "MRN", "value": mrn}]
    sex = grab(r"\b(?:sex|gender)\s*:\s*(male|female|m|f)\b")
    if sex:
        out["sex"] = "female" if sex.lower().startswith("f") else "male"
    return out


def _service_date(text: str) -> str | None:
    m = re.search(r"(?:date of service|dos|visit date|date of visit|encounter date|date)\s*:\s*(.{6,20})", text, re.I)
    if m:
        dm = DATE_RX.search(m.group(1))
        if dm:
            return _date(dm)
    return None


def _nearby_date(s: str) -> str | None:
    m = DATE_RX.search(s)
    return _date(m) if m else None


def parse(content: str, *, method: str = "rule_nlp", review: list[dict] | None = None) -> list[dict]:
    """Extract facts. Conditions the rules drop as uncertain are appended to `review` when given."""
    raw_lines = content.replace("\r", "\n").split("\n")
    lines = [l for l in raw_lines if not NOISE_RX.match(l)]
    cleaned, fixes = ocr_clean("\n".join(lines))
    lines = cleaned.split("\n")
    base_conf = 0.80 if fixes == 0 else 0.72
    facts: list[dict] = []

    head = _header(cleaned)
    if head:
        facts.append(fact("patient", locator="header", method=method, confidence=base_conf, **head))
    dos = _service_date(cleaned)
    if dos:
        facts.append(fact("encounter", locator="header", method=method, confidence=base_conf, type="clinic note",
                          date=dos, facility=None))

    seen_meds: set[tuple] = set()
    for section, n, line in _sections(lines):
        loc = f"line {n}"
        low = line.lower()
        if section in ("family_history", "social_history", "ros"):
            continue

        # ---- allergies
        if section == "allergies":
            if re.search(T.NKDA_PATTERNS, low):
                facts.append(fact("allergy", locator=loc, method=method, snippet=line, confidence=base_conf,
                                  no_known_allergies=True))
                continue
            for chunk in re.split(r"[;,]|\band\b", line):
                a = T.lookup_allergen(chunk)
                if a:
                    rx = re.search(r"\(([^)]+)\)|[-–:]\s*([a-z ]+)$", chunk, re.I)
                    reaction = (rx.group(1) or rx.group(2)).strip() if rx else None
                    facts.append(fact("allergy", locator=loc, method=method, snippet=line, confidence=base_conf,
                                      text=chunk.strip(" -•*"), reaction=reaction))
            continue
        if re.search(T.NKDA_PATTERNS, low):
            facts.append(fact("allergy", locator=loc, method=method, snippet=line, confidence=base_conf * 0.9,
                              no_known_allergies=True))

        # ---- explicit ICD-10 codes anywhere: "Type 2 DM (E11.9)"
        for m in re.finditer(r"\b([A-TV-Z]\d{2}(?:\.\d{1,4})?)\b", line):
            code = m.group(1)
            if T.lookup_condition(code=code, system="icd10") and not _negated(line, m.start()):
                facts.append(fact("condition", locator=loc, method=method, snippet=line, confidence=base_conf + 0.1,
                                  code=code, system="icd10", status="active", recorded=dos))

        # ---- conditions by name
        if section in ("problems", "assessment", "body", "hpi"):
            conf = base_conf if section in ("problems", "assessment") else base_conf - 0.15
            taken: list[tuple[int, int]] = []
            plain_low = _plain(low)
            for phrase, icd in T.condition_synonyms():
                if len(phrase) <= 3 and section not in ("problems", "assessment"):
                    continue
                if phrase not in plain_low:  # cheap pre-check; avoids compiling hundreds of regexes per line
                    continue
                for m in re.finditer(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", plain_low):
                    if any(a <= m.start() < b for a, b in taken):
                        continue
                    taken.append((m.start(), m.end()))
                    reason = _uncertain(low, m.start(), m.end())
                    if reason:
                        if review is not None:
                            review.append({"term": phrase, "sentence": _sentence(line, m.start(), m.end()),
                                           "locator": loc, "reason": reason,
                                           "fact": {"text": phrase, "mapped_code": icd, "recorded": dos,
                                                    "snippet": line}})
                        continue
                    status = "resolved" if re.search(r"\b(resolved|history of|h/o|s/p)\b", low) and \
                        section != "assessment" else "active"
                    if phrase == "diabetes" and "type 1" in low:
                        continue
                    facts.append(fact("condition", locator=loc, method=method, snippet=line, confidence=conf,
                                      text=phrase, mapped_code=icd, status=status, recorded=dos))

        # ---- medications (any section; plan lines often change meds)
        if section in ("medications", "assessment", "body", "hpi", "problems") and not re.search(r"allerg", low):
            hits: list[tuple[int, int, str, str]] = []
            plain = _plain(low)
            for phrase, ing in T.medication_synonyms():
                if phrase not in plain:
                    continue
                for mm in re.finditer(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", plain):
                    if not any(a <= mm.start() < b for a, b, _, _ in hits) and \
                            not any(h[3] == ing for h in hits):
                        hits.append((mm.start(), mm.end(), phrase, ing))
            hits.sort()
            for idx, (a, b, phrase, ing) in enumerate(hits):
                seg_end = hits[idx + 1][0] if idx + 1 < len(hits) else len(line)
                after = line[a:seg_end]
                before = low[max(0, a - 30):a]
                dose = DOSE_RX.search(after[:60])
                stopped = bool(STOP_RX.search(re.split(r"[.;,]", before)[-1] + " " +
                                             re.split(r"[.;]", after.lower())[0]))
                intent = re.search(r"\b(start|begin|initiate|increase|decrease|continue|taking|on|titrate|refill)\b",
                                   low)
                if section != "medications" and not (dose or stopped or intent):
                    continue
                if (ing, line) in seen_meds:
                    continue
                seen_meds.add((ing, line))
                freq = T.parse_frequency(after)
                changed = re.search(r"\b(increase|titrate|uptitrate|decrease|reduce)\b", before + after.lower())
                facts.append(fact("medication", locator=loc, method=method, snippet=line,
                                  confidence=base_conf if section == "medications" else base_conf - 0.05,
                                  text=phrase, dose_text=dose.group(0) if dose else None, route_text=after[:80],
                                  frequency_text=after[:80] if freq else None,
                                  status="stopped" if stopped else "active",
                                  change="dose_change" if changed and not stopped else None, start=dos))

        # ---- vitals: blood pressure "BP 142/88"
        for m in re.finditer(r"\b(?:bp|blood pressure)\s*[:=]?\s*(\d{2,3})\s*/\s*(\d{2,3})", low):
            d = _nearby_date(line) or dos
            for code, v in (("8480-6", m.group(1)), ("8462-4", m.group(2))):
                facts.append(fact("observation", locator=loc, method=method, snippet=line, confidence=base_conf,
                                  code=code, system="loinc", value=v, unit="mmHg", effective=d))

        # ---- labs and vitals by name (longest synonym first; skip overlapping shorter matches)
        obs_taken: list[tuple[int, int]] = []
        for phrase, loinc in T.observation_synonyms():
            if loinc in ("8480-6", "8462-4"):
                continue
            short = len(phrase) <= 3
            if short and section not in ("labs", "vitals"):
                continue
            if phrase not in low:
                continue
            rx = (rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])\s*(?:\([^)]*\))?\s*(?:level|value|was|of|is|=|:|-)?"
                  rf"\s*(?:was|of|is)?\s*([<>]?\d+(?:\.\d+)?)\s*([a-zA-Z%/µ\[\]\d.*^]+(?:/[a-zA-Z0-9.]+)?)?")
            for m in re.finditer(rx, line, re.I):
                if any(a <= m.start() < b for a, b in obs_taken) or _negated(low, m.start()):
                    continue
                obs_taken.append((m.start(), m.end()))
                unit = m.group(2)
                unit = unit.rstrip(".,;") if unit and T.is_unit(unit) else None
                if loinc == "8310-5" and unit is None:
                    unit = "F" if float(m.group(1).lstrip("<>")) > 50 else "C"
                facts.append(fact("observation", locator=loc, method=method, snippet=line, confidence=base_conf,
                                  code=loinc, system="loinc", text=phrase, value=m.group(1), unit=unit,
                                  effective=_nearby_date(line[m.end():m.end() + 30]) or _nearby_date(line) or dos))
        # ---- immunizations
        if section == "immunizations" or re.search(r"\b(vaccine|vaccinated|immuniz|shot|booster)\b", low):
            v = T.lookup_vaccine(text=low)
            if v and not _negated(low, low.find(v["vaccine"].split("-")[0])):
                facts.append(fact("immunization", locator=loc, method=method, snippet=line, confidence=base_conf,
                                  text=v["vaccine"], code=v["cvx"], date=_nearby_date(line)))
    return facts


def _plain(s: str) -> str:
    """Lower-case alnum view of s with identical character offsets."""
    return re.sub(r"[^a-z0-9]", " ", s.lower())


def _clause_before(low: str, pos: int) -> str:
    """Text between the start of the current clause and `pos` (at most 60 characters)."""
    window = low[max(0, pos - 60):pos]
    return re.split(r"[.;:]|\bbut\b", window)[-1]


def _negated(low: str, pos: int) -> bool:
    return bool(NEGATION_RX.search(PSEUDO_NEG_RX.sub(" ", _clause_before(low, pos))))


def _sentence(line: str, start: int, end: int) -> str:
    """The sentence of `line` containing line[start:end]; a period only ends a sentence before whitespace."""
    bounds = [m.end() for m in re.finditer(r"[.;!?](?=\s|$)", line)]
    begin = max([b for b in bounds if b <= start], default=0)
    finish = min([b for b in bounds if b >= end], default=len(line))
    return HEADING_RX.sub(r"\2", line[begin:finish].strip()) if begin == 0 else line[begin:finish].strip()


def _clause_after(low: str, end: int) -> str:
    """Text from `end` to the end of the current clause (at most 60 characters)."""
    return re.split(r"[.;]|\bbut\b", low[end:end + 60])[0]


def _uncertain(low: str, start: int, end: int) -> str | None:
    """Why the rules should not assert a condition at low[start:end], or None if it reads as a plain mention."""
    before, after = _clause_before(low, start), _clause_after(low, end)
    if NEGATION_RX.search(PSEUDO_NEG_RX.sub(" ", before)) or POST_NEGATION_RX.search(after):
        return "negated"
    if RELATIVE_RX.search(before) or POST_RELATIVE_RX.search(after):
        return "relative"
    if HEDGE_RX.search(before) or POST_HEDGE_RX.search(after):
        return "hedged"
    return None
