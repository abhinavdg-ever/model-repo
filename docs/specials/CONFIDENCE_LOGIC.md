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

**Blank / Junk and Provider Signature** (2026-11-24, user) are strictest: **High 0.99 and above, Medium 0.75 to
under 0.99, Low under 0.75**; the Confidence Analysis shows them in a 99-100% band and one 95-99% band.
**Orientation** (2026-11-25, user; was 0.95 / 0.75): **High 0.90 and above, Medium 0.60 to under 0.90, Low under 0.60**, with
1-point bands from 95% (page and chart levels, Confidence Analysis). The Pages view's ☰ "Colour by Confidence"
can colour the thumbnails by Page Confidence or by any one metric (e.g. Orientation), each by its own cuts. **DOS** is High from **0.75**
(Medium 0.50 to under 0.75, Low under 0.50).

**Calibration** (2026-11-24, user; review-ui): an admin can fit Page Type and Codability to their measured
accuracy from the profile menu on the Accuracy view ("Calibrate Confidence", admins; the charts listed); the curves (`confidence_calibration`) are applied
on read, so the stored values stay the pipeline's. The Codability tab's **i** (Agreement) shows Page Type / Codability accuracy per
tier A-E. The Overview's band tables count pages by streams matched (0/7 .. 7/7) before Match / No Match.

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
| **Member name / DOB** | raw: the KV row the shown value came from: score x `model_score` (score 0: `model_score`); found by the rules / GLiNER with no row: 0.60; dropped by post-processing: 0.60 or lower. Then **calibrated** per source (section 5a); a name / DOB / ID **not found: 0.80** (`member_not_found`, 2026-11-23, user), or once fitted the measured chance "not found" is right |
| **Member ID** | the mean of every ID row KV Extract accepted and selected |
| **Member verification (chart)** | not a confidence: the agreement rate (pages agreeing / pages checked) |
| **Provider signature** | the signature row's score x `model_score` (rules only: 0.60); several signers: their mean; a facility read as the provider: 0.60 or lower. No signature: 1 - the best model-scored candidate; KV read the page and offered none: 1.0 |
| **DOS** | the KV row the shown date came from: score x `model_score`. Label backups (edge label, keyword pass, dictated date, an admit / discharge side from rules): 0.60. Signature date: 0.40. Neighbours' agreed date, the document's carried date: the source x 0.90. No DOS / default: none |
| **Blank / Junk** | section 4 |
| **Duplicate** | from the match score s to the original: Yes `max(0.30, 1 - 10 x (1 - s))` (exact 1.0, 98% 0.80, 95% 0.50); Partial (a blocked exact match) 1.0. No: s < 0.80 -> 1.0; 0.80-0.90 -> 1.0 falling to 0.80; 0.90-1.00 -> 0.80 falling to 0.30; nothing to compare: 1.0 |
| **Page type / sub-type** (Extracted) | **By agreement of the leaf model and the keywords on the chosen type's FAMILY** (2026-11-24, user; on the family since 2026-11-25, the level the ground truth grades; `page_classify/agreement.py`, settings in `page_keywords.json` -> `confidence.agreement`). Each approach's families are ranked by their best path; *mass* = the model's probability over every type in the chosen family. **A** both rank the family first: mass; **B** model first, keywords 2nd-3rd: mass x 0.90; **C** model first only: mass x 0.75, at most 0.79; **D** model ranks it 2nd-3rd: 0.40 + 0.50 x mass, at most 0.69; **E** otherwise: 0.20 + 0.50 x mass, at most 0.49 (D and E spread with the model, not one value each: 2026-11-25, user, no pile on). No model: D (keywords' first family) 0.55, E 0.30. An Others leaf: 0.40; the model overruling a Deciding keyword: 0.30 |
| **Page type / sub-type** (Final) | the anchor page (the document's first page of the Final family): its own; other pages: anchor x 0.90; a page re-placed into a Progress Note (lab page inside a note): 0.30 |
| **Codability** | its own (2026-11-24, user): the model's probability summed over every type with the chosen codability; both approaches' first choices share that codability -> that sum; one does -> x 0.75, at most 0.79; neither -> 0.20 + 0.50 x that sum, at most 0.49; no model: keywords' first shares it 0.55, else 0.30. Carried like the page type (other pages of the document x 0.90). Rows from before: the page type's. A Blank / Junk page: Non-Codable with the Blank / Junk confidence |
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

**Features:** the seven accuracy metrics: Member Verification (the average of Member Name
and DOB, 2026-11-23), DOS, Blank / Junk, Page Type, Codability, Provider Signature, Orientation. A Blank / Junk
or duplicate page counts Blank / Junk and Codability only. Each feature is banded High / Medium /
Low as in section 1.

**Page:** score = (High features - Low features) / features (like an NPS: Medium counts 0); the page's
% = (1 + score) / 2, at least 20%, and its level is that % banded like any confidence.

**Page Score** (2026-11-25, user): the plain average of the same features' confidences. It is shown
beside the Page Confidence (Page Details, the page download's "Page Score %", its own tables in the
Confidence Analysis Overview) and never sets a level or a colour: those stay the Page Confidence's.

**Discharge Summary** (2026-11-25, user): on a page whose Final page type is a Discharge Summary,
Codability reads at most 49% (Low), after any calibration, so such a page is never High on
Codability. Applied when read (frontend and the History twin); the stored value is unchanged.

**Chart** (2026-11-23, user):

1. **Score** = the pages' levels averaged with weights **High 1.0, Medium 0.6, Low 0**. A **critical
   page** (Final page type Discharge Summary, Operative Note or Procedure Note) counts **twice**.
2. **Level** = the score banded like any confidence (2026-11-23, user: no guardrails):

| Chart level | Score | Review |
|---|---|---|
| **High** | 80% or more | straight through |
| **Medium** | 50% to under 80% | review the flagged Medium / Low pages |
| **Low** | under 50% | full manual review |

Examples (100 pages): 75 High, 25 Medium -> 90%, **High**. 82 High, 12 Medium, 6 Low -> 89.2%, **High**.
40 High, 15 Medium, 45 Low -> 49%, **Low**.
Code: `review-ui/frontend/src/confidenceLevel.ts`, twin `review-ui/backend/app/services/chart_confidence.py`.

**Filters** (2026-11-25, user): History and the Accuracy view filter on the **Confidence** column (levels
High / Medium / Low / (Blank) = not scored, and a Min / Max % on the shown, rounded value) and the **Pages**
column (Min / Max, inclusive; History: the chart's pages, Accuracy: the scored pages). History filters on
the server (`folder_list.py`: chart confidence is read only when that filter is on, after the other filters),
the Accuracy view in the browser.

---

## 5a. Member calibration (2026-11-23)

Member confidence was out of step with how often it is right (the 0.60 whole-page find right about
96%; pages at 95-100% right about 57%, suspected: a sure name beside a missed DOB that carried no
confidence). `core-pipeline/modules/member/calibration.py` measures each field (Name, DOB) by source
on the ground truth, with "right" as the Accuracy View's member call (Yes / No / Not Found, against
the manifest member; Blank / Junk and duplicate pages left out):

| Source | Calibrated confidence |
|---|---|
| `kv_row` (a key / value row) | the raw score through an increasing curve fitted on the labelled pages |
| `whole_page` (rules / GLiNER, no row) | that source's measured accuracy |
| `dropped` (post-processing dropped it) | that source's measured accuracy |
| `not_found` (nothing read) | how often "not found" was right |

A source with under 30 labelled pages keeps its raw value. The raw value and source are kept in
`imaging_final.confidences.member_raw`, so a refit uses raw scores. On the VM:
`python -m modules.member.calibration` (table), `--write` (stores it in `confidence_canon.json` ->
`modules.member.calibration`), then re-run the charts with `only: ["member_verify", "imaging_final"]`,
`skip_ocr: true`.

---

## 6. Confidence Analysis (review-ui, Accuracy)

Over the filtered charts' pages with ground truth, by confidence band (**95-100, 90-95, 85-90,
80-85, 75-80, 70-75, 60-70, 50-60, 40-50, 30-40, <30%**). Header: the average chart confidence, charts
and pages. Overview (2026-11-23, user): (1) charts by chart confidence band; (2) pages by their own
confidence band: Pages, Match, No Match, Accuracy, where Match = every metric on the page matches;
(3) by page confidence band, each metric's accuracy, one column per metric. Each metric's tab: its
accuracy by its own confidence; Blank / Junk, Provider Signature and Orientation split 95-100 into
**99-100, 98-99, 97-98, 96-97, 95-96**. Accuracy should rise with confidence.

---

## 7. Not done yet

* **Calibration.** A raw 0.80 does not yet mean "right 8 times in 10". The `calibration` slot in
  `confidence_canon.json` is empty; fit it per metric on the ground-truth charts. The continuity
  clue model is the one calibrated score so far (isotonic; out-of-fold calibration error 0.007).
* **The fixed values are set, not measured.** 0.60 / 0.40 / 0.30 should become each rule's
  measured accuracy on the ground truth, as the blank / junk rules (section 4) and the member
  fields (section 5a) now can; next the family priority rules and the DOS backups.
