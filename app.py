"""Annotation demo: clinical file on the left, observation / medication /
measurement / donor-level forms on the right.

Run:  pip install flask && python app.py   ->  http://127.0.0.1:5000

Donor files live in donors/<donor_id>/*.txt (a small header, a line with ---,
then the free text). Annotations are saved per donor and rater in data/.
"""
import csv
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
DONORS = BASE / "donors"
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)

# --------------------------------------------------------------------------
# Controlled vocabulary, read from the codebook CSV (feature set v3)
# --------------------------------------------------------------------------
# The item list, scales, donor-level fields and derived-variable definitions
# all come from the codebook. Only the reference lists that the codebook
# names but does not spell out (diagnoses, treatments, instruments) and the
# structural record fields stay in this file.

CODEBOOK = Path(os.environ.get("CODEBOOK", BASE / "codebook" / "feature_set_v3.csv"))
CODE_RE = re.compile(r"(?:^|,\s*|;\s*|\s(?=\d[a-z]?\s*[:=]))(\d[a-z]?|NA|NE|ND)(?:\s*[:=]\s*|(?=\s*(?:,|;|$|\()))")
NO_SCALE = "Severity is not used for this item: it is saved as 1 (documented)."
OBS_TYPES = {"observation": "symptom", "observation (substance)": "substance", "observation (event)": "event"}
FIELD_TARGETS = {"ideation": "suicidal_ideation", "attempt": "suicide_attempt", "NSSI": "non_suicidal_self_injury"}


ICD10_CHAPTERS = [
    "1: certain infectious and parasitic diseases", "2: neoplasms",
    "3: diseases of the blood and immune mechanism", "4: endocrine, nutritional and metabolic diseases",
    "5: mental and behavioural disorders", "6: diseases of the nervous system", "7: diseases of the eye and adnexa",
    "8: diseases of the ear and mastoid process", "9: diseases of the circulatory system",
    "10: diseases of the respiratory system", "11: diseases of the digestive system",
    "12: diseases of the skin and subcutaneous tissue", "13: diseases of the musculoskeletal system and connective tissue",
    "14: diseases of the genitourinary system", "15: pregnancy, childbirth and the puerperium",
    "16: certain conditions originating in the perinatal period",
    "17: congenital malformations, deformations and chromosomal abnormalities",
    "18: symptoms, signs and abnormal findings not elsewhere classified",
    "19: injury, poisoning and certain other consequences of external causes",
    "20: external causes of morbidity and mortality",
]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def parse_scale(opts):
    """'0: absent, 1: mild (…), ND' -> [{'v': '0', 't': '0: absent'}, …]; [] when the options are not coded."""
    hits = list(CODE_RE.finditer(opts))
    if not hits or hits[0].start() > 0:
        return []
    out = []
    for h, nxt in zip(hits, hits[1:] + [None]):
        text = opts[h.start():nxt.start() if nxt else len(opts)].strip(" ,;")
        out.append({"v": h.group(1), "t": text})
    return out


def parse_list(opts):
    """Uncoded option lists, e.g. 'Outcome: granted, refused, …' -> ('Outcome', [...])."""
    head, sep, rest = opts.partition(":")
    label = head.strip() if sep and "," not in head and len(head) < 25 else ""
    body = rest if label else opts
    parts = [p.strip() for p in re.split(r",(?![^(]*\))", body) if p.strip()]
    return label, parts if len(parts) > 1 else []


