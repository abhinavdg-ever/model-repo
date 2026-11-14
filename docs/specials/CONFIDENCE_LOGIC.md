# Confidence logic: every score in the imaging pipeline

Date: 2026-11-14. Written from the code as it stands in the working tree on that date
(some stage 1 and Final OCR 2 files were being edited the same day; those parts are marked).
File references are `path:line` from the repository root.

Who this is for: a technical product owner who needs to know what each number means,
how it is built, which cut-offs act on it, where it is stored and where it is shown.

Sections 0-11 describe the code **today**. **Section 12 is the proposed new rule** (user,
2026-11-14): a confidence is how sure we are that the value is correct, taken from the model
that produced it; a backup technique or a flipped decision gets a fixed low value. Section 12
is a proposal: nothing in it is built yet.

---

## 0. One-page summary

The house rule (user, 2026-10-25): one scale for every decided value.
**>= 0.80 good, 0.50 to 0.80 passable, < 0.50 needs review.** A confidence is a model
probability, lifted by keywords that agree and multiplied by penalties for doubt. It is
never a fixed constant. Calibration comes later.

| Metric | Range | How computed (short) | Bands / what it triggers | Stored in |
|---|---|---|---|---|
| Shared engine `conf.score / backed / band` | 0-1 | base x penalties, optional calibration, clip | good >= 0.80, passable >= 0.50, review < 0.50 | (library) |
| Image quality score | 0-1 | weighted sum of 10 image sub-metrics (0-10) / 10 | tag High >= 0.70, Medium >= 0.40, Low < 0.40 | `ocr_quality_results.quality_score`, `quality_tag` |
| Final Quality (OCR routing) | high/medium/low | routing table: type, HW %, quality, Docling vs Tesseract overlap | decides which pages go to Azure; Low -> review | `ocr_quality_results.quality_tag`, `quality_detail.routing` |
| Handwriting / page type | 0-1 | ConvNeXt softmax for the chosen class | under 0.55 (2-class) or 0.50 (page-tag) -> Uncertain, re-scaled under 0.50 | `ocr_quality_results.hw_confidence` |
| Visibility | 0-1 | page-tag model's visibility head, max prob | none | `quality_detail.confidences.visibility` |
| Orientation / mirror | 0-1 | Tesseract OSD 1 - exp(-c/3), backed by English-word share | none (shown only) | `quality_detail.confidences.orientation` |
| Tilt | 0-1 | English-word share at kept tilt, x 0.8 over 5 degrees | none | `quality_detail.confidences.tilt` |
| Azure word confidence | 0-1 | Azure's own | none (written, never read) | Final2 JSON `words[].confidence` |
| Section header match | 0-1 | lexical ratio or MiniLM cosine | kept at >= 0.90 | OCR JSON `section_headers[].match_score` |
| Blank / junk verdict | 0-1 | rule strength, or DistilBERT p backed by rule, or service confidence | < 0.50 flags the Final page | `blank_junk_classification.confidence` |
| Duplicate score | 0-1 | Jaccard of the two pages' distinct body words | Exact, Rule A >= 0.98, Rule B avg >= 0.95 min >= 0.90 | `blank_junk_classification.confidence` (duplicate rows) |
| Best duplicate similarity | 0-1 | highest Jaccard to any compared page | review-ui turns it into a display confidence | `blank_junk_classification.duplicate_confidence` |
| Duplicate display confidence | 0.5-1 | V-curve over similarity, minimum 0.5 at 0.95 | coloured by bands (never red) | computed in review-ui |
| Member page | 0-1 | strongest identifier, others back it; x 0.6 when not verified | < 0.50 flags | `member_extraction_results.confidence` |
| Member chart | 0-1 | share of agreeing pages x their mean confidence (x 0.6 without NER) | none | `member_verification_summary.confidence` |
| KV extraction row | 0-1 | LightGBM p backed by rule geometry score | rows above 0.7 count for signatures | staging JSON; `provider_signature_results.confidence`, `additional_page_details.confidence` |
| Provider signature | 0-1 | signature row's confidence x 0.75 / x 0.9, averaged over signers | < 0.50 flags | `provider_signature_results.confidence`, `imaging_final.provider_confidence` |
| DOS (page) | 0-1 | KV model p backed by rule score; else source strength x no_model | < 0.50 -> try signature date / neighbours | `dos_extraction_results.confidence`, `dates[].confidence` |
| Page type (leaf) | 0-1 | model p backed by keyword support; keyword-only and Others rules | < 0.50 -> `needs_review` | `page_classification.confidence`, `confidence_level`, `axes` |
| Specialty | 0-1 | specialty model p backed by keywords; General = fallback | < 0.50 flags | `page_classification.axes.specialty_confidence` |
| Codability | 0-1 | the page type's path confidence | < 0.50 flags | `axes.codeability_confidence`, `imaging_final.confidences.codability` |
| Continuity gap | 0-1 | abs(2 x sigmoid(score / 2) - 1); decisive >= 0.9 | < 0.50 or level low -> `review_required` | `page_continuity_results.confidence`, `confidence_level`, `review_required` |
| Encounter type | 0-1 | encounter model p backed by keyword tier; vote share x mean | < 0.50 flags | `encounter_type_results.confidence` |
| Page sequencing | 0-1 | marker confidence, or share of numbered pages that stayed in scan order | held page ~0.48 | `page_sequencing_results.confidence` |
| Final values | 0-1 | taken from the deciding page of each value | any < 0.50 -> `review_fields`, `needs_review` | `imaging_final.confidences`, `review_fields`, `needs_review` |
| OCR match rate (review-ui) | 0-100% | mean pairwise Jaccard of OCR engines' token sets | > 90 green, 70-90 amber, < 70 red | not stored |
| Accuracy metrics (review-ui) | 0-100% | ground truth vs pipeline: Match / Partial / No Match, P/R/F1 | Overall Good 5-7 streams | `accuracy_snapshot` cache |

---

## 1. The shared engine: `modules/confidence.py`

Most stages call one small library, `core-pipeline/modules/confidence.py`, with its
numbers in `core-pipeline/resources/canon/confidence_canon.json` (reloads on change).

| Function | What it does | Line |
|---|---|---|
| `score(base, *reasons, module)` | base x the penalty of each reason (each reason once), then calibration for `module`, clip to [0, 1], round to 4 places | `core-pipeline/modules/confidence.py:83` |
| `backed(p, support)` | lifts p toward 1 by keywords / rules that agree: `p + (1 - p) x 0.5 x support` | `core-pipeline/modules/confidence.py:76` |
| `under_bar(p, bar)` | a value decided although p is under the module's own bar: `p / bar x 0.49`, so it always reads as review | `core-pipeline/modules/confidence.py:110` |
| `absent(scores, *reasons)` | how sure "not found" is: `1 - best rejected candidate`; nothing found at all: `1.0 x no_model = 0.85` | `core-pipeline/modules/confidence.py:119` |
| `page_reasons(text, hw)` | adds `handwritten` (handwritten or mixed page) and `thin_text` (< 150 characters) | `core-pipeline/modules/confidence.py:131` |
| `band / needs_review / level` | good >= 0.80, passable >= 0.50, review below; level words high / medium / low | `core-pipeline/modules/confidence.py:141` |

Penalties (`core-pipeline/resources/canon/confidence_canon.json:11`):

