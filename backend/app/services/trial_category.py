"""Map a trial's ``conditions`` list to a single top-level category.

ClinicalTrials.gov stores conditions as free-text strings (e.g.
``"Non-Small Cell Lung Cancer"``, ``"Type 2 Diabetes Mellitus"``).
For the catalog dropdown we collapse those into a small, stable set of
clinical specialties so coordinators and patients can browse.

Design choices
--------------
* **Keyword-rules, not LLM.**  A classifier here would add latency to
  every sync without any meaningful accuracy gain — the condition
  strings are short and the categories are coarse.  An ordered list of
  ``(category, [keywords])`` tuples is faster, deterministic, and easy
  to audit.
* **Order matters.**  Earlier rules win.  We put the most specific /
  highest-volume specialties first so e.g. "lung cancer" classifies as
  Oncology, not Respiratory.
* **Substring matching, not exact match.**  The CT.gov condition
  strings are inconsistent ("Cancer", "Cancers", "Carcinoma") so
  matching against keyword *fragments* gives better coverage than a
  whitelist of full names would.

If no rule matches we fall back to :data:`GENERAL_CATEGORY`.  That's
not an error — it just means the trial doesn't fit one of the major
specialties cleanly.
"""

from __future__ import annotations

from typing import Iterable

GENERAL_CATEGORY = "General Medicine"

