# Page axes: what the next training round needs

Written 2026-10-16, after the first v24 page-axes model went in
([PAGE_AXES_MODEL.md](PAGE_AXES_MODEL.md)). Counts are labelled pages in
`training/bert-training/data/training_data.jsonl` (1,012 pages from **26
charts**); scores are that model's test split (162 pages), from
`output/page-axes/category_wise_metrics.csv`.

## The short version

1. **More charts, not just more pages.** The split is by chart, and 26 charts
   is the real limit: most classes come from two or three charts, so the model
   learns those charts' templates. Every new class sample should come from a
   chart not yet in the set where possible.
2. **Aim for 40 labelled pages per class, from at least 3 charts.** About 70%
   of them land in training. In this run the four L1s scoring under 0.45
   (Order and Requisition, Consent Form, Authorization, OTHER) had 15–17
   training pages each. With 3–8 test pages per small class these scores
   swing a lot, so 40 is a floor to measure against, not a guarantee.
3. **Specialty is the biggest gap.** 774 of 1,012 pages (76%) are labelled
   `Unknown`, 13 of the 30 specialties have no page at all, and the model
   asserts a specialty only with ≥ 10 training pages behind it.
4. **L3 is decided by the keyword canon today** and stays that way until its
   classes have data: only 2 of 60 L3 classes have 20+ pages.

The cut-offs `train_axes.py` uses to put a class in the model at all:
L1 ≥ 11 pages, L2 / L3 / specialty ≥ 3. Below that a class is folded into
`OTHER` / `… > Others` or dropped, and the canon covers it.

---

## 1. L1 — priority

| L1 | Pages now | Test F1 | Need |
|---|---:|---:|---|
| Order and Requisition | 21 | 0.25 | +20 |
| Authorization | 22 | 0.20 | +20 |
| Consent Form | 23 | 0.40 | +20 |
| Problem List | 14 | 0.50 | +25 |
| Administrative | 29 | 0.57 | +15 |
| Operative Note | 23 | 0.80 (3 test pages) | +15 |
| Procedure Note | 10 | not trained (folded into OTHER) | +30 |
| Immunization | 7 | not trained (folded into OTHER) | +30 |
| Others | 8 | OTHER 0.36 | +15, varied |

Healthy, no action: Progress Note (366, 0.84), Lab Report/Result (282, 0.73),
Discharge Summary (54), Demographics (45), Forms and Questionnaires (45),
Patient Communication (35), Medication Record (28).

Lab Report/Result has plenty of pages but only 0.69 recall. Of its 45 test
pages, 4 went to Order and Requisition, 4 to OTHER, 3 to Progress Note and 2
to Authorization; 5 Progress Note pages went the other way. More Order and
Requisition pages (lab orders vs lab results), plus more **Progress Note >
Lab Results / Imaging Results** pages (16 and 1 today), teach those boundaries.

## 2. L2 — priority

In the model, but wrong on the test pages, or with too few test pages to
score at all (the 26-chart split left them no test page):

| L2 | Pages now | Test | Need |
|---|---:|---|---|
| Demographics > Face Sheet | 15 | no test page; 2 of 8 Patient Demographics pages predicted Face Sheet | +25 |
| Forms and Questionnaires > Assessment | 15 | 1 test page, predicted Questionnaire; 2 Questionnaires predicted Assessment | +25 |
| Discharge Summary > Discharge Instruction / Transfer of Care | 5 / 5 | 2 test pages each, both wrong | +35 each |
| Lab Report/Result > Diagnostic Study | 120 | precision 0.36: 4 Laboratory and 3 Radiology pages predicted Diagnostic Study; check those labels first | — |
| Lab Report/Result > Pathology | 16 | no test page | +25 |
| Consent Form > HIPAA Form | 10 | no test page | +30 |
| Patient Communication > Patient Message / Provider Letter / Notice | 9 / 5 / 4 | no test page | +30 each |
| Discharge Summary > Short Stay Summary | 3 | no test page | +35 |

Not in the model, few or no pages. Collect only what actually turns up in
charts. Zero here may mean the documents are rare, not that they were missed:

- **1–5 pages:** Operative Note > Triage Report (5), Critical Care Record (1);
  Medication Record > Refill Record (2), Prescription Page (1); Authorization >
  Release of Information (5), Prior Authorization Request (1); Consent Form >
  Immunization Consent (3), Patient Agreement (1); Administrative >
  Appointment Record (3); Discharge Summary > Discharge Medication (1),
  Interim Summary (1); every Procedure Note L2 (≤ 5 each).
