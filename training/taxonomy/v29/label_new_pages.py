#!/usr/bin/env python3
"""Label new pages with the keyword canon and the v29 taxonomy, no model.

The trained page-axes model lives in the pipeline, not here, so these rows are
canon-decided and marked as such in `ocr_engine` / `canon_suggests`. They are
candidates for the verification tool, the same as every earlier batch.

    python3 label_new_pages.py <ocr_dir> <out_prefix>
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canon as canon_mod
import tag_sections
import detect_specialty

HERE = os.path.dirname(os.path.abspath(__file__))
OCR = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/new_ocr"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/mnt/user-data/outputs/new_batch"

TAX = json.load(open(f"{HERE}/taxonomy_v29.json"))
CANON = canon_mod.load_canon(f"{HERE}/codeable_canon.v3.json", strict=False)
SECT = tag_sections.load(f"{HERE}/sections.json", strict=False)
SPEC = detect_specialty.load(f"{HERE}/specialty.json")

FAMILY_MAP = {k: v for k, v in TAX["family_map"].items() if isinstance(v, dict)}
OVERRIDES = {k: v for k, v in TAX["page_type_overrides"].items()
             if isinstance(v, dict) and not k.startswith("_")}
L2_MERGE = TAX["L2_MERGE"] if "L2_MERGE" in TAX else TAX["L2_merge"]
PN = TAX["progress_note"]
PN_SECTION_L2 = PN.get("section_to_L2") or {}
PN_DOM = PN["dominance_order"]
CA_SIDE = TAX["consent_auth_side"]

# canon family -> L1 and the family's default L2. The canon knows what a page
# IS; the taxonomy knows where it BELONGS, and most canon entry labels are not
# page types the taxonomy names, so without this nearly everything fell to
# Others.
FAMILY_TO_L1 = {
    "progress_note": ("Progress Note", ""),
    "specialty_note": ("Progress Note", ""),
    "nursing_note": ("Progress Note", ""),
    "nutrition_note": ("Progress Note", ""),
    "behavioral_health_note": ("Progress Note", ""),
    "emergency_department_note": ("Progress Note", ""),
    "palliative_care_note": ("Progress Note", ""),
    "wound_care_note": ("Progress Note", ""),
    "eye_examination": ("Progress Note", ""),
    "obstetric_and_gynecologic_record": ("Progress Note", ""),
    "plan_of_care": ("Progress Note", "Assessment and Plan"),
    "flow_sheet": ("Progress Note", "Others"),
    "vitals_record": ("Progress Note", "Physical Exam"),
    "prehospital_record": ("Progress Note", "Others"),
    "telephone_encounter": ("Patient Communication", "Patient Message"),
    "patient_communication": ("Patient Communication", "Patient Message"),
    "provider_letter": ("Patient Communication", "Provider Letter"),
    "laboratory_report": ("Lab Report/Result", "Laboratory"),
    "pathology_report": ("Lab Report/Result", "Pathology"),
    "radiology_report": ("Lab Report/Result", "Radiology"),
    "nuclear_medicine_report": ("Lab Report/Result", "Radiology"),
    "diagnostic_study": ("Lab Report/Result", "Diagnostic Study"),
    "cardiac_diagnostic_test": ("Lab Report/Result", "Diagnostic Study"),
    "neurodiagnostic_test": ("Lab Report/Result", "Diagnostic Study"),
    "endoscopy_report": ("Lab Report/Result", "Diagnostic Study"),
    "sleep_study": ("Lab Report/Result", "Diagnostic Study"),
    "discharge_summary": ("Discharge Summary", "Depart/Discharge Note"),
    "discharge_instructions": ("Discharge Summary", "Discharge Instruction"),
    "discharge_medication": ("Discharge Summary", "Discharge Medication"),
    "interim_summary": ("Discharge Summary", "Interim Summary"),
    "transfer_of_care_summary": ("Discharge Summary", "Transfer of Care"),
    "therapy_discharge_summary": ("Discharge Summary", "Depart/Discharge Note"),
    "admission_record": ("Discharge Summary", "Others"),
    "operative_report": ("Operative Note", "Surgery Report"),
    "anesthesia_record": ("Operative Note", "Anesthesia Record"),
    "perioperative_evaluation": ("Operative Note", "Pre-operative Note"),
    "critical_care_record": ("Operative Note", "Critical Care Record"),
    "procedure_note": ("Procedure Note", ""),
    "dialysis_record": ("Procedure Note", "Dialysis Record"),
    "infusion_therapy_record": ("Procedure Note", "Infusion and Chemotherapy"),
    "therapy_note": ("Procedure Note", "Therapy Session"),
    "implant_record": ("Procedure Note", "Others"),
    "medication_record": ("Medication Record", ""),
    "problem_list": ("Problem List", ""),
    "patient_demographics": ("Demographics", "Patient Demographics"),
    "consent_form": ("Consent and Authorization", "General Consent"),
    "authorization_form": ("Consent and Authorization", "General Authorization"),
    "order_and_requisition": ("Order and Requisition", "Physician Order"),
    "screening_and_checklist": ("Forms and Questionnaires", "Questionnaire"),
    "assessment_and_evaluation": ("Forms and Questionnaires", "Assessment"),
    "appointment_record": ("Administrative", "Appointment Record"),
    "billing_statement": ("Administrative", "Billing Statement"),
    "administrative_record": ("Others", ""),
    "coding_record": ("Others", ""),
    "page_artefact": ("Others", ""),
}

# Families that win on furniture rather than content. Every page carries a
# patient banner, so patient_demographics outscores the real family on a page
# whose body OCR came out badly. It has to clear the runner-up by a margin.
WEAK_WINNERS = {"patient_demographics", "page_artefact", "administrative_record"}
WEAK_MARGIN = 3.0


def pick_family(fam_scores: dict, deciding: set) -> str:
    if not fam_scores:
        return ""
    ranked = sorted(fam_scores.items(), key=lambda kv: -kv[1])
    strong = [(f, s) for f, s in ranked if f in deciding]
    order = strong or ranked
    top = order[0]
    if top[0] in WEAK_WINNERS and len(order) > 1:
        runner = order[1]
        if top[1] - runner[1] < WEAK_MARGIN:
            return runner[0]
    return top[0]


def resolve(page_type: str, family: str, dominant_section: str | None) -> tuple[str, str, str]:
    """page type + content section -> L1, L2, L3. Mirrors the taxonomy's own rules."""
    sub = re.search(r"\(([^)]+)\)\s*$", page_type)
    for cand in (page_type, sub.group(1) if sub else None):
        ov = OVERRIDES.get(cand) if cand else None
        if ov and ov.get("L1"):
            return ov["L1"], ov.get("L2", ""), ov.get("L3", "")

    fam = FAMILY_MAP.get(page_type)
    if fam:
        l1 = fam["L1"]
        l2 = fam.get("L2") or ""
    elif family in FAMILY_TO_L1:
        l1, l2 = FAMILY_TO_L1[family]
        fam = {}
    else:
        return "Others", "", ""

    # L2 from the merge table for the L1 that owns this page type.
    # The canon returns "Consent Form (HIPAA Form)" for a subtype, so the
    # parenthetical is tried as well as the whole label: without it a HIPAA
    # page landed in General Consent.
    merge = L2_MERGE.get(l1) or {}
    key = page_type if page_type in merge else ""
    if not key:
        m = re.search(r"\(([^)]+)\)\s*$", page_type)
        if m and m.group(1) in merge:
            key = m.group(1)
        elif m:
            base = page_type[:m.start()].strip()
            key = base if base in merge else ""
    if not key and page_type in ("Authorization Form", "Consent Form"):
        key = page_type
    if key:
        tgt = merge[key]
        if tgt.startswith("__move:"):
            l1 = tgt.split(":", 1)[1]
            merge2 = L2_MERGE.get(l1) or {}
            l2 = merge2.get(key, "") or ""
        else:
            l2 = tgt

    # Progress Note takes its L2 from the page's dominant content section
    if l1 == "Progress Note":
        l2 = dominant_section if dominant_section in PN["L2_values"] else "Others"

    return l1, l2, fam.get("L3", "") or ""