# (category, [keywords]) — order matters.  The first rule that matches
# any keyword in the joined condition text wins.
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    # Cancer first — "lung cancer" must beat the "lung" rule below.
    ("Oncology", [
        "cancer", "carcinoma", "tumor", "tumour", "neoplasm",
        "lymphoma", "leukemia", "leukaemia", "sarcoma", "melanoma",
        "myeloma", "metastat", "oncolog", "malignan", "glioma",
        "glioblastoma", "mesothelioma",
    ]),
    # Endocrine before "Cardiovascular" so "diabetes mellitus" doesn't
    # leak into anything else.  We include both "diabetes" and
    # "diabetic" so complications like "diabetic foot ulcer" catch
    # here instead of falling through to Gastro on the bare "ulcer"
    # keyword.  Eye-specific complications still take Ophthalmology
    # via the "retin" rule below — that's the correct clinical home
    # for diabetic retinopathy.
    ("Endocrine & Metabolic", [
        "diabetes", "diabetic", "thyroid", "hypothyroid", "hyperthyroid",
        "metabolic syndrome", "obesity", "insulin resistance",
        "endocrin", "pituitary", "adrenal", "cushing", "addison",
        "polycystic ovary",
    ]),
    ("Cardiovascular", [
        "heart", "cardiac", "cardio", "coronary", "myocardial",
        "hypertension", "blood pressure", "stroke", "arrhythm",
        "atrial fibrill", "ventricular", "vascular", "cholesterol",
        "lipid", "atherosclero", "thromb", "embolism", "aneurysm",
    ]),
    ("Neurology", [
        "alzheimer", "parkinson", "epileps", "seizure", "migraine",
        "multiple sclerosis", " als ", "amyotrophic", "huntington",
        "neurodegen", "dementia", "neuropath", "cerebral palsy",
        "spinal cord", "tourette",
    ]),
    ("Mental Health", [
        "depression", "depressive", "anxiety", "bipolar",
        "schizophren", "psychosis", "psychotic", "ptsd",
        "post-traumatic", "post traumatic", "adhd", "autism",
        "psychiat", "mental health", "eating disorder",
        "substance use", "addiction", "alcohol use",
    ]),
    ("Infectious Diseases", [
        " hiv", "aids", "covid", "sars-cov", "tuberculosis",
        "hepatitis", "malaria", "influenza", "sepsis", "mers",
        "ebola", "infect", "pneumonia", "meningitis", "lyme",
        "syphilis", "gonorrh", "chlamydia",
    ]),
    ("Respiratory", [
        "asthma", "copd", "pulmonary", "lung disease", "respiratory",
        "cystic fibrosis", "bronch", "emphysema", "pulmonary fibrosis",
        "sleep apnea",
    ]),
    ("Gastroenterology", [
        "crohn", "colitis", "irritable bowel", " ibs", "gastro",
        "liver", "cirrhosis", "hepat", "pancrea", "bowel disease",
        "celiac", "coeliac", "gerd", "reflux",
        # Restrict "ulcer" to peptic / gastric variants so "diabetic
        # foot ulcer" doesn't end up here.
        "peptic ulcer", "gastric ulcer", "stomach ulcer",
    ]),
    ("Rheumatology & Autoimmune", [
        "rheumatoid", "arthritis", "lupus", "autoimmune", "psoriasis",
        "ankylosing", "scleroderma", "sjogren", "vasculitis",
        "fibromyalgia",
    ]),
    ("Nephrology", [
        "kidney", "renal", "dialysis", "nephritis", "nephrotic",
        "polycystic kidney",
    ]),
    ("Hematology", [
        "anemia", "anaemia", "hemophilia", "haemophilia", "thrombo",
        "coagulat", "platelet", "sickle cell", "thalassemia",
        "myelodysplastic", "bleeding disorder",
    ]),
    ("Dermatology", [
        "skin", "dermat", "eczema", "atopic", "acne", "rosacea",
        "vitiligo", "alopecia",
    ]),
    ("Ophthalmology", [
        "eye", "ocular", "retin", "glaucoma", "macular",
        "vision", "cataract", "uveitis",
    ]),
    ("Women's Health", [
        "pregnan", "menopause", "menstrual", "fertility",
        "gynecolog", "gynaecolog", "obstetric", "maternal",
        "preeclampsia", "endometrios",
    ]),
    ("Men's Health", [
        "prostate", "erectile", "testicular",
    ]),
    ("Pediatrics", [
        "pediatric", "paediatric", " children", "infant", "newborn",
        "neonatal", "adolescent",
    ]),
    ("Pain & Palliative Care", [
        "chronic pain", "analgesi", "palliat", "low back pain",
        "neuropathic pain",
    ]),
    ("Geriatric", [
        "geriatric", "elderly", "aging", "frailty",
    ]),
    ("Rare Diseases", [
        "rare disease", "orphan disease", "ultra-rare",
    ]),
    ("Musculoskeletal", [
        "osteoporos", "osteoarthrit", "tendon", "ligament",
        "fracture", "spine", "back pain", "rotator cuff",
    ]),
    ("Allergy & Immunology", [
        "allerg", "anaphyla", "immunodeficiency", "atopy",
    ]),
    ("Urology", [
        "urinary", "incontinence", "bladder", " utis",
    ]),
    ("ENT", [
        "tinnitus", "hearing loss", "sinus", "rhinit", "otitis",
    ]),
]


def derive_category(conditions: Iterable[str] | None) -> str:
    """Return the best-fit category for a CT.gov ``conditions`` list.

    The matching is case-insensitive substring against the *joined* set
    of condition strings — this gives a hit when the condition is
    written as "lung cancer", "Lung Cancer", or "non-small cell lung
    cancer (NSCLC)" without needing per-variant rules.
    """
    if not conditions:
        return GENERAL_CATEGORY
    # ``" "`` wraps the joined text so rules using leading/trailing
    # spaces (e.g. ``" hiv"``, ``" als "``) work even for the first /
    # last token in the list.
    haystack = " " + " ".join(str(c) for c in conditions if c).lower() + " "
    for category, keywords in _CATEGORY_RULES:
        if any(kw in haystack for kw in keywords):
            return category
    return GENERAL_CATEGORY


def all_categories() -> list[str]:
    """Return the canonical category list for the UI dropdown.

    Order matches the rule order so the UI can decide on its own
    sorting (alphabetic / by clinical specialty / …).
    """
    return [name for name, _ in _CATEGORY_RULES] + [GENERAL_CATEGORY]