| Reason | Multiplier | Meaning |
|---|---|---|
| `keywords_disagree` | 0.70 | keywords / rules point to another value |
| `no_model` | 0.85 | a rule decided, no model behind it |
| `fallback` | 0.60 | a default filled the value |
| `conflict` | 0.80 | other pages or sources say something else |
| `overturned` | 0.70 | a later pass reversed the first decision |
| `handwritten` | 0.90 | handwritten or mixed page |
| `thin_text` | 0.85 | under 150 characters of text |
| `degraded` | 0.60 | an optional dependency was missing |

`agree_lift` is 0.5: a strong keyword (support 1.0) closes half the gap to 1. A model at
0.6 backed by a strong keyword reads 0.8.

Calibration (`calibration` in the canon) is empty today, so `calibrate()` is the identity
for every module (`core-pipeline/modules/confidence.py:94`).

Common stand-in values that fall out of these rules:

| Situation | Value |
|---|---|
| Rule only, full strength (1.0 x no_model) | 0.85 |
| Nothing judged it (0.5 x degraded) | 0.30 |
| Pure fallback with no model (1.0 x fallback) | 0.60 |

---

## 2. Stage 1: image quality, page type, rotation (`ocr_quality`)

### 2.1 Image quality score and tag

**What it means.** An engineering grade of the scan, not a probability. Says nothing
about whether the text is right.

**Inputs.** The page image (after rotation correction) and its embedded DPI.

**Formula.** Ten sub-scores, each 0-10, combined with fixed weights
(`core-pipeline/modules/image_preprocess/quality_analyzer.py:244`):

| Sub-score | Weight |
|---|---|
| usable dimension (shortest side 400 -> 2200 px) | 0.16 |
| effective resolution (DPI vs 400 target) | 0.14 |
| sharpness (Laplacian variance) | 0.16 |
| blur (high-frequency ratio) | 0.10 |
| contrast | 0.12 |
| noise | 0.08 |
| clipping | 0.08 |
| brightness | 0.06 |
| text visibility (ink ratio 0.8%-20% is ideal) | 0.06 |
| compression | 0.04 |

`quality_score = sum / 10`, stored 0-1 (`quality_analyzer.py:52`). A `LOW_QUALITY`
warning is added below 4.0 / 10 (`quality_analyzer.py:230`); it goes into
`quality_detail.warnings` only.

**Tag thresholds.** `base_quality_label`
(`core-pipeline/modules/image_preprocess/quality_label_postprocess.py`, `QUALITY_HIGH_AT` /
`QUALITY_MEDIUM_AT`): High >= 0.70, Medium 0.40 to 0.70, Low < 0.40. On 2026-11-14 the user
priced 0.80 / 0.50 and 0.75 / 0.50 against Doc Intel volume (dev DB: 300 and 295 of 329 pages
sent, against 263) and kept 0.70 / 0.40. These differ from the 0.80 / 0.50 confidence bands.

**Adjustments to the tag (never to the score).**

1. Handwritten page type + High -> Medium
   (`quality_label_postprocess.py:43`).
2. **OCR routing / Final Quality** (2026-11-14, `core-pipeline/modules/ocr/routing.py`;
   called from `core-pipeline/modules/ocr/stage_final2.py:463`). Before
   Azure runs, each page gets a row of the user's routing table. Overlap is Dice on word
   counts between Docling (Final OCR 1) and Tesseract
   (`quality_label_postprocess.py:57`):

   | Row | Type (HW %) | Stage 1 quality | Docling vs Tesseract | Final Quality | Azure |
   |---|---|---|---|---|---|
   | 1 | Printed (< 10%) | High | >= 0.90 | High | No |
   | 2 | Printed | High | < 0.90 | Medium | Yes |
   | 3 | Printed | Medium | >= 0.95 | High | No |
   | 4 | Printed | Medium | < 0.95 | Medium | Yes |
   | 5 | Printed | Low | - | Low | Yes |
   | 6 / 7 / 8 | Mixed 10-40% / Handwritten > 40% / Form | any | - | 3-engine re-grade | Yes |
   | 9 | Visual (< 10%) | High / Medium | >= 0.90, or < 25 Docling words | High | No |
   | 10 | Visual, other | | | Medium | Yes |
   | 11 | anything else | | | 3-engine re-grade | Yes |
   | 12 | settled blank / junk / duplicate in pass 1 | | | stage 1 grade, else Low | No |

   Constants: `routing.py:34-39`. The 3-engine re-grade (rows 6, 7, 8, 11) runs after Azure:
   mean pairwise overlap of Tesseract, Docling and Azure; < 0.50 one level down, < 0.10 Low,
   never up (`routing.py:171`, `quality_label_postprocess.py:70`). This replaces the older
   one-step re-grade (Docling vs Tesseract < 50% one level down, < 10% Low, before the Azure
   skip). The routing row and overlaps are kept in `quality_detail.routing` and in
   `<chart>_ocr_routing.csv`.

**What the tag triggers.**

- Low (not on a blank page) -> `ocr_quality_results.review_required = true`
  (`core-pipeline/modules/image_preprocess/stage.py:113`; after routing:
  `stage_final2.py:488`, except row 12 and blank pages).
- Printed + Low is decided in blank / junk pass 2, not pass 1 (`routing.py:81`).
- Rows 1, 3, 9 skip Azure (no stage 5 bill).

**Stored.** `ocr_quality_results.quality_score`, `quality_tag`, `quality_detail`
(sub-scores, warnings, routing).

**Shown.** review-ui Page Details "Quality" row: the tag, with `quality_score` in the
confidence column, coloured by the confidence bands
(`review-ui/backend/app/store/rows.py:407`). A blank page shows no quality.

### 2.2 Handwriting / page type confidence (`hw_confidence`)

**What it means.** How sure the page classifier is of the page type it chose.

**Inputs.** Page pixels. Model: ConvNeXt-Tiny, either two-class (Printed / Handwritten) or
the page-tag checkpoint (Printed, Handwritten, Form, Visual, Blank, Uncertain + visibility
head + handwritten-area head) (`core-pipeline/modules/image_preprocess/hw_printed.py`).

**Formula.**

| Path | Confidence | Line |
|---|---|---|
| No ink at all (blank sheet) | 1.0 x no_model = 0.85 | `hw_printed.py:512` |
| Faint marks only | 1.0 x no_model x fallback = 0.51 | `hw_printed.py:512` |
| Two-class, clear call (p(HW) >= 0.58 or <= 0.42) | p(HW) or 1 - p(HW) | `hw_printed.py:649` |
| Two-class, near 0.50 and chosen class < 0.55 | Uncertain, `under_bar(c, 0.55)` = c / 0.55 x 0.49 | `hw_printed.py:61-65`, `:674` |
| Ink check overrules Printed -> Handwritten | backed(p(HW), 1.0) x keywords_disagree | `hw_printed.py:613` (ink score >= 0.48 and spread) |
| Page-tag model | top type's softmax probability | `hw_printed.py:719` |
| Page-tag, top < cut-off (0.50 unless the checkpoint sets one) | Uncertain, `under_bar` | `hw_printed.py:95` |
| Page-tag, Blank under the cut-off | stays Blank with its raw probability (can read < 0.50) | `hw_printed.py:719` |
| Classifier crashed | 0.5 x degraded = 0.30, method `fallback` | `core-pipeline/modules/image_preprocess/stage.py:110` |
| RandomForest fallback | `max(predict_proba)`; heuristic: abs(avg Tesseract conf - 50) / 50 | `core-pipeline/modules/image_preprocess/hw_printed_rf.py:191`, `:201` |

