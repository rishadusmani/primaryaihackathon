"""Build app/vocab/*.json from the curated lists below, verifying every code online.

    python scripts/build_vocab.py           # verify + write
    python scripts/build_vocab.py --check   # verify only, exit 1 on any problem

Codes and aliases are curated by hand here; display names are pulled from the
authoritative source so they are never typed from memory:

- LOINC:      NLM Clinical Tables  (clinicaltables.nlm.nih.gov/api/loinc_items)
- ICD-10-CM:  NLM Clinical Tables  (current billable codes only)
- SNOMED CT:  tx.fhir.org $lookup  (International Edition; must be active)
- RxNorm:     NLM RxNav            (must resolve to an ingredient IN/PIN concept;
                                    brand aliases must map to that ingredient)
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "app" / "vocab"

# --------------------------------------------------------------------------- #
# Observations: (LOINC, category, canonical UCUM unit, aliases, conversions)
# Conversions map another UCUM unit to a factor (v * f) or [scale, offset].
# --------------------------------------------------------------------------- #
L, V = "laboratory", "vital-signs"
MGDL_FROM_MMOL_GLU = {"mmol/L": 18.016}
MGDL_FROM_MMOL_CHOL = {"mmol/L": 38.67}
ELECTROLYTE = {"meq/L": 1}

OBSERVATIONS = [
    # Vital signs
    ("8867-4", V, "/min", ["heartrate", "hr", "pulse", "pulserate"], {}),
    ("9279-1", V, "/min", ["respiratoryrate", "resprate", "rr", "respirations", "resp"], {}),
    ("8310-5", V, "Cel", ["temperature", "temp", "bodytemperature", "bodytemp"], {"[degF]": [5 / 9, -160 / 9]}),
    ("29463-7", V, "kg", ["weight", "bodyweight", "wt"], {"[lb_av]": 0.45359237, "g": 0.001}),
    ("8302-2", V, "cm", ["height", "bodyheight", "ht", "length"], {"[in_i]": 2.54, "m": 100}),
    ("39156-5", V, "kg/m2", ["bmi", "bodymassindex"], {}),
    ("59408-5", V, "%", ["spo2", "o2sat", "oxygensaturation", "pulseox", "sao2", "pulseoximetry"], {}),
    ("8480-6", V, "mm[Hg]", ["systolic", "systolicbp", "sbp", "systolicbloodpressure"], {}),
    ("8462-4", V, "mm[Hg]", ["diastolic", "diastolicbp", "dbp", "diastolicbloodpressure"], {}),
    ("9843-4", V, "cm", ["headcircumference", "hc", "ofc"], {"[in_i]": 2.54}),
    ("8280-0", V, "cm", ["waistcircumference", "waist"], {"[in_i]": 2.54}),
    ("72514-3", V, "{score}", ["pain", "painscore", "painseverity", "painlevel"], {}),
    # Complete blood count
    ("6690-2", L, "10*3/uL", ["wbc", "whitebloodcells", "whitecellcount", "leukocytes", "whitebloodcellcount"], {"10*9/L": 1}),
    ("789-8", L, "10*6/uL", ["rbc", "redbloodcells", "redcellcount", "erythrocytes", "redbloodcellcount"], {"10*12/L": 1}),
    ("718-7", L, "g/dL", ["hemoglobin", "haemoglobin", "hgb", "hb"], {"g/L": 0.1}),
    ("4544-3", L, "%", ["hematocrit", "haematocrit", "hct", "pcv"], {"L/L": 100}),
    ("787-2", L, "fL", ["mcv", "meancorpuscularvolume"], {}),
    ("785-6", L, "pg", ["mch", "meancorpuscularhemoglobin"], {}),
    ("786-4", L, "g/dL", ["mchc"], {"g/L": 0.1}),
    ("788-0", L, "%", ["rdw", "redcelldistributionwidth", "rdwcv"], {}),
    ("777-3", L, "10*3/uL", ["platelets", "plt", "plateletcount", "plts"], {"10*9/L": 1}),
    ("32623-1", L, "fL", ["mpv", "meanplateletvolume"], {}),
    ("770-8", L, "%", ["neutrophils", "neut", "neutrophilspercent", "polys", "segs"], {}),
    ("736-9", L, "%", ["lymphocytes", "lymph", "lymphs", "lymphocytespercent"], {}),
    ("5905-5", L, "%", ["monocytes", "mono", "monos", "monocytespercent"], {}),
    ("713-8", L, "%", ["eosinophils", "eos", "eosinophilspercent"], {}),
    ("706-2", L, "%", ["basophils", "baso", "basos", "basophilspercent"], {}),
    ("751-8", L, "10*3/uL", ["anc", "absoluteneutrophils", "neutrophilsabsolute", "absoluteneutrophilcount", "neutabs"], {"10*9/L": 1}),
    ("731-0", L, "10*3/uL", ["alc", "absolutelymphocytes", "lymphocytesabsolute", "absolutelymphocytecount", "lymphabs"], {"10*9/L": 1}),
    ("4679-7", L, "%", ["reticulocytes", "retic", "reticcount", "reticulocytecount"], {}),
    ("30341-2", L, "mm/h", ["esr", "sedrate", "sedimentationrate", "erythrocytesedimentationrate"], {}),
    # Chemistry
    ("2345-7", L, "mg/dL", ["glucose", "glu", "bloodglucose", "bloodsugar", "serumglucose"], MGDL_FROM_MMOL_GLU),
    ("1558-6", L, "mg/dL", ["fastingglucose", "fbg", "fbs", "fastingbloodsugar", "fastingbloodglucose", "fpg"], MGDL_FROM_MMOL_GLU),
    ("41653-7", L, "mg/dL", ["fingerstickglucose", "fingerstick", "pocglucose", "cbg", "capillaryglucose", "glucometer"], MGDL_FROM_MMOL_GLU),
    ("2951-2", L, "mmol/L", ["sodium", "na", "serumsodium"], ELECTROLYTE),
    ("2823-3", L, "mmol/L", ["potassium", "k", "serumpotassium"], ELECTROLYTE),
    ("2075-0", L, "mmol/L", ["chloride", "cl", "serumchloride"], ELECTROLYTE),
    ("2028-9", L, "mmol/L", ["co2", "bicarbonate", "bicarb", "totalco2", "tco2", "hco3"], ELECTROLYTE),
    ("33037-3", L, "mmol/L", ["aniongap", "ag"], ELECTROLYTE),
    ("3094-0", L, "mg/dL", ["bun", "ureanitrogen", "bloodureanitrogen"], {"mmol/L": 2.801}),
    ("2160-0", L, "mg/dL", ["creatinine", "creat", "cr", "scr", "serumcreatinine"], {"umol/L": 1 / 88.42}),
    ("62238-1", L, "mL/min/{1.73_m2}", ["egfr", "gfr", "estimatedgfr", "egfrckdepi"], {}),
    ("17861-6", L, "mg/dL", ["calcium", "ca", "serumcalcium", "totalcalcium"], {"mmol/L": 4.008, "meq/L": 2.004}),
    ("2777-1", L, "mg/dL", ["phosphate", "phosphorus", "phos", "po4"], {"mmol/L": 3.097}),
    ("19123-9", L, "mg/dL", ["magnesium", "mg", "mag", "serummagnesium"], {"mmol/L": 2.431, "meq/L": 1.2153}),
    ("2885-2", L, "g/dL", ["totalprotein", "tp", "protein", "serumprotein"], {"g/L": 0.1}),
    ("1751-7", L, "g/dL", ["albumin", "alb", "serumalbumin"], {"g/L": 0.1}),
    ("1975-2", L, "mg/dL", ["bilirubin", "totalbilirubin", "tbili", "bilitotal"], {"umol/L": 1 / 17.1}),
    ("1968-7", L, "mg/dL", ["directbilirubin", "dbili", "conjugatedbilirubin", "bilidirect"], {"umol/L": 1 / 17.1}),
    ("6768-6", L, "U/L", ["alkalinephosphatase", "alkphos", "alp"], {}),
    ("1742-6", L, "U/L", ["alt", "sgpt", "alanineaminotransferase"], {}),
    ("1920-8", L, "U/L", ["ast", "sgot", "aspartateaminotransferase"], {}),
    ("2324-2", L, "U/L", ["ggt", "gammagt", "gammaglutamyltransferase"], {}),
    ("3084-1", L, "mg/dL", ["uricacid", "urate"], {"umol/L": 1 / 59.48}),
    ("1798-8", L, "U/L", ["amylase"], {}),
    ("3040-3", L, "U/L", ["lipase"], {}),
    ("2532-0", L, "U/L", ["ldh", "lactatedehydrogenase", "ld"], {}),
    ("2157-6", L, "U/L", ["ck", "cpk", "creatinekinase", "creatinephosphokinase"], {}),
    ("2692-2", L, "mosm/kg", ["osmolality", "serumosmolality", "osmo", "sosm"], {}),
    ("2524-7", L, "mmol/L", ["lactate", "lacticacid", "lactatelevel"], {"mg/dL": 1 / 9.008}),
    ("16362-6", L, "umol/L", ["ammonia", "nh3"], {"ug/dL": 0.5872}),
    # Lipids
    ("2093-3", L, "mg/dL", ["cholesterol", "totalcholesterol", "chol", "tc"], MGDL_FROM_MMOL_CHOL),
    ("2571-8", L, "mg/dL", ["triglycerides", "trig", "trigs", "tg"], {"mmol/L": 88.57}),
    ("2085-9", L, "mg/dL", ["hdl", "hdlc", "hdlcholesterol"], MGDL_FROM_MMOL_CHOL),
    ("13457-7", L, "mg/dL", ["ldl", "ldlc", "ldlcholesterol", "ldlcalculated"], MGDL_FROM_MMOL_CHOL),
    ("43396-1", L, "mg/dL", ["nonhdl", "nonhdlc", "nonhdlcholesterol"], MGDL_FROM_MMOL_CHOL),
    ("9830-1", L, "{ratio}", ["cholhdlratio", "cholesterolhdlratio", "tchdlratio"], {}),
    # Diabetes / endocrine
    ("4548-4", L, "%", ["hba1c", "a1c", "hemoglobina1c", "haemoglobina1c", "glycatedhemoglobin", "glycohemoglobin"], {"mmol/mol": [0.09148, 2.152]}),
    ("14959-1", L, "mg/g", ["uacr", "microalbumincreatinineratio", "albumincreatinineratio", "acr"], {}),
    ("20448-7", L, "m[IU]/L", ["insulin", "insulinlevel", "fastinginsulin"], {"pmol/L": 1 / 6}),
    ("1986-9", L, "ng/mL", ["cpeptide"], {"nmol/L": 3.02}),
    ("3016-3", L, "m[IU]/L", ["tsh", "thyrotropin", "thyroidstimulatinghormone"], {}),
    ("3024-7", L, "ng/dL", ["freet4", "ft4", "freethyroxine", "t4free"], {"pmol/L": 1 / 12.87}),
    ("3051-0", L, "pg/mL", ["freet3", "ft3", "freetriiodothyronine", "t3free"], {"pmol/L": 0.651}),
    ("3026-2", L, "ug/dL", ["t4", "totalt4", "thyroxine", "t4total"], {"nmol/L": 1 / 12.87}),
    ("2731-8", L, "pg/mL", ["pth", "parathyroidhormone", "intactpth"], {"pmol/L": 9.43}),
    ("2143-6", L, "ug/dL", ["cortisol", "serumcortisol"], {"nmol/L": 1 / 27.59}),
    ("2986-8", L, "ng/dL", ["testosterone", "totaltestosterone"], {"nmol/L": 28.84}),
    ("2857-1", L, "ng/mL", ["psa", "prostatespecificantigen"], {"ug/L": 1}),
    ("21198-7", L, "m[IU]/mL", ["hcg", "betahcg", "bhcg", "quantitativehcg", "serumhcg"], {"U/L": 1}),  # IU/L parses as U/L
    # Iron / vitamins
    ("2498-4", L, "ug/dL", ["iron", "serumiron", "fe"], {"umol/L": 5.585}),
    ("2276-4", L, "ng/mL", ["ferritin"], {"ug/L": 1}),
    ("2500-7", L, "ug/dL", ["tibc", "totalironbindingcapacity"], {"umol/L": 5.585}),
    ("2502-3", L, "%", ["transferrinsaturation", "tsat", "ironsaturation"], {}),
    ("2132-9", L, "pg/mL", ["b12", "vitaminb12", "cobalamin"], {"pmol/L": 1.355}),
    ("2284-8", L, "ng/mL", ["folate", "folicacidlevel", "serumfolate"], {"nmol/L": 1 / 2.266}),
    ("62292-8", L, "ng/mL", ["vitamind", "25ohd", "25ohvitamind", "vitd", "25hydroxyvitamind"], {"nmol/L": 1 / 2.496}),
    # Coagulation
    ("5902-2", L, "s", ["pt", "prothrombintime", "protime"], {}),
    ("6301-6", L, "{INR}", ["inr"], {}),
    ("14979-9", L, "s", ["aptt", "ptt", "partialthromboplastintime"], {}),
    ("3255-7", L, "mg/dL", ["fibrinogen"], {"g/L": 100}),
    ("48065-7", L, "ng/mL", ["ddimer", "dimer"], {"ug/mL": 1000, "mg/L": 1000}),  # FEU is in the code
    # Cardiac / inflammation
    ("10839-9", L, "ng/mL", ["troponin", "troponini", "tni", "ctni"], {"ng/L": 0.001}),
    ("89579-7", L, "ng/L", ["hstroponin", "hstroponini", "hstni", "highsensitivitytroponin"], {"ng/mL": 1000}),
    ("30934-4", L, "pg/mL", ["bnp", "brainnatriureticpeptide"], {"ng/L": 1}),
    ("33762-6", L, "pg/mL", ["ntprobnp", "probnp"], {"ng/L": 1}),
    ("1988-5", L, "mg/L", ["crp", "creactiveprotein"], {"mg/dL": 10}),
    ("30522-7", L, "mg/L", ["hscrp", "highsensitivitycrp", "cardiaccrp"], {"mg/dL": 10}),
    ("33959-8", L, "ng/mL", ["procalcitonin", "pct"], {"ug/L": 1}),
    # Arterial blood gas
    ("2744-1", L, "[pH]", ["ph", "arterialph", "abgph"], {}),
    ("2019-8", L, "mm[Hg]", ["pco2", "paco2", "arterialpco2"], {"kPa": 7.50062}),
    ("2703-7", L, "mm[Hg]", ["po2", "pao2", "arterialpo2"], {"kPa": 7.50062}),
    ("1960-4", L, "mmol/L", ["arterialbicarbonate", "abghco3", "arterialhco3"], ELECTROLYTE),
    # Urine
    ("2756-5", L, "[pH]", ["urineph"], {}),
    ("2965-2", L, "1", ["specificgravity", "urinespecificgravity", "sg", "usg"], {}),
    ("2161-8", L, "mg/dL", ["urinecreatinine"], {"umol/L": 1 / 88.42}),
]

# --------------------------------------------------------------------------- #
# Conditions: (SNOMED CT, ICD-10-CM, aliases)
# --------------------------------------------------------------------------- #
CONDITIONS = [
    # Cardiometabolic
    ("44054006", "E11.9", ["type2diabetes", "type2diabetesmellitus", "diabetesmellitustype2", "t2dm", "dm2", "dmii", "niddm", "diabetestype2"]),
    ("46635009", "E10.9", ["type1diabetes", "type1diabetesmellitus", "diabetesmellitustype1", "t1dm", "dm1", "iddm", "diabetestype1"]),
    ("714628002", "R73.03", ["prediabetes", "prediabetic", "impairedfastingglucose"]),
    ("302866003", "E16.2", ["hypoglycemia", "hypoglycaemia", "lowbloodsugar"]),
    ("59621000", "I10", ["hypertension", "essentialhypertension", "htn", "highbloodpressure"]),
    ("45007003", "I95.9", ["hypotension", "lowbloodpressure"]),
    ("55822004", "E78.5", ["hyperlipidemia", "hyperlipidaemia", "hld"]),
    ("13644009", "E78.00", ["hypercholesterolemia", "hypercholesterolaemia", "highcholesterol"]),
    ("302870006", "E78.1", ["hypertriglyceridemia", "hypertriglyceridaemia", "hightriglycerides"]),
    ("414916001", "E66.9", ["obesity", "obese"]),
    ("53741008", "I25.10", ["coronaryarterydisease", "cad", "coronaryheartdisease", "chd", "ischemicheartdisease", "ihd", "ascvd"]),
    ("22298006", "I21.9", ["myocardialinfarction", "mi", "heartattack", "ami", "acutemyocardialinfarction"]),
    ("84114007", "I50.9", ["heartfailure", "chf", "congestiveheartfailure", "hf", "cardiacfailure"]),
    ("49436004", "I48.91", ["atrialfibrillation", "afib", "af"]),
    ("5370000", "I48.92", ["atrialflutter", "aflutter"]),
    ("60573004", "I35.0", ["aorticstenosis", "aorticvalvestenosis"]),
    ("233873004", "I42.2", ["hypertrophiccardiomyopathy", "hcm"]),
    ("400047006", "I73.9", ["peripheralvasculardisease", "pvd", "peripheralarterydisease", "pad"]),
    ("128053003", "I82.409", ["dvt", "deepveinthrombosis", "deepvenousthrombosis"]),
    ("59282003", "I26.99", ["pulmonaryembolism", "pe"]),
    ("230690007", "I63.9", ["stroke", "cva", "cerebrovascularaccident"]),
    ("266257000", "G45.9", ["tia", "transientischemicattack", "ministroke"]),
    # Respiratory
    ("195967001", "J45.909", ["asthma"]),
    ("13645005", "J44.9", ["copd", "chronicobstructivepulmonarydisease"]),
    ("233604007", "J18.9", ["pneumonia", "cap", "communityacquiredpneumonia"]),
    ("78275009", "G47.33", ["obstructivesleepapnea", "osa", "sleepapnea"]),
    ("233703007", "J84.9", ["interstitiallungdisease", "ild", "pulmonaryfibrosis"]),
    ("190905008", "E84.9", ["cysticfibrosis", "cf"]),
    ("61582004", "J30.9", ["allergicrhinitis", "hayfever", "seasonalallergies"]),
    ("40055000", "J32.9", ["chronicsinusitis"]),
    ("43878008", "J02.0", ["strepthroat", "streptococcalpharyngitis", "streppharyngitis"]),
    ("6142004", "J11.1", ["influenza", "flu"]),
    ("840539006", "U07.1", ["covid19", "covid", "sarscov2", "coronavirusdisease2019"]),
    # Renal / GU
    ("709044004", "N18.9", ["chronickidneydisease", "ckd", "chronicrenaldisease", "crd"]),
    ("14669001", "N17.9", ["acutekidneyinjury", "aki", "acuterenalfailure", "arf"]),
    ("95570007", "N20.0", ["kidneystone", "kidneystones", "nephrolithiasis", "renalcalculus"]),
    ("68566005", "N39.0", ["uti", "urinarytractinfection"]),
    ("266569009", "N40.0", ["bph", "benignprostatichyperplasia", "enlargedprostate"]),
    ("237055002", "E28.2", ["pcos", "polycysticovarysyndrome", "polycysticovariansyndrome"]),
    ("129103003", "N80.9", ["endometriosis"]),
    # GI / liver
    ("235595009", "K21.9", ["gerd", "gastroesophagealrefluxdisease", "acidreflux", "reflux", "gord"]),
    ("10743008", "K58.9", ["ibs", "irritablebowelsyndrome"]),
    ("34000006", "K50.90", ["crohnsdisease", "crohns", "crohn", "crohndisease"]),
    ("64766004", "K51.90", ["ulcerativecolitis", "uc"]),
    ("19943007", "K74.60", ["cirrhosis", "livercirrhosis", "cirrhosisofliver"]),
    ("197321007", "K76.0", ["fattyliver", "hepaticsteatosis", "nafld", "masld", "steatosisofliver"]),
    ("61977001", "B18.1", ["hepatitisb", "chronichepatitisb", "hbv", "hepb"]),
    ("128302006", "B18.2", ["hepatitisc", "chronichepatitisc", "hcv", "hepc"]),
    ("197456007", "K85.90", ["acutepancreatitis", "pancreatitis"]),
    ("14760008", "K59.00", ["constipation"]),
    # Endocrine / nutrition / electrolytes
    ("40930008", "E03.9", ["hypothyroidism", "hypothyroid", "underactivethyroid"]),
    ("34486009", "E05.90", ["hyperthyroidism", "hyperthyroid", "overactivethyroid", "thyrotoxicosis"]),
    ("34713006", "E55.9", ["vitaminddeficiency", "lowvitamind"]),
    ("43339004", "E87.6", ["hypokalemia", "hypokalaemia", "lowpotassium"]),
    ("14140009", "E87.5", ["hyperkalemia", "hyperkalaemia", "highpotassium"]),
    ("89627008", "E87.1", ["hyponatremia", "hyponatraemia", "lowsodium"]),
    ("90560007", "M10.9", ["gout"]),
    # Hematology / oncology
    ("271737000", "D64.9", ["anemia", "anaemia"]),
    ("87522002", "D50.9", ["irondeficiencyanemia", "irondeficiencyanaemia", "ida"]),
    ("417357006", "D57.1", ["sicklecelldisease", "sicklecellanemia", "scd"]),
    ("363346000", "C80.1", ["cancer", "malignancy", "malignantneoplasm"]),
    ("254837009", "C50.919", ["breastcancer", "breastcarcinoma"]),
    ("399068003", "C61", ["prostatecancer", "prostatecarcinoma"]),
    ("363358000", "C34.90", ["lungcancer", "lungcarcinoma"]),
    ("363406005", "C18.9", ["coloncancer", "colorectalcancer", "coloncarcinoma"]),
    ("363418001", "C25.9", ["pancreaticcancer", "pancreascancer"]),
    ("372244006", "C43.9", ["melanoma", "malignantmelanoma"]),
    ("93143009", "C95.90", ["leukemia", "leukaemia"]),
    ("118600007", "C85.90", ["lymphoma"]),
    # Neurology
    ("84757009", "G40.909", ["epilepsy", "seizuredisorder"]),
    ("37796009", "G43.909", ["migraine", "migraines"]),
    ("49049000", "G20.A1", ["parkinsonsdisease", "parkinsons", "parkinson", "parkinsondisease", "pd"]),
    ("26929004", "G30.9", ["alzheimersdisease", "alzheimers", "alzheimer", "alzheimerdisease"]),
    ("52448006", "F03.90", ["dementia"]),
    ("302226006", "G62.9", ["peripheralneuropathy", "neuropathy", "polyneuropathy"]),
    ("193462001", "G47.00", ["insomnia"]),
    ("82423001", "G89.29", ["chronicpain"]),
    # Mental health / substance use
    ("35489007", "F32.A", ["depression", "depressivedisorder"]),
    ("370143000", "F32.9", ["majordepressivedisorder", "mdd", "majordepression"]),
    ("197480006", "F41.9", ["anxiety", "anxietydisorder"]),
    ("21897009", "F41.1", ["generalizedanxietydisorder", "gad", "generalisedanxietydisorder"]),
    ("13746004", "F31.9", ["bipolardisorder", "bipolar", "manicdepression"]),
    ("58214004", "F20.9", ["schizophrenia"]),
    ("47505003", "F43.10", ["ptsd", "posttraumaticstressdisorder"]),
    ("406506008", "F90.9", ["adhd", "attentiondeficithyperactivitydisorder", "add"]),
    ("191736004", "F42.9", ["ocd", "obsessivecompulsivedisorder"]),
    ("7200002", "F10.20", ["alcoholism", "alcoholdependence", "alcoholusedisorder", "aud"]),
    ("56294008", "F17.200", ["nicotinedependence", "tobaccouse", "smoker", "tobaccodependence"]),
    ("75544000", "F11.20", ["opioiddependence", "opioidusedisorder", "oud"]),
    # Musculoskeletal / rheumatology / skin
    ("69896004", "M06.9", ["rheumatoidarthritis", "ra"]),
    ("396275006", "M19.90", ["osteoarthritis", "oa", "degenerativejointdisease", "djd"]),
    ("64859006", "M81.0", ["osteoporosis"]),
    ("55464009", "M32.9", ["sle", "lupus", "systemiclupuserythematosus"]),
    ("279039007", "M54.50", ["lowbackpain", "lbp"]),
    ("9014002", "L40.9", ["psoriasis"]),
    ("24079001", "L20.9", ["atopicdermatitis", "eczema"]),
    ("128045006", "L03.90", ["cellulitis"]),
    ("126485001", "L50.9", ["urticaria", "hives"]),
    # Infectious disease
    ("86406008", "B20", ["hiv", "hivinfection", "humanimmunodeficiencyvirusinfection", "aids"]),
    ("56717001", "A15.9", ["tuberculosis", "tb", "pulmonarytuberculosis"]),
    ("91302008", "A41.9", ["sepsis", "septicemia"]),
    # Eye / ENT
    ("23986001", "H40.9", ["glaucoma"]),
    ("193570009", "H26.9", ["cataract", "cataracts"]),
    ("267718000", "H35.30", ["maculardegeneration", "amd", "agerelatedmaculardegeneration", "armd"]),
    ("15188001", "H91.90", ["hearingloss", "deafness"]),
    # Common symptoms (problem-list entries)
    ("29857009", "R07.9", ["chestpain"]),
    ("267036007", "R06.00", ["dyspnea", "shortnessofbreath", "sob", "dyspnoea"]),
    ("49727002", "R05.9", ["cough"]),
    ("25064002", "R51.9", ["headache"]),
    ("386661006", "R50.9", ["fever", "pyrexia"]),
    ("21522001", "R10.9", ["abdominalpain", "abdpain", "stomachache"]),
    ("422587007", "R11.0", ["nausea"]),
    ("62315008", "R19.7", ["diarrhea", "diarrhoea"]),
    ("84229001", "R53.83", ["fatigue", "tiredness"]),
    ("404640003", "R42", ["dizziness"]),
]

# --------------------------------------------------------------------------- #
# Medications: RxNorm ingredient name -> brand / alternative-name aliases.
# Single-ingredient drugs only; brands are verified to map to the ingredient.
# --------------------------------------------------------------------------- #
MEDICATIONS = {
    # Diabetes
    "metformin": ["Glucophage"],
    "glipizide": ["Glucotrol"],
    "glimepiride": ["Amaryl"],
    "pioglitazone": ["Actos"],
    "sitagliptin": ["Januvia"],
    "empagliflozin": ["Jardiance"],
    "dapagliflozin": ["Farxiga"],
    "semaglutide": ["Ozempic", "Wegovy", "Rybelsus"],
    "liraglutide": ["Victoza", "Saxenda"],
    "dulaglutide": ["Trulicity"],
    "tirzepatide": ["Mounjaro", "Zepbound"],
    "insulin glargine": ["Lantus", "Basaglar", "Toujeo"],
    "insulin lispro": ["Humalog", "Admelog"],
    "insulin aspart": ["Novolog", "Fiasp"],
    "insulin detemir": ["Levemir"],
    # Cardiovascular
    "lisinopril": ["Zestril", "Prinivil"],
    "enalapril": ["Vasotec"],
    "losartan": ["Cozaar"],
    "valsartan": ["Diovan"],
    "amlodipine": ["Norvasc"],
    "diltiazem": ["Cardizem"],
    "verapamil": ["Calan"],
    "metoprolol": [],
    "carvedilol": ["Coreg"],
    "atenolol": ["Tenormin"],
    "propranolol": ["Inderal"],
    "hydrochlorothiazide": ["HCTZ"],
    "chlorthalidone": [],
    "furosemide": ["Lasix"],
    "spironolactone": ["Aldactone"],
    "hydralazine": [],
    "isosorbide mononitrate": [],
    "isosorbide dinitrate": ["Isordil"],
    "nitroglycerin": ["Nitrostat"],
    "digoxin": ["Lanoxin"],
    "amiodarone": ["Pacerone"],
    "atorvastatin": ["Lipitor"],
    "rosuvastatin": ["Crestor"],
    "simvastatin": ["Zocor"],
    "pravastatin": ["Pravachol"],
    "ezetimibe": ["Zetia"],
    "fenofibrate": ["Tricor"],
    "aspirin": ["ASA"],
    "clopidogrel": ["Plavix"],
    "warfarin": ["Coumadin", "Jantoven"],
    "apixaban": ["Eliquis"],
    "rivaroxaban": ["Xarelto"],
    "enoxaparin": ["Lovenox"],
    "heparin": [],
    # Respiratory / allergy
    "albuterol": ["salbutamol", "Ventolin", "ProAir"],
    "fluticasone": ["Flonase", "Flovent"],
    "budesonide": ["Pulmicort"],
    "tiotropium": ["Spiriva"],
    "montelukast": ["Singulair"],
    "loratadine": ["Claritin"],
    "cetirizine": ["Zyrtec"],
    "fexofenadine": ["Allegra"],
    # GI
    "omeprazole": ["Prilosec"],
    "pantoprazole": ["Protonix"],
    "famotidine": ["Pepcid"],
    "ondansetron": [],
    # Pain / musculoskeletal / rheumatology
    "acetaminophen": ["paracetamol", "Tylenol", "APAP"],
    "ibuprofen": ["Advil", "Motrin"],
    "naproxen": ["Aleve", "Naprosyn"],
    "meloxicam": ["Mobic"],
    "tramadol": ["Ultram"],
    "oxycodone": ["OxyContin", "Roxicodone"],
    "morphine": [],
    "cyclobenzaprine": [],
    "baclofen": [],
    "tizanidine": ["Zanaflex"],
    "prednisone": [],
    "methotrexate": ["Trexall"],
    "hydroxychloroquine": ["Plaquenil"],
    "allopurinol": ["Zyloprim"],
    "colchicine": ["Colcrys"],
    "alendronate": ["Fosamax"],
    # Neuro / psych
    "gabapentin": ["Neurontin"],
    "pregabalin": ["Lyrica"],
    "levetiracetam": ["Keppra"],
    "lamotrigine": ["Lamictal"],
    "topiramate": ["Topamax"],
    "sumatriptan": ["Imitrex"],
    "donepezil": ["Aricept"],
    "memantine": ["Namenda"],
    "sertraline": ["Zoloft"],
    "escitalopram": ["Lexapro"],
    "citalopram": ["Celexa"],
    "fluoxetine": ["Prozac"],
    "paroxetine": ["Paxil"],
    "duloxetine": ["Cymbalta"],
    "venlafaxine": ["Effexor"],
    "bupropion": ["Wellbutrin"],
    "mirtazapine": ["Remeron"],
    "trazodone": [],
    "amitriptyline": [],
    "buspirone": [],
    "quetiapine": ["Seroquel"],
    "aripiprazole": ["Abilify"],
    "risperidone": ["Risperdal"],
    "olanzapine": ["Zyprexa"],
    "lithium carbonate": [],
    "alprazolam": ["Xanax"],
    "lorazepam": ["Ativan"],
    "clonazepam": ["Klonopin"],
    "zolpidem": ["Ambien"],
    "methylphenidate": ["Ritalin", "Concerta"],
    "lisdexamfetamine": ["Vyvanse"],
    # Anti-infectives
    "amoxicillin": [],
    "azithromycin": ["Zithromax"],
    "doxycycline": ["Vibramycin"],
    "cephalexin": ["Keflex"],
    "ciprofloxacin": ["Cipro"],
    "levofloxacin": ["Levaquin"],
    "nitrofurantoin": ["Macrobid"],
    "metronidazole": ["Flagyl"],
    "clindamycin": ["Cleocin"],
    "fluconazole": ["Diflucan"],
    "valacyclovir": ["Valtrex"],
    "oseltamivir": ["Tamiflu"],
    # Endocrine / GU / other
    "levothyroxine": ["Synthroid", "Levoxyl"],
    "tamsulosin": ["Flomax"],
    "finasteride": ["Proscar", "Propecia"],
    "sildenafil": ["Viagra"],
    "estradiol": ["Estrace"],
    "potassium chloride": ["KCl", "Klor-Con"],
    "cholecalciferol": ["vitamin D3", "vitamin D"],
    "cyanocobalamin": ["vitamin B12", "B12"],
    "folic acid": ["folate"],
    "ferrous sulfate": [],
}

# --------------------------------------------------------------------------- #

client = httpx.Client(timeout=30, follow_redirects=True)


def get_json(url: str, **params):
    for attempt in range(4):
        try:
            r = client.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError("retry", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def loinc_name(code: str) -> str | None:
    data = get_json("https://clinicaltables.nlm.nih.gov/api/loinc_items/v3/search",
                    terms=code, sf="LOINC_NUM", df="LOINC_NUM,LONG_COMMON_NAME", maxList=50)
    return next((name for num, name in data[3] if num == code), None)


def icd10_name(code: str) -> str | None:
    data = get_json("https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search",
                    terms=code, sf="code", maxList=50)
    return next((name for c, name in data[3] if c == code), None)


def snomed_name(code: str) -> tuple[str | None, bool]:
    data = get_json("https://tx.fhir.org/r4/CodeSystem/$lookup",
                    system="http://snomed.info/sct", code=code, _format="json")
    if data.get("resourceType") != "Parameters":
        return None, False
    params = data["parameter"]
    display = next((p.get("valueString") for p in params if p["name"] == "display"), None)
    inactive = any(
        p["name"] == "property"
        and {"name": "code", "valueCode": "inactive"} in p.get("part", [])
        and {"name": "value", "valueBoolean": True} in p.get("part", [])
        for p in params
    )
    return display, not inactive


def rxnorm_ingredient(name: str) -> tuple[str, str, str] | None:
    data = get_json("https://rxnav.nlm.nih.gov/REST/rxcui.json", name=name, search=0)
    candidates = []
    for rxcui in data.get("idGroup", {}).get("rxnormId", []) or []:
        props = get_json(f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/properties.json")["properties"]
        # PIN only when no IN exists (e.g. isosorbide mononitrate); prefer IN ("heparin", not "heparin, porcine")
        if props["tty"] in ("IN", "PIN"):
            candidates.append((props["tty"] != "IN", rxcui, props["name"], props["tty"]))
    return min(candidates)[1:] if candidates else None


def rxnorm_alias_ingredients(alias: str) -> set[str]:
    """RxCUIs of the ingredients an alias (brand or synonym) resolves to."""
    data = get_json("https://rxnav.nlm.nih.gov/REST/rxcui.json", name=alias, search=2)
    found: set[str] = set()
    for rxcui in data.get("idGroup", {}).get("rxnormId", []) or []:
        props = get_json(f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/properties.json")["properties"]
        if props["tty"] == "IN":
            found.add(rxcui)
            continue
        related = get_json(f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/related.json", tty="IN")
        for group in related["relatedGroup"].get("conceptGroup", []) or []:
            for c in group.get("conceptProperties", []) or []:
                found.add(c["rxcui"])
    return found


def _key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def check_aliases(kind: str, groups: list[tuple[str, list[str]]], problems: list[str]) -> None:
    seen: dict[str, str] = {}
    for code, aliases in groups:
        for a in aliases:
            k = _key(a)
            if k in seen and seen[k] != code:
                problems.append(f"{kind}: alias {a!r} used by both {seen[k]} and {code}")
            seen[k] = code


def main() -> int:
    check_only = "--check" in sys.argv
    problems: list[str] = []
    pool = ThreadPoolExecutor(max_workers=8)

    print(f"Verifying {len(OBSERVATIONS)} LOINC codes...")
    check_aliases("observation", [(o[0], o[3]) for o in OBSERVATIONS], problems)
    observations = []
    for (code, category, unit, aliases, conv), name in zip(OBSERVATIONS, pool.map(lambda o: loinc_name(o[0]), OBSERVATIONS)):
        if name is None:
            problems.append(f"LOINC {code}: not found")
            continue
        observations.append({"loinc": code, "display": name, "category": category, "unit": unit,
                             "aliases": aliases, "convert": conv})

    print(f"Verifying {len(CONDITIONS)} conditions (SNOMED CT + ICD-10-CM)...")
    check_aliases("condition", [(c[0], c[2]) for c in CONDITIONS], problems)
    conditions = []
    snomed = list(pool.map(lambda c: snomed_name(c[0]), CONDITIONS))
    icd = list(pool.map(lambda c: icd10_name(c[1]), CONDITIONS))
    for (sct, icd_code, aliases), (sct_name, active), icd_name in zip(CONDITIONS, snomed, icd):
        if sct_name is None:
            problems.append(f"SNOMED {sct}: not found")
            continue
        if not active:
            problems.append(f"SNOMED {sct} ({sct_name}): inactive")
        if icd_name is None:
            problems.append(f"ICD-10-CM {icd_code} (for {sct_name}): not a current billable code")
            continue
        conditions.append({"snomed": sct, "display": sct_name, "icd10": icd_code,
                           "icd10_display": icd_name, "aliases": aliases})

    print(f"Verifying {len(MEDICATIONS)} RxNorm ingredients and their aliases...")
    check_aliases("medication", list(MEDICATIONS.items()), problems)
    medications = []
    names = list(MEDICATIONS)
    for name, found in zip(names, pool.map(rxnorm_ingredient, names)):
        if found is None:
            problems.append(f"RxNorm: no ingredient (IN/PIN) concept named {name!r}")
            continue
        rxcui, rx_name, tty = found
        # Keep the curated name searchable when RxNorm names it differently
        # (cyanocobalamin -> "vitamin B12", insulin aspart -> "insulin aspart, human").
        aliases = [name] if _key(name) != _key(rx_name) else []
        for alias in MEDICATIONS[name]:
            ingredients = rxnorm_alias_ingredients(alias)
            if ingredients == {rxcui}:
                aliases.append(alias)
            elif _key(alias) in {"asa", "hctz", "apap", "kcl", "b12", "vitaminb12", "vitamind", "vitamind3", "folate"}:
                aliases.append(alias)  # common clinical abbreviations RxNorm doesn't index
            else:
                problems.append(f"RxNorm: alias {alias!r} -> {sorted(ingredients) or 'nothing'}, expected {rxcui} ({rx_name})")
        medications.append({"rxcui": rxcui, "name": rx_name, "tty": tty, "aliases": aliases})

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