def load_codebook(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    items, fields, donor_fields, derived, nodes = [], [], [], [], {}
    for r in rows:
        var = r.get("Preferred Label") or r.get("Variable", "")
        cat, table, opts = r["Category"], r["Table"], r["Options"]
        rid = r.get("Class ID") or r.get("ID") or slug(var)
        r["Variable"] = var
        nodes[rid] = {"label": var, "parent": r.get("Parents", ""), "category": cat}
        if "removed" in (r.get("Status vs v2"), r["Status vs old sheet"]) or cat.startswith(("Record fields", "Document fields")):
            continue
        if table in OBS_TYPES and r.get("Selectable", "yes") != "no":
            scale = parse_scale(opts)
            label, choices = ("", []) if scale else parse_list(opts)
            if choices and (label or r["Type"].lower().startswith(("multi", "categorical"))):
                scale = [{"v": c, "t": c} for c in choices] + ([] if "ND" in choices else [{"v": "ND", "t": "ND"}])
            items.append({"id": rid, "label": var, "domain": cat, "type": OBS_TYPES[table],
                          "definition": r["Definition"], "anchors": opts if scale else NO_SCALE,
                          "scale": scale, "scale_label": label or ("Value" if scale and not scale[0]["v"].isdigit() else "Severity"),
                          "multi": r["Type"].lower().startswith("multi"),
                          "needs_label": "(specify)" in var or r["Type"].lower().startswith("free text")
                          or r["Type"].lower().endswith("+ label"),
                          "parent": r.get("Parents", ""), "rollup": r.get("Rolls up to parent", "") != "no",
                          "synonyms": r.get("Synonyms", ""), "extra": []})
        elif table.startswith("observation (") and "field" in table:
            fields.append(r)
        elif table.startswith("donor"):
            section = "Summary layer (to validate the derived values against)" if "summary" in table else cat
            if var.startswith("Lifetime-worst summary"):
                for key, name in [("depressive", "depressive symptoms"), ("manic", "manic symptoms"),
                                  ("psychotic", "psychotic symptoms"), ("anxiety", "anxiety / OCD / trauma"),
                                  ("suicidality", "suicidality")]:
                    donor_fields.append({"section": section, "id": f"lw_{key}", "label": f"Lifetime-worst: {name} (0-3)",
                                         "kind": "select", "options": ["0", "1", "2", "3", "ND"], "definition": r["Definition"]})
                continue
            scale = parse_scale(opts)
            _, choices = ("", []) if scale else parse_list(opts)
            f = {"section": section, "id": rid, "label": var, "definition": r["Definition"]}
            if "diagnosis" in var.lower():
                f.update(kind="text", list="dx_list")
            elif scale:
                f.update(kind="select", options=[s["t"] for s in scale])
            elif r["Type"].lower().startswith("categorical") and choices:
                f.update(kind="select", options=choices)
            elif r["Type"].lower().startswith("numeric"):
                f.update(kind="number")
            else:
                f.update(kind="text")
            donor_fields.append(f)
            if "ICD-10 chapter" in opts:
                donor_fields.append({"section": section, "id": rid + "_icd10_chapter", "label": var + " - ICD-10 chapter",
                                     "kind": "select", "options": ICD10_CHAPTERS + ["ND"], "definition": ""})
        elif table == "derived":
            derived.append({"variable": var, "category": cat, "rule": r["Derivation rule"]})

    for i in items:                                  # depth within its category, for the indented drop-down
        d, p = 0, i["parent"]
        while p in nodes and nodes[p]["category"] == i["domain"]:
            d, p = d + 1, nodes[p]["parent"]
        i["depth"] = d
        i["path"] = []
        p = i["parent"]
        while p in nodes:
            i["path"].insert(0, nodes[p]["label"])
            p = nodes[p]["parent"]
    children = {}                                    # tree order: each class directly after its parent
    for nid, n in nodes.items():
        children.setdefault(n["parent"], []).append(nid)
    rank = []

    def walk(nid):
        rank.append(nid)
        for k in children.get(nid, []):
            walk(k)
    for root in children.get("", []):
        walk(root)
    items.sort(key=lambda i: rank.index(i["id"]))
    order = list(dict.fromkeys(f["section"] for f in donor_fields))   # one block per section, in codebook order
    donor_fields.sort(key=lambda f: order.index(f["section"]))
    by_id = {i["id"]: i for i in items}
    for r in fields:
        var, table = r["Variable"], r["Table"]
        scale = parse_scale(r["Options"])
        if table == "observation (event field)":            # e.g. 'Suicide attempt - medical severity'
            base = by_id.get(slug(var.split(" - ")[0]))
            if base and scale:
                base.update(scale=scale, anchors=r["Options"], scale_label=var.split(" - ", 1)[1].capitalize())
            continue
        targets = re.search(r"field on (.+)\)", table).group(1)
        _, choices = parse_list(r["Options"])
        extra = {"id": slug(var), "label": var, "multi": r["Type"].lower().startswith("multi"),
                 "choices": [s["t"] for s in scale] if scale else choices}
        for word, item_id in FIELD_TARGETS.items():
            if re.search(rf"\b{word}\b", targets) and item_id in by_id:
                by_id[item_id]["extra"].append(extra)
    return items, donor_fields, derived


ITEMS, DONOR_FIELDS, DERIVED = load_codebook(CODEBOOK)
ITEM_IDS = {i["id"] for i in ITEMS}
CODEBOOK_VERSION = CODEBOOK.stem

DIAGNOSES = [
    "schizophrenia", "schizoaffective disorder", "brief psychotic disorder", "delusional disorder",
    "bipolar i disorder", "bipolar ii disorder", "cyclothymic disorder",
    "major depressive disorder", "persistent depressive disorder", "generalized anxiety disorder",
    "panic disorder", "agoraphobia", "social anxiety disorder", "specific phobia", "obsessive-compulsive disorder",
    "posttraumatic stress disorder", "anorexia nervosa", "bulimia nervosa", "binge-eating disorder",
    "paranoid personality disorder", "schizoid personality disorder", "schizotypal personality disorder",
    "antisocial personality disorder", "borderline personality disorder", "histrionic personality disorder",
    "narcissistic personality disorder", "avoidant personality disorder", "dependent personality disorder",
    "obsessive-compulsive personality disorder", "other specified personality disorder",
    "autism spectrum disorder", "attention-deficit/hyperactivity disorder", "intellectual disability",
    "tic disorder", "tourette's disorder",
    "alcohol use disorder", "cannabis use disorder", "other substance use disorder",
    "dementia - alzheimer", "dementia - vascular", "dementia - frontotemporal", "dementia - lewy body",
    "dementia - other", "dementia - unspecified", "mild neurocognitive disorder", "delirium",
    "adjustment disorder", "overspannenheid (non-DSM, as written)",
]

DRUGS = [
    ("sertraline", "SSRI"), ("citalopram", "SSRI"), ("escitalopram", "SSRI"),
    ("fluoxetine", "SSRI"), ("paroxetine", "SSRI"), ("fluvoxamine", "SSRI"),
    ("venlafaxine", "SNRI"), ("duloxetine", "SNRI"),
    ("mirtazapine", "other antidepressant"), ("bupropion", "other antidepressant"),
    ("amitriptyline", "TCA"), ("nortriptyline", "TCA"), ("clomipramine", "TCA"),
    ("lithium", "mood stabiliser"), ("valproate", "mood stabiliser"),
    ("carbamazepine", "mood stabiliser"), ("lamotrigine", "mood stabiliser"),
    ("haloperidol", "antipsychotic"), ("risperidone", "antipsychotic"),
    ("olanzapine", "antipsychotic"), ("quetiapine", "antipsychotic"),
    ("aripiprazole", "antipsychotic"), ("clozapine", "antipsychotic"),
    ("flupentixol", "antipsychotic"), ("zuclopenthixol", "antipsychotic"),
    ("perphenazine", "antipsychotic"), ("pipamperone", "antipsychotic"),
    ("diazepam", "benzodiazepine / hypnotic"), ("oxazepam", "benzodiazepine / hypnotic"),
    ("lorazepam", "benzodiazepine / hypnotic"), ("temazepam", "benzodiazepine / hypnotic"),
    ("zolpidem", "benzodiazepine / hypnotic"),
    ("methylphenidate", "stimulant / ADHD"), ("atomoxetine", "stimulant / ADHD"),
    ("esketamine", "rapid-acting antidepressant"),
    ("ECT (electroconvulsive therapy)", "neurostimulation"),
    ("CBT", "psychotherapy"), ("IPT", "psychotherapy"), ("psychodynamic therapy", "psychotherapy"),
    ("EMDR", "psychotherapy"), ("schema therapy", "psychotherapy"), ("family / system therapy", "psychotherapy"),
    ("supportive therapy", "psychotherapy"), ("other psychotherapy", "psychotherapy"),
    ("day treatment / partial hospitalisation", "intensified treatment"),
    ("intensive home treatment (IHT, FACT intensified)", "intensified treatment"),
    ("high-intensity outpatient programme", "intensified treatment"),
]
DRUG_CLASS = dict(DRUGS)
ANTIDEPRESSANT_CLASSES = {"SSRI", "SNRI", "other antidepressant", "TCA"}

INSTRUMENTS = ["GAF", "HDRS-17", "MADRS", "YMRS", "PANSS", "CGI-S", "MMSE", "MoCA",
               "neuropsychological assessment", "IQ (WAIS/WISC)", "BMI", "weight (kg)", "other (specify)"]

# Summary-layer fields and the codebook categories their derived peak is taken from.
VALIDATION_MAP = {
    "lw_depressive": ["Depressive symptoms"], "lw_manic": ["Manic symptoms"], "lw_psychotic": ["Psychotic symptoms"],
    "lw_anxiety": ["Anxiety symptoms", "Obsessive-compulsive symptoms", "Trauma-related symptoms"],
    "lw_suicidality": ["Suicidality", "Self-harm"],
}
# Categories whose events still count towards the domain overview.
EVENT_DOMAINS = {"Suicidality", "Self-harm", "Disordered eating"}

VOCAB = {
    "version": CODEBOOK_VERSION,
    "types": ["symptom", "diagnosis", "event", "substance"],
    "items": ITEMS,
    "diagnoses": DIAGNOSES,
    "systems": ["DSM-5-TR", "DSM-5", "DSM-IV-TR", "DSM-IV", "DSM-III-R", "DSM-III", "DSM-II",
                "ICD-10", "ICD-9", "none stated", "other"],
    "certainty": {
        "symptom": ["present", "suspected", "explicitly absent"],
        "event": ["present", "suspected", "explicitly absent"],
        "substance": ["present", "suspected", "explicitly absent"],
        "diagnosis": ["established", "working hypothesis", "suspected", "ruled out"],
    },
    "asserted_by": [
        {"v": "gp", "t": "GP"}, {"v": "psychiatrist", "t": "psychiatrist"},
        {"v": "psychologist", "t": "psychologist"}, {"v": "nurse", "t": "nurse / care staff"},
        {"v": "patient", "t": "patient (self-report)"}, {"v": "relative", "t": "relative"},
        {"v": "other", "t": "other (specify)"},
    ],
    "author_map": {"huisarts": "gp", "psychiater": "psychiatrist", "psycholoog": "psychologist",
                   "verpleegkundige": "nurse"},
    "periods": ["childhood, unspecified", "adolescence, unspecified", "lifelong"],
    "changes": ["none stated", "new", "worse", "better", "resolved", "stable", "fluctuating"],
    "drugs": [{"name": n, "cls": c} for n, c in DRUGS],
    "stop_reasons": ["not stopped / ongoing", "planned end", "no effect", "side effects",
                     "patient choice", "switched", "unknown"],
    "effects": ["0: no response or deterioration", "1: minimal response",
                "2: clear response without remission", "3: remission",
                "NE: not evaluable", "ND: effect not documented"],
    "instruments": INSTRUMENTS,
    "donor_fields": DONOR_FIELDS,
    "derived": DERIVED,
}
# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def parse_doc(path: Path):
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    head, _, body = raw.partition("\n---\n")
    meta = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    return meta, body.strip()


def donor_ids():
    if not DONORS.exists():
        return []
    return sorted(p.name for p in DONORS.iterdir() if p.is_dir())


def list_docs(donor):
    out = []
    for p in sorted((DONORS / donor).glob("*.txt")):
        meta, _ = parse_doc(p)
        out.append({"id": p.stem, "date": meta.get("datum", ""), "type": meta.get("type", ""),
                    "author": meta.get("auteur", "")})
    out.sort(key=lambda d: (d["date"], d["id"]))
    return out


def check_donor(donor):
    if donor not in donor_ids():
        abort(404)


def rater():
    r = re.sub(r"[^a-zA-Z0-9_-]", "", request.args.get("rater", ""))[:30]
    return r or "rater1"


def ann_path(donor, r):
    return DATA / f"{donor}__{r}.json"


def empty_ann(donor, r):
    return {"donor": donor, "rater": r, "codebook_version": CODEBOOK_VERSION,
            "observations": [], "medications": [], "measurements": [],
            "doc_flags": {}, "donor_level": {}}


# Item ids from codebook demo-0.1 that were renamed or merged later (v3 ids are the v2 ids plus new classes).
LEGACY_IDS = {
    "anhedonia": "anhedonia_loss_of_interest", "sleep_disturbance": "sleep_disturbance_insomnia_hypersomnia",
    "fatigue": "fatigue_loss_of_energy", "elevated_mood": "elevated_expansive_mood",
    "decreased_need_sleep": "decreased_need_for_sleep", "hallucinations_other": "hallucinations",
    "disorganisation": "disorganisation_symptoms",
    "negative_symptoms_stable": "negative_symptoms_during_clinical_stability", "catatonia": "catatonia_catatonic_features",
    "impaired_reality_testing": "reality_testing",
    "tardive_dyskinesia": "tardive_dyskinesia_or_persistent_extrapyramidal_symptoms",
    "avoidance": "avoidance_driven_functional_restriction", "generalised_worry": "generalised_worry_anxiety",
    "hyperarousal": "hyperarousal_startle", "dissociation": "dissociation_depersonalisation_derealisation",
    "nssi": "non_suicidal_self_injury", "purging": "purging_self_induced_vomiting",
    "laxative_misuse": "laxative_or_diuretic_misuse", "tube_feeding": "tube_feeding_or_involuntary_feeding",
    "memory_impairment": "memory_and_learning_impairment",
    "attention_impairment": "complex_attention_processing_speed_impairment",
    "cognitive_complaints_post_ect": "persistent_cognitive_complaints_after_ect", "insociability": "in_sociability",
    "other_symptom": "other_symptom_specify", "stimulants": "amfetamines_stimulants",
    "involuntary_admission": "involuntary_admission_cm_zm_rm_ibs", "assisted_living": "assisted_sheltered_living",
    "adult_trauma": "other_trauma_specify", "other_event": "other_event_specify",
    "sexual_abuse": "sexual_abuse_sexual_violence",
    "parental_psychiatric_illness": "parental_psychiatric_illness_or_substance_use",
    "parental_loss_separation": "parental_loss_or_separation",
}


def load_ann(donor, r):
    p = ann_path(donor, r)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            base = empty_ann(donor, r)
            base.update(data)
            for o in base["observations"]:
                if o.get("item") in LEGACY_IDS and o["item"] not in ITEM_IDS:
                    o["legacy_item"], o["item"] = o["item"], LEGACY_IDS[o["item"]]
            return base
        except json.JSONDecodeError:
            pass
    return empty_ann(donor, r)


# --------------------------------------------------------------------------
# Derivations
# --------------------------------------------------------------------------


def yr(s):
    m = re.search(r"(1[89]\d\d|20\d\d)", str(s or ""))
    return int(m.group(1)) if m else None


def sev_num(v):
    v = str(v if v is not None else "")
    return int(v) if v.isdigit() else None


def summarize(donor, ann):
    docs = list_docs(donor)
    date_of = {d["id"]: d["date"] for d in docs}
    flags = ann.get("doc_flags", {})
    relevant = {d["id"] for d in docs if not flags.get(d["id"], {}).get("no_relevant")}
    meta = {i["id"]: i for i in ITEMS}
    obs = ann.get("observations", [])

    # ---- per item / diagnosis -------------------------------------------------
    groups = {}
    for o in obs:
        it = meta.get(o.get("item"), {})
        label = (o.get("label") or "").strip().lower() if (o["type"] == "diagnosis" or it.get("needs_label")) else ""
        key = (o["type"], o["item"], label)
        g = groups.setdefault(key, {
            "type": o["type"], "item": o["item"], "label": label,
            "domain": it.get("domain", "Diagnoses"),
            "name": (o.get("label") or "diagnosis") if o["type"] == "diagnosis"
            else it.get("label", o["item"]) + (f" ({label})" if label else ""),
            "n_present": 0, "n_suspected": 0, "n_absent": 0, "docs": set(), "peak": None,
            "first_periods": [], "doc_years": [], "first_stated": [], "changes": [], "new_evidence": 0,
        })
        cert = o.get("certainty")
        if cert in ("present", "established"):
            g["n_present"] += 1
            g["docs"].add(o["doc"])
            s = sev_num(o.get("severity"))
            if s is not None and (g["peak"] is None or s > g["peak"]):
                g["peak"] = s
            y = yr(o.get("period_from"))
            if y:
                g["first_periods"].append(y)
            dy = yr(date_of.get(o["doc"]))
            if dy:
                g["doc_years"].append(dy)
            if o.get("recap") != "recapitulated from earlier":
                g["new_evidence"] += 1
        elif cert in ("suspected", "working hypothesis"):
            g["n_suspected"] += 1
        elif cert in ("explicitly absent", "ruled out"):
            g["n_absent"] += 1
        if o.get("first_obs"):
            g["first_stated"].append(f"{date_of.get(o['doc'], '?')}: {o.get('period_from') or 'no period given'}")
        if o.get("change") and o["change"] != "none stated":
            g["changes"].append(f"{date_of.get(o['doc'], '?')}: {o['change']}")

    items_out = []
    for g in groups.values():
        first_p = min(g["first_periods"]) if g["first_periods"] else (min(g["doc_years"]) if g["doc_years"] else None)
        items_out.append({
            "type": g["type"], "domain": g["domain"], "name": g["name"],
            "n_present": g["n_present"], "n_suspected": g["n_suspected"], "n_absent": g["n_absent"],
            "n_docs": len(g["docs"]), "new_evidence_rows": g["new_evidence"], "peak": g["peak"],
            "first_year": first_p,
            "first_year_basis": "stated period" if g["first_periods"] else "first document",
            "last_doc_year": max(g["doc_years"]) if g["doc_years"] else None,
            "first_stated": g["first_stated"], "changes": g["changes"],
        })
    items_out.sort(key=lambda r: (r["domain"], r["name"]))

    # ---- domain overview (symptoms only) --------------------------------------
    domains = {}
    for o in obs:
        it = meta.get(o.get("item"), {})
        in_domain = o["type"] == "symptom" or it.get("domain") in EVENT_DOMAINS
        if not in_domain or not it.get("rollup", True) or o.get("certainty") != "present":
            continue
        d = domains.setdefault(it.get("domain", "?"), {"docs": set(), "peak": None, "years": []})
        d["docs"].add(o["doc"])
        s = sev_num(o.get("severity"))
        if s is not None and (d["peak"] is None or s > d["peak"]):
            d["peak"] = s
        y = yr(o.get("period_from")) or yr(date_of.get(o["doc"]))
        if y:
            d["years"].append(y)
    domains_out = []
    for name, d in domains.items():
        n_rel = len(relevant) or 1
        domains_out.append({"domain": name, "peak": d["peak"], "docs_present": len(d["docs"] & relevant),
                            "docs_relevant": len(relevant),
                            "share": round(len(d["docs"] & relevant) / n_rel, 2),
                            "first_year": min(d["years"]) if d["years"] else None})
    domains_out.sort(key=lambda r: r["domain"])

    # ---- class hierarchy: each parent class rolls up its own rows and its subclasses
    # A Boolean parent (0/1 scale) is 1 when any rolled-up row is above 0.
    kids = {}
    for i in ITEMS:
        kids.setdefault(i["parent"], []).append(i["id"])

    def descendants(cid):
        out = []
        for k in kids.get(cid, []):
            if meta[k]["rollup"]:
                out += [k] + descendants(k)
        return out

    hierarchy = []
    for i in ITEMS:
        sub = descendants(i["id"])
        if not sub:
            continue
        ids = {i["id"], *sub}
        rows = [o for o in obs if o.get("item") in ids and o.get("certainty") == "present"]
        if not rows:
            continue
        sevs = [s for s in (sev_num(o.get("severity")) for o in rows) if s is not None]
        boolean = [c["v"] for c in i["scale"] if c["v"].isdigit()] == ["0", "1"]
        peak = (1 if any(v > 0 for v in sevs) else 0) if boolean and sevs else (max(sevs) if sevs else None)
        named = sorted({meta[o["item"]]["label"] for o in rows if o["item"] != i["id"]})
        hierarchy.append({"name": i["label"], "domain": i["domain"], "peak": peak,
                          "n_docs": len({o["doc"] for o in rows}),
                          "n_unspecified": sum(o["item"] == i["id"] for o in rows), "subclasses": named})

    # ---- medication ------------------------------------------------------------
    meds = sorted(ann.get("medications", []), key=lambda m: (yr(m.get("start")) or 9999, m.get("drug", "")))
    by_class = {}
    ad_fail = 0
    for m in meds:
        name = m.get("drug_other") if str(m.get("drug", "")).startswith("other") else m.get("drug")
        cls = DRUG_CLASS.get(m.get("drug"), "other / unclassified")
        by_class[cls] = by_class.get(cls, 0) + 1
        if cls in ANTIDEPRESSANT_CLASSES and str(m.get("effect", ""))[:1] in ("0", "1") \
                and m.get("adequate") != "no":
            ad_fail += 1
        m["_name"], m["_class"] = name, cls
    meds_out = [{"name": m["_name"], "class": m["_class"], "start": m.get("start"), "stop": m.get("stop"),
                 "dose": m.get("dose"), "effect": m.get("effect"), "stop_reason": m.get("stop_reason"),
                 "doc_date": date_of.get(m.get("doc"), "")} for m in meds]

    # ---- coverage / progress ---------------------------------------------------
    per_year = {}
    for d in docs:
        y = yr(d["date"])
        if y:
            per_year[y] = per_year.get(y, 0) + 1
    years = sorted(per_year)
    gaps = [{"from": a, "to": b, "years": b - a} for a, b in zip(years, years[1:]) if b - a >= 3]
    touched = {x["doc"] for k in ("observations", "medications", "measurements") for x in ann.get(k, [])}
    todo = [d for d in docs if d["id"] not in touched and d["id"] not in {k for k, v in flags.items() if v.get("no_relevant")}]

    # ---- validation of derived peaks against the summary layer ----------------
    dl = ann.get("donor_level", {})
    peaks = {d["domain"]: d["peak"] for d in domains_out}
    validation = []
    for field, doms in VALIDATION_MAP.items():
        rv = (dl.get(field) or {}).get("value", "")
        if rv == "":
            continue
        dom = " / ".join(doms)
        found = [peaks[d] for d in doms if peaks.get(d) is not None]
        dv = max(found) if found else None
        match = None if (rv == "ND" or dv is None) else (str(dv) == rv)
        validation.append({"field": field, "domain": dom, "rater": rv, "derived": dv, "match": match})

    return {
        "n_docs": len(docs), "n_relevant": len(relevant), "n_observations": len(obs),
        "n_medications": len(meds), "n_measurements": len(ann.get("measurements", [])),
        "todo": [{"id": d["id"], "date": d["date"], "type": d["type"]} for d in todo],
        "domains": domains_out, "items": items_out, "medications": meds_out,
        "med_classes": by_class, "antidepressant_nonresponse": ad_fail,
        "docs_per_year": [{"year": y, "n": per_year[y]} for y in years], "gaps": gaps,
        "validation": validation, "hierarchy": hierarchy,
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/vocab")
def vocab():
    return jsonify(VOCAB)


@app.get("/api/donors")
def donors():
    return jsonify([{"id": d, "docs": list_docs(d)} for d in donor_ids()])


@app.get("/api/doc/<donor>/<doc_id>")
def get_doc(donor, doc_id):
    check_donor(donor)
    if not re.fullmatch(r"[\w.-]+", doc_id):
        abort(400)
    p = DONORS / donor / f"{doc_id}.txt"
    if not p.exists():
        abort(404)
    meta, body = parse_doc(p)
    return jsonify({"id": doc_id, "meta": meta, "text": body})


@app.get("/api/annotations/<donor>")
def get_ann(donor):
    check_donor(donor)
    return jsonify(load_ann(donor, rater()))


@app.put("/api/annotations/<donor>")
def put_ann(donor):
    check_donor(donor)
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        abort(400)
    r = rater()
    ann = empty_ann(donor, r)
    for k in ("observations", "medications", "measurements"):
        if isinstance(payload.get(k), list):
            ann[k] = payload[k]
    for k in ("doc_flags", "donor_level"):
        if isinstance(payload.get(k), dict):
            ann[k] = payload[k]
    ann["saved_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = ann_path(donor, r).with_suffix(".tmp")
    tmp.write_text(json.dumps(ann, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ann_path(donor, r))
    return jsonify({"ok": True, "saved_at": ann["saved_at"]})


@app.get("/api/summary/<donor>")
def get_summary(donor):
    check_donor(donor)
    return jsonify(summarize(donor, load_ann(donor, rater())))


@app.get("/api/export/<donor>/<table>.csv")
def export(donor, table):
    check_donor(donor)
    if table not in ("observations", "medications", "measurements"):
        abort(404)
    ann = load_ann(donor, rater())
    date_of = {d["id"]: d["date"] for d in list_docs(donor)}
    rows = []
    for row in ann[table]:
        row = dict(row)
        row["donor"], row["rater"] = donor, ann["rater"]
        row["doc_date"] = date_of.get(row.get("doc"), "")
        row["codebook_version"] = CODEBOOK_VERSION
        rows.append(row)
    cols = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols or ["donor"])
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={donor}_{table}.csv"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