**Triggers.** Uncertain page type -> `review_required`. The page type steers OCR routing,
blank / junk pass 1 vs pass 2 and the `handwritten` penalty downstream.

**Stored.** `ocr_quality_results.hw_confidence`, `hw_method`, `document_type`,
`handwritten_probability`. The handwritten-area confidence reuses `hw_confidence`
(`quality_detail.confidences.handwritten_area`, `stage.py:541`).

**Shown.** Page Details rows Handwritten / Printed, Handwritten Area; Final
`imaging_final.confidences.handwriting` (raw `hw_confidence`).

### 2.3 Visibility confidence

The page-tag model's visibility head: the max of its two softmax values
(`hw_printed.py:742`). Raw probability, no penalties. `is_visible = false` sets
`review_required`. Stored in `quality_detail.confidences.visibility`; shown in Page Details.

### 2.4 Rotation, mirror and tilt confidence

**What it means.** How sure the turn (with any flip) and the small tilt correction are.

**Inputs.** Tesseract OSD orientation confidence (open-ended, about 0-20), the
letter-shape detector `rotation_cv`, and a reading test (share of 3+ letter tokens that
are English words; "reads" = >= 8 tokens and >= 40% English,
`core-pipeline/modules/image_preprocess/rotation.py:752`).

**Formula** (`core-pipeline/modules/image_preprocess/stage.py:387`):

- OSD probability: `1 - exp(-c / 3.0)` (`osd_scale` in `confidence_canon.json:34`):
  c = 1 -> 0.28, 3 -> 0.63, 6 -> 0.86, 10 -> 0.96. OSD below 1.0 is ignored
  (`core-pipeline/modules/image_preprocess/osd.py:45`).

| Decision method | Orientation confidence |
|---|---|
| `osd_cv_agree` (OSD and CV agree) | backed(OSD p, 1.0) |
| `osd` | backed(OSD p, English share); OSD p alone on a page with nothing to read |
| `readability` / `cv_final` (reading test or CV overruled OSD) | English share x overturned (0.7); 0.5 x 0.7 if no share |
| `osd_undecided` (no OSD answer) | English share x fallback (0.6); 0.5 x 0.6 if no share |
| Detection crashed | 0.5 x degraded = 0.30 (`stage.py:358`) |

- Mirror confidence = orientation confidence (one decision).
- Tilt: English share at the kept tilt; x conflict (0.8) when the tilt is over
  `MAX_TILT_TO_APPLY` (5 degrees, recorded but not applied); 0.5 x fallback = 0.30 with no
  share.

**Triggers.** Nothing. These are shown, not acted on.

**Stored.** `ocr_quality_results.quality_detail.confidences` (`orientation`, `mirrored`,
`tilt`) (`stage.py:541`); the CSV `<chart>_rotation.csv`. The geometric detector in
`rotation.py` still computes its own rotation / tilt / mirror / overall confidences
(review below 0.45, `rotation.py:1418`), but they are not stored.

**Shown.** Page Details Orientation, Tilt, Mirrored rows.

---

## 3. OCR (stages 2, 4, 5, 6)

| Number | Status |
|---|---|
| Tesseract (prelim) word confidences | Not stored. Only used by the RF handwriting fallback heuristic. |
| Docling / RapidOCR (Final 1) word confidences | Not stored. |
| Azure Document Intelligence (Final 2) word, barcode and language confidences | Written into the Final2 JSON (`core-pipeline/modules/ocr/stage_final2.py:177`). No code reads them. |
| Docling vs Tesseract overlap (Dice) | Drives Final Quality and Azure routing (section 2.1). |
| Section header `match_score` | Best of a lexical SequenceMatcher ratio (raised to 0.93 when the canon phrase's words are all in the OCR text and it is >= 55% of its length) and MiniLM cosine (counted only when token Jaccard with the phrase >= 0.40). A candidate is kept at >= 0.90 (`SECTION_HEADER_SEMANTIC_THRESHOLD`, `core-pipeline/common/config.py:242`; `core-pipeline/modules/ocr/section_header_match.py:350`). Stored in the OCR JSON `section_headers[].match_score`. |

The **OCR match rate** shown in review-ui is described in section 10.

---

## 4. Blank / junk / duplicate (stages 3 and 8)

### 4.1 Verdict confidence

**What it means.** How sure the page's verdict (Main, Blank, Junk / subtype) is.

**Inputs.** OCR text, word boxes, stage 1 page type and handwritten share, kv_extract's
Electronic Signature, and the DistilBERT page classifier wrapped in the teammate's
`PageClassifierService` (p(keep), p(junk), p(blank), a flag and a confidence).

**Rule strengths** (`RULE_SUPPORT`, `core-pipeline/modules/blank_junk/method.py:112`):
R1 1.0, R2 0.99, R3 0.95, R4 0.95, R6 0.95, CONTENT 0.95, FAX 0.85, LAB 0.95,
INVOICE 0.95, INDEX 0.95, R1E 1.0.

**`rule_confidence`** (`method.py:595`):

- R1, R2 (no text to read): the strength itself (1.0, 0.99).
- Other rules: p = DistilBERT's p(keep) for Main, p(junk) otherwise.
  `backed(p, strength)`, x keywords_disagree (0.7) when DistilBERT's other side is >= 0.5.
  Without DistilBERT: strength x no_model.

Example: R4 signed page, p(keep) = 0.2, p(junk) = 0.75 ->
0.2 + 0.8 x 0.5 x 0.95 = 0.58, x 0.7 = 0.41 (review).

**Per rule, as the ladder runs** (`method.py:628`):

| Rule | Verdict | Confidence |
|---|---|---|
| R1 no ink / blank type / no text / "blank" | Blank | 1.0 |
| R2 "intentionally left blank" | Blank | 0.99 |
| R1 cover mark (< 10 words) | Junk / Cover Page | 1.0 |
| R1E "End of Report" (< 10 words) | Junk / Cover Page | **fixed 1.0** (`method.py:696`) |
| R1 tiny (< 15 characters) | Junk / Others | 1.0 |
| P1 pass 1, nothing fired | Main for now | 0.5 x degraded = 0.30 (`method.py:702`) |
| R3V visual < 20 words | Main | **fixed 0.90** (`method.py:720`) |
| HW > 30% handwritten | Main | **fixed 0.90** (`method.py:724`) |
| R4 signed | Main | rule_confidence |
| LAB / CLINICAL_CONTENT | Main | rule_confidence |
| R3 card | Junk / Cards | **fixed 0.95** (`method.py:747`); kept Main when p(keep) >= 0.99: p(keep) |
| FAX unsigned | Junk / Letter/Fax | rule_confidence (0.85 strength) |
| INDEX contents page | Junk / Others | rule_confidence |
| R5 classifier unavailable | Main | 0.30 |
| R5 service JUNK | Junk / subtype | service confidence (`method.py:781`) |
| R5 service JUNK, Invoice subtype | Main (Invoice page) unless a terms page | rule_confidence(INVOICE) (`method.py:959`) |
| R5 service BLANK | Junk / Others | service confidence |
| R5 service KEEP | Main | service confidence |
| Short page (stage) | Main or Junk / Others | **fixed 0.90** (`CONTAINED_CONFIDENCE`, `core-pipeline/modules/blank_junk/stage.py:227`) |
| No confidence given (fallback) | | Blank 0.90, Duplicate 0.99, Junk 0.85 (`core-pipeline/modules/blank_junk/classify.py:170`); else 0.85 / Main 0.70 (`stage.py:153`) |

**The service's own confidence** (`core-pipeline/modules/blank_junk/model/src/models/decision.py`,
config `core-pipeline/modules/blank_junk/model/configs/default.json:19`):