def codeability(l1: str, has_signal: bool) -> tuple[str, str, str]:
    if l1 == "Discharge Summary":
        return "discharge", "l1_discharge", "every page of a discharge packet"
    if l1 in ("Progress Note", "Operative Note", "Procedure Note"):
        return ("codeable" if has_signal else "needs_review"), "section_signal", \
               ("a codeable section is present" if has_signal
                else "clinical note with no diagnosis or plan section found")
    if l1 in ("Lab Report/Result", "Immunization"):
        return "non_codeable", "l1_result", "a result is not a codeable encounter"
    return "non_codeable", "l1_default", f"{l1} carries no codeable encounter"


rows = []
for f in sorted(os.listdir(OCR)):
    if not f.endswith(".txt"):
        continue
    stem = f[:-4]
    text = open(os.path.join(OCR, f), encoding="utf-8", errors="replace").read()
    fam_scores, ent_scores, deciding, hits = CANON.score(text)
    sec = SECT.tag(text)
    sp = SPEC.tag(text)

    family = pick_family(fam_scores, deciding)
    if family:
        own = [(e, v) for e, v in ent_scores.items()
               if CANON.entries.get(e, {}).get("family") == family]
        top_entry = max(own, key=lambda kv: kv[1])[0] if own else None
        page_type = CANON.subtype_label(family, top_entry) or CANON.display_of(family)
    else:
        page_type = ""

    if len(text.strip()) < 120:
        l1, l2, l3 = "Others", "", ""
        page_type = page_type or "Page Artefact"
    else:
        l1, l2, l3 = resolve(page_type, family, sec.get("dominant"))
    cb, cb_rule, cb_why = codeability(l1, sec.get("has_codeable_signal", False))
    spec = sp["specialty"]
    if spec in ("Unspecified", "Ambiguous"):
        spec = "Unknown"

    rows.append({
        "image": stem + ".jpg", "page_id": stem, "chart_id": "", "page_index": "",
        "L1": l1, "L2": l2, "L3": l3,
        "path": " > ".join(x for x in (l1, l2, l3) if x),
        "specialty": spec,
        "codeability": cb, "codeability_rule": cb_rule, "codeability_reason": cb_why,
        "page_type": page_type, "page_family": family,
        "dominant_section": sec.get("dominant") or "",
        "sections": "; ".join(sec.get("sections", [])),
        "canon_suggests": page_type,
        "label_agrees_with_text": "unreviewed",
        "annotator_page_type": "", "annotator_page_subtype": "",
        "n_chars": len(text), "ocr_engine": "tesseract-5.3.4-candidate",
        "text": re.sub(r"[ \t]+", " ", text).strip(),
    })

with open(f"{OUT}_labels.csv", "w", newline="") as f:
    flds = [k for k in rows[0] if k != "text"]
    w = csv.DictWriter(f, fieldnames=flds, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)

with open(f"{OUT}.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps({
            "image": r["image"], "text": r["text"], "L1": r["L1"], "L2": r["L2"],
            "L3": r["L3"], "path": r["path"], "specialty": r["specialty"],
            "codeability": r["codeability"], "page_type": r["page_type"],
            "chart_id": "", "page_index": "", "n_chars": r["n_chars"],
            "L1_train": r["L1"],
            "L2_train": (r["L1"] + " > " + r["L2"]) if r["L2"] else r["L1"],
        }, ensure_ascii=False) + "\n")

import collections
print(len(rows), "pages labelled")
for k, v in collections.Counter(r["L1"] for r in rows).most_common():
    print(f"  {v:3} {k}")
print("no canon hit:", sum(1 for r in rows if not r["page_type"]))
print("empty OCR:", sum(1 for r in rows if r["n_chars"] < 50))
print("specialty found:", sum(1 for r in rows if r["specialty"] != "Unknown"))
