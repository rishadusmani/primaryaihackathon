"""Build canon/vocab/*.json from the curated lists below, verifying every code online.

    python scripts/build_vocab.py           # verify + write
    python scripts/build_vocab.py --check   # verify only, exit 1 on any problem

canon/terminology.py loads these files on top of its hand-written tables. Codes
and synonyms are curated here; official names come from the source of truth:

- LOINC:      NLM Clinical Tables  (clinicaltables.nlm.nih.gov/api/loinc_items)
- ICD-10-CM:  NLM Clinical Tables  (current billable codes only)
- SNOMED CT:  tx.fhir.org $lookup  (International Edition; must be active)
- RxNorm:     NLM RxNav            (ingredient IN, or PIN when no IN exists;
                                    every brand synonym must map to that ingredient)
- Drug class: NLM RxClass          (FDA Established Pharmacologic Class; ATC level 4 fallback)

Synonyms are free-text phrases. Canon's text parser scans documents for them,
so anything short or ambiguous ("pe", "cap", "aids") goes in `exact` instead:
it still maps when it is the whole field, but is never searched for in prose.
Lab synonyms that double as drug names ("vitamin d", "b12", "insulin") are
qualified ("vitamin d level") so a medication line never becomes a lab result.

Stdlib only, like the rest of Canon.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "canon" / "vocab"
LAB, VITAL = "lab", "vital"

# --------------------------------------------------------------------------- #
# Free-text unit spellings for each UCUM unit (matched case-insensitively).
# --------------------------------------------------------------------------- #
UNIT_SPELLINGS = {
    "%": ["%", "percent", "pct"],
    "mg/dL": ["mg/dL"],
    "g/dL": ["g/dL", "gm/dL"],
    "g/L": ["g/L"],
    "mmol/L": ["mmol/L"],
    "umol/L": ["umol/L", "µmol/L", "μmol/L", "micromol/L"],
    "nmol/L": ["nmol/L"],
    "pmol/L": ["pmol/L"],
    "meq/L": ["mEq/L"],
    "U/L": ["U/L", "IU/L", "units/L"],
    "ng/mL": ["ng/mL"],
    "ng/dL": ["ng/dL"],
    "ng/L": ["ng/L"],
    "ug/L": ["ug/L", "mcg/L", "µg/L"],
    "ug/dL": ["ug/dL", "mcg/dL", "µg/dL"],
    "ug/mL": ["ug/mL", "mcg/mL", "µg/mL", "ug/mL FEU", "mcg/mL FEU", "µg/mL FEU"],
    "mg/L": ["mg/L", "mg/L FEU"],
    "U/mL": ["U/mL", "units/mL"],
    "/uL": ["cells/uL", "/uL", "cells/µL", "/µL", "cells/mm3", "/mm3"],
    "pg/mL": ["pg/mL"],
    "mg/g": ["mg/g", "mg/g creat", "mg/gCr"],
    "m[IU]/L": ["mIU/L", "uIU/mL", "µIU/mL", "μIU/mL", "mU/L", "m[IU]/L"],
    "m[IU]/mL": ["mIU/mL", "m[IU]/mL"],
    "10*3/uL": ["10*3/uL", "10^3/uL", "x10^3/uL", "x10e3/uL", "K/uL", "K/mcL", "thou/uL"],
    "10*9/L": ["10*9/L", "10^9/L", "x10^9/L", "x10e9/L"],
    "10*6/uL": ["10*6/uL", "10^6/uL", "x10^6/uL", "x10e6/uL", "M/uL", "mil/uL"],
    "10*12/L": ["10*12/L", "10^12/L", "x10^12/L", "x10e12/L"],
    "L/L": ["L/L"],
    "fL": ["fL"],
    "pg": ["pg"],
    "s": ["s", "sec", "secs", "seconds"],
    "{INR}": ["{INR}", "INR", "ratio"],
    "{ratio}": ["{ratio}", "ratio"],
    "mm/h": ["mm/h", "mm/hr", "mm/hour"],
    "mosm/kg": ["mOsm/kg", "mosm/kg", "mmol/kg"],
    "kPa": ["kPa"],
    "mm[Hg]": ["mm[Hg]", "mmHg"],
    "cm": ["cm"],
    "[in_i]": ["in", "inch", "inches", "[in_i]"],
    "/min": ["/min", "bpm", "beats/min", "breaths/min", "br/min"],
    "{score}": ["{score}", "/10"],
    "[pH]": ["[pH]", "pH"],
    "1": ["1"],
}

GLU = {"mmol/L": 18.016}
CHOL = {"mmol/L": 38.67}
LYTE = {"meq/L": 1}

# --------------------------------------------------------------------------- #
# Observations: LOINC, category, canonical UCUM unit, short display,
#               synonyms (scanned), exact-only synonyms, conversions into canonical
# --------------------------------------------------------------------------- #
OBSERVATIONS = [
    # Vital signs (Canon already has HR, RR, temp, weight, height, BMI, SpO2, BP)
    ("9843-4", VITAL, "cm", "Head circumference", ["head circumference", "occipital frontal circumference"], ["hc", "ofc"], {"[in_i]": 2.54}),
    ("8280-0", VITAL, "cm", "Waist circumference", ["waist circumference"], ["waist"], {"[in_i]": 2.54}),
    ("72514-3", VITAL, "{score}", "Pain severity (0-10)", ["pain score", "pain severity", "pain level", "pain scale"], ["pain"], {}),
    # Complete blood count
    ("789-8", LAB, "10*6/uL", "RBC count", ["rbc", "red blood cell count", "red cell count", "erythrocyte count"], ["red blood cells"], {"10*12/L": 1}),
    ("4544-3", LAB, "%", "Hematocrit", ["hematocrit", "haematocrit", "hct"], ["pcv"], {"L/L": 100}),
    ("787-2", LAB, "fL", "MCV", ["mcv", "mean corpuscular volume"], [], {}),
    ("785-6", LAB, "pg", "MCH", ["mch", "mean corpuscular hemoglobin"], [], {}),
    ("786-4", LAB, "g/dL", "MCHC", ["mchc", "mean corpuscular hemoglobin concentration"], [], {"g/L": 0.1}),
    ("788-0", LAB, "%", "RDW", ["rdw", "red cell distribution width", "rdw cv"], [], {}),
    ("32623-1", LAB, "fL", "MPV", ["mpv", "mean platelet volume"], [], {}),
    ("770-8", LAB, "%", "Neutrophils %", ["neutrophils", "neutrophil %", "neut %", "polys", "segs"], ["neut"], {}),
    ("736-9", LAB, "%", "Lymphocytes %", ["lymphocytes", "lymphocyte %", "lymph %", "lymphs"], ["lymph"], {}),
    ("5905-5", LAB, "%", "Monocytes %", ["monocytes", "monocyte %", "mono %", "monos"], ["mono"], {}),
    ("713-8", LAB, "%", "Eosinophils %", ["eosinophils", "eosinophil %", "eos %"], ["eos"], {}),
    ("706-2", LAB, "%", "Basophils %", ["basophils", "basophil %", "baso %", "basos"], ["baso"], {}),
    ("751-8", LAB, "10*3/uL", "Absolute neutrophil count", ["absolute neutrophil count", "absolute neutrophils", "neutrophils absolute", "anc"], [], {"10*9/L": 1}),
    ("731-0", LAB, "10*3/uL", "Absolute lymphocyte count", ["absolute lymphocyte count", "absolute lymphocytes", "lymphocytes absolute", "alc"], [], {"10*9/L": 1}),
    ("4679-7", LAB, "%", "Reticulocytes", ["reticulocyte count", "reticulocytes", "retic count", "retic"], [], {}),
    ("30341-2", LAB, "mm/h", "ESR", ["esr", "sed rate", "sedimentation rate", "erythrocyte sedimentation rate"], [], {}),
    # Chemistry
    ("41653-7", LAB, "mg/dL", "Glucose (glucometer)", ["fingerstick glucose", "fingerstick", "poc glucose", "point of care glucose", "capillary glucose", "cbg", "glucometer"], [], GLU),
    ("2075-0", LAB, "mmol/L", "Chloride", ["serum chloride", "chloride level"], ["chloride", "cl"], LYTE),  # "potassium chloride 20 mEq"
    ("2028-9", LAB, "mmol/L", "CO2, total", ["co2", "total co2", "bicarbonate", "bicarb", "tco2", "hco3"], [], LYTE),
    ("33037-3", LAB, "mmol/L", "Anion gap", ["anion gap"], ["ag"], LYTE),
    ("3094-0", LAB, "mg/dL", "BUN", ["bun", "urea nitrogen", "blood urea nitrogen"], [], {"mmol/L": 2.801}),
    ("17861-6", LAB, "mg/dL", "Calcium", ["serum calcium", "total calcium", "calcium level"], ["calcium", "ca"], {"mmol/L": 4.008, "meq/L": 2.004}),
    ("2777-1", LAB, "mg/dL", "Phosphorus", ["phosphorus", "phosphate", "phos"], ["po4"], {"mmol/L": 3.097}),
    ("19123-9", LAB, "mg/dL", "Magnesium", ["serum magnesium", "magnesium level"], ["magnesium", "mg", "mag"], {"mmol/L": 2.431, "meq/L": 1.2153}),
    ("2885-2", LAB, "g/dL", "Total protein", ["total protein", "serum protein"], ["tp"], {"g/L": 0.1}),
    ("1751-7", LAB, "g/dL", "Albumin", ["albumin", "serum albumin"], ["alb"], {"g/L": 0.1}),
    ("1975-2", LAB, "mg/dL", "Bilirubin, total", ["total bilirubin", "bilirubin", "t bili", "tbili"], [], {"umol/L": 1 / 17.1}),
    ("1968-7", LAB, "mg/dL", "Bilirubin, direct", ["direct bilirubin", "conjugated bilirubin", "d bili", "dbili"], [], {"umol/L": 1 / 17.1}),
    ("6768-6", LAB, "U/L", "Alkaline phosphatase", ["alkaline phosphatase", "alk phos", "alp"], [], {}),
    ("1920-8", LAB, "U/L", "AST", ["ast", "sgot", "aspartate aminotransferase"], [], {}),
    ("2324-2", LAB, "U/L", "GGT", ["ggt", "gamma gt", "gamma glutamyl transferase"], [], {}),
    ("3084-1", LAB, "mg/dL", "Uric acid", ["uric acid", "urate"], [], {"umol/L": 1 / 59.48}),
    ("1798-8", LAB, "U/L", "Amylase", ["amylase"], [], {}),
    ("3040-3", LAB, "U/L", "Lipase", ["lipase"], [], {}),
    ("2532-0", LAB, "U/L", "LDH", ["ldh", "lactate dehydrogenase"], ["ld"], {}),
    ("2157-6", LAB, "U/L", "Creatine kinase", ["creatine kinase", "cpk", "ck"], [], {}),
    ("2692-2", LAB, "mosm/kg", "Osmolality, serum", ["serum osmolality", "osmolality"], ["osmo"], {}),
    ("2524-7", LAB, "mmol/L", "Lactate", ["lactate", "lactic acid"], [], {"mg/dL": 1 / 9.008}),
    ("16362-6", LAB, "umol/L", "Ammonia", ["ammonia"], ["nh3"], {"ug/dL": 0.5872}),
    # Lipids
    ("43396-1", LAB, "mg/dL", "Non-HDL cholesterol", ["non hdl cholesterol", "non hdl", "non hdl c"], [], CHOL),
    ("9830-1", LAB, "{ratio}", "Cholesterol/HDL ratio", ["cholesterol hdl ratio", "chol hdl ratio", "tc hdl ratio"], [], {}),
    # Endocrine
    ("20448-7", LAB, "m[IU]/L", "Insulin", ["insulin level", "fasting insulin", "serum insulin"], ["insulin"], {"pmol/L": 1 / 6}),
    ("1986-9", LAB, "ng/mL", "C-peptide", ["c peptide", "cpeptide"], [], {"nmol/L": 3.02}),
    ("3024-7", LAB, "ng/dL", "Free T4", ["free t4", "ft4", "free thyroxine"], [], {"pmol/L": 1 / 12.87}),
    ("3051-0", LAB, "pg/mL", "Free T3", ["free t3", "ft3", "free triiodothyronine"], [], {"pmol/L": 0.651}),
    ("3026-2", LAB, "ug/dL", "T4, total", ["total t4", "t4 total"], ["t4", "thyroxine"], {"nmol/L": 1 / 12.87}),
    ("2731-8", LAB, "pg/mL", "PTH, intact", ["pth", "parathyroid hormone", "intact pth"], [], {"pmol/L": 9.43}),
    ("2143-6", LAB, "ug/dL", "Cortisol", ["cortisol", "serum cortisol"], [], {"nmol/L": 1 / 27.59}),
    ("2986-8", LAB, "ng/dL", "Testosterone", ["total testosterone", "serum testosterone", "testosterone level"], ["testosterone"], {"nmol/L": 28.84}),
    ("2857-1", LAB, "ng/mL", "PSA", ["psa", "prostate specific antigen"], [], {"ug/L": 1}),
    ("21198-7", LAB, "m[IU]/mL", "hCG, beta (quantitative)", ["beta hcg", "quantitative hcg", "serum hcg", "hcg", "bhcg"], [], {"U/L": 1}),
    # Iron / vitamins
    ("2498-4", LAB, "ug/dL", "Iron", ["serum iron", "iron level"], ["fe", "iron"], {"umol/L": 5.585}),
    ("2276-4", LAB, "ng/mL", "Ferritin", ["ferritin"], [], {"ug/L": 1}),
    ("2500-7", LAB, "ug/dL", "TIBC", ["tibc", "total iron binding capacity"], [], {"umol/L": 5.585}),
    ("2502-3", LAB, "%", "Transferrin saturation", ["transferrin saturation", "iron saturation", "tsat"], [], {}),
    ("2132-9", LAB, "pg/mL", "Vitamin B12", ["vitamin b12 level", "b12 level", "serum b12", "cobalamin level"], ["vitamin b12", "b12"], {"pmol/L": 1.355}),
    ("2284-8", LAB, "ng/mL", "Folate", ["folate level", "serum folate"], ["folate"], {"nmol/L": 1 / 2.266}),
    ("62292-8", LAB, "ng/mL", "25-hydroxyvitamin D", ["vitamin d level", "25 oh vitamin d", "25 hydroxyvitamin d", "25 oh d", "vit d level"], ["vitamin d", "vit d"], {"nmol/L": 1 / 2.496}),
    # Coagulation
    ("5902-2", LAB, "s", "Prothrombin time", ["prothrombin time", "protime"], ["pt"], {}),
    ("6301-6", LAB, "{INR}", "INR", ["inr"], [], {}),
    ("14979-9", LAB, "s", "aPTT", ["aptt", "ptt", "partial thromboplastin time"], [], {}),
    ("3255-7", LAB, "mg/dL", "Fibrinogen", ["fibrinogen"], [], {"g/L": 100}),
    ("48065-7", LAB, "ng/mL", "D-dimer (FEU)", ["d dimer", "ddimer"], [], {"ug/mL": 1000, "mg/L": 1000}),
    # Cardiac / inflammation
    ("10839-9", LAB, "ng/mL", "Troponin I", ["troponin i", "troponin", "tni", "ctni"], [], {"ng/L": 0.001}),
    ("89579-7", LAB, "ng/L", "Troponin I (high sensitivity)", ["hs troponin", "hs troponin i", "high sensitivity troponin", "hstni"], [], {"ng/mL": 1000}),
    ("30934-4", LAB, "pg/mL", "BNP", ["bnp", "brain natriuretic peptide", "b type natriuretic peptide"], [], {"ng/L": 1}),
    ("33762-6", LAB, "pg/mL", "NT-proBNP", ["nt probnp", "ntprobnp", "probnp"], [], {"ng/L": 1}),
    ("1988-5", LAB, "mg/L", "CRP", ["crp", "c reactive protein"], [], {"mg/dL": 10}),
    ("30522-7", LAB, "mg/L", "hs-CRP", ["hs crp", "hscrp", "high sensitivity crp", "cardiac crp"], [], {"mg/dL": 10}),
    ("33959-8", LAB, "ng/mL", "Procalcitonin", ["procalcitonin"], ["pct"], {"ug/L": 1}),
    # Arterial blood gas
    ("2744-1", LAB, "[pH]", "pH, arterial", ["arterial ph", "abg ph"], ["ph"], {}),
    ("2019-8", LAB, "mm[Hg]", "pCO2, arterial", ["paco2", "pco2", "arterial pco2"], [], {"kPa": 7.50062}),
    ("2703-7", LAB, "mm[Hg]", "pO2, arterial", ["pao2", "po2", "arterial po2"], [], {"kPa": 7.50062}),
    ("1960-4", LAB, "mmol/L", "Bicarbonate, arterial", ["arterial bicarbonate", "arterial hco3", "abg hco3"], [], LYTE),
    # Urine
    ("2756-5", LAB, "[pH]", "pH, urine", ["urine ph"], [], {}),
    ("2965-2", LAB, "1", "Specific gravity, urine", ["specific gravity", "urine specific gravity"], ["sg", "usg"], {}),
    ("2161-8", LAB, "mg/dL", "Creatinine, urine", ["urine creatinine"], [], {"umol/L": 1 / 88.42}),
    # --- Expansion ---
    # Hormones
    ("2243-4", LAB, "pg/mL", "Estradiol", ["estradiol level", "serum estradiol", "e2 level"], ["estradiol", "e2"], {"pmol/L": 0.2724}),
    ("2839-9", LAB, "ng/mL", "Progesterone", ["progesterone level", "serum progesterone"], ["progesterone"], {"nmol/L": 0.3145}),
    ("2842-3", LAB, "ng/mL", "Prolactin", ["prolactin", "serum prolactin"], ["prl"], {"ug/L": 1}),
    ("15067-2", LAB, "m[IU]/mL", "FSH", ["follicle stimulating hormone", "fsh level"], ["fsh"], {}),
    ("10501-5", LAB, "m[IU]/mL", "LH", ["luteinizing hormone", "lh level"], ["lh"], {}),
    ("2991-8", LAB, "pg/mL", "Testosterone, free", ["free testosterone"], [], {}),
    ("2484-4", LAB, "ng/mL", "IGF-1", ["igf-1", "igf 1", "insulin-like growth factor 1", "somatomedin c"], [], {"ug/L": 1}),
    ("2191-5", LAB, "ug/dL", "DHEA-S", ["dhea-s", "dhea sulfate", "dehydroepiandrosterone sulfate"], ["dheas"], {}),
    # Tumor markers
    ("1834-1", LAB, "ng/mL", "AFP", ["alpha fetoprotein", "alpha-fetoprotein"], ["afp"], {"ug/L": 1}),
    ("2039-6", LAB, "ng/mL", "CEA", ["carcinoembryonic antigen"], ["cea"], {"ug/L": 1}),
    ("10334-1", LAB, "U/mL", "CA-125", ["ca-125", "ca 125", "cancer antigen 125"], [], {}),
    ("24108-3", LAB, "U/mL", "CA 19-9", ["ca 19-9", "ca19-9", "cancer antigen 19-9"], [], {}),
    ("10886-0", LAB, "ng/mL", "PSA, free", ["free psa", "psa free"], [], {"ug/L": 1}),
    # Blood counts and chemistry
    ("742-7", LAB, "10*3/uL", "Absolute monocyte count", ["absolute monocytes", "monocytes absolute", "absolute monocyte count"], [], {"10*9/L": 1}),
    ("711-2", LAB, "10*3/uL", "Absolute eosinophil count", ["absolute eosinophils", "eosinophils absolute", "absolute eosinophil count"], [], {"10*9/L": 1}),
    ("704-7", LAB, "10*3/uL", "Absolute basophil count", ["absolute basophils", "basophils absolute", "absolute basophil count"], [], {"10*9/L": 1}),
    ("1994-3", LAB, "mmol/L", "Calcium, ionized", ["ionized calcium", "calcium ionized"], [], {}),
    ("33863-2", LAB, "mg/L", "Cystatin C", ["cystatin c"], [], {}),
    ("14338-8", LAB, "mg/dL", "Prealbumin", ["prealbumin", "transthyretin"], [], {"g/L": 100}),
    ("4542-7", LAB, "mg/dL", "Haptoglobin", ["haptoglobin"], [], {"g/L": 100}),
    ("13965-9", LAB, "umol/L", "Homocysteine", ["homocysteine"], [], {}),
    ("24467-3", LAB, "/uL", "CD4 count", ["cd4 count", "cd4 cells", "absolute cd4"], [], {}),
    # Urine
    ("14957-5", LAB, "mg/L", "Microalbumin, urine", ["urine microalbumin", "microalbumin urine"], [], {}),
    ("2888-6", LAB, "mg/dL", "Protein, urine", ["urine protein", "protein urine"], [], {}),
    # Cardiovascular risk
    ("10835-7", LAB, "mg/dL", "Lipoprotein(a)", ["lipoprotein a", "lipoprotein(a)", "lp(a)"], [], {}),
    ("1884-6", LAB, "mg/dL", "Apolipoprotein B", ["apolipoprotein b", "apo b"], ["apob"], {"g/L": 100}),
    # Drug levels (qualified so a medication line never becomes a lab result)
    ("10535-3", LAB, "ng/mL", "Digoxin level", ["digoxin level", "serum digoxin"], [], {"ug/L": 1}),
    ("14334-7", LAB, "mmol/L", "Lithium level", ["lithium level", "serum lithium"], [], {"meq/L": 1}),
    ("4092-3", LAB, "ug/mL", "Vancomycin trough", ["vancomycin trough", "vanc trough"], [], {"mg/L": 1}),
    ("4086-5", LAB, "ug/mL", "Valproate level", ["valproic acid level", "valproate level", "depakote level"], [], {"mg/L": 1}),
    ("3968-5", LAB, "ug/mL", "Phenytoin level", ["phenytoin level", "dilantin level"], [], {"mg/L": 1}),
    ("11253-2", LAB, "ng/mL", "Tacrolimus level", ["tacrolimus level", "tacrolimus trough", "fk506 level"], [], {"ug/L": 1}),
]

# Adult reference ranges in each observation's canonical unit, transcribed from
# "ABIM Laboratory Test Reference Ranges, January 2026"
# (https://www.abim.org/media/e2wdwdqu/laboratory-reference-ranges.pdf). The third
# element is ABIM's wording. Sex-specific ranges use the outer bounds of both sexes,
# as Canon's hand-written hemoglobin range does. Tests with no single normal range
# (PSA, cortisol, hCG, NT-proBNP, hs-CRP, testosterone, random urine) get none.
RANGE_SOURCE = "ABIM Laboratory Test Reference Ranges, January 2026"
REFERENCE_RANGES = {
    "789-8": (4.2, 5.9, "Erythrocyte count 4.2–5.9 million/μL"),
    "4544-3": (37, 50, "Hematocrit, blood: Female 37%–47%; male 42%–50%"),
    "787-2": (80, 98, "Mean corpuscular volume 80–98 fL"),
    "785-6": (28, 32, "Mean corpuscular hemoglobin 28–32 pg"),
    "786-4": (33, 36, "Mean corpuscular hemoglobin concentration 33–36 g/dL"),
    "788-0": (9.0, 14.5, "Red cell distribution width (RDW) 9.0%–14.5%"),
    "32623-1": (7, 9, "Mean platelet volume 7–9 fL"),
    "770-8": (50, 70, "Leukocyte count: segmented neutrophils 50%–70%"),
    "736-9": (30, 45, "Leukocyte count: lymphocytes 30%–45%"),
    "5905-5": (0, 6, "Leukocyte count: monocytes 0%–6%"),
    "713-8": (0, 3, "Leukocyte count: eosinophils 0%–3%"),
    "706-2": (0, 1, "Leukocyte count: basophils 0%–1%"),
    "751-8": (2.0, 8.25, "Absolute neutrophil count (ANC) 2000–8250/μL"),
    "731-0": (1.2, 4.95, "Absolute lymphocyte count 1200–4950/μL"),
    "4679-7": (0.5, 1.5, "Reticulocyte count 0.5%–1.5% of red cells"),
    "30341-2": (0, 20, "Erythrocyte sedimentation rate (Westergren): Female 0–20 mm/hr; male 0–15 mm/hr"),
    "2075-0": (98, 106, "Chloride, serum 98–106 mEq/L"),
    "2028-9": (23, 30, "Carbon dioxide, serum 23–30 mEq/L"),
    "33037-3": (7, 13, "Anion gap, serum 7–13 mEq/L"),
    "3094-0": (8, 20, "Blood urea nitrogen (BUN), serum or plasma 8–20 mg/dL"),
    "17861-6": (8.6, 10.2, "Calcium, serum 8.6–10.2 mg/dL"),
    "2777-1": (3.0, 4.5, "Phosphorus, serum 3.0–4.5 mg/dL"),
    "19123-9": (1.6, 2.6, "Magnesium, serum 1.6–2.6 mg/dL"),
    "2885-2": (5.5, 9.0, "Proteins, serum: total 5.5–9.0 g/dL"),
    "1751-7": (3.5, 5.5, "Albumin, serum 3.5–5.5 g/dL"),
    "1975-2": (0.3, 1.0, "Bilirubin, serum: total 0.3–1.0 mg/dL"),
    "1968-7": (0.1, 0.3, "Bilirubin, serum: direct 0.1–0.3 mg/dL"),
    "6768-6": (30, 120, "Alkaline phosphatase, serum 30–120 U/L"),
    "1920-8": (10, 40, "Aminotransferase, serum aspartate (AST, SGOT) 10–40 U/L"),
    "2324-2": (8, 50, "Gamma-glutamyltransferase, serum: Female 8–40 U/L; male 9–50 U/L"),
    "3084-1": (3.0, 7.0, "Uric acid, serum 3.0–7.0 mg/dL"),
    "1798-8": (25, 125, "Amylase, serum 25–125 U/L"),
    "3040-3": (10, 140, "Lipase, serum 10–140 U/L"),
    "2532-0": (80, 225, "Lactate dehydrogenase, serum 80–225 U/L"),
    "2157-6": (30, 170, "Creatine kinase, serum, total: Female 30–135 U/L; male 55–170 U/L"),
    "2692-2": (275, 295, "Osmolality, serum 275–295 mOsm/kg H2O"),
    "2524-7": (0.7, 2.1, "Lactate, serum or plasma 0.7–2.1 mmol/L"),
    "16362-6": (23.5, 41.1, "Ammonia, plasma 40–70 μg/dL (converted to μmol/L at 0.5872)"),
    "20448-7": (None, 20, "Insulin, serum (fasting) <20 μU/mL"),
    "1986-9": (0.8, 3.1, "C peptide, serum 0.8–3.1 ng/mL"),
    "3024-7": (0.8, 1.8, "Thyroxine (T4), serum: free 0.8–1.8 ng/dL"),
    "3051-0": (2.3, 4.2, "Triiodothyronine (T3), serum: free 2.3–4.2 pg/mL"),
    "3026-2": (5, 12, "Thyroxine (T4), serum: total 5–12 μg/dL"),
    "2731-8": (10, 65, "Parathyroid hormone, serum: intact 10–65 pg/mL"),
    "2498-4": (50, 150, "Iron, serum 50–150 μg/dL"),
    "2276-4": (24, 336, "Ferritin, serum: Female 24–307 ng/mL; male 24–336 ng/mL"),
    "2500-7": (250, 310, "Iron-binding capacity, serum (total) 250–310 μg/dL"),
    "2502-3": (20, 50, "Transferrin saturation 20%–50%"),
    "2132-9": (200, 800, "Vitamin B12, serum 200–800 pg/mL"),
    "2284-8": (1.8, 9.0, "Folate, serum 1.8–9.0 ng/mL"),
    "62292-8": (30, 60, "Vitamin D metabolites, serum: 25-hydroxyvitamin D 30–60 ng/mL"),
    "5902-2": (11, 13, "Prothrombin time, plasma 11–13 seconds"),
    "14979-9": (25, 35, "Activated partial thromboplastin time 25–35 seconds"),
    "3255-7": (200, 400, "Fibrinogen, plasma 200–400 mg/dL"),
    "48065-7": (None, 500, "D-dimer, plasma <0.5 μg/mL"),
    "10839-9": (None, 0.04, "Troponin I, cardiac, serum ≤0.04 ng/mL"),
    "89579-7": (None, 20, "Troponin I, cardiac, high-sensitivity, plasma: Female ≤15 ng/L; male ≤20 ng/L"),
    "30934-4": (None, 100, "B-type natriuretic peptide, plasma <100 pg/mL"),
    "1988-5": (None, 8, "C-reactive protein, serum ≤0.8 mg/dL"),
    "33959-8": (None, 0.10, "Procalcitonin, serum ≤0.10 ng/mL"),
    "2744-1": (7.38, 7.44, "Arterial blood gas (room air): pH 7.38–7.44"),
    "2019-8": (38, 42, "Arterial blood gas (room air): PaCO2 38–42 mm Hg"),
    "2703-7": (75, 100, "Arterial blood gas (room air): PaO2 75–100 mm Hg"),
    "1960-4": (23, 26, "Arterial blood gas (room air): bicarbonate 23–26 mEq/L"),
    "2756-5": (4.5, 8.0, "pH, urine 4.5–8.0"),
    "2965-2": (1.002, 1.030, "Specific gravity, urine 1.002–1.030"),
}

# Synonyms added to observations Canon already defines (its units/ranges stay as they are).
EXTRA_OBSERVATION_SYNONYMS = {
    "9279-1": (["respiratory rate", "resp rate", "respirations"], ["rr", "resp"]),
    "59408-5": (["oxygen saturation", "pulse ox", "pulse oximetry", "o2 sat", "spo2"], ["sao2"]),
    "718-7": (["hemoglobin", "haemoglobin", "hgb"], ["hb"]),
    "6690-2": (["white blood cell count", "white cell count", "leukocyte count", "wbc"], []),
    "777-3": (["platelet count", "platelets", "plts"], ["plt"]),
    "1742-6": (["alanine aminotransferase", "sgpt"], []),
    "2093-3": (["total cholesterol", "cholesterol"], ["tc"]),
    "2085-9": (["hdl cholesterol", "hdl c"], []),
    "2160-0": (["serum creatinine", "creatinine"], ["scr", "cr"]),
    "2823-3": (["serum potassium"], ["k"]),
    "2951-2": (["serum sodium"], ["na"]),
}

# --------------------------------------------------------------------------- #
# Conditions: SNOMED CT, ICD-10-CM, synonyms (scanned), exact-only synonyms
# --------------------------------------------------------------------------- #
CONDITIONS = [
    # Cardiometabolic
    ("46635009", "E10.9", ["type 1 diabetes", "type i diabetes", "type 1 diabetes mellitus", "diabetes mellitus type 1", "t1dm", "dm1", "iddm", "dm type 1"], []),
    ("714628002", "R73.03", ["prediabetes", "pre diabetes", "prediabetic"], []),
    ("302866003", "E16.2", ["hypoglycemia", "hypoglycaemia"], ["low blood sugar"]),
    ("45007003", "I95.9", ["hypotension"], ["low blood pressure"]),
    ("13644009", "E78.00", ["hypercholesterolemia", "hypercholesterolaemia", "pure hypercholesterolemia"], []),
    ("302870006", "E78.1", ["hypertriglyceridemia", "hypertriglyceridaemia"], ["high triglycerides"]),
    ("53741008", "I25.10", ["coronary artery disease", "coronary heart disease", "ischemic heart disease", "atherosclerotic heart disease"], ["cad", "chd", "ihd", "ascvd"]),
    ("22298006", "I21.9", ["myocardial infarction", "heart attack", "acute myocardial infarction", "stemi", "nstemi"], ["mi", "ami"]),
    ("84114007", "I50.9", ["heart failure", "congestive heart failure", "chf", "cardiac failure"], ["hf"]),
    ("5370000", "I48.92", ["atrial flutter", "a flutter", "aflutter"], []),
    ("60573004", "I35.0", ["aortic stenosis", "aortic valve stenosis"], []),
    ("233873004", "I42.2", ["hypertrophic cardiomyopathy"], ["hcm"]),
    ("400047006", "I73.9", ["peripheral vascular disease", "peripheral arterial disease", "peripheral artery disease"], ["pvd", "pad"]),
    ("128053003", "I82.409", ["deep vein thrombosis", "deep venous thrombosis", "dvt"], []),
    ("59282003", "I26.99", ["pulmonary embolism", "pulmonary embolus"], ["pe"]),
    ("230690007", "I63.9", ["stroke", "cerebrovascular accident", "cva", "cerebral infarction"], []),
    ("266257000", "G45.9", ["transient ischemic attack", "mini stroke"], ["tia"]),
    # Respiratory
    ("233604007", "J18.9", ["pneumonia", "community acquired pneumonia"], ["cap"]),
    ("78275009", "G47.33", ["obstructive sleep apnea", "sleep apnea", "osa"], []),
    ("233703007", "J84.9", ["interstitial lung disease", "pulmonary fibrosis"], ["ild"]),
    ("190905008", "E84.9", ["cystic fibrosis"], ["cf"]),
    ("61582004", "J30.9", ["allergic rhinitis", "hay fever", "seasonal allergies"], []),
    ("40055000", "J32.9", ["chronic sinusitis"], []),
    ("43878008", "J02.0", ["strep throat", "streptococcal pharyngitis", "strep pharyngitis"], []),
    ("6142004", "J11.1", [], ["influenza", "flu"]),  # "influenza vaccine given"
    ("840539006", "U07.1", ["sars cov 2 infection", "covid 19 infection"], ["covid 19", "covid19", "covid"]),
    # Renal / GU
    ("709044004", "N18.9", ["chronic kidney disease", "ckd", "chronic renal disease"], []),
    ("14669001", "N17.9", ["acute kidney injury", "acute renal failure", "aki"], ["arf"]),
    ("95570007", "N20.0", ["kidney stone", "kidney stones", "nephrolithiasis", "renal calculus"], []),
    ("68566005", "N39.0", ["urinary tract infection", "uti"], []),
    ("266569009", "N40.0", ["benign prostatic hyperplasia", "enlarged prostate", "bph"], []),
    ("237055002", "E28.2", ["polycystic ovary syndrome", "polycystic ovarian syndrome", "pcos"], []),
    ("129103003", "N80.9", ["endometriosis"], []),
    # GI / liver
    ("10743008", "K58.9", ["irritable bowel syndrome", "ibs"], []),
    ("34000006", "K50.90", ["crohn's disease", "crohns disease", "crohn disease", "crohn's"], []),
    ("64766004", "K51.90", ["ulcerative colitis"], ["uc"]),
    ("19943007", "K74.60", ["cirrhosis", "liver cirrhosis", "cirrhosis of liver"], []),
    ("197321007", "K76.0", ["fatty liver", "hepatic steatosis", "nafld", "masld", "steatotic liver disease"], []),
    ("61977001", "B18.1", ["chronic hepatitis b"], ["hepatitis b", "hep b", "hbv"]),
    ("128302006", "B18.2", ["chronic hepatitis c"], ["hepatitis c", "hep c", "hcv"]),
    ("197456007", "K85.90", ["acute pancreatitis", "pancreatitis"], []),
    ("14760008", "K59.00", ["constipation"], []),
    # Endocrine / electrolytes
    ("34486009", "E05.90", ["hyperthyroidism", "overactive thyroid", "thyrotoxicosis"], []),
    ("34713006", "E55.9", ["vitamin d deficiency"], ["low vitamin d"]),
    ("43339004", "E87.6", ["hypokalemia", "hypokalaemia"], ["low potassium"]),
    ("14140009", "E87.5", ["hyperkalemia", "hyperkalaemia"], ["high potassium"]),
    ("89627008", "E87.1", ["hyponatremia", "hyponatraemia"], ["low sodium"]),  # "low sodium diet"
    ("90560007", "M10.9", ["gout"], []),
    # Hematology / oncology
    ("271737000", "D64.9", ["anemia", "anaemia"], []),
    ("87522002", "D50.9", ["iron deficiency anemia", "iron deficiency anaemia"], ["ida"]),
    ("417357006", "D57.1", ["sickle cell disease", "sickle cell anemia"], ["scd"]),
    ("363346000", "C80.1", ["malignant neoplasm"], ["cancer", "malignancy"]),
    ("254837009", "C50.919", ["breast cancer", "breast carcinoma"], []),
    ("399068003", "C61", ["prostate cancer", "prostate carcinoma"], []),
    ("363358000", "C34.90", ["lung cancer", "lung carcinoma"], []),
    ("363406005", "C18.9", ["colon cancer", "colorectal cancer", "colon carcinoma"], []),
    ("363418001", "C25.9", ["pancreatic cancer"], []),
    ("372244006", "C43.9", ["melanoma", "malignant melanoma"], []),
    ("93143009", "C95.90", ["leukemia", "leukaemia"], []),
    ("118600007", "C85.90", ["lymphoma"], []),
    # Neurology
    ("84757009", "G40.909", ["epilepsy", "seizure disorder"], []),
    ("37796009", "G43.909", ["migraine", "migraines"], []),
    ("49049000", "G20.A1", ["parkinson's disease", "parkinsons disease", "parkinson disease", "parkinson's"], ["pd"]),
    ("26929004", "G30.9", ["alzheimer's disease", "alzheimers disease", "alzheimer disease", "alzheimer's"], []),
    ("52448006", "F03.90", ["dementia"], []),
    ("302226006", "G62.9", ["peripheral neuropathy", "polyneuropathy", "neuropathy"], []),
    ("193462001", "G47.00", ["insomnia"], []),
    ("82423001", "G89.29", ["chronic pain"], []),
    # Mental health / substance use
    ("35489007", "F32.A", ["depression", "depressive disorder"], []),
    ("197480006", "F41.9", ["anxiety", "anxiety disorder"], []),
    ("13746004", "F31.9", ["bipolar disorder", "bipolar", "manic depression"], []),
    ("58214004", "F20.9", ["schizophrenia"], []),
    ("47505003", "F43.10", ["post traumatic stress disorder", "posttraumatic stress disorder", "ptsd"], []),
    ("406506008", "F90.9", ["attention deficit hyperactivity disorder", "adhd"], ["add"]),
    ("191736004", "F42.9", ["obsessive compulsive disorder", "ocd"], []),
    ("7200002", "F10.20", ["alcohol dependence", "alcohol use disorder", "alcoholism"], ["aud"]),
    ("56294008", "F17.200", ["nicotine dependence", "tobacco dependence", "tobacco use disorder"], []),
    ("75544000", "F11.20", ["opioid dependence", "opioid use disorder"], ["oud"]),
    # Musculoskeletal / rheumatology / skin
    ("69896004", "M06.9", ["rheumatoid arthritis"], ["ra"]),
    ("396275006", "M19.90", ["osteoarthritis", "degenerative joint disease"], ["oa", "djd"]),
    ("64859006", "M81.0", ["osteoporosis"], []),
    ("55464009", "M32.9", ["systemic lupus erythematosus", "lupus"], ["sle"]),
    ("279039007", "M54.50", ["low back pain", "lower back pain"], ["lbp"]),
    ("9014002", "L40.9", ["psoriasis"], []),
    ("128045006", "L03.90", ["cellulitis"], []),
    ("126485001", "L50.9", ["urticaria", "hives"], []),
    # Infectious disease
    ("86406008", "B20", ["hiv infection", "human immunodeficiency virus infection", "hiv disease"], ["hiv", "aids"]),
    ("56717001", "A15.9", ["pulmonary tuberculosis"], ["tuberculosis", "tb"]),  # "tuberculosis screening"
    ("91302008", "A41.9", ["sepsis", "septicemia"], []),
    # Eye / ENT
    ("23986001", "H40.9", ["glaucoma"], []),
    ("193570009", "H26.9", ["cataract", "cataracts"], []),
    ("267718000", "H35.30", ["macular degeneration", "age related macular degeneration"], ["amd", "armd"]),
    ("15188001", "H91.90", ["hearing loss"], []),
    # Common symptoms: exact-only, so narrative ("pt reports fever") does not fill the problem list
    ("29857009", "R07.9", [], ["chest pain"]),
    ("267036007", "R06.00", [], ["dyspnea", "shortness of breath", "dyspnoea", "sob"]),
    ("49727002", "R05.9", [], ["cough"]),
    ("25064002", "R51.9", [], ["headache"]),
    ("386661006", "R50.9", [], ["fever", "pyrexia"]),
    ("21522001", "R10.9", [], ["abdominal pain"]),
    ("422587007", "R11.0", [], ["nausea"]),
    ("62315008", "R19.7", [], ["diarrhea", "diarrhoea"]),
    ("84229001", "R53.83", [], ["fatigue"]),
    ("404640003", "R42", [], ["dizziness"]),
    # Already in Canon with these exact codes: verified here and given extra synonyms
    ("44054006", "E11.9", ["type 2 diabetes mellitus", "diabetes mellitus type 2", "type 2 diabetes", "t2dm"], ["dm2", "dmii"]),
    ("55822004", "E78.5", ["hyperlipidemia", "hyperlipidaemia"], ["hld"]),
    ("195967001", "J45.909", ["asthma"], []),
    ("24079001", "L20.9", ["atopic dermatitis", "eczema"], []),
    ("21897009", "F41.1", ["generalized anxiety disorder", "generalised anxiety disorder"], ["gad"]),
    ("370143000", "F32.9", ["major depressive disorder", "major depression"], ["mdd"]),
    ("40930008", "E03.9", ["hypothyroidism", "underactive thyroid"], []),
    ("235595009", "K21.9", ["gastroesophageal reflux disease", "gerd", "acid reflux"], ["gord"]),
    ("414916001", "E66.9", ["obesity"], []),
    ("49436004", "I48.91", ["atrial fibrillation", "afib", "a fib"], ["af"]),
    ("13645005", "J44.9", ["chronic obstructive pulmonary disease", "copd"], []),
    # --- Expansion ---
    # Cardiovascular
    ("194828000", "I20.9", ["angina pectoris", "angina", "stable angina"], []),
    ("1755008", "I25.2", ["old myocardial infarction", "history of myocardial infarction", "prior myocardial infarction"], []),
    ("27885002", "I44.2", ["complete heart block", "third degree heart block", "third degree av block"], []),
    ("48724000", "I34.0", ["mitral regurgitation", "mitral valve regurgitation", "mitral insufficiency"], []),
    ("85898001", "I42.9", ["cardiomyopathy"], []),
    ("70995007", "I27.20", ["pulmonary hypertension"], []),
    ("72866009", "I83.90", ["varicose veins"], []),
    ("233985008", "I71.40", ["abdominal aortic aneurysm"], ["aaa"]),
    ("441509002", "Z95.0", ["cardiac pacemaker", "pacemaker in situ"], []),
    # Endocrine and metabolic
    ("238136002", "E66.01", ["morbid obesity", "severe obesity", "class 3 obesity"], []),
    ("66999008", "E21.3", ["hyperparathyroidism"], []),
    ("190855004", "E83.42", ["hypomagnesemia", "hypomagnesaemia"], []),
    ("34095006", "E86.0", ["dehydration"], []),
    ("21983002", "E06.3", ["hashimoto thyroiditis", "hashimoto's thyroiditis", "hashimotos thyroiditis"], []),
    ("237495005", "E04.1", ["thyroid nodule"], []),
    ("302215000", "D69.6", ["thrombocytopenia"], []),
    ("28293008", "D66", ["hemophilia a", "haemophilia a"], []),
    ("31541009", "D86.9", ["sarcoidosis"], []),
    # Respiratory
    ("10509002", "J20.9", ["acute bronchitis", "bronchitis"], []),
    ("54150009", "J06.9", ["upper respiratory infection", "upper respiratory tract infection"], ["uri", "urti"]),
    ("405737000", "J02.9", ["pharyngitis"], []),
    ("15805002", "J01.90", ["acute sinusitis"], []),
    ("12295008", "J47.9", ["bronchiectasis"], []),
    ("60046008", "J90", ["pleural effusion"], []),
    ("36118008", "J93.9", ["pneumothorax"], []),
    # Gastrointestinal
    ("397825006", "K25.9", ["gastric ulcer", "stomach ulcer"], []),
    ("4556007", "K29.70", ["gastritis"], []),
    ("307496006", "K57.92", ["diverticulitis"], []),
    ("235919008", "K80.20", ["cholelithiasis", "gallstones", "gallstone"], []),
    ("76581006", "K81.9", ["cholecystitis"], []),
    ("70153002", "K64.9", ["hemorrhoids", "haemorrhoids"], []),
    ("396331005", "K90.0", ["celiac disease", "coeliac disease"], []),
    ("74474003", "K92.2", ["gastrointestinal hemorrhage", "gi bleed", "gastrointestinal bleeding"], []),
    ("396232000", "K40.90", ["inguinal hernia"], []),
    ("84089009", "K44.9", ["hiatal hernia", "hiatus hernia"], []),
    ("235494005", "K86.1", ["chronic pancreatitis"], []),
    ("25374005", "A09", ["gastroenteritis"], []),
    ("186431008", "A04.72", ["clostridioides difficile infection", "clostridium difficile infection", "c diff colitis", "c difficile infection"], ["cdi"]),
    # Renal and genitourinary
    ("431857002", "N18.4", ["chronic kidney disease stage 4", "ckd stage 4", "ckd 4"], []),
    ("433146000", "N18.5", ["chronic kidney disease stage 5", "ckd stage 5", "ckd 5"], []),
    ("46177005", "N18.6", ["end stage renal disease", "end-stage renal disease", "esrd"], []),
    ("43064006", "N13.30", ["hydronephrosis"], []),
    ("165232002", "R32", ["urinary incontinence"], []),
    ("860914002", "N52.9", ["erectile dysfunction"], ["ed"]),
    ("68226007", "N30.00", ["acute cystitis"], []),
    ("95315005", "D25.9", ["uterine fibroids", "uterine leiomyoma", "fibroid uterus"], []),
    ("77386006", "Z33.1", ["pregnancy", "pregnant"], []),
    # Musculoskeletal
    ("239872002", "M16.9", ["osteoarthritis of hip", "hip osteoarthritis"], []),
    ("81680005", "M54.2", ["neck pain", "cervicalgia"], []),
    ("23056005", "M54.30", ["sciatica"], []),
    ("203082005", "M79.7", ["fibromyalgia"], []),
    ("65323003", "M35.3", ["polymyalgia rheumatica"], ["pmr"]),
    ("9631008", "M45.9", ["ankylosing spondylitis"], []),
    ("400130008", "M31.6", ["giant cell arteritis", "temporal arteritis"], ["gca"]),
    ("89155008", "M34.9", ["systemic sclerosis", "scleroderma"], []),
    # Neurology
    ("24700007", "G35.D", ["multiple sclerosis"], ["ms"]),
    ("57406009", "G56.00", ["carpal tunnel syndrome"], []),
    ("193093009", "G51.0", ["bell palsy", "bell's palsy", "bells palsy"], []),
    ("609558009", "G25.0", ["essential tremor"], []),
    ("32914008", "G25.81", ["restless legs syndrome", "restless leg syndrome"], ["rls"]),
    ("398057008", "G44.209", ["tension headache", "tension-type headache"], []),
    ("111541001", "H81.10", ["benign paroxysmal positional vertigo"], ["bppv"]),
    ("60862001", "H93.19", ["tinnitus"], []),
    # Psychiatry
    ("66344007", "F33.9", ["recurrent major depression", "major depressive disorder recurrent", "recurrent depression"], []),
    ("78667006", "F34.1", ["dysthymia", "persistent depressive disorder"], []),
    ("371631005", "F41.0", ["panic disorder"], []),
    ("25501002", "F40.10", ["social anxiety disorder", "social phobia"], []),
    ("20010003", "F60.3", ["borderline personality disorder"], []),
    ("35919005", "F84.0", ["autism", "autistic disorder", "autism spectrum disorder"], []),
    ("85005007", "F12.20", ["cannabis dependence", "cannabis use disorder"], []),
    ("31956009", "F14.20", ["cocaine dependence", "cocaine use disorder"], []),
    ("68890003", "F25.9", ["schizoaffective disorder"], []),
    # Infectious disease
    ("4740000", "B02.9", ["herpes zoster", "shingles"], []),
    ("88594005", "B00.9", ["herpes simplex"], ["hsv"]),
    ("79740000", "B37.0", ["oral candidiasis", "oral thrush", "thrush"], []),
    ("414941008", "B35.1", ["onychomycosis", "nail fungus"], []),
    ("65363002", "H66.90", ["otitis media", "ear infection"], []),
    ("9826008", "H10.9", ["conjunctivitis", "pink eye"], []),
    # Dermatology
    ("88616000", "L70.0", ["acne vulgaris", "acne"], []),
    ("50563003", "L21.9", ["seborrheic dermatitis"], []),
    ("398909004", "L71.9", ["rosacea"], []),
    ("56727007", "L80", ["vitiligo"], []),
    ("254701007", "C44.91", ["basal cell carcinoma"], ["bcc"]),
    # Oncology
    ("399326009", "C67.9", ["bladder cancer", "malignant neoplasm of bladder"], []),
    ("363518003", "C64.9", ["kidney cancer", "renal cancer", "renal cell carcinoma"], ["rcc"]),
    ("363478007", "C73", ["thyroid cancer"], []),
    ("363443007", "C56.9", ["ovarian cancer"], []),
    ("363354003", "C53.9", ["cervical cancer"], []),
    ("109989006", "C90.00", ["multiple myeloma", "myeloma"], []),
    ("363349007", "C16.9", ["stomach cancer", "gastric cancer"], []),
    ("25370001", "C22.0", ["hepatocellular carcinoma"], ["hcc"]),
    ("363351006", "C20", ["rectal cancer"], []),
    ("118599009", "C81.90", ["hodgkin lymphoma", "hodgkin's lymphoma", "hodgkin disease"], []),
]

# --------------------------------------------------------------------------- #
# Medications: ingredient key -> (scanned synonyms incl. brands, exact-only synonyms)
# Single-ingredient drugs only; brands are verified to map to the ingredient.
# --------------------------------------------------------------------------- #
MEDICATIONS = {
    # Diabetes
    "glipizide": (["glucotrol"], []),
    "glimepiride": (["amaryl"], []),
    "pioglitazone": (["actos"], []),
    "sitagliptin": (["januvia"], []),
    "dapagliflozin": (["farxiga"], []),
    "liraglutide": (["victoza", "saxenda"], []),
    "dulaglutide": (["trulicity"], []),
    "tirzepatide": (["mounjaro", "zepbound"], []),
    "insulin lispro": (["humalog", "admelog"], []),
    "insulin aspart": (["novolog", "fiasp"], []),
    "insulin detemir": (["levemir"], []),
    # Cardiovascular
    "enalapril": (["vasotec"], []),
    "valsartan": (["diovan"], []),
    "diltiazem": (["cardizem"], []),
    "verapamil": (["calan"], []),
    "carvedilol": (["coreg"], []),
    "atenolol": (["tenormin"], []),
    "propranolol": (["inderal"], []),
    "chlorthalidone": ([], []),
    "furosemide": (["lasix"], []),
    "spironolactone": (["aldactone"], []),
    "hydralazine": ([], []),
    "isosorbide mononitrate": ([], []),
    "isosorbide dinitrate": (["isordil"], []),
    "nitroglycerin": (["nitrostat"], []),
    "digoxin": (["lanoxin"], []),
    "amiodarone": (["pacerone"], []),
    "pravastatin": (["pravachol"], []),
    "ezetimibe": (["zetia"], []),
    "fenofibrate": (["tricor"], []),
    "clopidogrel": (["plavix"], []),
    "rivaroxaban": (["xarelto"], []),
    "enoxaparin": (["lovenox"], []),
    "heparin": ([], []),
    # Respiratory / allergy
    "budesonide": (["pulmicort"], []),
    "tiotropium": (["spiriva"], []),
    "montelukast": (["singulair"], []),
    "loratadine": (["claritin"], []),
    "cetirizine": (["zyrtec"], []),
    "fexofenadine": (["allegra"], []),
    # GI
    "famotidine": (["pepcid"], []),
    "ondansetron": ([], []),
    # Pain / musculoskeletal / rheumatology
    "naproxen": (["aleve", "naprosyn"], []),
    "meloxicam": (["mobic"], []),
    "tramadol": (["ultram"], []),
    "oxycodone": (["oxycontin", "roxicodone"], []),
    "morphine": ([], []),
    "cyclobenzaprine": ([], []),
    "baclofen": ([], []),
    "tizanidine": (["zanaflex"], []),
    "methotrexate": (["trexall"], []),
    "hydroxychloroquine": (["plaquenil"], []),
    "allopurinol": (["zyloprim"], []),
    "colchicine": (["colcrys"], []),
    "alendronate": (["fosamax"], []),
    # Neuro / psych
    "pregabalin": (["lyrica"], []),
    "levetiracetam": (["keppra"], []),
    "lamotrigine": (["lamictal"], []),
    "topiramate": (["topamax"], []),
    "sumatriptan": (["imitrex"], []),
    "donepezil": (["aricept"], []),
    "memantine": (["namenda"], []),
    "citalopram": (["celexa"], []),
    "fluoxetine": (["prozac"], []),
    "paroxetine": (["paxil"], []),
    "duloxetine": (["cymbalta"], []),
    "venlafaxine": (["effexor"], []),
    "bupropion": (["wellbutrin"], []),
    "mirtazapine": (["remeron"], []),
    "trazodone": ([], []),
    "amitriptyline": ([], []),
    "buspirone": ([], []),
    "quetiapine": (["seroquel"], []),
    "aripiprazole": (["abilify"], []),
    "risperidone": (["risperdal"], []),
    "olanzapine": (["zyprexa"], []),
    "lithium carbonate": (["lithium"], []),
    "alprazolam": (["xanax"], []),
    "lorazepam": (["ativan"], []),
    "clonazepam": (["klonopin"], []),
    "zolpidem": (["ambien"], []),
    "methylphenidate": (["ritalin", "concerta"], []),
    "lisdexamfetamine": (["vyvanse"], []),
    # Anti-infectives
    "azithromycin": (["zithromax", "z pak", "zpak"], []),
    "doxycycline": (["vibramycin"], []),
    "cephalexin": (["keflex"], []),
    "ciprofloxacin": (["cipro"], []),
    "levofloxacin": (["levaquin"], []),
    "nitrofurantoin": (["macrobid"], []),
    "metronidazole": (["flagyl"], []),
    "clindamycin": (["cleocin"], []),
    "fluconazole": (["diflucan"], []),
    "valacyclovir": (["valtrex"], []),
    "oseltamivir": (["tamiflu"], []),
    # Endocrine / GU / other
    "tamsulosin": (["flomax"], []),
    "finasteride": (["proscar", "propecia"], []),
    "sildenafil": (["viagra"], []),
    "estradiol": (["estrace"], []),
    "potassium chloride": (["klor con", "kcl"], []),
    "cholecalciferol": (["vitamin d3"], []),
    "cyanocobalamin": (["vitamin b12"], []),
    "folic acid": ([], []),
    "ferrous sulfate": ([], []),
    # Already in Canon (verified here, extra synonyms only)
    "metformin": (["glucophage"], []),
    "lisinopril": (["zestril", "prinivil"], []),
    "atorvastatin": (["lipitor"], []),
    "rosuvastatin": (["crestor"], []),
    "simvastatin": (["zocor"], []),
    "amlodipine": (["norvasc"], []),
    "losartan": (["cozaar"], []),
    "hydrochlorothiazide": ([], ["hctz"]),
    "levothyroxine": (["synthroid", "levoxyl"], []),
    "omeprazole": (["prilosec"], []),
    "pantoprazole": (["protonix"], []),
    "albuterol": (["ventolin", "proair", "salbutamol"], []),
    "fluticasone": (["flonase", "flovent"], []),
    "semaglutide": (["ozempic", "wegovy", "rybelsus"], []),
    "insulin glargine": (["lantus", "basaglar", "toujeo"], []),
    "empagliflozin": (["jardiance"], []),
    "sertraline": (["zoloft"], []),
    "escitalopram": (["lexapro"], []),
    "aspirin": ([], ["asa"]),
    "apixaban": (["eliquis"], []),
    "warfarin": (["coumadin", "jantoven"], []),
    "metoprolol": ([], []),
    "gabapentin": (["neurontin"], []),
    "ibuprofen": (["advil", "motrin"], []),
    "acetaminophen": (["tylenol", "paracetamol"], ["apap"]),
    "amoxicillin": ([], []),
    "prednisone": ([], []),
    # --- Expansion: common outpatient drugs (brands verified against RxNorm by this script) ---
    # Cardiovascular
    "bisoprolol": ([], []),
    "nebivolol": (["bystolic"], []),
    "labetalol": ([], []),
    "nadolol": (["corgard"], []),
    "nifedipine": (["procardia"], []),
    "felodipine": ([], []),
    "benazepril": (["lotensin"], []),
    "ramipril": (["altace"], []),
    "quinapril": (["accupril"], []),
    "irbesartan": (["avapro"], []),
    "olmesartan": (["benicar"], []),
    "telmisartan": (["micardis"], []),
    "candesartan": (["atacand"], []),
    "torsemide": (["soaanz"], []),
    "bumetanide": (["bumex"], []),
    "metolazone": ([], []),
    "eplerenone": (["inspra"], []),
    "clonidine": (["catapres"], []),
    "doxazosin": (["cardura"], []),
    "terazosin": ([], []),
    "prazosin": (["minipress"], []),
    "ranolazine": (["ranexa"], []),
    "dabigatran etexilate": (["pradaxa", "dabigatran"], []),
    "edoxaban": (["savaysa"], []),
    "ticagrelor": (["brilinta"], []),
    "prasugrel": (["effient"], []),
    "cilostazol": ([], []),
    "sotalol": (["betapace"], []),
    "flecainide": ([], []),
    "dofetilide": (["tikosyn"], []),
    "ivabradine": (["corlanor"], []),
    "minoxidil": ([], []),
    "lovastatin": (["altoprev"], []),
    "pitavastatin": (["livalo"], []),
    "evolocumab": (["repatha"], []),
    "alirocumab": (["praluent"], []),
    "icosapent ethyl": (["vascepa"], []),
    "gemfibrozil": (["lopid"], []),
    "niacin": (["niaspan"], []),
    # Diabetes
    "insulin degludec": (["tresiba"], []),
    "canagliflozin": (["invokana"], []),
    "ertugliflozin": (["steglatro"], []),
    "linagliptin": (["tradjenta"], []),
    "saxagliptin": (["onglyza"], []),
    "alogliptin": (["nesina"], []),
    "exenatide": (["byetta", "bydureon"], []),
    "glyburide": (["glynase"], []),
    "acarbose": (["precose"], []),
    "repaglinide": ([], []),
    # Gastrointestinal
    "lansoprazole": (["prevacid"], []),
    "esomeprazole": (["nexium"], []),
    "rabeprazole": (["aciphex"], []),
    "dexlansoprazole": (["dexilant"], []),
    "sucralfate": (["carafate"], []),
    "metoclopramide": (["reglan"], []),
    "dicyclomine": (["bentyl"], []),
    "loperamide": (["imodium"], []),
    "docusate": (["colace"], []),
    "sennosides": (["senokot"], []),
    "polyethylene glycol 3350": (["miralax"], []),
    "lactulose": ([], []),
    "bisacodyl": (["dulcolax"], []),
    "mesalamine": (["lialda", "pentasa", "apriso"], []),
    "linaclotide": (["linzess"], []),
    "prochlorperazine": (["compazine"], []),
    "promethazine": (["phenergan"], []),
    "ursodiol": ([], []),
    "rifaximin": (["xifaxan"], []),
    # Respiratory and allergy
    "salmeterol": (["serevent"], []),
    "formoterol": (["perforomist"], []),
    "ipratropium": (["atrovent"], []),
    "umeclidinium": (["incruse"], []),
    "benzonatate": ([], []),
    "guaifenesin": (["mucinex"], []),
    "levalbuterol": (["xopenex"], []),
    "mometasone": (["nasonex", "asmanex"], []),
    "beclomethasone": (["qvar"], []),
    "roflumilast": (["daliresp"], []),
    "levocetirizine": (["xyzal"], []),
    "diphenhydramine": (["benadryl"], []),
    "azelastine": ([], []),
    "epinephrine": (["epipen"], []),
    # Psychiatry and neurology
    "desvenlafaxine": (["pristiq"], []),
    "vortioxetine": (["trintellix"], []),
    "nortriptyline": (["pamelor"], []),
    "doxepin": (["silenor"], []),
    "hydroxyzine": (["vistaril"], []),
    "ziprasidone": (["geodon"], []),
    "haloperidol": (["haldol"], []),
    "lurasidone": (["latuda"], []),
    "clozapine": (["clozaril"], []),
    "carbamazepine": (["tegretol"], []),
    "oxcarbazepine": (["trileptal"], []),
    "phenytoin": (["dilantin"], []),
    "zonisamide": (["zonegran"], []),
    "lacosamide": (["vimpat"], []),
    "rizatriptan": (["maxalt"], []),
    "ropinirole": ([], []),
    "pramipexole": (["mirapex"], []),
    "rivastigmine": (["exelon"], []),
    "galantamine": ([], []),
    "temazepam": (["restoril"], []),
    "diazepam": (["valium"], []),
    "eszopiclone": (["lunesta"], []),
    "atomoxetine": (["strattera"], []),
    "guanfacine": (["intuniv"], []),
    "dextroamphetamine": (["dexedrine"], []),
    "naltrexone": (["vivitrol"], []),
    "buprenorphine": ([], []),
    "methadone": ([], []),
    "varenicline": (["chantix"], []),
    # Pain and musculoskeletal
    "celecoxib": (["celebrex"], []),
    "diclofenac": (["voltaren"], []),
    "ketorolac": ([], []),
    "tapentadol": (["nucynta"], []),
    "fentanyl": ([], []),
    "hydromorphone": (["dilaudid"], []),
    "hydrocodone": ([], []),
    "lidocaine": (["lidoderm"], []),
    "methocarbamol": (["robaxin"], []),
    # Anti-infectives
    "cefdinir": ([], []),
    "cefuroxime": ([], []),
    "ceftriaxone": ([], []),
    "penicillin V": ([], []),
    "sulfamethoxazole": ([], []),
    "trimethoprim": ([], []),
    "vancomycin": (["vancocin"], []),
    "linezolid": (["zyvox"], []),
    "clarithromycin": ([], []),
    "erythromycin": ([], []),
    "minocycline": (["minocin"], []),
    "acyclovir": (["zovirax"], []),
    "famciclovir": ([], []),
    "terbinafine": (["lamisil"], []),
    "nystatin": ([], []),
    "ivermectin": (["stromectol"], []),
    "mupirocin": (["bactroban"], []),
    # Endocrine and hormones
    "methimazole": ([], []),
    "propylthiouracil": ([], []),
    "liothyronine": (["cytomel"], []),
    "hydrocortisone": ([], []),
    "methylprednisolone": (["medrol"], []),
    "dexamethasone": (["decadron"], []),
    "prednisolone": ([], []),
    "testosterone": (["androgel"], []),
    "medroxyprogesterone": (["provera", "depo-provera"], []),
    "norethindrone": ([], []),
    "progesterone": (["prometrium"], []),
    "raloxifene": (["evista"], []),
    "denosumab": (["prolia", "xgeva"], []),
    "risedronate": (["actonel"], []),
    "ibandronate": (["boniva"], []),
    "calcitriol": (["rocaltrol"], []),
    "cinacalcet": (["sensipar"], []),
    "desmopressin": (["ddavp"], []),
    # Urology
    "oxybutynin": ([], []),
    "tolterodine": (["detrol"], []),
    "solifenacin": (["vesicare"], []),
    "mirabegron": (["myrbetriq"], []),
    "tadalafil": (["cialis"], []),
    "dutasteride": (["avodart"], []),
    # Immunology and rheumatology
    "adalimumab": (["humira"], []),
    "etanercept": (["enbrel"], []),
    "infliximab": (["remicade"], []),
    "sulfasalazine": (["azulfidine"], []),
    "leflunomide": (["arava"], []),
    "azathioprine": (["imuran"], []),
    "tacrolimus": (["prograf"], []),
    "cyclosporine": ([], []),
    "ustekinumab": (["stelara"], []),
    "secukinumab": (["cosentyx"], []),
    "tofacitinib": (["xeljanz"], []),
    "apremilast": (["otezla"], []),
    # Oncology (oral endocrine therapy)
    "tamoxifen": ([], []),
    "anastrozole": (["arimidex"], []),
    "letrozole": (["femara"], []),
    "exemestane": (["aromasin"], []),
    # Dermatology
    "tretinoin": (["retin-a"], []),
    "isotretinoin": ([], []),
    "ketoconazole": ([], []),
    "clotrimazole": ([], []),
    "permethrin": ([], []),
    # Supplements and electrolytes
    "magnesium oxide": ([], []),
    "calcium carbonate": (["tums"], []),
    "sodium bicarbonate": ([], []),
    "thiamine": ([], []),
    "melatonin": ([], []),
}
# Synonyms RxNorm doesn't index as names but that unambiguously mean the ingredient.
# Where RxClass offers several classes and the default pick is misleading, name the one
# to use. It must be one of the classes RxClass returns for that drug (checked).
PREFERRED_CLASS = {
    "topiramate": "Anti-epileptic Agent",   # default ATC pick is "Centrally acting antiobesity products"
    "lithium carbonate": "Lithium",         # no FDA EPC; ATC "Lithium"
    "icosapent ethyl": "Other lipid modifying agents",  # no FDA EPC; ATC C10AX
    "risedronate": "Bisphosphonates",                   # no FDA EPC; ATC M05BA
}
UNINDEXED_OK = {"asa", "hctz", "apap", "kcl", "vitamin d3", "vitamin b12", "lithium", "z pak", "zpak", "klor con",
                "dabigatran"}  # RxNorm splits dabigatran from the marketed prodrug dabigatran etexilate

# --------------------------------------------------------------------------- #


def get_json(url: str, **params):
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    for attempt in range(4):
        try:
            req = urllib.request.Request(full, headers={"Accept": "application/json", "User-Agent": "canon-build-vocab"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def loinc_name(code: str) -> str | None:
    data = get_json("https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search",
                    terms=code, sf="LOINC_NUM", df="LOINC_NUM,LONG_COMMON_NAME", maxList=50)
    return next((name for num, name in data[3] if num == code), None)


def icd10_name(code: str) -> str | None:
    data = get_json("https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search", terms=code, sf="code", maxList=50)
    return next((name for c, name in data[3] if c == code), None)


def snomed_name(code: str) -> tuple[str | None, bool]:
    data = get_json("https://tx.fhir.org/r4/CodeSystem/$lookup",
                    system="http://snomed.info/sct", code=code, _format="json")
    if data.get("resourceType") != "Parameters":
        return None, False
    params = data["parameter"]
    display = next((p.get("valueString") for p in params if p["name"] == "display"), None)
    inactive = any(p["name"] == "property"
                   and {"name": "code", "valueCode": "inactive"} in p.get("part", [])
                   and {"name": "value", "valueBoolean": True} in p.get("part", [])
                   for p in params)
    return display, not inactive


def _rx_props(rxcui: str) -> dict:
    return get_json(f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/properties.json")["properties"]


def rxnorm_ingredient(name: str) -> tuple[str, str, str] | None:
    data = get_json("https://rxnav.nlm.nih.gov/REST/rxcui.json", name=name, search=0)
    candidates = []
    for rxcui in data.get("idGroup", {}).get("rxnormId", []) or []:
        props = _rx_props(rxcui)
        if props["tty"] in ("IN", "PIN"):  # prefer IN ("heparin", not "heparin, porcine")
            candidates.append((props["tty"] != "IN", rxcui, props["name"], props["tty"]))
    return min(candidates)[1:] if candidates else None


def rxnorm_alias_ingredients(alias: str) -> set[str]:
    data = get_json("https://rxnav.nlm.nih.gov/REST/rxcui.json", name=alias, search=2)
    found: set[str] = set()
    for rxcui in data.get("idGroup", {}).get("rxnormId", []) or []:
        if _rx_props(rxcui)["tty"] == "IN":
            found.add(rxcui)
            continue
        # Base ingredients only: brands also relate to salt PINs (metformin hydrochloride)
        related = get_json(f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/related.json", tty="IN")
        for group in related["relatedGroup"].get("conceptGroup", []) or []:
            found |= {c["rxcui"] for c in group.get("conceptProperties", []) or []}
    return found


def drug_class(rxcui: str, tty: str, preferred: str | None = None) -> str | None:
    """FDA Established Pharmacologic Class via RxClass; ATC level 4 when there is none."""
    if preferred:
        all_classes = {name for _, name in classes(rxcui)}
        return preferred if preferred in all_classes else None
    return _default_class(rxcui, tty)


def classes(cui: str, **params) -> list[tuple[str, str]]:
    data = get_json("https://rxnav.nlm.nih.gov/REST/rxclass/class/byRxcui.json", rxcui=cui, **params)
    return sorted({(c["rxclassMinConceptItem"]["classId"], c["rxclassMinConceptItem"]["className"])
                   for c in data.get("rxclassDrugInfoList", {}).get("rxclassDrugInfo", [])})


def _default_class(rxcui: str, tty: str) -> str | None:
    lookups = [rxcui]
    if tty == "PIN":  # classes may hang off the base ingredient
        related = get_json(f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/related.json", tty="IN")
        lookups += [c["rxcui"] for g in related["relatedGroup"].get("conceptGroup", []) or []
                    for c in g.get("conceptProperties", []) or []]
    for cui in lookups:
        # Skip classes that come from diagnostic uses (fluoroestradiol F-18 -> "Radioactive Diagnostic Agent")
        epc = [name for _, name in classes(cui, relaSource="DAILYMED", relas="has_epc") if "diagnostic" not in name.lower()]
        if epc:
            return "; ".join(epc)
    for cui in lookups:
        atc = [name for _, name in classes(cui, relaSource="ATC") if "combination" not in name.lower()]
        if atc:
            return atc[0]
    return None


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def check_collisions(kind: str, groups: list[tuple[str, list[str]]], problems: list[str]) -> None:
    seen: dict[str, str] = {}
    for code, phrases in groups:
        for p in phrases:
            k = _key(p)
            if k in seen and seen[k] != code:
                problems.append(f"{kind}: synonym {p!r} used by both {seen[k]} and {code}")
            seen[k] = code


def expand_units(convert: dict, canonical: str) -> dict:
    """UCUM-keyed conversions -> every free-text spelling Canon's convert_unit may see."""
    out: dict[str, float | list[float]] = {s: 1 for s in UNIT_SPELLINGS[canonical]}
    for ucum, rule in convert.items():
        rule = [round(rule[0], 10), round(rule[1], 10)] if isinstance(rule, list) else round(rule, 10)
        for s in UNIT_SPELLINGS[ucum]:
            out.setdefault(s, rule)
    return out