1. Argmax of the KEEP / BLANK / JUNK buckets; confidence = that bucket's probability.
2. Protocol retention forces KEEP with confidence max(p_keep, conf, 0.99) (`decision.py:180`).
3. Keep veto: a JUNK / BLANK flag with p(keep) > 0.10 becomes KEEP, confidence = p(keep), review (`decision.py:189`).
4. JUNK / BLANK with confidence or top label < 0.85 becomes KEEP, confidence = max(p(keep), conf), review (`decision.py:204`).
5. Any flag under 0.70 becomes KEEP, review; the confidence is left as it was (`decision.py:209`).

The service's `review_required` is kept in the page's votes (`blank_junk.json`), not in a column.

**Triggers.** The verdict itself is rule-based; the confidence does not change it. In
Final, a blank / junk confidence < 0.50 puts `blank_junk` in `review_fields`
(section 9). Continuity weights junk runs by the mean junk confidence.

**Stored.** `blank_junk_classification.confidence` (one row per page per pass),
`reason`, votes in `staging/blank_junk.json`.

**Shown.** Page Details Blank / Junk row (Final value if stored, else Extracted).

### 4.2 Duplicate score

**Formula.** Jaccard of the two pages' distinct body words (header / footer lines
removed): `|A n B| / |A u B|` (`core-pipeline/modules/blank_junk/dup_rules.py:147`). An exact
match scores 1.0.

**Cut-offs** (`dup_rules.py:38`): exact at any distance; within 3 pages only exact;
further apart Rule A >= 0.98 next to a duplicate, Rule B three in a row with mean >= 0.95
and none < 0.90. Hard blocks (length, dates / numbers, signature, short signed page,
image match < 0.90) override all.

**Stored.** A duplicate row's `blank_junk_classification.confidence` is its match score
(`stage.py:371`). `blank_junk_classification.duplicate_confidence` is the page's best
similarity to any compared page, whatever its verdict (`stage.py:479`).

### 4.3 Duplicate display confidence (review-ui)

`similarity_confidence` (`review-ui/backend/app/services/duplicate_label.py:21`,
`review-ui/frontend/src/duplicateLabel.ts:21`), over the best similarity s:

- s >= 0.95: `0.5 ^ ((1 - s) / 0.05)`: 1.0 at 100%, 0.87 at 99%, 0.76 at 98%, 0.5 at 95%.
- 0.80 < s < 0.95: `0.5 ^ ((s - 0.80) / 0.15)`: confidence the page is not a duplicate.
- s <= 0.80: 1.0.
- A flagged page under 0.95: 0.5. A page never compared: 100% (not flagged) or none.

The minimum is 0.5, so this value is never in the review band.

---

## 5. Member extraction and verification (stage 9)

**Page confidence** (`core-pipeline/modules/member/stage.py:128`):

- Each identifier read off the page (name, DOB, member ID) is one piece of evidence:
  - read by the rules and scored by the KV extraction: its row confidence (section 7.4);
  - read by the rules without a score: `field_support.rule` = 0.6;
  - read by NER (GLiNER): `field_support.ner` = 0.4 (`confidence_canon.json:28`).
- The strongest is the base; each other one backs it (`backed`). Two rule fields at 0.6:
  0.6 + 0.4 x 0.5 x 0.6 = 0.72.
