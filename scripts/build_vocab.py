"""Build canon/vocab/*.json from the curated lists below, verifying every code online.

    python scripts/build_vocab.py           # verify + write
    python scripts/build_vocab.py --check   # verify only, exit 1 on any problem

canon/terminology.py loads these files on top of its hand-written tables. Codes
and synonyms are curated here; official names come from the source of truth:

- LOINC:      NLM Clinical Tables  (clinicaltables.nlm.nih.gov/api/loinc_items)
- ICD-10-CM:  NLM Clinical Tables  (current billable codes only)
- SNOMED CT:  tx.fhir.org $lookup  (International Edition; must be active)
- RxNorm:     NLM RxNav            (ingredient IN, or PIN when no IN exists, or MIN for a combination;
                                    every brand synonym must map to that ingredient)
- Drug class: NLM RxClass          (FDA Established Pharmacologic Class; ATC level 4 fallback)
- CVX:        tx.fhir.org $lookup  (CDC vaccine codes; must be active)

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
    "[IU]/mL": ["IU/mL", "[IU]/mL", "kIU/L", "kU/L"],
    "{copies}/mL": ["copies/mL", "{copies}/mL", "cp/mL"],
    "L/min": ["L/min", "lpm", "liters/min"],
    "mg/(24.h)": ["mg/(24.h)", "mg/24h", "mg/24 h", "mg/24hr", "mg/day"],
    "g/(24.h)": ["g/(24.h)", "g/24h", "g/24 h", "g/24hr", "g/day"],
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
    # --- Expansion round 2 ---
    ("6598-7", LAB, "ng/mL", "Troponin T", ["troponin t", "cardiac troponin t"], [], {"ug/L": 1}),
    ("67151-1", LAB, "ng/L", "Troponin T (high sensitivity)", ["hs troponin t", "high sensitivity troponin t", "hs-tnt"], [], {}),
    ("13969-1", LAB, "ng/mL", "CK-MB", ["ck-mb", "ckmb", "creatine kinase mb"], [], {"ug/L": 1}),
    ("2639-3", LAB, "ng/mL", "Myoglobin", ["myoglobin"], [], {"ug/L": 1}),
    ("2141-0", LAB, "pg/mL", "ACTH", ["acth", "adrenocorticotropic hormone", "corticotropin"], [], {"ng/L": 1}),
    ("1763-2", LAB, "ng/dL", "Aldosterone", ["aldosterone", "serum aldosterone"], [], {}),
    ("15069-8", LAB, "umol/L", "Fructosamine", ["fructosamine"], [], {}),
    ("3013-0", LAB, "ng/mL", "Thyroglobulin", ["thyroglobulin"], [], {"ug/L": 1}),
    ("2064-4", LAB, "mg/dL", "Ceruloplasmin", ["ceruloplasmin"], [], {"g/L": 100}),
    ("5763-8", LAB, "ug/dL", "Zinc", ["zinc level", "serum zinc"], [], {}),
    ("5631-7", LAB, "ug/dL", "Copper", ["copper level", "serum copper"], [], {}),
    ("2955-3", LAB, "mmol/L", "Sodium, urine", ["urine sodium", "sodium urine"], [], {"meq/L": 1}),
    ("2828-2", LAB, "mmol/L", "Potassium, urine", ["urine potassium", "potassium urine"], [], {"meq/L": 1}),
    ("5640-8", LAB, "mg/dL", "Ethanol", ["blood alcohol", "serum ethanol", "ethanol level", "alcohol level"], [], {}),
    ("4024-6", LAB, "mg/dL", "Salicylate level", ["salicylate level", "aspirin level"], [], {}),
    # --- Expansion round 3 ---
    ("2458-8", LAB, "mg/dL", "IgA", ["iga level", "immunoglobulin a", "serum iga"], ["iga"], {"g/L": 100}),
    ("2465-3", LAB, "mg/dL", "IgG", ["igg level", "immunoglobulin g", "serum igg"], ["igg"], {"g/L": 100}),
    ("2472-9", LAB, "mg/dL", "IgM", ["igm level", "immunoglobulin m", "serum igm"], ["igm"], {"g/L": 100}),
    ("19113-0", LAB, "[IU]/mL", "IgE", ["ige level", "immunoglobulin e", "total ige", "serum ige"], ["ige"], {}),
    ("4485-9", LAB, "mg/dL", "Complement C3", ["complement c3", "c3 complement", "c3 level"], [], {"g/L": 100}),
    ("4498-2", LAB, "mg/dL", "Complement C4", ["complement c4", "c4 complement", "c4 level"], [], {"g/L": 100}),
    ("11572-5", LAB, "[IU]/mL", "Rheumatoid factor", ["rheumatoid factor"], ["rf"], {}),
    ("8099-4", LAB, "[IU]/mL", "TPO antibodies", ["tpo antibodies", "tpo antibody", "thyroid peroxidase antibodies", "anti-tpo", "thyroperoxidase antibody"], [], {}),
    ("3053-6", LAB, "ng/dL", "T3, total", ["total t3", "t3 total", "triiodothyronine"], [], {"nmol/L": 65.1}),
    ("1992-7", LAB, "pg/mL", "Calcitonin", ["calcitonin level", "serum calcitonin"], [], {"ng/L": 1}),
    ("13967-5", LAB, "nmol/L", "SHBG", ["shbg", "sex hormone binding globulin"], [], {}),
    ("16935-9", LAB, "m[IU]/mL", "Hepatitis B surface antibody", ["hepatitis b surface antibody", "hep b surface antibody", "hbsab", "anti-hbs"], [], {}),
    ("36916-5", LAB, "mg/L", "Free kappa light chains", ["free kappa light chains", "kappa free light chains", "free kappa"], [], {"mg/dL": 10}),
    ("33944-0", LAB, "mg/L", "Free lambda light chains", ["free lambda light chains", "lambda free light chains", "free lambda"], [], {"mg/dL": 10}),
    ("1952-1", LAB, "mg/L", "Beta-2 microglobulin", ["beta-2 microglobulin", "beta 2 microglobulin", "b2 microglobulin"], [], {"ug/mL": 1}),
    ("1925-7", LAB, "mmol/L", "Base excess", ["base excess"], [], {"meq/L": 1}),
    ("20563-3", LAB, "%", "Carboxyhemoglobin", ["carboxyhemoglobin", "carboxyhaemoglobin", "cohb"], [], {}),
    ("3520-4", LAB, "ng/mL", "Cyclosporine level", ["cyclosporine level", "cyclosporine trough", "ciclosporin level"], [], {"ug/L": 1}),
    ("50544-6", LAB, "ng/mL", "Everolimus level", ["everolimus level", "everolimus trough"], [], {"ug/L": 1}),
    ("764-1", LAB, "%", "Band neutrophils", ["band neutrophils", "band forms"], ["bands"], {}),
    ("14196-0", LAB, "10*3/uL", "Reticulocyte count, absolute", ["absolute reticulocyte count", "reticulocyte count absolute"], [], {"10*9/L": 1, "10*6/uL": 1000}),
    ("11011-4", LAB, "[IU]/mL", "HCV viral load", ["hcv viral load", "hepatitis c viral load", "hcv rna"], [], {}),
    ("20447-9", LAB, "{copies}/mL", "HIV viral load", ["hiv viral load", "hiv rna", "hiv-1 rna"], [], {}),
    ("2695-5", LAB, "mosm/kg", "Osmolality, urine", ["urine osmolality", "osmolality urine"], [], {}),
    ("2890-2", LAB, "mg/g", "Protein/creatinine ratio, urine", ["urine protein creatinine ratio", "protein/creatinine ratio", "upcr"], [], {}),
    ("8478-0", VITAL, "mm[Hg]", "Mean arterial pressure", ["mean arterial pressure", "mean blood pressure"], ["map"], {}),
    ("3150-0", VITAL, "%", "FiO2", ["inhaled oxygen concentration", "fio2"], [], {}),
    ("3151-8", VITAL, "L/min", "Oxygen flow rate", ["oxygen flow rate", "o2 flow rate", "oxygen flow"], [], {}),
    # --- Expansion round 5 ---
    ("53027-9", LAB, "U/mL", "Anti-CCP antibody", ["anti-ccp", "anti ccp", "ccp antibody", "cyclic citrullinated peptide antibody"], [], {}),
    ("5130-0", LAB, "[IU]/mL", "dsDNA antibody", ["anti-dsdna", "anti dsdna", "dsdna antibody", "double stranded dna antibody"], [], {}),
    ("13458-5", LAB, "mg/dL", "VLDL cholesterol", ["vldl cholesterol", "vldl"], [], {"mmol/L": 38.67}),
    ("1869-7", LAB, "mg/dL", "Apolipoprotein A-I", ["apolipoprotein a1", "apolipoprotein a-i", "apo a1", "apoa1"], [], {"g/L": 100}),
    ("5671-3", LAB, "ug/dL", "Lead, blood", ["blood lead", "lead level"], [], {}),
    ("62290-2", LAB, "pg/mL", "1,25-dihydroxyvitamin D", ["1,25-dihydroxyvitamin d", "1,25 dihydroxyvitamin d", "calcitriol level"], [], {"pmol/L": 0.4166}),
    ("6875-9", LAB, "U/mL", "CA 15-3", ["ca 15-3", "ca15-3", "cancer antigen 15-3"], [], {}),
    ("17842-6", LAB, "U/mL", "CA 27-29", ["ca 27-29", "ca 27.29", "cancer antigen 27-29"], [], {}),
    ("2889-4", LAB, "mg/(24.h)", "Protein, 24-hour urine", ["24 hour urine protein", "24-hour urine protein", "24h urine protein"], [], {"g/(24.h)": 1000}),
    ("71695-1", LAB, "%", "Immature granulocytes", ["immature granulocytes", "immature grans"], [], {}),
    ("4049-3", LAB, "ug/mL", "Theophylline level", ["theophylline level"], [], {"mg/L": 1}),
    ("14836-1", LAB, "umol/L", "Methotrexate level", ["methotrexate level"], [], {}),
    ("6948-4", LAB, "ug/mL", "Lamotrigine level", ["lamotrigine level", "lamictal level"], [], {"mg/L": 1}),
    ("8098-6", LAB, "[IU]/mL", "Thyroglobulin antibody", ["thyroglobulin antibody", "thyroglobulin antibodies", "anti-thyroglobulin"], [], {}),
    ("31017-7", LAB, "U/mL", "tTG IgA", ["ttg iga", "tissue transglutaminase iga", "anti-ttg iga", "tissue transglutaminase antibody"], [], {}),
    ("33358-3", LAB, "g/dL", "M-spike", ["m-spike", "m spike", "monoclonal protein", "paraprotein"], [], {"g/L": 0.1}),
    ("48378-4", LAB, "{ratio}", "Free kappa/lambda ratio", ["kappa/lambda ratio", "kappa lambda ratio", "free light chain ratio", "flc ratio"], [], {}),
    ("1971-1", LAB, "mg/dL", "Bilirubin, indirect", ["indirect bilirubin", "unconjugated bilirubin"], [], {"umol/L": 0.05848}),
    ("1759-0", LAB, "{ratio}", "Albumin/globulin ratio", ["albumin/globulin ratio", "albumin globulin ratio", "a/g ratio"], [], {}),
    ("3097-3", LAB, "{ratio}", "BUN/creatinine ratio", ["bun/creatinine ratio", "bun creatinine ratio", "bun/cr ratio"], [], {}),
    ("13964-2", LAB, "nmol/L", "Methylmalonic acid", ["methylmalonic acid", "mma level"], [], {"umol/L": 1000}),
    ("3034-6", LAB, "mg/dL", "Transferrin", ["transferrin"], [], {"g/L": 100}),
    ("9269-2", VITAL, "{score}", "Glasgow Coma Scale", ["glasgow coma scale", "glasgow coma score"], ["gcs"], {}),
    ("19935-6", VITAL, "L/min", "Peak expiratory flow", ["peak expiratory flow", "peak flow"], ["pefr"], {}),
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
    "59408-5": (["oxygen saturation", "pulse ox", "pulse oximetry", "o2 sat", "spo2", "arterial oxygen saturation"], ["sao2"]),
    "13457-7": (["direct ldl", "ldl direct"], []),
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
    # --- Expansion round 2 (SNOMED resolved by exact name; ICD-10-CM verified by name) ---
    ("38907003", "B01.9", ["chickenpox", "varicella"], []),
    ("14189004", "B05.9", ["measles"], []),
    ("36989005", "B26.9", ["mumps"], []),
    ("27836007", "A37.90", ["pertussis", "whooping cough"], []),
    ("4120002", "J21.9", ["bronchiolitis"], []),
    ("71186008", "J05.0", ["croup"], []),
    ("128869009", "B86", ["scabies"], []),
    ("81000006", "B85.0", ["head lice", "pediculosis capitis"], []),
    ("387712008", "P59.9", ["neonatal jaundice"], []),
    ("11687002", "O24.419", ["gestational diabetes"], ["gdm"]),
    ("398254007", "O14.90", ["preeclampsia", "pre-eclampsia"], []),
    ("266599000", "N94.6", ["dysmenorrhea", "dysmenorrhoea"], []),
    ("386692008", "N92.0", ["menorrhagia", "heavy menstrual bleeding"], []),
    ("14302001", "N91.2", ["amenorrhea", "amenorrhoea"], []),
    ("79883001", "N83.209", ["ovarian cyst"], []),
    ("6738008", "N97.9", ["female infertility"], []),
    ("30800001", "N76.0", ["vaginitis"], []),
    ("56882008", "F50.00", ["anorexia nervosa"], []),
    ("78004001", "F50.20", ["bulimia nervosa", "bulimia"], []),
    ("439960005", "F50.819", ["binge eating disorder"], []),
    ("17226007", "F43.20", ["adjustment disorder"], []),
    ("15167005", "F10.10", ["alcohol abuse"], []),
    ("67195008", "F43.0", ["acute stress reaction", "acute stress disorder"], []),
    ("76272004", "A53.9", ["syphilis"], []),
    ("15628003", "A54.9", ["gonorrhea", "gonorrhoea"], []),
    ("105629000", "A74.9", ["chlamydia infection", "chlamydial infection"], []),
    ("23502006", "A69.20", ["lyme disease"], []),
    ("40468003", "B15.9", ["hepatitis a"], []),
    ("271558008", "B27.90", ["infectious mononucleosis", "mononucleosis"], []),
    ("1296960002", "B07.9", ["viral warts", "common wart", "verruca"], []),
    ("6020002", "B35.3", ["tinea pedis", "athlete's foot"], []),
    ("36689008", "N10", ["acute pyelonephritis", "pyelonephritis"], []),
    ("52254009", "N04.9", ["nephrotic syndrome"], []),
    ("36171008", "N05.9", ["glomerulonephritis"], []),
    ("722223000", "N28.1", ["renal cyst", "kidney cyst"], []),
    ("420054005", "K70.30", ["alcoholic cirrhosis"], []),
    ("408335007", "K75.4", ["autoimmune hepatitis"], []),
    ("31712002", "K74.3", ["primary biliary cholangitis", "primary biliary cirrhosis"], ["pbc"]),
    ("197441003", "K83.01", ["primary sclerosing cholangitis"], ["psc"]),
    ("35400008", "E83.110", ["hereditary hemochromatosis", "hemochromatosis"], []),
    ("88518009", "E83.01", ["wilson disease", "wilson's disease"], []),
    ("3135009", "H60.90", ["otitis externa", "swimmer's ear"], []),
    ("46152009", "H04.129", ["dry eye syndrome", "dry eye"], []),
    ("42059000", "H33.009", ["retinal detachment"], []),
    ("126660000", "J34.2", ["deviated nasal septum", "deviated septum"], []),
    ("90979004", "J35.01", ["chronic tonsillitis"], []),
    ("6456007", "I47.10", ["supraventricular tachycardia"], ["svt"]),
    ("25569003", "I47.20", ["ventricular tachycardia"], []),
    ("71908006", "I49.01", ["ventricular fibrillation"], ["vfib"]),
    ("195042002", "I44.1", ["second degree heart block", "second degree av block"], []),
    ("426749004", "I48.20", ["chronic atrial fibrillation", "permanent atrial fibrillation"], []),
    ("3238004", "I30.9", ["pericarditis", "acute pericarditis"], []),
    ("50920009", "I40.9", ["myocarditis"], []),
    ("79619009", "I05.0", ["mitral stenosis"], []),
    ("60234000", "I35.1", ["aortic regurgitation", "aortic insufficiency"], []),
    ("266261006", "I73.00", ["raynaud phenomenon", "raynaud's phenomenon", "raynauds"], []),
    ("128609009", "I67.1", ["cerebral aneurysm", "brain aneurysm"], []),
    ("274100004", "I61.9", ["intracerebral hemorrhage", "hemorrhagic stroke", "brain bleed"], []),
    ("21454007", "I60.9", ["subarachnoid hemorrhage"], ["sah"]),
    ("417996009", "I50.20", ["systolic heart failure", "heart failure with reduced ejection fraction", "hfref"], []),
    ("418304008", "I50.30", ["diastolic heart failure", "heart failure with preserved ejection fraction", "hfpef"], []),
    ("428061005", "C71.9", ["brain cancer", "glioblastoma"], []),
    ("363402007", "C15.9", ["esophageal cancer", "oesophageal cancer"], []),
    ("363353009", "C23", ["gallbladder cancer"], []),
    ("254878006", "C54.1", ["endometrial cancer"], []),
    ("363449006", "C62.90", ["testicular cancer"], []),
    ("92814006", "C91.10", ["chronic lymphocytic leukemia"], ["cll"]),
    ("91861009", "C92.00", ["acute myeloid leukemia"], ["aml"]),
    ("92818009", "C92.10", ["chronic myeloid leukemia"], ["cml"]),
    ("91857003", "C91.00", ["acute lymphoblastic leukemia"], []),
    ("254651007", "C44.92", ["squamous cell carcinoma of skin"], ["scc"]),
    ("91935009", "Z91.010", ["peanut allergy"], []),
    ("39579001", "T78.2XXA", ["anaphylaxis"], []),
    ("41291007", "T78.3XXA", ["angioedema"], []),
    ("230572002", "E11.40", ["diabetic neuropathy"], []),
    ("127013003", "E11.21", ["diabetic nephropathy"], []),
    ("4855003", "E11.319", ["diabetic retinopathy"], []),
    ("420422005", "E10.10", ["diabetic ketoacidosis"], ["dka"]),
    ("373662000", "E27.1", ["addison disease", "addison's disease", "primary adrenal insufficiency"], []),
    ("47270006", "E24.9", ["cushing syndrome", "cushing's syndrome"], []),
    ("74107003", "E22.0", ["acromegaly"], []),
    ("45369008", "E23.2", ["diabetes insipidus"], []),
    ("36976004", "E20.9", ["hypoparathyroidism"], []),
    ("66931009", "E83.52", ["hypercalcemia", "hypercalcaemia"], []),
    ("5291005", "E83.51", ["hypocalcemia", "hypocalcaemia"], []),
    # --- Expansion round 3 ---
    ("59393003", "L73.2", ["hidradenitis suppurativa", "hidradenitis"], []),
    ("68225006", "L63.9", ["alopecia areata"], []),
    ("394726009", "L82.1", ["seborrheic keratosis", "seborrhoeic keratosis"], []),
    ("201101007", "L57.0", ["actinic keratosis", "solar keratosis"], []),
    ("48277006", "L01.00", ["impetigo"], []),
    ("271807003", "R21", ["skin rash", "rash"], []),
    ("234097001", "I89.0", ["lymphedema", "lymphoedema"], []),
    ("64156001", "I80.9", ["thrombophlebitis", "superficial thrombophlebitis"], []),
    ("308546005", "I71.00", ["aortic dissection", "dissection of aorta"], []),
    ("64586002", "I65.29", ["carotid artery stenosis", "carotid stenosis"], []),
    ("409712001", "I34.1", ["mitral valve prolapse"], ["mvp"]),
    ("74390002", "I45.6", ["wolff-parkinson-white", "wolff parkinson white"], ["wpw"]),
    ("36083008", "I49.5", ["sick sinus syndrome"], []),
    ("48867003", "R00.1", ["bradycardia"], []),
    ("3424008", "R00.0", ["tachycardia"], []),
    ("80313002", "R00.2", ["palpitations"], []),
    ("271594007", "R55", ["syncope", "fainting"], []),
    ("28651003", "I95.1", ["orthostatic hypotension", "postural hypotension"], []),
    ("56819008", "I38", ["endocarditis"], []),
    ("373945007", "I31.39", ["pericardial effusion"], []),
    ("83291003", "I27.81", ["cor pulmonale"], []),
    ("28670008", "I85.00", ["esophageal varices", "oesophageal varices"], []),
    ("267434003", "E78.2", ["mixed hyperlipidemia", "combined hyperlipidemia"], []),
    ("68154008", "R05.3", ["chronic cough"], []),
    ("66857006", "R04.2", ["hemoptysis", "haemoptysis"], []),
    ("90176007", "J03.90", ["tonsillitis"], []),
    ("45913009", "J04.0", ["laryngitis"], []),
    ("422588002", "J69.0", ["aspiration pneumonia"], []),
    ("427359005", "R91.1", ["pulmonary nodule", "lung nodule", "solitary pulmonary nodule"], []),
    ("409622000", "J96.90", ["respiratory failure"], []),
    ("67782005", "J80", ["acute respiratory distress syndrome"], ["ards"]),
    ("389087006", "R09.02", ["hypoxemia", "hypoxaemia"], []),
    ("41446000", "H01.009", ["blepharitis"], []),
    ("13445001", "H81.09", ["meniere's disease", "meniere disease", "ménière's disease"], []),
    ("302914006", "K22.70", ["barrett's esophagus", "barrett esophagus", "barrett's oesophagus"], []),
    ("235599003", "K20.0", ["eosinophilic esophagitis", "eosinophilic oesophagitis"], ["eoe"]),
    ("45564002", "K22.0", ["achalasia"], []),
    ("30037006", "K60.2", ["anal fissure"], []),
    ("13920009", "K76.82", ["hepatic encephalopathy"], []),
    ("389026000", "R18.8", ["ascites"], []),
    ("34742003", "K76.6", ["portal hypertension"], []),
    ("81060008", "K56.609", ["bowel obstruction", "small bowel obstruction", "intestinal obstruction"], ["sbo"]),
    ("74400008", "K37", ["appendicitis"], []),
    ("40739000", "R13.10", ["dysphagia", "difficulty swallowing"], []),
    ("165517008", "D70.9", ["neutropenia"], []),
    ("111583006", "D72.829", ["leukocytosis"], []),
    ("40108008", "D56.9", ["thalassemia", "thalassaemia"], []),
    ("128105004", "D68.00", ["von willebrand disease"], ["vwd"]),
    ("26843008", "D68.61", ["antiphospholipid syndrome", "antiphospholipid antibody syndrome"], []),
    ("771115008", "E87.0", ["hypernatremia", "hypernatraemia"], []),
    ("35885006", "E79.0", ["hyperuricemia", "hyperuricaemia"], []),
    ("88213004", "E26.9", ["hyperaldosteronism", "primary aldosteronism"], []),
    ("302835009", "D35.00", ["pheochromocytoma", "phaeochromocytoma"], []),
    ("237662005", "E22.1", ["hyperprolactinemia", "hyperprolactinaemia"], []),
    ("254956000", "D35.2", ["pituitary adenoma"], []),
    ("3716002", "E04.9", ["goiter", "goitre"], []),
    ("353295004", "E05.00", ["graves disease", "graves' disease"], []),
    ("48130008", "E29.1", ["hypogonadism", "low testosterone"], []),
    ("312894000", "M85.80", ["osteopenia"], []),
    ("84017003", "M71.9", ["bursitis"], []),
    ("34840004", "M77.9", ["tendinitis", "tendonitis"], []),
    ("76107001", "M48.00", ["spinal stenosis"], []),
    ("83901003", "M35.00", ["sjogren's syndrome", "sjogren syndrome", "sjögren's syndrome"], []),
    ("156370009", "L40.50", ["psoriatic arthritis"], []),
    ("31681005", "G50.0", ["trigeminal neuralgia"], []),
    ("91637004", "G70.00", ["myasthenia gravis"], []),
    ("86044005", "G12.21", ["amyotrophic lateral sclerosis", "lou gehrig's disease"], ["als"]),
    ("58756001", "G10", ["huntington's disease", "huntington disease"], []),
    ("60380001", "G47.419", ["narcolepsy"], []),
    ("230745008", "G91.9", ["hydrocephalus"], []),
    ("2776000", "F05", ["delirium"], []),
    ("91175000", "R56.9", ["seizure", "seizures"], []),
    ("193031009", "G44.009", ["cluster headache", "cluster headaches"], []),
    ("2177002", "B02.29", ["postherpetic neuralgia", "post-herpetic neuralgia"], []),
    ("128188000", "G80.9", ["cerebral palsy"], []),
    ("59770006", "R48.0", ["dyslexia"], []),
    ("267064002", "R33.9", ["urinary retention"], []),
    ("29738008", "R80.9", ["proteinuria"], []),
    ("9713002", "N41.9", ["prostatitis"], []),
    ("34801009", "O00.90", ["ectopic pregnancy"], []),
    ("17369002", "O03.9", ["miscarriage", "spontaneous abortion"], []),
    ("14094001", "O21.0", ["hyperemesis gravidarum"], []),
    ("58703003", "F53.0", ["postpartum depression", "postnatal depression"], []),
    ("78048006", "B37.9", ["candidiasis", "yeast infection"], []),
    ("61462000", "B54", ["malaria"], []),
    ("267038008", "R60.9", ["edema", "oedema", "swelling of legs"], []),
    ("67531005", "Q05.9", ["spina bifida"], []),
    ("54840006", "R62.51", ["failure to thrive"], []),
    ("1163215007", "L89.90", ["pressure ulcer", "pressure injury", "decubitus ulcer", "bed sore"], []),
    ("418363000", "L29.9", ["pruritus", "itching"], []),
    ("251175005", "I49.3", ["premature ventricular contractions", "ventricular premature beats", "pvcs"], ["pvc"]),
    ("88610006", "R01.1", ["heart murmur", "cardiac murmur"], []),
    ("20696009", "I87.2", ["chronic venous insufficiency", "venous insufficiency"], []),
    ("372070002", "I96", ["gangrene"], []),
    ("249366005", "R04.0", ["epistaxis", "nosebleed"], []),
    ("235675006", "K31.84", ["gastroparesis"], []),
    ("721730009", "B96.81", ["h. pylori infection", "helicobacter pylori infection", "h pylori infection"], []),
    ("733657002", "K57.30", ["diverticulosis"], []),
    ("782415009", "E73.9", ["lactose intolerance"], []),
    ("109992005", "D45", ["polycythemia vera"], []),
    ("84027009", "D51.0", ["pernicious anemia", "pernicious anaemia"], []),
    ("421527008", "D68.51", ["factor v leiden"], []),
    ("277577000", "D47.2", ["monoclonal gammopathy of undetermined significance", "monoclonal gammopathy of uncertain significance", "monoclonal gammopathy"], ["mgus"]),
    ("109995007", "D46.9", ["myelodysplastic syndrome"], ["mds"]),
    ("237602007", "E88.810", ["metabolic syndrome"], []),
    ("926335004", "M75.100", ["rotator cuff tear"], []),
    ("298382003", "M41.9", ["scoliosis"], []),
    ("201637001", "M11.20", ["pseudogout", "cppd disease"], []),
    ("57676002", "M25.50", ["arthralgia", "joint pain"], []),
    ("1003722009", "M25.569", ["knee pain"], []),
    ("45326000", "M25.519", ["shoulder pain"], []),
    ("49218002", "M25.559", ["hip pain"], []),
    ("55300003", "R25.2", ["muscle cramps", "muscle cramp"], []),
    ("5158005", "F95.2", ["tourette syndrome", "tourette's syndrome", "tourette's disorder"], []),
    ("386805003", "G31.84", ["mild cognitive impairment"], ["mci"]),
    ("786457000", "N32.81", ["overactive bladder"], ["oab"]),
    ("34436003", "R31.9", ["hematuria", "haematuria"], []),
    ("41040004", "Q90.9", ["down syndrome", "trisomy 21"], []),
    ("202882003", "M72.20", ["plantar fasciitis"], []),
    ("109378008", "C45.9", ["mesothelioma"], []),
    # --- Expansion round 4 ---
    ("87557004", "N39.41", ["urge incontinence"], []),
    ("139394000", "R35.1", ["nocturia"], []),
    ("51070004", "I86.1", ["varicocele"], []),
    ("302233006", "I70.1", ["renal artery stenosis"], []),
    ("51387008", "E87.20", ["acidosis", "metabolic acidosis"], []),
    ("21420006", "E87.3", ["alkalosis", "metabolic alkalosis"], []),
    ("4996001", "E83.39", ["hypophosphatemia", "hypophosphataemia"], []),
    ("119247004", "E88.09", ["hypoalbuminemia", "hypoalbuminaemia"], []),
    ("248342006", "R63.6", ["underweight"], []),
    ("64226004", "K52.9", ["colitis"], []),
    ("235753003", "K52.839", ["microscopic colitis"], []),
    ("47367009", "K86.81", ["exocrine pancreatic insufficiency", "pancreatic insufficiency"], []),
    ("82403002", "K83.09", ["cholangitis"], []),
    ("30188007", "E88.01", ["alpha-1 antitrypsin deficiency", "alpha 1 antitrypsin deficiency"], []),
    ("16761005", "K20.90", ["esophagitis", "oesophagitis"], []),
    ("13200003", "K27.9", ["peptic ulcer", "peptic ulcer disease"], []),
    ("396347007", "K42.9", ["umbilical hernia"], []),
    ("236037000", "K43.2", ["incisional hernia"], []),
    ("47639008", "L05.91", ["pilonidal cyst"], []),
    ("93163002", "D17.9", ["lipoma"], []),
    ("33659008", "L91.0", ["keloid"], []),
    ("400097005", "L60.0", ["ingrown toenail", "ingrown nail"], []),
    ("84849002", "B35.4", ["tinea corporis", "ringworm"], []),
    ("399029005", "B35.6", ["tinea cruris", "jock itch"], []),
    ("56454009", "B36.0", ["tinea versicolor", "pityriasis versicolor"], []),
    ("4776004", "L43.9", ["lichen planus"], []),
    ("312230002", "R61", ["hyperhidrosis", "excessive sweating"], []),
    ("22066006", "H50.9", ["strabismus"], []),
    ("387742006", "H53.009", ["amblyopia", "lazy eye"], []),
    ("65636009", "H18.609", ["keratoconus"], []),
    ("41256004", "H52.4", ["presbyopia"], []),
    ("57190000", "H52.10", ["myopia", "nearsightedness"], []),
    ("24982008", "H53.2", ["diplopia", "double vision"], []),
    ("66760008", "H46.9", ["optic neuritis"], []),
    ("60700002", "H90.5", ["sensorineural hearing loss"], []),
    ("18070006", "H61.20", ["cerumen impaction", "impacted cerumen", "impacted earwax"], []),
    ("72863001", "R06.83", ["snoring"], []),
    ("68235000", "R09.81", ["nasal congestion"], []),
    ("87433001", "J43.9", ["emphysema"], []),
    ("63480004", "J42", ["chronic bronchitis"], []),
    ("196075003", "R09.1", ["pleurisy", "pleuritic chest pain"], []),
    ("46621007", "J98.11", ["atelectasis"], []),
    ("56018004", "R06.2", ["wheezing"], []),
    ("401314000", "I21.4", ["non-st elevation myocardial infarction"], []),
    ("111287006", "I36.1", ["tricuspid regurgitation"], []),
    ("9651007", "I45.81", ["long qt syndrome", "prolonged qt"], []),
    ("8186001", "I51.7", ["cardiomegaly"], []),
    ("410429000", "I46.9", ["cardiac arrest"], []),
    ("27942005", "R57.9", [], ["shock"]),
    ("89138009", "R57.0", ["cardiogenic shock"], []),
    ("76571007", "R65.21", ["septic shock"], []),
    ("443482000", "I16.0", ["hypertensive urgency"], []),
    ("16402000", "D57.3", ["sickle cell trait"], []),
    ("2897005", "D69.3", ["immune thrombocytopenia", "idiopathic thrombocytopenic purpura"], []),
    ("35240004", "E61.1", ["iron deficiency"], []),
    ("6631009", "D75.839", ["thrombocytosis"], []),
    ("84828003", "D72.819", ["leukopenia"], []),
    ("48813009", "D72.810", ["lymphopenia", "lymphocytopenia"], []),
    ("16294009", "R16.1", ["splenomegaly"], []),
    ("30746006", "R59.9", ["lymphadenopathy"], []),
    ("109994006", "D47.3", ["essential thrombocythemia", "essential thrombocytosis"], []),
    ("52967002", "D47.4", ["myelofibrosis"], []),
    ("109969005", "C83.30", ["diffuse large b-cell lymphoma", "dlbcl"], []),
    ("308121000", "C82.90", ["follicular lymphoma"], []),
    ("230736007", "G45.4", ["transient global amnesia"], []),
    ("40956001", "G61.0", ["guillain-barre syndrome", "guillain barre syndrome", "guillain-barré syndrome"], []),
    ("128209004", "G61.81", ["chronic inflammatory demyelinating polyneuropathy", "cidp"], []),
    ("15802004", "G24.9", ["dystonia"], []),
    ("568005", "F95.9", ["tic disorder"], []),
    ("52702003", "G93.32", ["chronic fatigue syndrome", "myalgic encephalomyelitis"], []),
    ("40425004", "F07.81", ["post-concussion syndrome", "postconcussion syndrome"], []),
    ("54404000", "M54.12", ["cervical radiculopathy"], []),
    ("128196005", "M54.16", ["lumbar radiculopathy"], []),
    ("85007004", "G57.10", ["meralgia paresthetica"], []),
    ("87486003", "R47.01", ["aphasia"], []),
    ("8011004", "R47.1", ["dysarthria"], []),
    ("50582007", "G81.90", ["hemiplegia"], []),
    ("429998004", "F01.50", ["vascular dementia"], []),
    ("230270009", "G31.09", ["frontotemporal dementia"], []),
    ("69322001", "F29", ["psychosis", "psychotic disorder"], []),
    ("6471006", "R45.851", ["suicidal ideation", "suicidal thoughts"], []),
    ("110359009", "F79", ["intellectual disability"], []),
    ("70691001", "F40.00", ["agoraphobia"], []),
    ("17155009", "F63.3", ["trichotillomania"], []),
    ("18941000", "F91.3", ["oppositional defiant disorder"], []),
    ("399114005", "M75.00", ["frozen shoulder", "adhesive capsulitis"], []),
    ("202855006", "M77.10", ["lateral epicondylitis", "tennis elbow"], []),
    ("203045001", "M72.0", ["dupuytren's contracture", "dupuytren contracture"], []),
    ("445008009", "M67.40", ["ganglion cyst"], []),
    ("122480009", "M20.10", ["hallux valgus", "bunion"], []),
    ("30085007", "G57.60", ["morton's neuroma", "morton neuroma"], []),
    ("240131006", "M62.82", ["rhabdomyolysis"], []),
    ("397758007", "M87.9", ["osteonecrosis", "avascular necrosis"], []),
    ("2089002", "M88.9", ["paget's disease of bone"], []),
    ("274152003", "M43.10", ["spondylolisthesis"], []),
    ("396230008", "M33.90", ["dermatomyositis"], []),
    ("31384009", "M33.20", ["polymyositis"], []),
    ("195353004", "M31.30", ["granulomatosis with polyangiitis", "wegener's granulomatosis"], []),
    ("82119001", "E06.9", ["thyroiditis"], []),
    ("74728003", "E23.0", ["hypopituitarism"], []),
    ("4754008", "N62", ["gynecomastia", "gynaecomastia"], []),
    ("399939002", "L68.0", ["hirsutism"], []),
    ("763325000", "E88.819", ["insulin resistance"], []),
    ("80394007", "R73.9", ["hyperglycemia", "hyperglycaemia"], []),
    ("236407003", "N02.8", ["iga nephropathy"], []),
    ("38481006", "I12.9", ["hypertensive nephropathy", "hypertensive kidney disease"], []),
    ("31822004", "N34.2", ["urethritis"], []),
    ("31070006", "N45.1", ["epididymitis"], []),
    ("2904007", "N46.9", ["male infertility"], []),
    ("73391008", "N87.9", ["cervical dysplasia"], []),
    ("76742009", "N95.0", ["postmenopausal bleeding"], []),
    ("82639001", "N94.3", ["premenstrual syndrome"], []),
    ("27431007", "N60.19", ["fibrocystic breast disease", "fibrocystic breasts"], []),
    ("33839006", "A60.00", ["genital herpes"], []),
    ("52486002", "M72.6", ["necrotizing fasciitis"], []),
    ("5758002", "R78.81", ["bacteremia", "bacteraemia"], []),
    ("7180009", "G03.9", ["meningitis"], []),
    ("45170000", "G04.90", ["encephalitis"], []),
    ("12962009", "B39.9", ["histoplasmosis"], []),
    ("60826002", "B38.9", ["coccidioidomycosis", "valley fever"], []),
    ("65553006", "B44.9", ["aspergillosis"], []),
    ("187192000", "B58.9", ["toxoplasmosis"], []),
    ("58265007", "A07.1", ["giardiasis"], []),
    ("30242009", "A38.9", ["scarlet fever"], []),
    ("186772009", "A77.0", ["rocky mountain spotted fever"], []),
    ("76902006", "A35", ["tetanus"], []),
    ("38362002", "A90", ["dengue fever", "dengue"], []),
    ("63650001", "A00.9", ["cholera"], []),
    ("4834000", "A01.00", ["typhoid fever"], []),
    ("35363006", "R10.83", ["infantile colic"], ["colic"]),
    ("8004003", "K00.7", ["teething"], []),
    ("91487003", "L22", ["diaper rash", "diaper dermatitis"], []),
    ("248290002", "R62.50", ["developmental delay"], []),
    ("8009008", "N39.44", ["nocturnal enuresis", "bedwetting"], []),
    ("302690004", "F98.1", ["encopresis"], []),
    ("400179000", "E30.1", ["precocious puberty"], []),
    ("416010008", "Q54.9", ["hypospadias"], []),
    ("204878001", "Q53.9", ["undescended testicle", "undescended testis", "cryptorchidism"], []),
    ("13213009", "Q24.9", ["congenital heart disease"], []),
    ("30288003", "Q21.0", ["ventricular septal defect"], ["vsd"]),
    ("70142008", "Q21.10", ["atrial septal defect"], ["asd"]),
    ("204317008", "Q21.12", ["patent foramen ovale"], ["pfo"]),
    ("72352009", "Q23.81", ["bicuspid aortic valve"], []),
    ("7305005", "Q25.1", ["coarctation of the aorta", "coarctation of aorta"], []),
    ("86299006", "Q21.3", ["tetralogy of fallot"], []),
    ("38804009", "Q96.9", ["turner syndrome", "turner's syndrome"], []),
    ("22053006", "Q98.4", ["klinefelter syndrome"], []),
    ("613003", "Q99.2", ["fragile x syndrome"], []),
    ("398114001", "Q79.60", ["ehlers-danlos syndrome", "ehlers danlos syndrome"], []),
    ("92824003", "Q85.01", ["neurofibromatosis type 1", "nf1"], []),
    ("73297009", "G71.00", ["muscular dystrophy"], []),
    ("91937001", "Z91.013", ["shellfish allergy", "seafood allergy"], []),
    ("43724002", "R68.83", ["chills"], []),
    ("367391008", "R53.81", ["malaise"], []),
    ("422400008", "R11.10", ["vomiting"], []),
    ("16331000", "R12", ["heartburn"], []),
    ("116289008", "R14.0", ["bloating", "abdominal bloating"], []),
    ("18165001", "R17", ["jaundice"], []),
    ("2901004", "K92.1", ["melena", "melaena", "black stools"], []),
    ("12063002", "K62.5", ["rectal bleeding", "bright red blood per rectum"], ["brbpr"]),
    ("8765009", "K92.0", ["hematemesis", "haematemesis"], []),
    ("49650001", "R30.0", ["dysuria", "painful urination"], []),
    ("91019004", "R20.2", ["paresthesia", "paraesthesia", "tingling"], []),
    ("26079004", "R25.1", ["tremor"], []),
    ("398064005", "N31.9", ["neurogenic bladder"], []),
    ("26614003", "N43.3", ["hydrocele"], []),
    ("27503000", "E80.4", ["gilbert syndrome", "gilbert's syndrome"], []),
    ("40070004", "B08.1", ["molluscum contagiosum"], []),
    ("89105000", "L85.3", ["xerosis", "dry skin"], []),
    ("1332435005", "J33.9", ["nasal polyps", "nasal polyp"], []),
    ("301354004", "H92.09", ["otalgia", "ear pain"], []),
    ("4557003", "I20.0", ["unstable angina"], []),
    ("74883004", "I71.20", ["thoracic aortic aneurysm"], []),
    ("62403005", "D55.0", ["g6pd deficiency", "glucose-6-phosphate dehydrogenase deficiency"], []),
    ("73397007", "D75.829", ["heparin-induced thrombocytopenia", "heparin induced thrombocytopenia"], []),
    ("386789004", "D72.10", ["eosinophilia"], []),
    ("80515008", "R16.0", ["hepatomegaly"], []),
    ("190818004", "C88.00", ["waldenstrom macroglobulinemia", "waldenström macroglobulinemia"], []),
    ("1260050006", "C22.1", ["cholangiocarcinoma", "intrahepatic cholangiocarcinoma"], []),
    ("94225005", "C79.31", ["brain metastases", "brain metastasis"], []),
    ("94222008", "C79.51", ["bone metastases", "bone metastasis"], []),
    ("94381002", "C78.7", ["liver metastases", "liver metastasis", "hepatic metastases"], []),
    ("94391008", "C78.00", ["lung metastases", "lung metastasis", "pulmonary metastases"], []),
    ("71760005", "M54.81", ["occipital neuralgia"], []),
    ("6077001", "M21.379", ["foot drop"], []),
    ("1539003", "M65.30", ["trigger finger"], []),
    ("1290249005", "M71.20", ["baker's cyst", "popliteal cyst"], []),
    ("53226007", "M21.40", ["flat feet", "flat foot", "pes planus"], []),
    ("64109004", "M94.0", ["costochondritis"], []),
    ("68962001", "M79.10", ["myalgia", "muscle pain"], []),
    ("396234004", "M00.9", ["septic arthritis"], []),
    ("398049005", "M35.1", ["mixed connective tissue disease"], ["mctd"]),
    ("19034001", "N25.81", ["secondary hyperparathyroidism"], []),
    ("55004003", "E22.2", ["siadh", "syndrome of inappropriate antidiuretic hormone"], []),
    ("190966007", "E66.2", ["obesity hypoventilation syndrome"], []),
    ("190634004", "E53.8", ["vitamin b12 deficiency", "b12 deficiency"], []),
    ("784314006", "N80.03", ["adenomyosis"], []),
    ("198130006", "N73.9", ["pelvic inflammatory disease"], []),
    ("48194001", "O13.9", ["gestational hypertension", "pregnancy-induced hypertension"], []),
    ("89164003", "N63.0", ["breast lump", "breast mass"], []),
    ("266579006", "N61.0", ["mastitis"], []),
    ("56335008", "A59.9", ["trichomoniasis"], []),
    ("266108008", "B08.4", ["hand foot and mouth disease", "hand, foot and mouth disease"], []),
    ("79974007", "A28.1", ["cat scratch disease", "cat-scratch disease"], []),
    ("237836003", "R62.52", ["short stature"], []),
    ("19346006", "Q87.40", ["marfan syndrome", "marfan's syndrome"], []),
    ("1003755004", "Z91.040", ["latex allergy"], []),
    ("13791008", "R53.1", ["weakness", "generalized weakness"], []),
    ("162116003", "R35.0", ["urinary frequency"], []),
    ("111516008", "H53.8", ["blurred vision", "blurry vision"], []),
    # --- Expansion round 5 ---
    ("396152005", "R97.20", ["elevated psa", "raised psa"], []),
    ("390951007", "R73.01", ["impaired fasting glucose"], []),
    ("64715009", "I11.9", ["hypertensive heart disease"], []),
    ("31992008", "I15.9", ["secondary hypertension"], []),
    ("81817003", "I70.0", ["aortic atherosclerosis", "atherosclerosis of aorta"], []),
    ("445512009", "I25.84", ["coronary artery calcification", "coronary calcification"], []),
    ("35486000", "I62.00", ["subdural hematoma", "subdural hemorrhage"], []),
    ("387800004", "M47.812", ["cervical spondylosis"], []),
    ("239880009", "M47.816", ["lumbar spondylosis"], []),
    ("431855005", "N18.1", ["ckd stage 1", "chronic kidney disease stage 1"], ["ckd 1"]),
    ("431856006", "N18.2", ["ckd stage 2", "chronic kidney disease stage 2"], ["ckd 2"]),
    ("700378005", "N18.31", ["ckd stage 3a", "chronic kidney disease stage 3a"], ["ckd 3a"]),
    ("700379002", "N18.32", ["ckd stage 3b", "chronic kidney disease stage 3b"], ["ckd 3b"]),
    ("1521000119100", "E11.621", ["diabetic foot ulcer"], []),
    ("237527007", "E89.0", ["postablative hypothyroidism", "postsurgical hypothyroidism"], []),
    ("54823002", "E03.8", ["subclinical hypothyroidism"], []),
    ("26389007", "E05.20", ["toxic multinodular goiter"], []),
    ("399357009", "E51.9", ["thiamine deficiency"], []),
    ("27405005", "G47.31", ["central sleep apnea"], []),
    ("86094006", "J31.0", ["chronic rhinitis", "nonallergic rhinitis"], []),
    ("8229003", "J30.0", ["vasomotor rhinitis"], []),
    ("15033003", "J36", ["peritonsillar abscess"], []),
    ("80967001", "K02.9", ["dental caries", "tooth decay"], []),
    ("5689008", "K05.30", ["periodontitis", "periodontal disease"], []),
    ("426965005", "K12.0", ["aphthous ulcer", "canker sore"], []),
    ("57773001", "K62.3", ["rectal prolapse"], []),
    ("72042002", "R15.9", ["fecal incontinence", "faecal incontinence", "bowel incontinence"], []),
    ("26629001", "K91.2", ["short bowel syndrome"], []),
    ("18773000", "R11.15", ["cyclic vomiting syndrome", "cyclical vomiting syndrome"], []),
    ("16932000", "R11.2", ["nausea and vomiting"], []),
    ("63305008", "K22.2", ["esophageal stricture", "oesophageal stricture"], []),
    ("235875008", "K70.10", ["alcoholic hepatitis"], []),
    ("59927004", "K72.90", ["liver failure", "hepatic failure"], []),
    ("31258000", "K86.2", ["pancreatic cyst"], []),
    ("50063009", "K41.90", ["femoral hernia"], []),
    ("75570004", "J12.9", ["viral pneumonia"], []),
    ("53084003", "J15.9", ["bacterial pneumonia"], []),
    ("1119303003", "U09.9", ["long covid", "post covid syndrome", "post-covid condition"], []),
    ("302231008", "A02.9", ["salmonella infection", "salmonellosis"], []),
    ("36188001", "A03.9", ["shigellosis", "shigella infection"], []),
    ("44653001", "A46", ["erysipelas"], []),
    ("13600006", "L73.9", ["folliculitis"], []),
    ("266096002", "A49.02", ["mrsa infection", "methicillin resistant staphylococcus aureus infection"], []),
    ("266162007", "B80", ["pinworms", "pinworm infection", "enterobiasis"], []),
    ("41497008", "R56.00", ["febrile seizure", "febrile seizures", "febrile convulsion"], []),
    ("48644003", "Q40.0", ["pyloric stenosis"], []),
    ("70070008", "M43.6", ["torticollis", "wry neck"], []),
    ("21850008", "Q67.3", ["plagiocephaly"], []),
    ("87132004", "F11.23", ["opioid withdrawal"], []),
    ("231473004", "F13.20", ["benzodiazepine dependence", "benzodiazepine use disorder"], []),
    ("83482000", "F45.22", ["body dysmorphic disorder"], []),
    ("26665006", "F60.2", ["antisocial personality disorder"], []),
    ("80711002", "F60.81", ["narcissistic personality disorder"], []),
    ("48500005", "F22", ["delusional disorder"], []),
    ("18973006", "N81.4", ["uterine prolapse", "uterovaginal prolapse"], []),
    ("252005008", "N81.10", ["cystocele"], []),
    ("237072009", "N85.00", ["endometrial hyperplasia"], []),
    ("71315007", "N94.10", ["dyspareunia"], []),
    ("52441000", "N95.2", ["atrophic vaginitis", "vaginal atrophy"], []),
    ("1335005", "N48.6", ["peyronie's disease", "peyronie disease"], []),
    ("49263001", "N43.40", ["spermatocele"], []),
    ("449826002", "N47.1", ["phimosis"], []),
    ("44001008", "F52.4", ["premature ejaculation"], []),
    ("1482004", "H00.19", ["chalazion"], []),
    ("15013002", "H43.399", ["eye floaters", "vitreous floaters"], []),
    ("6962006", "H35.039", ["hypertensive retinopathy"], []),
    ("77971008", "H20.9", ["iritis", "iridocyclitis", "anterior uveitis"], []),
    ("367469000", "H53.50", ["color blindness", "colour blindness"], []),
    ("82649003", "H52.209", ["astigmatism"], []),
    ("38101003", "H52.00", ["hyperopia", "farsightedness", "hypermetropia"], []),
    ("11934000", "H02.409", ["ptosis of eyelid", "eyelid ptosis"], []),
    ("4210003", "H40.059", ["ocular hypertension"], []),
    ("60442001", "H72.90", ["tympanic membrane perforation", "perforated eardrum"], []),
    ("23919004", "H83.09", ["labyrinthitis"], []),
    ("46689006", "J35.1", ["tonsillar hypertrophy", "enlarged tonsils"], []),
    ("111591002", "J35.2", ["adenoid hypertrophy", "enlarged adenoids"], []),
    ("42982001", "K11.20", ["sialadenitis", "sialoadenitis"], []),
    ("238575004", "L23.9", ["allergic contact dermatitis"], []),
    ("110979008", "L24.9", ["irritant contact dermatitis"], []),
    ("400096001", "D22.9", ["melanocytic nevus", "nevus"], ["mole"]),
    ("419893006", "L72.0", ["epidermoid cyst", "epidermal cyst"], []),
    ("58759008", "L30.4", ["intertrigo"], []),
    ("77252004", "L42", ["pityriasis rosea"], []),
    ("32861005", "L52", ["erythema nodosum"], []),
    ("77090002", "L12.0", ["bullous pemphigoid"], []),
    ("65172003", "L10.9", ["pemphigus"], []),
    ("63501000", "L28.1", ["prurigo nodularis"], []),
    ("11654001", "M76.60", ["achilles tendinitis", "achilles tendonitis", "achilles tendinopathy"], []),
    ("53286005", "M77.00", ["medial epicondylitis", "golfer's elbow"], []),
    ("47374004", "G57.50", ["tarsal tunnel syndrome"], []),
    ("122481008", "M20.40", ["hammer toe", "hammertoe"], []),
    ("10085004", "M77.40", ["metatarsalgia"], []),
    ("129179000", "G57.00", ["piriformis syndrome"], []),
    ("85551004", "M35.7", ["hypermobility syndrome", "joint hypermobility"], []),
    ("4598005", "M83.9", ["osteomalacia"], []),
    ("66978005", "E83.41", ["hypermagnesemia"], []),
    ("234347009", "D63.8", ["anemia of chronic disease", "anaemia of chronic disease"], []),
    ("306058006", "D61.9", ["aplastic anemia"], []),
    ("61261009", "D59.9", ["hemolytic anemia", "haemolytic anaemia"], []),
    ("127034005", "D61.818", ["pancytopenia"], []),
    ("363392002", "C10.9", ["oropharyngeal cancer"], []),
    ("420120006", "C49.A0", ["gastrointestinal stromal tumor"], ["gist"]),
    ("363490009", "C21.0", ["anal cancer"], []),
    ("189878003", "C41.9", ["osteosarcoma"], []),
    ("189758001", "D03.9", ["melanoma in situ"], []),
    ("126949007", "D33.3", ["acoustic neuroma", "vestibular schwannoma"], []),
    ("697930002", "R03.0", ["white coat hypertension"], []),
    ("41888000", "M26.609", ["tmj disorder", "temporomandibular joint disorder"], ["tmj"]),
    ("80193009", "K91.1", ["dumping syndrome", "postgastric surgery syndrome"], []),
    ("442685003", "K75.81", ["nash", "nonalcoholic steatohepatitis", "non-alcoholic steatohepatitis", "metabolic dysfunction-associated steatohepatitis"], ["mash"]),
    ("240542006", "A63.0", ["genital warts", "condyloma acuminatum"], []),
    ("75053002", "M30.3", ["kawasaki disease"], []),
    ("67787004", "Q38.1", ["tongue tie", "ankyloglossia"], []),
    ("191480000", "F10.239", ["alcohol withdrawal"], []),
    ("18085000", "F63.0", ["gambling disorder", "pathological gambling"], []),
    ("1489008", "H00.019", ["stye", "hordeolum"], []),
    ("56713002", "H69.90", ["eustachian tube dysfunction"], []),
    ("44169009", "R43.0", ["anosmia", "loss of smell"], []),
    ("87872006", "L64.9", ["androgenetic alopecia", "male pattern baldness"], []),
    ("423849004", "M76.30", ["iliotibial band syndrome", "it band syndrome"], []),
    ("7674000", "M70.60", ["trochanteric bursitis", "greater trochanteric pain syndrome"], []),
    ("230631009", "G56.20", ["cubital tunnel syndrome"], []),
    ("55146009", "M46.1", ["sacroiliitis"], []),
    ("413343005", "N39.46", ["mixed urinary incontinence", "mixed incontinence"], []),
    ("31054009", "N20.1", ["ureteral stone", "ureteral calculus", "ureterolithiasis"], []),
    ("363429002", "C32.9", ["laryngeal cancer", "cancer of larynx"], []),
    ("109889007", "D05.10", ["ductal carcinoma in situ"], ["dcis"]),
    ("302820008", "D32.9", ["meningioma"], []),
    ("76618002", "N35.919", ["urethral stricture"], []),
]

# --------------------------------------------------------------------------- #
# Medications: ingredient key -> (scanned synonyms incl. brands, exact-only synonyms)
# Single-ingredient drugs (combinations are in COMBINATIONS); brands are verified to map to the ingredient.
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
    # --- Expansion round 2 ---
    # Women's health
    "levonorgestrel": ([], []),
    "clomiphene": ([], []),
    "misoprostol": (["cytotec"], []),
    "oxytocin": (["pitocin"], []),
    # Psychiatry and sleep
    "amphetamine": ([], []),
    "paliperidone": (["invega"], []),
    "brexpiprazole": (["rexulti"], []),
    "cariprazine": (["vraylar"], []),
    "valproic acid": ([], []),
    "perphenazine": ([], []),
    "chlorpromazine": ([], []),
    "disulfiram": (["antabuse"], []),
    "acamprosate": ([], []),
    "naloxone": (["narcan"], []),
    "modafinil": (["provigil"], []),
    "armodafinil": (["nuvigil"], []),
    "ramelteon": (["rozerem"], []),
    "suvorexant": (["belsomra"], []),
    # Infectious disease
    "ampicillin": ([], []),
    "cefazolin": ([], []),
    "meropenem": ([], []),
    "ertapenem": (["invanz"], []),
    "gentamicin": ([], []),
    "tobramycin": ([], []),
    "daptomycin": (["cubicin"], []),
    "rifampin": (["rifadin"], []),
    "isoniazid": ([], []),
    "ethambutol": ([], []),
    "pyrazinamide": ([], []),
    "voriconazole": (["vfend"], []),
    "posaconazole": (["noxafil"], []),
    "itraconazole": (["sporanox"], []),
    "tinidazole": ([], []),
    "valganciclovir": (["valcyte"], []),
    "baloxavir marboxil": (["xofluza"], []),
    "remdesivir": (["veklury"], []),
    "dolutegravir": (["tivicay"], []),
    "raltegravir": (["isentress"], []),
    # Hematology
    "fondaparinux": (["arixtra"], []),
    "hydroxyurea": (["hydrea"], []),
    "epoetin alfa": (["epogen", "procrit", "retacrit"], []),
    "filgrastim": (["neupogen"], []),
    # Neurology
    "ubrogepant": (["ubrelvy"], []),
    "rimegepant": (["nurtec"], []),
    "erenumab": (["aimovig"], []),
    "galcanezumab": (["emgality"], []),
    "levodopa": ([], []),
    "amantadine": ([], []),
    "benztropine": ([], []),
    "dimethyl fumarate": (["tecfidera"], []),
    "ocrelizumab": (["ocrevus"], []),
    "natalizumab": (["tysabri"], []),
    "fingolimod": (["gilenya"], []),
    "glatiramer": (["copaxone"], []),
    # Gastrointestinal
    "vedolizumab": (["entyvio"], []),
    "simethicone": (["gas-x"], []),
    # Respiratory biologics and lung disease
    "benralizumab": (["fasenra"], []),
    "mepolizumab": (["nucala"], []),
    "omalizumab": (["xolair"], []),
    "nintedanib": (["ofev"], []),
    "pirfenidone": (["esbriet"], []),
    # Dermatology
    "clobetasol": ([], []),
    "betamethasone": ([], []),
    "calcipotriene": ([], []),
    "adapalene": (["differin"], []),
    "benzoyl peroxide": ([], []),
    # Kidney
    "sevelamer": (["renvela"], []),
    "calcium acetate": ([], []),
    "patiromer": (["veltassa"], []),
    "sodium zirconium cyclosilicate": (["lokelma"], []),
    "finerenone": (["kerendia"], []),
    # Endocrine
    "glucagon": (["baqsimi"], []),
    "dronedarone": (["multaq"], []),
    # Oncology
    "imatinib": (["gleevec"], []),
    "capecitabine": (["xeloda"], []),
    "lenalidomide": (["revlimid"], []),
    "ibrutinib": (["imbruvica"], []),
    "palbociclib": (["ibrance"], []),
    "osimertinib": (["tagrisso"], []),
    "pembrolizumab": (["keytruda"], []),
    "nivolumab": (["opdivo"], []),
    "bevacizumab": (["avastin"], []),
    "trastuzumab": (["herceptin"], []),
    "rituximab": (["rituxan"], []),
    "enzalutamide": (["xtandi"], []),
    "abiraterone": (["zytiga"], []),
    "bicalutamide": (["casodex"], []),
    "leuprolide": (["lupron"], []),
    # Supplements and electrolytes
    "magnesium oxide": ([], []),
    "calcium carbonate": (["tums"], []),
    "sodium bicarbonate": ([], []),
    "thiamine": ([], []),
    "melatonin": ([], []),
    # --- Expansion round 3 ---
    # Cardiovascular
    "nateglinide": ([], []),
    "triamterene": (["dyrenium"], []),
    "amiloride": ([], []),
    "indapamide": ([], []),
    "captopril": ([], []),
    "fosinopril": ([], []),
    "perindopril": ([], []),
    "trandolapril": ([], []),
    "azilsartan": (["edarbi"], []),
    "nicardipine": (["cardene"], []),
    "isradipine": ([], []),
    "methyldopa": ([], []),
    "mexiletine": ([], []),
    "propafenone": (["rythmol"], []),
    "dipyridamole": ([], []),
    "pentoxifylline": ([], []),
    "vericiguat": (["verquvo"], []),
    "mavacamten": (["camzyos"], []),
    "bempedoic acid": (["nexletol"], []),
    "colesevelam": (["welchol"], []),
    "cholestyramine": ([], []),
    "inclisiran": (["leqvio"], []),
    "dalteparin": (["fragmin"], []),
    "argatroban": ([], []),
    "bivalirudin": (["angiomax"], []),
    "alteplase": (["activase"], []),
    "tranexamic acid": (["lysteda"], []),
    "phytonadione": (["mephyton"], []),
    "norepinephrine": (["levophed"], []),
    "phenylephrine": ([], []),
    "dobutamine": ([], []),
    "midodrine": ([], []),
    # Respiratory
    "dupilumab": (["dupixent"], []),
    "tezepelumab": (["tezspire"], []),
    "theophylline": ([], []),
    "zafirlukast": (["accolate"], []),
    "cromolyn": ([], []),
    "glycopyrrolate": ([], []),
    "dextromethorphan": ([], []),
    "pseudoephedrine": ([], []),
    "oxymetazoline": (["afrin"], []),
    "ciclesonide": (["alvesco"], []),
    "acetylcysteine": ([], []),
    # Gastrointestinal
    "prucalopride": (["motegrity"], []),
    "lubiprostone": (["amitiza"], []),
    "plecanatide": (["trulance"], []),
    "methylnaltrexone": (["relistor"], []),
    "naloxegol": (["movantik"], []),
    "hyoscyamine": (["levsin"], []),
    "meclizine": (["antivert"], []),
    "scopolamine": ([], []),
    "granisetron": ([], []),
    "aprepitant": (["emend"], []),
    "bismuth subsalicylate": ([], []),
    # Neurology
    "valbenazine": (["ingrezza"], []),
    "deutetrabenazine": (["austedo"], []),
    "tetrabenazine": ([], []),
    "carbidopa": (["lodosyn"], []),
    "rasagiline": (["azilect"], []),
    "selegiline": ([], []),
    "entacapone": (["comtan"], []),
    "rotigotine": (["neupro"], []),
    "dalfampridine": (["ampyra"], []),
    "teriflunomide": (["aubagio"], []),
    "brivaracetam": (["briviact"], []),
    "clobazam": (["onfi"], []),
    "eslicarbazepine": (["aptiom"], []),
    "cenobamate": (["xcopri"], []),
    "perampanel": (["fycompa"], []),
    "phenobarbital": ([], []),
    "primidone": (["mysoline"], []),
    "ethosuximide": (["zarontin"], []),
    "fremanezumab": (["ajovy"], []),
    "eptinezumab": (["vyepti"], []),
    "lasmiditan": (["reyvow"], []),
    "atogepant": (["qulipta"], []),
    "zolmitriptan": (["zomig"], []),
    "eletriptan": (["relpax"], []),
    "naratriptan": ([], []),
    # Psychiatry
    "fluvoxamine": ([], []),
    "clomipramine": (["anafranil"], []),
    "imipramine": ([], []),
    "desipramine": (["norpramin"], []),
    "phenelzine": (["nardil"], []),
    "levomilnacipran": (["fetzima"], []),
    "vilazodone": (["viibryd"], []),
    "esketamine": (["spravato"], []),
    "lumateperone": (["caplyta"], []),
    "asenapine": (["saphris"], []),
    "iloperidone": (["fanapt"], []),
    "loxapine": ([], []),
    "fluphenazine": ([], []),
    "chlordiazepoxide": ([], []),
    "oxazepam": ([], []),
    "zaleplon": ([], []),
    "doxylamine": ([], []),
    "dexmethylphenidate": (["focalin"], []),
    "viloxazine": (["qelbree"], []),
    # Pain and musculoskeletal
    "oxaprozin": (["daypro"], []),
    "indomethacin": (["indocin"], []),
    "ketoprofen": ([], []),
    "nabumetone": ([], []),
    "etodolac": ([], []),
    "piroxicam": (["feldene"], []),
    "sulindac": ([], []),
    "codeine": ([], []),
    "oxymorphone": ([], []),
    "butorphanol": ([], []),
    "carisoprodol": ([], ["soma"]),
    "metaxalone": (["skelaxin"], []),
    "orphenadrine": ([], []),
    "dantrolene": ([], []),
    "febuxostat": (["uloric"], []),
    "probenecid": ([], []),
    "pegloticase": (["krystexxa"], []),
    "teriparatide": (["forteo"], []),
    "abaloparatide": (["tymlos"], []),
    "romosozumab": (["evenity"], []),
    "zoledronic acid": (["reclast"], []),
    # Immunology
    "certolizumab pegol": (["cimzia"], []),
    "golimumab": (["simponi"], []),
    "abatacept": (["orencia"], []),
    "tocilizumab": (["actemra"], []),
    "sarilumab": (["kevzara"], []),
    "anakinra": (["kineret"], []),
    "baricitinib": (["olumiant"], []),
    "upadacitinib": (["rinvoq"], []),
    "ixekizumab": (["taltz"], []),
    "guselkumab": (["tremfya"], []),
    "risankizumab": (["skyrizi"], []),
    "mycophenolic acid": (["myfortic"], []),
    "sirolimus": (["rapamune"], []),
    "everolimus": (["afinitor", "zortress"], []),
    "belimumab": (["benlysta"], []),
    # Infection
    "cefpodoxime": ([], []),
    "cefadroxil": ([], []),
    "cefepime": ([], []),
    "ceftazidime": ([], []),
    "aztreonam": ([], []),
    "moxifloxacin": (["avelox"], []),
    "ofloxacin": ([], []),
    "fosfomycin": ([], []),
    "dapsone": ([], []),
    "rifabutin": ([], []),
    "efavirenz": ([], []),
    "tenofovir disoproxil": (["viread"], []),
    "emtricitabine": (["emtriva"], []),
    "lamivudine": (["epivir"], []),
    "abacavir": (["ziagen"], []),
    "zidovudine": (["retrovir"], []),
    "darunavir": (["prezista"], []),
    "rilpivirine": (["edurant"], []),
    "cabotegravir": (["vocabria"], []),
    "entecavir": (["baraclude"], []),
    "sofosbuvir": (["sovaldi"], []),
    "ribavirin": ([], []),
    "zanamivir": (["relenza"], []),
    "albendazole": (["albenza"], []),
    "mebendazole": ([], []),
    "praziquantel": ([], []),
    "atovaquone": (["mepron"], []),
    "chloroquine": ([], []),
    "primaquine": ([], []),
    "mefloquine": ([], []),
    "silver sulfadiazine": (["silvadene"], []),
    "bacitracin": ([], []),
    "neomycin": ([], []),
    # Endocrine
    "cabergoline": ([], []),
    "bromocriptine": (["cycloset", "parlodel"], []),
    "octreotide": (["sandostatin"], []),
    "fludrocortisone": ([], []),
    "megestrol": ([], []),
    "fulvestrant": (["faslodex"], []),
    "paricalcitol": (["zemplar"], []),
    "doxercalciferol": (["hectorol"], []),
    "ergocalciferol": (["drisdol"], []),
    "orlistat": (["xenical"], ["alli"]),
    "phentermine": ([], []),
    # Urology
    "alfuzosin": (["uroxatral"], []),
    "silodosin": (["rapaflo"], []),
    "trospium": ([], []),
    "darifenacin": (["enablex"], []),
    "fesoterodine": (["toviaz"], []),
    "vibegron": (["gemtesa"], []),
    "bethanechol": ([], []),
    "phenazopyridine": (["pyridium"], []),
    "vardenafil": ([], []),
    "avanafil": (["stendra"], []),
    # Dermatology
    "triamcinolone": ([], []),
    "desonide": ([], []),
    "fluocinonide": ([], []),
    "pimecrolimus": (["elidel"], []),
    "crisaborole": (["eucrisa"], []),
    # Oncology and hematology
    "cyclophosphamide": ([], []),
    "carboplatin": ([], []),
    "cisplatin": ([], []),
    "oxaliplatin": ([], []),
    "paclitaxel": ([], []),
    "docetaxel": ([], []),
    "doxorubicin": ([], []),
    "fluorouracil": ([], []),
    "gemcitabine": ([], []),
    "pemetrexed": (["alimta"], []),
    "etoposide": ([], []),
    "atezolizumab": (["tecentriq"], []),
    "durvalumab": (["imfinzi"], []),
    "ipilimumab": (["yervoy"], []),
    "daratumumab": (["darzalex"], []),
    "bortezomib": (["velcade"], []),
    "pomalidomide": (["pomalyst"], []),
    "venetoclax": (["venclexta"], []),
    "acalabrutinib": (["calquence"], []),
    "olaparib": (["lynparza"], []),
    "ribociclib": (["kisqali"], []),
    "abemaciclib": (["verzenio"], []),
    "erlotinib": (["tarceva"], []),
    "dasatinib": (["sprycel"], []),
    "nilotinib": (["tasigna"], []),
    "ruxolitinib": (["jakafi"], []),
    "pegfilgrastim": (["neulasta"], []),
    "darbepoetin alfa": (["aranesp"], []),
    "iron sucrose": (["venofer"], []),
    "ferumoxytol": (["feraheme"], []),
    # Anesthesia and acute care
    "midazolam": ([], []),
    "dexmedetomidine": (["precedex"], []),
    "ketamine": ([], []),
    "propofol": ([], []),
    "flumazenil": ([], []),
    "atropine": ([], []),
    # Supplements and electrolytes
    "potassium citrate": (["urocit-k"], []),
    "magnesium sulfate": ([], []),
    "pyridoxine": ([], []),
    "ascorbic acid": ([], []),
    "zinc sulfate": ([], []),
    # --- Expansion round 4 ---
    # Eye
    "latanoprost": (["xalatan"], []),
    "timolol": ([], []),
    "brimonidine": (["alphagan"], []),
    "dorzolamide": ([], []),
    "bimatoprost": (["lumigan"], []),
    "travoprost": (["travatan"], []),
    "olopatadine": (["pataday"], []),
    "ketotifen": ([], []),
    "lifitegrast": (["xiidra"], []),
    "pilocarpine": ([], []),
    "fluorometholone": ([], []),
    # Dermatology
    "hydroquinone": ([], []),
    "imiquimod": ([], []),
    "azelaic acid": ([], []),
    "tapinarof": (["vtama"], []),
    "chlorhexidine": (["peridex"], []),
    "miconazole": ([], []),
    "terconazole": ([], []),
    # Women's health
    "ulipristal": ([], []),
    "etonogestrel": (["nexplanon"], []),
    "ospemifene": (["osphena"], []),
    "elagolix": (["orilissa"], []),
    "relugolix": (["orgovyx"], []),
    "methylergonovine": (["methergine"], []),
    "terbutaline": ([], []),
    # Urology
    "pentosan polysulfate": (["elmiron"], []),
    # Neurology
    "riluzole": (["rilutek"], []),
    "edaravone": (["radicava"], []),
    "pyridostigmine": (["mestinon"], []),
    "onabotulinumtoxinA": ([], []),
    "ofatumumab": (["kesimpta"], []),
    "siponimod": (["mayzent"], []),
    "ozanimod": (["zeposia"], []),
    "diroximel fumarate": (["vumerity"], []),
    "cladribine": (["mavenclad"], []),
    "alemtuzumab": (["lemtrada"], []),
    "lecanemab": (["leqembi"], []),
    "donanemab": (["kisunla"], []),
    "safinamide": (["xadago"], []),
    "istradefylline": (["nourianz"], []),
    "pimavanserin": (["nuplazid"], []),
    "trihexyphenidyl": ([], []),
    "dihydroergotamine": ([], []),
    "acetazolamide": ([], []),
    # Psychiatry
    "zuranolone": (["zurzuvae"], []),
    # Infection
    "cefprozil": ([], []),
    "cefixime": (["suprax"], []),
    "cefoxitin": ([], []),
    "ceftaroline fosamil": (["teflaro"], []),
    "dalbavancin": (["dalvance"], []),
    "tedizolid": (["sivextro"], []),
    "amikacin": ([], []),
    "nitazoxanide": (["alinia"], []),
    "fidaxomicin": (["dificid"], []),
    "secnidazole": ([], []),
    "micafungin": (["mycamine"], []),
    "caspofungin": (["cancidas"], []),
    "amphotericin B": ([], []),
    "letermovir": (["prevymis"], []),
    "tecovirimat": (["tpoxx"], []),
    "ritonavir": (["norvir"], []),
    "doravirine": (["pifeltro"], []),
    "lenacapavir": (["sunlenca"], []),
    "tenofovir alafenamide": (["vemlidy"], []),
    # Cardiopulmonary
    "sotagliflozin": (["inpefa"], []),
    "bosentan": (["tracleer"], []),
    "ambrisentan": (["letairis"], []),
    "macitentan": (["opsumit"], []),
    "riociguat": (["adempas"], []),
    "treprostinil": (["tyvaso", "remodulin"], []),
    "selexipag": (["uptravi"], []),
    "sotatercept": (["winrevair"], []),
    # Endocrine
    "teprotumumab": (["tepezza"], []),
    "lanreotide": (["somatuline"], []),
    "pegvisomant": (["somavert"], []),
    "somatropin": (["genotropin", "norditropin"], []),
    "calcifediol": (["rayaldee"], []),
    "pramlintide": (["symlin"], []),
    "insulin glulisine": (["apidra"], []),
    # Gastrointestinal
    "tenapanor": (["ibsrela"], []),
    "eluxadoline": (["viberzi"], []),
    "alosetron": ([], []),
    "mirikizumab": (["omvoh"], []),
    "etrasimod": (["velsipity"], []),
    "obeticholic acid": (["ocaliva"], []),
    "resmetirom": (["rezdiffra"], []),
    "vonoprazan": (["voquezna"], []),
    # Hematology
    "emicizumab": (["hemlibra"], []),
    "luspatercept": (["reblozyl"], []),
    "crizanlizumab": (["adakveo"], []),
    "eltrombopag": (["promacta"], []),
    "romiplostim": (["nplate"], []),
    "avatrombopag": (["doptelet"], []),
    "deferasirox": (["jadenu", "exjade"], []),
    "ferrous gluconate": ([], []),
    "anagrelide": ([], []),
    # Oncology
    "sacituzumab govitecan": ([], []),
    "cetuximab": (["erbitux"], []),
    "panitumumab": (["vectibix"], []),
    "ramucirumab": (["cyramza"], []),
    "alectinib": (["alecensa"], []),
    "crizotinib": (["xalkori"], []),
    "lorlatinib": (["lorbrena"], []),
    "sotorasib": (["lumakras"], []),
    "lapatinib": (["tykerb"], []),
    "tucatinib": (["tukysa"], []),
    "cabozantinib": (["cabometyx"], []),
    "lenvatinib": (["lenvima"], []),
    "sorafenib": (["nexavar"], []),
    "sunitinib": (["sutent"], []),
    "pazopanib": (["votrient"], []),
    "axitinib": (["inlyta"], []),
    "regorafenib": (["stivarga"], []),
    "temozolomide": (["temodar"], []),
    "irinotecan": ([], []),
    "vincristine": ([], []),
    "vinorelbine": ([], []),
    "cytarabine": ([], []),
    "azacitidine": (["vidaza"], []),
    "decitabine": ([], []),
    "bendamustine": ([], []),
    "carfilzomib": (["kyprolis"], []),
    "zanubrutinib": (["brukinsa"], []),
    "obinutuzumab": (["gazyva"], []),
    "brentuximab vedotin": (["adcetris"], []),
    "degarelix": (["firmagon"], []),
    "apalutamide": (["erleada"], []),
    "darolutamide": (["nubeqa"], []),
    "goserelin": (["zoladex"], []),
    # --- Expansion round 5 ---
    # Symptom relief and supplements
    "trimethobenzamide": (["tigan"], []),
    "dronabinol": (["marinol"], []),
    "nabilone": (["cesamet"], []),
    "magnesium citrate": ([], []),
    "methylcellulose": (["citrucel"], []),
    "calcium citrate": ([], []),
    "biotin": ([], []),
    "leucovorin": ([], []),
    "mercaptopurine": (["purixan"], []),
    "methenamine": (["hiprex"], []),
    "malathion": (["ovide"], []),
    "spinosad": (["natroba"], []),
    "desoximetasone": (["topicort"], []),
    "halobetasol": ([], []),
    "fluocinolone": ([], []),
    "griseofulvin": ([], []),
    "econazole": ([], []),
    "ciclopirox": ([], []),
    "efinaconazole": (["jublia"], []),
    "penciclovir": (["denavir"], []),
    "docosanol": (["abreva"], []),
    "brompheniramine": ([], []),
    "chlorpheniramine": ([], []),
    "cyproheptadine": ([], []),
    "desloratadine": (["clarinex"], []),
    "nicotine": (["nicorette", "nicotrol"], []),
    "colestipol": (["colestid"], []),
    # Cardiology
    "tafamidis": (["vyndamax"], []),
    "vutrisiran": (["amvuttra"], []),
    "disopyramide": (["norpace"], []),
    "quinidine": ([], []),
    "procainamide": ([], []),
    "adenosine": (["adenocard"], []),
    "esmolol": (["brevibloc"], []),
    "mannitol": ([], []),
    "tolvaptan": (["jynarque", "samsca"], []),
    "droxidopa": (["northera"], []),
    # Sleep and pain
    "solriamfetol": (["sunosi"], []),
    "pitolisant": (["wakix"], []),
    "sodium oxybate": ([], []),
    "lemborexant": (["dayvigo"], []),
    "daridorexant": (["quviviq"], []),
    "milnacipran": (["savella"], []),
    "capsaicin": ([], []),
}
# --------------------------------------------------------------------------- #
# Vaccines: name -> (CVX code, phrases). Matched only on lines that talk about vaccines
# (an Immunizations section, or words like "vaccine", "booster", "shot"), so generic words
# such as "polio" or "rsv" are safe here. Order matters: the first vaccine whose phrase
# appears claims that span, so put specific products before generic names.
# "Unspecified formulation" codes are used because notes rarely name the exact product.
# --------------------------------------------------------------------------- #
VACCINES = [
    ("mmrv", "94", ["mmrv", "proquad"]),
    ("mmr", "03", ["mmr", "measles mumps rubella", "measles mumps and rubella", "m-m-r ii", "priorix"]),
    ("varicella", "21", ["varicella", "varivax", "chickenpox"]),
    ("hepatitis a-hepatitis b", "104", ["twinrix"]),
    ("hepatitis a", "85", ["hep a", "hepatitis a", "havrix", "vaqta"]),
    ("hpv", "165", ["hpv", "gardasil", "gardasil 9", "human papillomavirus"]),
    ("menabcwy penbraya", "316", ["penbraya"]),
    ("menabcwy penmenvy", "328", ["penmenvy"]),
    ("meningococcal b", "164", ["menb", "bexsero", "trumenba", "meningococcal b"]),
    ("meningococcal acwy", "108", ["menacwy", "menveo", "menquadfi", "menactra", "meningococcal"]),
    ("dtap-hepb-ipv", "110", ["pediarix"]),
    ("dtap-ipv-hib-hepb", "146", ["vaxelis"]),
    ("dtap-ipv-hib", "120", ["pentacel", "dtap-ipv-hib", "dtap-ipv/hib"]),
    ("dtap-ipv", "130", ["kinrix", "quadracel", "dtap-ipv"]),
    ("dtap", "107", ["dtap", "daptacel", "infanrix"]),
    ("td", "139", ["td", "tenivac", "tetanus diphtheria"]),
    ("ipv", "10", ["ipv", "ipol", "polio"]),
    ("hib", "17", ["hib", "acthib", "pedvaxhib", "hiberix", "haemophilus influenzae type b"]),
    ("rotavirus", "122", ["rotavirus", "rotateq", "rotarix"]),
    ("rsv monoclonal antibody", "315", ["nirsevimab", "beyfortus"]),
    ("rsv monoclonal antibody clesrovimab", "332", ["clesrovimab", "enflonsia"]),
    ("rsv", "314", ["rsv", "arexvy", "abrysvo", "mresvia", "respiratory syncytial virus"]),
    ("mpox", "325", ["mpox", "monkeypox", "jynneos", "smallpox"]),
    ("typhoid", "91", ["typhoid", "vivotif", "typhim vi"]),
    ("yellow fever", "37", ["yellow fever", "yf-vax"]),
    ("rabies", "90", ["rabies", "imovax", "rabavert"]),
    ("japanese encephalitis", "134", ["japanese encephalitis", "ixiaro"]),
    ("bcg", "19", ["bcg"]),
    ("cholera", "26", ["cholera", "vaxchora"]),
    ("anthrax", "319", ["anthrax", "biothrax", "cyfendus"]),
    ("dengue", "330", ["dengue", "dengvaxia"]),
    ("chikungunya live", "317", ["ixchiq"]),
    ("chikungunya vlp", "329", ["vimkunya"]),
    ("tick-borne encephalitis", "222", ["tick-borne encephalitis", "tick borne encephalitis", "ticovac"]),
]

# --------------------------------------------------------------------------- #
# Combination products: RxNorm multiple-ingredient (MIN) name -> (scanned synonyms incl.
# brands, exact-only synonyms). Keys are RxNorm's own MIN names (components in
# alphabetical order); synonyms carry the order clinicians write. Every brand must map to
# exactly the MIN's ingredients, and the drug class joins the components' classes.
# A combination's synonyms are longer than its components' names, so "lisinopril-HCTZ"
# matches the combination rather than lisinopril alone.
# --------------------------------------------------------------------------- #
COMBINATIONS = {
    # Antibiotics and antivirals
    "amoxicillin / clavulanate": (["amoxicillin clavulanate", "amoxicillin-clavulanate", "amox-clav", "augmentin"], []),
    "sulfamethoxazole / trimethoprim": (["sulfamethoxazole-trimethoprim", "trimethoprim-sulfamethoxazole", "tmp-smx", "smx-tmp", "bactrim", "septra"], ["tmp smx", "smx tmp"]),
    "piperacillin / tazobactam": (["piperacillin-tazobactam", "pip-tazo", "zosyn"], []),
    "ampicillin / sulbactam": (["ampicillin-sulbactam", "unasyn"], []),
    "avibactam / ceftazidime": (["ceftazidime-avibactam", "avycaz"], []),
    "emtricitabine / tenofovir disoproxil": (["emtricitabine-tenofovir", "truvada"], []),
    "bictegravir / emtricitabine / tenofovir alafenamide": (["biktarvy"], []),
    "abacavir / dolutegravir / lamivudine": (["triumeq"], []),
    "sofosbuvir / velpatasvir": (["sofosbuvir-velpatasvir", "epclusa"], []),
    "glecaprevir / pibrentasvir": (["glecaprevir-pibrentasvir", "mavyret"], []),
    "ledipasvir / sofosbuvir": (["ledipasvir-sofosbuvir", "harvoni"], []),
    # Cardiovascular
    "sacubitril / valsartan": (["sacubitril-valsartan", "entresto"], []),
    "hydrochlorothiazide / lisinopril": (["lisinopril-hydrochlorothiazide", "lisinopril-hctz", "zestoretic"], []),
    "hydrochlorothiazide / losartan": (["losartan-hydrochlorothiazide", "losartan-hctz", "hyzaar"], []),
    "hydrochlorothiazide / valsartan": (["valsartan-hydrochlorothiazide", "valsartan-hctz"], []),
    "hydrochlorothiazide / olmesartan": (["olmesartan-hydrochlorothiazide", "olmesartan-hctz"], []),
    "hydrochlorothiazide / triamterene": (["triamterene-hydrochlorothiazide", "triamterene-hctz", "maxzide", "dyazide"], []),
    "bisoprolol / hydrochlorothiazide": (["bisoprolol-hydrochlorothiazide", "bisoprolol-hctz"], []),
    "amlodipine / benazepril": (["amlodipine-benazepril", "lotrel"], []),
    "amlodipine / valsartan": (["amlodipine-valsartan", "exforge"], []),
    "amlodipine / olmesartan": (["amlodipine-olmesartan", "azor"], []),
    "amlodipine / atorvastatin": (["amlodipine-atorvastatin", "caduet"], []),
    "ezetimibe / simvastatin": (["ezetimibe-simvastatin", "vytorin"], []),
    # Diabetes
    "metformin / sitagliptin": (["sitagliptin-metformin", "janumet"], []),
    "empagliflozin / metformin": (["empagliflozin-metformin", "synjardy"], []),
    "dapagliflozin / metformin": (["dapagliflozin-metformin", "xigduo"], []),
    "empagliflozin / linagliptin": (["empagliflozin-linagliptin", "glyxambi"], []),
    "glyburide / metformin": (["glyburide-metformin", "glucovance"], []),
    "insulin degludec / liraglutide": (["xultophy"], []),
    "insulin glargine / lixisenatide": (["soliqua"], []),
    # Respiratory
    "fluticasone / salmeterol": (["fluticasone-salmeterol", "advair", "wixela"], []),
    "budesonide / formoterol": (["budesonide-formoterol", "symbicort", "breyna"], []),
    "fluticasone / vilanterol": (["fluticasone-vilanterol", "breo", "breo ellipta"], []),
    "umeclidinium / vilanterol": (["umeclidinium-vilanterol", "anoro", "anoro ellipta"], []),
    "fluticasone / umeclidinium / vilanterol": (["fluticasone-umeclidinium-vilanterol", "trelegy", "trelegy ellipta"], []),
    "formoterol / mometasone": (["mometasone-formoterol", "dulera"], []),
    "albuterol / ipratropium": (["ipratropium-albuterol", "albuterol-ipratropium", "duoneb", "combivent"], []),
    "azelastine / fluticasone": (["azelastine-fluticasone", "dymista"], []),
    # Pain, neurology and psychiatry
    "acetaminophen / hydrocodone": (["hydrocodone-acetaminophen", "hydrocodone/apap", "norco", "vicodin", "lortab"], []),
    "acetaminophen / oxycodone": (["oxycodone-acetaminophen", "oxycodone/apap", "percocet", "endocet"], []),
    "acetaminophen / codeine": (["acetaminophen-codeine", "tylenol with codeine", "tylenol #3"], []),
    "buprenorphine / naloxone": (["buprenorphine-naloxone", "suboxone", "zubsolv"], []),
    "carbidopa / levodopa": (["carbidopa-levodopa", "sinemet", "rytary"], []),
    "bupropion / naltrexone": (["naltrexone-bupropion", "contrave"], []),
    "olanzapine / samidorphan": (["lybalvi"], []),
    "dextromethorphan / quinidine": (["nuedexta"], []),
    # Women's health and urology
    "drospirenone / ethinyl estradiol": (["drospirenone-ethinyl estradiol", "yaz", "yasmin"], []),
    "ethinyl estradiol / norgestimate": (["norgestimate-ethinyl estradiol", "sprintec", "tri-sprintec", "ortho tri-cyclen"], []),
    "ethinyl estradiol / levonorgestrel": (["levonorgestrel-ethinyl estradiol", "seasonique", "aviane"], []),
    "ethinyl estradiol / norethindrone": (["norethindrone-ethinyl estradiol", "loestrin", "junel"], []),
    "dutasteride / tamsulosin": (["dutasteride-tamsulosin", "jalyn"], []),
    # Eye and other
    "dorzolamide / timolol": (["dorzolamide-timolol", "cosopt"], []),
    "brimonidine / timolol": (["brimonidine-timolol", "combigan"], []),
    "aspirin / dipyridamole": (["aspirin-dipyridamole", "aggrenox"], []),
    "hydralazine / isosorbide dinitrate": (["isosorbide dinitrate-hydralazine", "bidil"], []),
    "bupropion / dextromethorphan": (["dextromethorphan-bupropion", "auvelity"], []),
    "fluoxetine / olanzapine": (["olanzapine-fluoxetine", "symbyax"], []),
    "tipiracil / trifluridine": (["trifluridine-tipiracil", "lonsurf"], []),
    "ivacaftor / lumacaftor": (["orkambi"], []),
    # --- Expansion round 5 ---
    "candesartan / hydrochlorothiazide": (["candesartan-hydrochlorothiazide", "candesartan-hctz", "atacand hct"], []),
    "hydrochlorothiazide / irbesartan": (["irbesartan-hydrochlorothiazide", "irbesartan-hctz", "avalide"], []),
    "hydrochlorothiazide / telmisartan": (["telmisartan-hydrochlorothiazide", "telmisartan-hctz", "micardis hct"], []),
    "hydrochlorothiazide / metoprolol": (["metoprolol-hydrochlorothiazide", "metoprolol-hctz", "lopressor hct", "dutoprol"], []),
    "atenolol / chlorthalidone": (["atenolol-chlorthalidone", "tenoretic"], []),
    "amlodipine / telmisartan": (["amlodipine-telmisartan"], []),
    "hydrochlorothiazide / spironolactone": (["spironolactone-hydrochlorothiazide", "spironolactone-hctz", "aldactazide"], []),
    "amiloride / hydrochlorothiazide": (["amiloride-hydrochlorothiazide", "amiloride-hctz"], []),
    "amlodipine / hydrochlorothiazide / olmesartan": (["olmesartan-amlodipine-hydrochlorothiazide", "tribenzor"], []),
    "amlodipine / hydrochlorothiazide / valsartan": (["amlodipine-valsartan-hydrochlorothiazide", "exforge hct"], []),
    "ezetimibe / rosuvastatin": (["rosuvastatin-ezetimibe", "roszet"], []),
    "bempedoic acid / ezetimibe": (["bempedoic acid-ezetimibe", "nexlizet"], []),
    "alogliptin / metformin": (["alogliptin-metformin", "kazano"], []),
    "linagliptin / metformin": (["linagliptin-metformin", "jentadueto"], []),
    "metformin / saxagliptin": (["saxagliptin-metformin", "kombiglyze"], []),
    "metformin / pioglitazone": (["pioglitazone-metformin", "actoplus met"], []),
    "canagliflozin / metformin": (["canagliflozin-metformin", "invokamet"], []),
    "ertugliflozin / sitagliptin": (["ertugliflozin-sitagliptin", "steglujan"], []),
    "olodaterol / tiotropium": (["tiotropium-olodaterol", "stiolto"], []),
    "albuterol / budesonide": (["albuterol-budesonide", "airsupra"], []),
    "famotidine / ibuprofen": (["ibuprofen-famotidine", "duexis"], []),
    "diclofenac / misoprostol": (["diclofenac-misoprostol", "arthrotec"], []),
    "acetaminophen / butalbital / caffeine": (["butalbital-acetaminophen-caffeine", "fioricet"], []),
    "acetaminophen / tramadol": (["tramadol-acetaminophen", "ultracet"], []),
    "naproxen / sumatriptan": (["sumatriptan-naproxen", "treximet"], []),
    "atropine / diphenoxylate": (["diphenoxylate-atropine", "lomotil"], []),
    "efavirenz / emtricitabine / tenofovir disoproxil": (["atripla"], []),
    "dolutegravir / lamivudine": (["dolutegravir-lamivudine", "dovato"], []),
    "dolutegravir / rilpivirine": (["dolutegravir-rilpivirine", "juluca"], []),
    "emtricitabine / tenofovir alafenamide": (["emtricitabine-tenofovir alafenamide", "descovy"], []),
    "emtricitabine / rilpivirine / tenofovir alafenamide": (["odefsey"], []),
    "lopinavir / ritonavir": (["lopinavir-ritonavir", "kaletra"], []),
    "atovaquone / proguanil": (["atovaquone-proguanil", "malarone"], []),
    "artemether / lumefantrine": (["artemether-lumefantrine", "coartem"], []),
    "elbasvir / grazoprevir": (["elbasvir-grazoprevir", "zepatier"], []),
    "ceftolozane / tazobactam": (["ceftolozane-tazobactam", "zerbaxa"], []),
    "cilastatin / imipenem": (["imipenem-cilastatin", "primaxin"], []),
    "meropenem / vaborbactam": (["meropenem-vaborbactam", "vabomere"], []),
    "dexamethasone / tobramycin": (["tobramycin-dexamethasone", "tobradex"], []),
    "ciprofloxacin / dexamethasone": (["ciprofloxacin-dexamethasone", "ciprodex"], []),
    "carbidopa / entacapone / levodopa": (["carbidopa-levodopa-entacapone", "stalevo"], []),
    "donepezil / memantine": (["memantine-donepezil", "namzaric"], []),
    "estradiol / norethindrone": (["estradiol-norethindrone", "activella"], []),
    "ethinyl estradiol / etonogestrel": (["etonogestrel-ethinyl estradiol", "nuvaring"], []),
    "ethinyl estradiol / norelgestromin": (["norelgestromin-ethinyl estradiol", "xulane"], []),
    "desogestrel / ethinyl estradiol": (["desogestrel-ethinyl estradiol", "apri"], []),
    "brimonidine / brinzolamide": (["brinzolamide-brimonidine", "simbrinza"], []),
    "latanoprost / netarsudil": (["netarsudil-latanoprost", "rocklatan"], []),
    "betamethasone / clotrimazole": (["clotrimazole-betamethasone", "lotrisone"], []),
    "betamethasone / calcipotriene": (["calcipotriene-betamethasone", "taclonex", "enstilar"], []),
    "adapalene / benzoyl peroxide": (["adapalene-benzoyl peroxide", "epiduo"], []),
    "benzoyl peroxide / clindamycin": (["clindamycin-benzoyl peroxide", "benzaclin", "duac"], []),
    "clindamycin / tretinoin": (["clindamycin-tretinoin", "ziana"], []),
    "calcium carbonate / cholecalciferol": (["calcium with vitamin d", "calcium-vitamin d"], []),
}

# Synonyms RxNorm doesn't index as names but that unambiguously mean the ingredient.
# Where RxClass offers several classes and the default pick is misleading, name the one
# to use. It must be one of the classes RxClass returns for that drug (checked).
PREFERRED_CLASS = {
    "topiramate": "Anti-epileptic Agent",   # default ATC pick is "Centrally acting antiobesity products"
    "lithium carbonate": "Lithium",         # no FDA EPC; ATC "Lithium"
    "icosapent ethyl": "Other lipid modifying agents",  # no FDA EPC; ATC C10AX
    "risedronate": "Bisphosphonates",                   # no FDA EPC; ATC M05BA
    "simethicone": "Other drugs for functional gastrointestinal disorders",  # no FDA EPC; ATC A03AX
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


def cvx_name(code: str) -> tuple[str | None, bool]:
    try:
        data = get_json("https://tx.fhir.org/r4/CodeSystem/$lookup",
                        system="http://hl7.org/fhir/sid/cvx", code=code, _format="json")
    except urllib.error.HTTPError:
        return None, False
    params = data.get("parameter", [])
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


def verify_medication(name: str) -> tuple[dict | None, list[str]]:
    """RxNorm ingredient, brand checks and drug class for one MEDICATIONS entry."""
    found = rxnorm_ingredient(name)
    if found is None:
        return None, [f"RxNorm: no ingredient (IN/PIN) concept named {name!r}"]
    problems = []
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
    return ({"ingredient": name, "rxnorm": rxcui, "rxnorm_name": rx_name, "tty": tty,
             "drug_class": cls, "synonyms": syns, "exact": exact}, problems)


def verify_combination(name: str) -> tuple[dict | None, list[str]]:
    """RxNorm MIN, brand checks and joined component classes for one COMBINATIONS entry."""
    data = get_json("https://rxnav.nlm.nih.gov/REST/rxcui.json", name=name, search=0)
    mins = [c for c in data.get("idGroup", {}).get("rxnormId", []) or [] if _rx_props(c)["tty"] == "MIN"]
    if not mins:
        return None, [f"RxNorm: no multiple-ingredient (MIN) concept named {name!r}"]
    rxcui = mins[0]
    related = get_json(f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/related.json", tty="IN")
    parts = {c["rxcui"]: c["name"] for g in related["relatedGroup"].get("conceptGroup", []) or []
             for c in g.get("conceptProperties", []) or []}
    problems = []
    syns, exact = COMBINATIONS[name]
    for alias in syns + exact:
        found = rxnorm_alias_ingredients(alias)
        if found and found != set(parts):
            problems.append(f"RxNorm: {alias!r} -> {sorted(found)}, expected the ingredients of {name} {sorted(parts)}")
    # Components RxClass has no class for yet (sacubitril, bictegravir) are left out of the joined class
    classes_ = [c for c in (_default_class(c, "IN") for c in sorted(parts, key=parts.get)) if c]
    if not classes_:
        problems.append(f"RxClass: no usable class for any component of {name} ({rxcui})")
        return None, problems
    cls = " + ".join(dict.fromkeys(classes_))
    return ({"ingredient": name, "rxnorm": rxcui, "rxnorm_name": name, "tty": "MIN", "drug_class": cls,
             "components": sorted(parts.values()), "synonyms": syns, "exact": exact}, problems)


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
    for row, issues in pool.map(verify_medication, list(MEDICATIONS)):
        problems.extend(issues)
        if row:
            medications.append(row)

    print(f"Verifying {len(COMBINATIONS)} combination products (RxNorm MIN)...")
    check_collisions("medication", [(k, s + e) for k, (s, e) in {**MEDICATIONS, **COMBINATIONS}.items()], problems)
    for row, issues in pool.map(verify_combination, list(COMBINATIONS)):
        problems.extend(issues)
        if row:
            medications.append(row)

    print(f"Verifying {len(VACCINES)} CVX vaccine codes...")
    vaccines = []
    seen_codes: set[str] = set()
    for (name, code, phrases), (cvx_display, active) in zip(VACCINES, pool.map(lambda v: cvx_name(v[1]), VACCINES)):
        if cvx_display is None:
            problems.append(f"CVX {code} ({name}): not found")
            continue
        if not active:
            problems.append(f"CVX {code} ({name}, {cvx_display}): inactive")
        if code in seen_codes:
            problems.append(f"CVX {code}: listed twice")
        seen_codes.add(code)
        vaccines.append({"vaccine": name, "cvx": code, "cvx_display": cvx_display, "synonyms": phrases})

    print(f"\n{len(observations)} observations, {len(conditions)} conditions, {len(medications)} medications, "
          f"{len(vaccines)} vaccines")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        return 1
    if not check_only:
        OUT.mkdir(parents=True, exist_ok=True)
        for fname, rows in (("observations.json", observations), ("conditions.json", conditions),
                            ("medications.json", medications), ("vaccines.json", vaccines)):
            (OUT / fname).write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
        print(f"Wrote {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