def main() -> int:
    check_only = "--check" in sys.argv
    problems: list[str] = []
    pool = ThreadPoolExecutor(max_workers=8)

    n_obs = len(OBSERVATIONS) + len(EXTRA_OBSERVATION_SYNONYMS)
    unknown = set(REFERENCE_RANGES) - {o[0] for o in OBSERVATIONS}
    if unknown:
        problems.append(f"reference ranges for LOINC codes not in OBSERVATIONS: {sorted(unknown)}")
    print(f"Verifying {n_obs} LOINC codes...")
    check_collisions("observation", [(o[0], o[4] + o[5]) for o in OBSERVATIONS]
                     + [(c, s + e) for c, (s, e) in EXTRA_OBSERVATION_SYNONYMS.items()], problems)
    observations = []
    for (code, cat, unit, display, syns, exact, conv), name in zip(
            OBSERVATIONS, pool.map(lambda o: loinc_name(o[0]), OBSERVATIONS)):
        if name is None:
            problems.append(f"LOINC {code}: not found")
            continue
        missing = [u for u in (unit, *conv) if u not in UNIT_SPELLINGS]
        if missing:
            problems.append(f"LOINC {code}: no spellings for unit(s) {missing}")
            continue
        row = {"loinc": code, "display": display, "loinc_name": name, "category": cat, "unit": unit,
               "synonyms": syns, "exact": exact, "convert": expand_units(conv, unit)}
        if code in REFERENCE_RANGES:
            lo, hi, text = REFERENCE_RANGES[code]
            row["reference_range"] = {"low": lo, "high": hi, "source": RANGE_SOURCE, "source_text": text}
        observations.append(row)
    extra_codes = list(EXTRA_OBSERVATION_SYNONYMS)
    for code, name in zip(extra_codes, pool.map(loinc_name, extra_codes)):
        if name is None:
            problems.append(f"LOINC {code}: not found")
            continue
        syns, exact = EXTRA_OBSERVATION_SYNONYMS[code]
        observations.append({"loinc": code, "loinc_name": name, "synonyms": syns, "exact": exact, "extends": True})

    print(f"Verifying {len(CONDITIONS)} conditions (SNOMED CT + ICD-10-CM)...")
    check_collisions("condition", [(c[1], c[2] + c[3]) for c in CONDITIONS], problems)
    conditions = []
    snomed = list(pool.map(lambda c: snomed_name(c[0]), CONDITIONS))
    icd = list(pool.map(lambda c: icd10_name(c[1]), CONDITIONS))
    for (sct, icd_code, syns, exact), (sct_name, active), icd_name in zip(CONDITIONS, snomed, icd):
        if sct_name is None:
            problems.append(f"SNOMED {sct}: not found")
            continue
        if not active:
            problems.append(f"SNOMED {sct} ({sct_name}): inactive")
        if icd_name is None:
            problems.append(f"ICD-10-CM {icd_code} (for {sct_name}): not a current billable code")
            continue
        conditions.append({"icd10": icd_code, "display": icd_name, "snomed": sct, "snomed_display": sct_name,
                           "synonyms": syns, "exact": exact})

    print(f"Verifying {len(MEDICATIONS)} RxNorm ingredients, brands and ATC classes...")
    check_collisions("medication", [(k, s + e) for k, (s, e) in MEDICATIONS.items()], problems)
    medications = []
    names = list(MEDICATIONS)
    for name, found in zip(names, pool.map(rxnorm_ingredient, names)):
        if found is None:
            problems.append(f"RxNorm: no ingredient (IN/PIN) concept named {name!r}")
            continue
        rxcui, rx_name, tty = found
        syns, exact = MEDICATIONS[name]
        for alias in syns + exact:
            if _key(alias) in UNINDEXED_OK:
                continue
            ingredients = rxnorm_alias_ingredients(alias)
            if ingredients != {rxcui}:
                problems.append(f"RxNorm: {alias!r} -> {sorted(ingredients) or 'nothing'}, expected {rxcui} ({rx_name})")
        if _key(rx_name) != _key(name):
            syns = syns + [rx_name]  # e.g. cyanocobalamin is "vitamin B12" in RxNorm
        cls = drug_class(rxcui, tty, PREFERRED_CLASS.get(name))
        if cls is None:
            problems.append(f"RxClass: no usable class for {name} ({rxcui})"
                            + (f"; {PREFERRED_CLASS[name]!r} is not one of its classes" if name in PREFERRED_CLASS else ""))
        medications.append({"ingredient": name, "rxnorm": rxcui, "rxnorm_name": rx_name, "tty": tty,
                            "drug_class": cls, "synonyms": syns, "exact": exact})

    print(f"\n{len(observations)} observations, {len(conditions)} conditions, {len(medications)} medications")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        return 1
    if not check_only:
        OUT.mkdir(parents=True, exist_ok=True)
        for fname, rows in (("observations.json", observations), ("conditions.json", conditions),
                            ("medications.json", medications)):
            (OUT / fname).write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
        print(f"Wrote {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