- Not verified (identifiers match nobody): x fallback (0.6).
- Wrong member with no identifier: 0.4 (another person's name in a patient-name sentence).
- Nothing read: the weakest "not found" of name / DOB / member ID (`absent`, section 1).

**Chart confidence** (`stage.py:165`): share of checked pages that agree with the decision
(verified pages for verified, wrong-member pages for failed) x their mean confidence,
x degraded (0.6) when NER is off.

**Decision cut-offs (counts, not confidences).** Reject when wrong-member pages reach
`min(10, 2 x max(1, min(5, ceil(10% of pages))))` (`core-pipeline/modules/member/rules/what_if_rules.py:29`).
Status: skipped / failed / verified / needs_review (`core-pipeline/modules/member/engine.py:678`).

**NER score.** GLiNER keeps entities at >= 0.25
(`core-pipeline/modules/member/extractors/ner_based/model.py:280`). Its score is only logged; the
page confidence uses the fixed 0.4 support instead.

**Stored.** `member_extraction_results.confidence`, `member_verification_summary.confidence`.
The CSV `<chart>_member_verification.csv` writes `confidence` as pages verified / pages
checked (`stage.py:695`), which is a different number from the database column.

**Shown.** Page Details Member rows; Final `imaging_final.confidences.member`.

---

## 6. Page classification, sub-type, codability, specialty (stage 10)

The default classifier is the **leaf** classifier (`PAGE_CLASSIFIER=leaf`,
`core-pipeline/common/config.py:323`): one leaf L1 > L2 > L3 from a page-leaf model and the
per-leaf keyword canon.

### 6.1 Page type confidence (`leaf_confidence`)

`core-pipeline/modules/page_classify/leaf.py:377`, settings in `leaf_keywords.json`
(`confidence`), defaults `core-pipeline/modules/page_classify/leaf_canon.py:57`.

Keyword support for the winner (`leaf.py:356`):

| Evidence | Support |
|---|---|
| Deciding word in a section header | 1.0 |
| Deciding word on the page | 0.85 |
| Keywords rank it 1st | 0.70 |
| 2nd-3rd | 0.45 |
| 4th-5th | 0.25 |
| none | 0 |

| Case | Confidence |
|---|---|
| Model ranked the winner, keywords back it | backed(p, support) |
| Model only | p; x keywords_disagree (0.7) when keywords ranked other leaves; x 0.9 when the model's top two are within 0.10 |
| Keywords only | 0.4 + 0.5 x support; x no_model without a model; x 0.8 when the model ranked others |
| Neither | p (0.3 with no model), x no_model |
| Then | x conflict when a Deciding word was overruled by the model (> 0.90 sure of another L1); an Others leaf capped at 0.6 |

The stage then applies the page's own doubts: x 0.9 handwritten, x 0.85 thin text
(`core-pipeline/modules/page_classify/stage.py:263`).

**Triggers.** `needs_review` when there is no page type, codability is needs_review,
confidence < 0.50, or the leaf is Others (`stage.py:279`).

**Stored.** `page_classification.confidence`, `confidence_level` (high / medium / low from
the bands, `core-pipeline/common/db/__init__.py:1273`), `needs_review`, `axes` JSON
(`l1/l2/l3_confidence`, `page_type_confidence`, `codeability_confidence`,
`specialty_confidence`, the leaf's working).

Blank / junk / duplicate pages carry the blank / junk confidence; an invoice page kept
Main carries the INVOICE rule's confidence (`stage.py:230`, `:246`).

**Legacy cascade (`PAGE_CLASSIFIER=axes`).** Level confidence: model p backed by keywords
or a title pin; without a model, keyword share x strength; Others: folded OTHER p or
1 - best, x fallback (`core-pipeline/modules/page_classify/axes.py:459`, `:486`). Keyword canon
confidence: title 0.6 + 0.05 per margin point, cap 0.9; body 0.3 + 0.05, cap 0.6
(`core-pipeline/modules/page_classify/arbitration.py:65`, `page_arbitration.json`).

### 6.2 Sub-type, codability, specialty

- **Sub-type** (review-ui): the deepest named L2 / L3 confidence; with the leaf classifier
  all levels carry the same path confidence (`leaf.py:465`).
- **Codability** (Codable / Non Codable): the path confidence (`codeability_confidence`).
  In Final it is re-scaled by the Final page type's confidence (section 9).
- **Specialty** (`axes.py:560`): model p backed by specialty keywords, x keywords_disagree
  when they name another; keywords only: their own value (x no_model without a model);
  General fallback: 1 - best named specialty, x fallback. The leaf path takes the leaf
  model's own specialty probability when >= 0.5, as a raw value (`leaf.py:473`).

**Shown.** Page Details Page Type, Sub-type, Specialty, Codable rows.

---

## 7. DOS, continuity, encounter, sequencing, provider

### 7.1 Date of service (stage 11)

`core-pipeline/modules/dos/dos_logic.py`, numbers in `dos_canon.json:19`
(kv 0.9, edge 0.8, dictation 0.5, keyword_pass 0.7, signature 0.6).

| Source | Confidence | Line |
|---|---|---|
| KV date the model scored | backed(model p, rule score) | `dos_logic.py:530` |
| ... another date of the same group within 80% of its score | x conflict (0.8) | `dos_logic.py:526` |
| KV date, rules only | rule score x no_model | |
| KV date with no score | 0.9 x 0.85 = 0.765 | |
| Edge keyword date | 0.8 x 0.85 = 0.68 | `dos_logic.py:377` |
| Keyword pass | 0.7 x 0.85 = 0.595 | `dos_logic.py:645` |
| Dictation date | 0.5 x 0.85 x 0.6 = 0.255 | `dos_logic.py:565` |
| Signature date | 0.6 x 0.85 = 0.51 | `dos_logic.py:650` |
| Admit / discharge pair | the weaker of the two | `dos_logic.py:618` |
| Neighbours agree | weaker neighbour's x 0.9 | `dos_logic.py:665` |

The KV rule score is geometric: `max(0.55, 0.92 - 0.12 x words between key and value)`
(`core-pipeline/modules/extraction/dos/extract.py:156`); keyless edge rows are a fixed 0.7
(`dos/extract.py:376`). The KV model takes the best DOS pair at any score
(`SELECT_THRESHOLDS = {"dos": 0.0}`, `core-pipeline/modules/extraction/util/config.py:45`).

**Triggers.** Under 0.5 the page tries the last signature date (`signature_fallback_below`).
A page with no DOS, a DOS under 0.5 or a fallback DOS is checked against sure neighbours
within 2 pages: it takes their date when both agree, or a stray date is dropped.

**Stored.** `dos_extraction_results.confidence` and `dates[].confidence`. The Final DOS is
the document's dated page's confidence; a default date (2222-02-02) has none.

### 7.2 Continuity / document splitting (stage 12)

`core-pipeline/modules/continuity/engine.py`. Each gap gets a points score (1 weak, 2
moderate, 3 strong, 5 decisive; + break, - join).

- Score confidence: `P = 1 / (1 + exp(-(score - b) / a))`, a = 2, b = 0 (to be fitted);
  confidence = `abs(2P - 1)` when the score points the way the gap was decided, else 0
  (`engine.py:719`). Score 1 -> 0.24, 2 -> 0.46, 3 -> 0.64, 5 -> 0.85; score 0 -> 0.
- Decisive signal or rule: at least 0.9; 0.9 x keywords_disagree when the score points the
  other way by >= 2 (`engine.py:730`).
- x conflict when sub-scores or decisive signals disagree, or pattern A / B fires.
- Final-pass split or merge that reverses a gap: backed((1 - old) / 2, 0.9) x overturned
  (`engine.py:740`).
- First page of the chart: 1.0, level high.

**Triggers.** A "low confidence" flag when < 0.50 (`review_below`) or the internal level
is low (`engine.py:803`); `review_required` when either gap of a page has a flag
(`engine.py:1164`).

**Stored.** `page_continuity_results.confidence`, `confidence_level`, `review_required`.
The engine also keeps an internal level from |score| (high >= 5, medium >= 2) that steers
the final pass; it is not the stored band.

### 7.3 Encounter type (stage 13)

`core-pipeline/modules/encounter/encounter_classify.py`, canon `encounter_canon.json`
(support: tier1 1.0, tier1_conflict 0.8, tier2 0.8, tier2_conflict 0.6).

- Page vote (`vote_confidence`, `encounter_classify.py:498`): encounter model probability
  for the setting, backed by the keyword support; x keywords_disagree when the model's own
  answer (>= 0.5) is another setting. No model: support x no_model, or 0.5 x 0.85 = 0.425.
- Family decides (Discharge Summary / Operative Note -> Inpatient; lab -> Not Applicable):
  backed(p, 1.0) (`:574`).
- First page decides: its vote, x conflict when another page with evidence disagrees (`:596`).
- Pages vote (first page x2): winner's share of the weight x weighted mean confidence of its
  voters (`:619`).
- Default F2F: model p for F2F x fallback (`:691`).

**Stored.** `encounter_type_results.confidence`. In Final, only when the setting is a
visit setting (not Not Applicable).

### 7.4 Key / value extraction, provider name, credentials, signature (stage 7)

`core-pipeline/modules/extraction/results.py:44`:

- Rule score: geometric, `max(0.55, 0.92 - 0.12 x word gap)`
  (`core-pipeline/modules/extraction/provider_name/extract.py:383`; same for member ID and
  signature dates).
- Model score: LightGBM ranker probability per candidate (`extraction/training/model.py:237`).
- Row confidence: backed(model, rule) when both; model x keywords_disagree when the rules
  rejected it; rule x no_model with no model.
- Credentials row naming its provider: the provider_name row's model p backed by the
  credentials rule score (`results.py:72`).
- Signature page confidence: each signer's row confidence x 0.75 when name or credentials
  came from outside the signature, x 0.9 when the page has several signatures; the page
  value is the mean over signers (`results.py:219`, `:373`). A row counts when accepted or
  scored > 0.7.
- No signature: the "not found" confidence (`absent`) for electronic_signature
  (`core-pipeline/modules/extraction/stage.py:408`).
- Page number: the row confidence (`results.py:405`).

**Stored.** Staging JSON (rows and `absent`), `provider_signature_results.confidence`,
`additional_page_details.confidence`, `imaging_final.provider_confidence`.

**Shown.** Page Details Provider Name, Credentials, Signature rows (one value for all three).

### 7.5 Page sequencing (stage 14)

`core-pipeline/modules/sequencing/detect.py:160`:

- Marker confidence: "page x of y" 1.0, "page x" 0.85 (`core-pipeline/modules/sequencing/stage.py:93`);
  engine patterns 1.0 / 0.92 / 0.85 / 0.78 (`core-pipeline/modules/sequencing/engine/marker_detector.py:210`, `:165`).
- A page a marker family placed: its marker's confidence.
- A held page (two claim one number, or it would move too far): marker x conflict x
  fallback (1.0 -> 0.48).
- Any other page: the share of numbered pages that did not move (with >= 3), else
  1.0 x fallback = 0.60.

**Stored.** `page_sequencing_results.confidence`. Not part of the Final confidences.

---

## 8. imaging_final (stage 15)

`final_confidences` (`core-pipeline/modules/imaging_final/stage.py:186`) picks each Final
value's confidence from the page that decided it. Nothing new is modelled here.

| Key | Source |
|---|---|
| `page_type` | confidence of the page whose type the document took, x conflict when another page of the document is >= 0.80 sure of another L1 |
| `codability` | page_type base x codeability / path confidence, same conflict |
| `specialty` | the encounter's first page's `specialty_confidence` |
| `dos` | the document's dated page; none for a default date |
| `encounter_type` | the encounter's first page, when a visit setting |
| `member` | the page's own |
| `provider` | the page's own signature confidence |
| `blank_junk` | the page's own verdict, Main included |
| `handwriting` | the page's raw `hw_confidence` |

**Triggers.** `review_fields` = every key below 0.50; `needs_review` is set when any is,
or when the Final page type verdict asks for it (`stage.py:244`, `:506`).

**Stored.** `imaging_final.confidences` (JSONB), `review_fields`, `needs_review`,
`provider_confidence`. Quality, orientation, tilt, visibility, continuity and sequencing
confidences are not in this set.

---

## 9. review-ui: how confidences are shown

- **Format.** Percent with one decimal; "NA" when missing or skipped
  (`review-ui/frontend/src/ImagingPanel.tsx:260`).
- **Colour bands** (`ImagingPanel.tsx:271`, `:281`; CSS `review-ui/frontend/src/styles.css:6092`):
  >= 80% green (good), 50-80% amber (passable), < 50% red and bold (review; the tooltip says
  "Below 50%: review this value").
- **Which number.** `finalOr`: the Final value's confidence when `imaging_final.confidences`
  has it, else the Extracted stage's (`ImagingPanel.tsx:275`).
- **Rows with a confidence.** Handwritten / Printed, Handwritten Area, Visibility, Quality
  (shows `quality_score`), Orientation, Tilt, Mirrored, Encounter Type, Blank / Junk,
  Duplicate (display curve, section 4.3), Page Type, Sub-type, Specialty, Codable,
  Provider Name / Credentials / Signature, DOS, Member.

---

## 10. Accuracy metrics (review-ui) and the OCR match rate

These measure the pipeline against ground truth or against itself. They are not
confidences, but they are the other numbers on screen.

### 10.1 OCR match rate (Page viewer)

`review-ui/frontend/src/ocrMatchRate.ts`: the page text of each available engine
(Tesseract, Docling, Azure), tokens of 2+ letters / digits, as sets. Rate = mean of the
pairwise Jaccard values (one pair for 2 engines, 3 pairs for 3); NA with fewer than 2.
Colours: > 90% green, 70-90% amber, < 70% red (`ocrMatchRate.ts:135`). Not stored.

### 10.2 Accuracy View

Built server-side from the view `v_accuracy_page` and cached
(`review-ui/backend/app/services/accuracy_cache.py`); scored in
`review-ui/frontend/src/accuracy.ts`. Charts last run before 2026-10-12 are left out.

| Metric | Rule (short) |
|---|---|
| Seven streams: Member Verification, DOS, Blank / Junk, Page Type, Codable / Non-Codable, Provider Signature, Orientation | Each page is Match / Partial / No Match / Not Available against the label. Only Page Type has a Partial (0.5): same L1 or a listed neighbour (`accuracy.ts:32`). |
| Stream accuracy | (Match + 0.5 x Partial) / scored (`accuracy.ts:431`) |
| Overall | per page, k of the 7 streams match; accuracy = pages with 7 of 7 / scored; bands Good 5-7, Fair 3-4, Bad 0-2 (`accuracy.ts:660`, `:958`) |
| Class-1 precision / recall / F1 | per stream on the "Yes" class (blank / junk, rotated, Name & DOB present, signed, dated); Provider Signature also "Cred. M" (credentials match share) |
| Visibility Match % (Additional Metrics) | labelled Visible Yes / No pages the pipeline measured: matches / scored (`accuracy.ts:217`) |
| Invoice | ground truth Is Invoice = Yes or Invoice in Encounter Type; pipeline Final Page Type = Invoice. Precision, recall, F1, accuracy from both / GT only / algo only / neither (`accuracy.ts:242`) |
| Duplicate | GT Is Duplicate vs pipeline; Yes and Partial both count as a duplicate on either side; precision, recall, F1, accuracy plus a 3 x 3 matrix (`accuracy.ts:252`) |

Manual Review (ExcelCompare) has its own Exact Match Rate = correct / total ground truth
(`review-ui/backend/app/services/excel_compare/compare.py:262`).

---

## 11. Inconsistencies and open questions

1. **Fixed constants remain**, against the "never fixed" rule:
   blank / junk R1E 1.0, R3 card 0.95, R3V 0.90, HW 0.90, short page 0.90, fallback
   constants 0.85 / 0.9 / 0.95 / 0.99 / 0.7; sequencing marker values 1.0 / 0.92 / 0.85 /
   0.78; KV rule geometry floor 0.55 and keyless DOS 0.7; signature factors 0.75 / 0.9 live
   in code, not in `confidence_canon.json`; member NER support is a fixed 0.4 while the
   GLiNER score is only logged.
2. **Values that bypass `conf.score`** (no penalties, no calibration hook): raw
   `hw_confidence` from the model, visibility, the leaf model's specialty probability,
   duplicate Jaccard scores, DOS neighbour value (min x 0.9), encounter tally, signature
   mean. Several `conf.score` calls pass no `module` (blank / junk R5 and P1, encounter
   first page, sequencing scan order, stage 1 HW fallback, Final page type), so a future
   per-module calibration would not reach them.
3. **Calibration is empty.** No value is calibrated yet, so 0.80 does not yet mean "right
   8 times in 10".
4. **Quality score is not a confidence** but is shown in the confidence column with the
   confidence colours. The tag can be lowered (handwritten, routing, 3-engine re-grade)
   while the score stays, so a green 85% can sit next to "Medium" or "Low".
5. **Docs and schema lag the code.** `CLAUDE.md` section 3 still describes `quality_tag` as
   a placeholder from `printed_or_handwritten`; the code measures it. `schema/schema.sql:603`
   still says 0.70 / 0.40.
6. **Two overlap formulas.** The pipeline routes on Dice over word counts; review-ui's match
   rate is Jaccard over token sets with 90 / 70 colour cut-offs. The same page gets two
   different "overlap" numbers.
7. **Blank / junk service confidences can describe the other verdict.** When the service
   turns a low-confidence JUNK into KEEP (`decision.py:204`, `:209`), the confidence stays the
   junk-side number, and R5 then stores it as the Main verdict's confidence.
8. **The 0.99 keep rule is unreachable for R5.** The service already turns any JUNK with
   p(keep) > 0.10 into KEEP (keep veto), so `JUNK_UNLESS_KEEP_AT = 0.99` (`method.py:618`) can
   only act on the R3 card rule.
9. **Unused canon thresholds.** `distilbert.junk_at`, `review_junk_at`,
   `pass1_final_keep_above`, `pass1_final_junk_above` are read but never used; the
   `method.py` module docstring still describes a pass-1 DistilBERT keep / junk that `decide()`
   no longer runs (pass 1 is rules only, survivors get 0.30).
10. **Duplicate display confidence never goes below 0.5**, so it can never be red, and a page
    never compared shows 100%.
11. **Member confidence differs between DB and CSV**: the database stores share x mean
    page confidence; the CSV writes pages verified / pages checked.
12. **Continuity levels vs bands.** A score of 2 is "medium" inside the engine but its
    confidence is 0.46, which is the review band. a and b of the curve are still the
    placeholders 2 and 0.
13. **Encounter family decisions** read 0.5 with a model that gives the setting p = 0 (for
    example Not Applicable on a lab page) but 0.85 with no model at all.
14. **Azure word confidences are stored and never read**; handwriting and OCR quality
    could use them.
15. **Final confidences leave out** quality, orientation, tilt, visibility, continuity and
    sequencing, so those never set `imaging_final.needs_review` on their own.
16. **Quality tag vs the confidence bands.** The quality tag keeps 0.70 / 0.40 (user,
    2026-11-14, for Doc Intel volume) while every confidence uses 0.80 / 0.50.


---

## 12. Proposed: one confidence rule for every Final metric (2026-11-14, for decision)

### 12.1 The rule

**Confidence = how sure we are that the value shown is correct.**

1. **The model's own probability for the value it chose**, unchanged: no keyword backing,
   no penalty multipliers. If the KV model scored the row, use its `model_score`. If the
   blank / junk classifier decided, use its p for that class.
2. **A backup technique decided the value** (rules only, keywords only, NER fallback,
   a neighbour's value, a default): a **fixed low, `BACKUP_LOW` = 0.40**.
3. **The value goes against the model** (a rule overrode the model's answer, or a veto
   turned it round): a **fixed low, `FLIPPED_LOW` = 0.30**.
4. **No value, or a default value** (no DOS found, default date 2222-02-02, nothing
   extracted): **no confidence (NA)**, not a number.
5. A **measured fact** that no model guesses (a page with no ink, no OCR text): proposed
   **1.0** (decided 2026-11-14). This is the one exception to rule 2.
6. **Carried by continuity** (user, 2026-11-14): when a page takes its value from its
   document's anchor page (the page that decided it: Final page type, sub-type, codable,
   DOS, encounter type, specialty), its confidence is **the anchor page's confidence x 0.90**
   (`CARRIED_FACTOR` = 0.90). The anchor page itself keeps its own confidence. A backup or
   flipped anchor carries its fixed low the same way (0.40 -> 0.36, 0.30 -> 0.27).

Both fixed lows sit under 0.50, so every backup or flipped value is flagged for review
(`review_fields`). The two numbers go in `confidence_canon.json` so they can be changed
without code. The 0.80 / 0.50 colour bands stay.

What goes away: `conf.backed` (keyword lift), the penalty multipliers (`no_model`,
`keywords_disagree`, `conflict`, `fallback`, `degraded`), the 0.75 / 0.9 signature
factors, and the hand-set rule strengths (`RULE_SUPPORT`, card 0.95, short page 0.90 and
so on) as confidences. The rules still decide the value; only the number changes.

### 12.2 Per Final metric

| Final metric | Proposed confidence | Backup -> 0.40 | Flipped -> 0.30 | NA |
|---|---|---|---|---|
| **Member name / DOB / member ID** | **three separate confidences** (decided 2026-11-14): each field's own KV `model_score`; no combined member confidence. Each field is its own entry in `review_fields` | Rules only (no model score); GLiNER / NER; manifest name found by text search only | KV model rejected the row but the rules kept it (`rule_accepted` vs model) | nothing extracted |
| **Provider name** | KV `model_score` of the provider_name row | rules only (geometry score) | rules kept a row the model rejected | no provider |
| **Credentials** | the provider_name row's `model_score` when the credentials sit with that name (as today, without the backing) | credentials from anywhere else: the model never scores credentials | | none |
| **Signature present** | KV `model_score` of the electronic_signature row; drop the 0.75 / 0.9 factors; several signers: the lowest | signature found by rules only | | see 12.3 (no signature) |
| **Blank / Junk / Main** | Main: p(keep). Blank: p(blank). Junk: p(junk). From the DistilBERT service's raw probabilities | short-page rule, fax rule, index rule, lab / clinical-content keep, HW > 30% keep, visual < 20 words keep, pass-1 rules with no model run | a rule or the service's own veto went against the model's top class: signed page kept (R4) while p(junk) was top; card rule against the model; keep veto (p(keep) > 0.10 turns JUNK to KEEP); weak JUNK turned KEEP; invoice kept Main | |
| **Junk subtype** | the classifier's probability for that subtype if the service exposes it, else the p(junk) bucket | subtype from a rule (Cards, Letter/Fax, Others by rule) | card rule overriding the model's Invoice | |
| **Invoice** (page type) | p(junk) of the service's Invoice call (the page is Invoice because the model said so) | | a terms page moved to Junk / Others | |
| **Duplicate: Yes / Partial** | from the **match score** s to the original (Jaccard of body words; exact = 1.0), scaled so 95% reads 50% (user, 2026-11-14): 1.0 -> 1.0, 0.98 -> 0.80, 0.95 -> 0.50, floor 0.30. See 12.2.1 | | | |
| **Duplicate: No** | from the **best match score** s to any compared page (user, 2026-11-14): s < 0.80 -> **1.0**; 0.80-0.90 -> good, 1.0 falling to 0.80; 0.90-1.00 -> 0.80 falling to 0.30 (0.95 -> 0.55, 0.98 -> 0.40 flagged). See 12.2.1 | | | page with nothing to compare (no earlier page): 1.0 |
| **Page type / sub-type** (Extracted) | leaf model p for the chosen leaf | keywords only (no model, or model unsure); Others leaf | a Deciding keyword overruled the model, or the model overruled a Deciding keyword | no page type |
| **Page type / sub-type** (Final) | anchor page: its Extracted confidence (above); every other page of the document: anchor x 0.90 (rule 6) | | rules 7 / 8 re-placed the page (a lab page inside a Progress Note) | |
| **Codable** | same as the page type it comes from (it is a lookup from the type), so x 0.90 on carried pages | | | |
| **Specialty** | specialty model p on the encounter's first page; carried pages anchor x 0.90 | keywords only; General fallback | keywords named another specialty than the model | |
| **DOS** | KV `model_score` of the chosen DOS pair on the dated (anchor) page; carried pages anchor x 0.90 | edge keyword, keyword pass, dictation date, signature date, neighbour's date, admit / discharge pair from rules | a rule picked another date than the model's best | no DOS; default 2222-02-02 |
| **Encounter type** | encounter model p for the chosen setting, on the deciding (anchor) page; carried pages anchor x 0.90 | family rule (Discharge Summary -> Inpatient, lab -> Not Applicable), keywords only, default F2F | page vote chose a setting other than the deciding page model's top | Not Applicable shown with no model |
| **Handwritten / Printed / Form / Visual** | page-tag model's softmax for the chosen type (as today) | older 2-class model's rule upgrade (ink check) | handwritten-share rule changed the model's type (Handwritten with < 10% -> Printed; Printed with > 10% -> Mixed / Handwritten) | |
| **Visibility** | page-tag model's visibility p (as today) | | | older model: not measured |
| **Rotation / orientation** | see 12.3 | | | |
| **Quality / Final Quality** | see 12.3 | | | |
| **Continuity (document breaks)** | see 12.3 | | | |
| **Page sequence** | see 12.3 | | | |
| **Member verification (chart)** | not a confidence: show it as an **agreement rate** (pages agreeing / pages checked), one formula for the DB and the CSV | | | |

#### 12.2.1 Duplicate "No" confidence (user, 2026-11-14)

Two pages under 80% alike are not duplicates: no doubt. Between 80% and 90% the answer No is
still good. Above 90% the page came close (a near miss, or a hard block stopped a high match),
so No becomes doubtful and is flagged near the top.

| Best match s | Confidence in "No" |
|---|---|
| < 0.80 | 1.0 |
| 0.80 to 0.90 | 1.0 - 2 x (s - 0.80): 0.80 -> 1.0, 0.85 -> 0.90, 0.90 -> 0.80 |
| 0.90 to 1.00 | 0.80 - 5 x (s - 0.90): 0.92 -> 0.70, 0.95 -> 0.55, 0.98 -> 0.40, 1.00 -> 0.30 |

**Yes / Partial** (user, 2026-11-14: scale down to about 50% at a 95% match):

| Match s | Confidence in "Yes" / "Partial" |
|---|---|
| 1.00 (exact) | 1.0 |
| 0.99 | 0.90 |
| 0.98 | 0.80 |
| 0.97 | 0.70 |
| 0.95 | 0.50 |
| 0.90 to 0.95 | 0.30 floor (only Rule B pages, three in a row averaging >= 0.95, reach here) |

Formula: `max(0.30, 1 - 10 x (1 - s))`. A Partial is a blocked 100% match, so it is always
1.0 on the match; the block is why it is not called Yes.

This replaces today's review-ui
V-curve (section 4.3). The cut points 0.80 / 0.90 and the floor 0.30 go in
`confidence_canon.json`.

### 12.3 Where we cannot get a true confidence

| Metric | Why | Proposal |
|---|---|---|
| **No signature / nothing found** | A model scores candidates that exist; it gives no probability that a thing is absent | 1 - best signature candidate's `model_score` when the model scored any; none scored -> `BACKUP_LOW` |
| **Pass-1 blank / junk** | DistilBERT does not run in pass 1 (decided: it stays off); pass-1 verdicts are rules on Tesseract text of near-empty pages | Fixed 0.95 per text-pattern rule (`PASS1_RULE`); no ink / no text 1.0; exact duplicate 1.0 |
| **Credentials** | The KV model never scores credentials | Borrow the provider_name row's p when they sit together, else `BACKUP_LOW` (above) |
| **Image quality / Final Quality** | A measurement (sharpness, contrast, overlap), not a guess with a probability | Show the score as a measurement, not in confidence colours; no confidence |
| **Rotation / orientation** | Tesseract OSD gives a score, not a probability; today it is squashed `1 - exp(-c/3)` | Keep the squashed value but label it "OSD score", or calibrate on the rotation ground truth |
| **Continuity** | Points score turned into a number by a curve whose constants are placeholders | No confidence until fitted on ground truth; meanwhile decisive rules -> no number, points-only breaks -> `BACKUP_LOW` |
| **Page sequencing** | Rules on page markers, no model | Marker found ("page x of y") -> no confidence needed (read off the page); held / unnumbered pages -> `BACKUP_LOW` |
| **Junk subtype probability** | Needs checking whether the service returns per-subtype probabilities | If it does, use it; if not, the p(junk) bucket |
| **Any model probability** | Raw probabilities are not calibrated: 0.80 does not yet mean right 8 times in 10 | Fit a calibration per model on the ground truth (`confidence_canon.json` already has an empty calibration slot) |

### 12.4 Decisions (all made 2026-11-14)

1. (Decided 2026-11-14: two fixed lows, `BACKUP_LOW` = 0.40 and `FLIPPED_LOW` = 0.30, so a
   flipped value sorts ahead of a backup one in review.)
   (Carried by continuity = anchor x 0.90: decided, 2026-11-14.)
2. (Decided 2026-11-14: measured facts, no ink / no OCR text -> Blank, are 1.0.)
3. (Decided 2026-11-14: member name, DOB and member ID keep separate confidences.)
4. (Decided 2026-11-14: duplicate "No" = 1.0 under 80%, good 0.80-0.90, falling to 0.30 above 90%; 12.2.1.)
5. (Decided 2026-11-14: DistilBERT does **not** run in pass 1. The pass-1 text-pattern rules
   get a fixed **0.95** each: the word "blank" on a page of a few characters, "intentionally
   left blank" (R2), a cover mark on a page under 10 words, "End of Report" under 10 words
   (R1E), a page under 15 characters. No ink / no text / stage-1 Blank stay 1.0; an exact
   duplicate is its 1.0 match. `PASS1_RULE` = 0.95 in `confidence_canon.json`.)

### 12.5 Changes this needs (once agreed)

| Where | Change |
|---|---|
| `core-pipeline/modules/confidence.py`, `resources/canon/confidence_canon.json` | add `BACKUP_LOW` (0.40), `FLIPPED_LOW` (0.30), `CARRIED_FACTOR` (0.90), `PASS1_RULE` (0.95); helper `chosen(p)`, `backup()`, `flipped()`; stop using `backed` and the penalty table for Final values |
| `core-pipeline/modules/extraction/results.py` | row confidence = `model_score`; rules only -> backup; `rule_accepted` disagreement -> flipped; drop the 0.75 / 0.9 signature factors; several signers -> lowest |
| `core-pipeline/modules/member/stage.py` | one confidence per field (name, DOB, member ID) = its KV `model_score`; NER / rules only -> backup; chart value renamed agreement rate (DB and CSV the same) |
| `core-pipeline/modules/blank_junk/method.py`, `stage.py`, `classify.py` | verdict confidence = the service's p for the final class; rule-decided -> backup; against the model's top class -> flipped; remove `RULE_SUPPORT` / fixed 0.90 / 0.95 as confidences |
| `core-pipeline/modules/blank_junk/dup_rules.py` / `stage.py`; `review-ui/.../duplicate_label.py`, `duplicateLabel.ts` | Yes / Partial = max(0.30, 1 - 10 x (1 - match)); No by 12.2.1 (1.0 under 80%, 1.0 -> 0.80 to 90%, 0.80 -> 0.30 to 100%); remove the V-curve |
| `core-pipeline/modules/page_classify/leaf.py`, `stage.py`, `axes.py` | page type = leaf model p; keywords only / Others -> backup; keyword vs model override -> flipped; drop the handwritten / thin-text multipliers |
| `core-pipeline/modules/dos/dos_logic.py` | KV pair `model_score`; every other source -> backup; default date -> NA |
| `core-pipeline/modules/encounter/encounter_classify.py` | model p for the setting; family rule / keywords / default -> backup; vote against the model -> flipped |
| `core-pipeline/modules/image_preprocess/hw_printed.py` | handwritten-share rule changing the type -> flipped |
| `core-pipeline/modules/imaging_final/stage.py` | carry the new per-field confidences (`member` splits into `member_name`, `member_dob`, `member_id`; review-ui shows one per row); a value taken from the document's anchor page = anchor x `CARRIED_FACTOR` (0.90), the anchor keeps its own (today the anchor's value is copied unchanged, x conflict); `review_fields` unchanged (< 0.50) |
| review-ui | quality score shown as a measurement (no confidence colours); duplicate confidence from the new values |
| tests | each module's confidence tests rewritten to the new rule |

Expected effect: more values under 0.50 (every backup and flip is flagged), and the numbers
that remain high are the models' own. After building, measure on the dev DB how many Final
values move into review before switching it on for the VM.
