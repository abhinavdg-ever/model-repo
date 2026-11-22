# Confidence logic

How every confidence in the imaging pipeline is scored, as the code works today (2026-11-22).
History is in git; this page is only the current logic.

---

## 1. The rule

**Confidence = how sure we are that the value shown is correct.** One scale everywhere:

| Band | Confidence | Colour |
|---|---|---|
| **High** | 0.80 and above | green |
| **Medium** | 0.50 to under 0.80 | amber |
| **Low** | under 0.50 | red: listed in `imaging_final.review_fields` |

No confidence is shown below **0.20** (the floor; a lower value is raised to it). A field with no
value read (no DOS, nothing extracted, a default date) has **no confidence**, not a number.

1. **A model decided:** the model's own probability for the value it chose.
2. **A rule or a backup decided:** a fixed value (section 2).
3. **A rule went against the model:** a fixed low value (section 2).
4. **A page took its value from its document's anchor page:** the anchor's confidence x 0.90.

All numbers are in `core-pipeline/resources/canon/confidence_canon.json` and reload on change;
`core-pipeline/modules/confidence.py` applies them.

---

## 2. The fixed values

| Case | Confidence | Canon key |
|---|---|---|
| A model decided | the model's probability | — |
| A key / value row (KV Extract) | rule score x LightGBM `model_score`; rule score 0 -> `model_score`; no `model_score` -> 0.60 | `rules_find` |
| A rule / label found it with no model behind it | **0.60** | `rules_find` |
| A value post-processing dropped (a clinic read as the provider, a DOB far from the manifest's) | **0.60**, or its own score if lower | `dropped_value` |
| A backup that is not a label (signature date, no model available, keywords only) | **0.40** | `backup_low` |
| A rule went against the model's top answer | **0.30** | `flipped_low` |
| Carried from the anchor page | anchor x **0.90** | `carried_factor` |
| A measured fact (no ink, no text) | **1.0** | `measured` |
| A blank / junk pass-1 text rule | **0.95** | `pass1_rule` |
| Floor | **0.20** | `floor` |

---

## 3. Per field

| Field | Confidence |
|---|---|
| **Member name / DOB** | the KV row the shown value came from: score x `model_score` (score 0: `model_score`); found by the rules / GLiNER with no row: 0.60; dropped by post-processing: 0.60 or lower |
| **Member ID** | the mean of every ID row KV Extract accepted and selected |
| **Member verification (chart)** | not a confidence: the agreement rate (pages agreeing / pages checked) |
| **Provider signature** | the signature row's score x `model_score` (rules only: 0.60); several signers: their mean; a facility read as the provider: 0.60 or lower. No signature: 1 - the best model-scored candidate; KV read the page and offered none: 1.0 |
| **DOS** | the KV row the shown date came from: score x `model_score`. Label backups (edge label, keyword pass, dictated date, an admit / discharge side from rules): 0.60. Signature date: 0.40. Neighbours' agreed date, the document's carried date: the source x 0.90. No DOS / default: none |
| **Blank / Junk** | section 4 |
| **Duplicate** | from the match score s to the original: Yes `max(0.30, 1 - 10 x (1 - s))` (exact 1.0, 98% 0.80, 95% 0.50); Partial (a blocked exact match) 1.0. No: s < 0.80 -> 1.0; 0.80-0.90 -> 1.0 falling to 0.80; 0.90-1.00 -> 0.80 falling to 0.30; nothing to compare: 1.0 |
| **Page type / sub-type** (Extracted) | the page-leaf model's probability for the chosen leaf. Progress Note model guard (model >= 0.50 Progress Note): the model's probability. Keywords only, an Others leaf, no model: 0.40. A Deciding title or a family priority rule against the model's family: 0.30 (`decided_by = rule`). A Deciding title against the model within the same family: the model's probability for that family |
| **Page type / sub-type** (Final) | the anchor page (the document's first page of the Final family): its own; other pages: anchor x 0.90; a page re-placed into a Progress Note (lab page inside a note): 0.30 |
| **Codability** | the page type's confidence (a Blank / Junk page: Non-Codable with the Blank / Junk confidence) |
| **Specialty** | the page-leaf model's specialty probability (>= 0.50, not Unknown / Others); else a specialty title, specialty keywords or General: 0.40. Final: the encounter's first page; other pages x 0.90 |
| **Encounter type** | the encounter model's probability for the setting on the deciding page; family rule / keywords / default: 0.40; another setting than the model's top: 0.30; carried x 0.90 |
| **Handwritten / printed / form / visual** | the page-tag model's probability; the handwritten-share rule changed the type: 0.30; no ink: 1.0; faint marks / ink upgrade: 0.40 |
| **Orientation** | stage 1's OSD score, squashed `1 - exp(-c / 3)` (a score, not a probability) |
| **Image quality** | a measurement (score / 100), shown outside the confidence colours |
| **Continuity** (document break) | rules engine (live): a points score through `1 / (1 + exp(-score / 2))`, `abs(2P - 1)`; a decisive rule at least 0.90. Continuity clue model (shadow until switched on): its **calibrated** P(break): confidence `max(p, 1 - p)` |
| **Page sequence** | printed "page x of y" 1.0, "page x" 0.85; a held page 0.48; other pages: the share of numbered pages that kept their place (>= 3), else 0.60. Not part of the Final confidences |

---

## 4. Blank / Junk

| Who decided | Confidence |
|---|---|
| DistilBERT, or a rule agreeing with its top class | the model's probability for the class filed (Main p(keep), Blank p(blank), Junk p(junk)) |
| No ink, no text | 1.0 |
| Pass-1 text rule | 0.95 |
| A short-page rule, contents page, cover sheet, card, or any rule against the model | **from evidence** (below) |

**From evidence** (`core-pipeline/modules/blank_junk/evidence.py`):

```
rule        = 0.5 + rule_span x margin x support          (a coin flip on the rule's own cut)
logit(conf) = ocr_factor x logit(rule) + logit(model p for the verdict)
ocr_factor  = 0.5 + 0.5 x the page's OCR match rate
```

* **margin**: how far past the rule's own cut the page is (0 = on the cut, 1 = far past it):
  words of its own against the short-page cut, the share of lines in order on the neighbour,
  how many cover-sheet fields or contents entries, how much card evidence.
* **support**: how sure the measurement itself is: both neighbours compared or one, the same OCR
  engine on both, the share of lines that are the neighbour's (Junk) or its own (Main).
* **rule_span**: each rule's own ceiling (2026-11-22, user: different rules, different confidence):
  its **measured accuracy** on the ground truth minus 0.5, once it has 30+ ground-truth pages; until
  then the shared 0.45 (a rule alone never passes 95%). A rule right half the time or less adds
  nothing: the model decides. Measure and store on the VM:
  `python -m modules.blank_junk.rule_reliability` (table) and `--write` (stores it).
* Every part is stored with the verdict (`votes.confidence_parts`). Weights:
  `confidence_canon.json` -> `modules.blank_junk.evidence`.

---

## 5. Page and chart levels

**Features:** the seven accuracy metrics: Member Verification (the lower of Member Name and
DOB), DOS, Blank / Junk, Page Type, Codability, Provider Signature, Orientation. A Blank / Junk
or duplicate page counts Blank / Junk and Codability only. Each feature is banded High / Medium /
Low as in section 1.

| | Score | % shown | Level |
|---|---|---|---|
| **Page** | (High features - Low features) / features | (1 + score) / 2, at least 20% | the % banded like any confidence |
| **Chart** | (High pages - Low pages) / pages | (1 + score) / 2, at least 20% | the % banded like any confidence |

Like an NPS: Medium counts 0. All High is 100%, all Medium 50%, all Low 0% (shown at the 20%
floor). High needs a score of +0.6 or more; Low means more Low than High.
Code: `review-ui/frontend/src/confidenceLevel.ts`, twin `review-ui/backend/app/services/chart_confidence.py`.

---

## 6. Confidence Analysis (review-ui, Accuracy)

Over the filtered charts' pages with ground truth: the distribution of page and field levels,
page level against the page's overall match, each metric's accuracy per level, and each metric's
accuracy by confidence band: **95-100, 90-95, 85-90, 80-85, 75-80, 70-75, 60-70, 50-60, 40-50,
30-40, <30%**. Accuracy should rise with confidence.

---

## 7. Not done yet

* **Calibration.** A raw 0.80 does not yet mean "right 8 times in 10". The `calibration` slot in
  `confidence_canon.json` is empty; fit it per metric on the ground-truth charts. The continuity
  clue model is the one calibrated score so far (isotonic; out-of-fold calibration error 0.007).
* **The fixed values are set, not measured.** 0.60 / 0.40 / 0.30 should become each rule's
  measured accuracy on the ground truth, as the blank / junk rules now can (section 4); next the
  family priority rules and the DOS / member backups.