- **0 pages (16):** Operative Note > Anesthesia Record, Procedure; Procedure
  Note > Elastography, Infusion and Chemotherapy, Injection Visit, Knee /
  Laser / Lumbar / Office / Pathology Procedure; Medication Record > Blood
  Transfusion Record, Medication Reconciliation; Order and Requisition >
  Laboratory Requisition; Consent Form > Surgical Consent, Transfusion Consent;
  Authorization > Assignment of Benefits.

Progress Note L2 is the content section, decided by section tagging and not
trained, so it is not on this list.

## 3. L3

The canon decides L3 (`l3_canon_min` 0.50), so this is the longer-term list.
To let the model take over, the common report types need ~30 pages each:

| L3 | Pages now |
|---|---:|
| Radiology > CT Scan Report | 32 |
| Diagnostic Study > Echocardiogram Report | 21 |
| Diagnostic Study > Cardiac Catheterization / Electrocardiogram | 12 / 12 |
| Diagnostic Study > Holter Monitor Report | 11 |
| Laboratory > Hematology / Allergy Testing | 11 / 11 |
| Radiology > Ultrasound Report | 11 |
| Diagnostic Study > Colonoscopy Report | 10 |
| Radiology > Mammogram / X-Ray Report | 9 / 8 |
| Laboratory > Urinalysis Report | 8 |
| Pathology > Biopsy Report | 7 |
| Radiology > MRI Report | 4 |
| Laboratory > Chemistry Report | 3 |

11 L3 classes with 1–2 pages were folded into `… > Others` (EGD, Stress Test,
Carotid Duplex, Nuclear Medicine, INR/PT, Genetic Test, …), and **19 have no
page at all** (Bronchoscopy, Cystoscopy, EEG, CT / MR Angiography, Myocardial
Perfusion, Sigmoidoscopy, …).

## 4. Specialty — the biggest gap

| Specialty | Pages now | In the model? | Asserted in the pipeline? (≥ 10 training pages) |
|---|---:|---|---|
| Cardiology | 62 | yes, F1 0.67 | yes |
| Emergency | 24 | yes (1 test page, missed) | yes |
| Obstetrics and Gynecology | 22 | yes (no test page) | yes |
| Gastroenterology | 20 | yes | yes |
| Oncology / Ophthalmology | 19 / 19 | yes | Ophthalmology yes, Oncology no (9 train) |
| Behavioral Health | 17 | yes | yes |
| Pathology | 12 | yes | yes |
| Sleep Study | 10 | yes | yes |
| Nursing / Audiology / Anesthesia | 9 / 7 / 5 | yes | no |
| Critical Care, Orthopedics, Radiology, Wound Care | 2 each | no | no |
| Dialysis, Nephrology, Neurology, Rehabilitation | 1 each | no | no |
| Primary Care, Surgery, Pulmonology, Dermatology, Urology, Home Health, Infusion, Occupational Health, Palliative Care, Others | **0** | no | no |

What to do:

1. **Relabel before collecting.** Many of the 774 `Unknown` pages carry a
   service line (letterhead, department, provider credentials). A specialty
   pass over the pages already labelled is the cheapest gain. **Primary Care at
   0** is almost certainly a labelling gap, not an absence.
2. Then **30+ pages for each specialty** that appears in the charts. Start
   with Primary Care, Surgery, Pulmonology, Neurology, Orthopedics,
   Nephrology and Urology, which are common in risk-adjustment charts.
3. Keep `Unknown` for pages with genuinely no signal. Until the model asserts
   a specialty, the pipeline writes `Unknown` (`imaging_final.specialty`).
   There is no ground truth for specialty, so a labelled sample is also the
   only way to measure it.

## 5. Not needed

- **Codeability.** The pipeline takes it from the canon only, never from the
  model (its `needs_review` class scores 0.36, and it doesn't matter).
- **Progress Note sections.** Section tagging decides them, not the model.

## After the next round

Retrain (`train_axes.py`), rerun `tune_thresholds.py` on the new
`predictions_val.csv`, then install the model ([PAGE_AXES_MODEL.md §2–4](PAGE_AXES_MODEL.md)).
Recount with the snippet below, and rewrite this note against the new numbers.

```bash
cd training/bert-training
python - <<'EOF'
import json, collections
recs = [json.loads(l) for l in open("data/training_data.jsonl")]
print(len(recs), "pages,", len({r["chart_id"] for r in recs}), "charts")
for axis in ("L1", "specialty"):
    print(axis, collections.Counter(r[axis] or "Unknown" for r in recs).most_common())
EOF
```
