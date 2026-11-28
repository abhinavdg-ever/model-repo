# Page Readability Logic v2

Nov 28, 2026 · @Algodel

## At a glance

**v2 judges readability on the best OCR text we have, not on Tesseract alone: a page is readable when that text reads as words and the OCR engines agree on them, not readable when it does not (and the page is poor or the engines disagree), and judged on the image when there are too few words.** The visibility model becomes a second opinion, blank pages become Not Applicable, and every answer carries a rule, a score and a review flag for close calls.

*Decision flow (first rule that fires decides; text score = 2 parts word score + 1 part OCR agreement; every verdict has a confidence): R0 Blank page → Not Applicable · R1 20+ tokens, text score ≥ 0.60 → Yes (review if the model said not visible or engines disagree) · R2 model very sure not visible (0.90+) and text short or text score < 0.35 → No · R3 text score < 0.30 and Final Quality Low or OCR agreement < 30% → No · R4 under 20 tokens, ink badly blurred or faded (under 30) → No · R5 nothing above → Yes (review when text score 0.30–0.60 or OCR agreement < 30%). Every No is flagged for review.*

"Best text" is Azure Document Intelligence when it read the page, else Docling, else Tesseract. The word score is today's gibberish score (dictionary share and word-shape share), plus Azure's own word confidence when Azure read the page. The **text score** adds the **OCR match rate** as a factor: two parts word score, one part agreement between the best text and the OCR engine closest to it.

## What changes from today

**Today a page is No only when the visibility model says so, or when three weak signals all line up. v2 asks a direct question: can the best OCR engine we ran read words on this page?** The Yes / No column and its meaning for reviewers stay.

| Topic | Today | v2 | Why |
| --- | --- | --- | --- |
| **Which text is tested** | Tesseract only, even on handwritten and Low pages where Azure read the page | The **best text** (Azure, else Docling, else Tesseract); Tesseract's score is kept as a diagnostic | A faxed handwritten note that Azure reads well is readable, even if Tesseract makes noise of it |
| **Visibility model** | "Not visible" decides No, whatever the text says (example 2: 150 good tokens, still No) | Decides No only when the text is also poor; a good text overrules it and sets a review flag | The text is the direct evidence; the model is a second opinion |
| **Shape of the rule** | Low **and** match < 50% **and** gibberish; one missing value gives Yes | A short decision table: readable by text, not readable by text, not readable by image, else Yes with a review band | One poor signal on its own (match rate 61%, example 5) no longer decides; two strong ones do |
| **Azure word confidence** | Not used | Part of the word score when Azure read the page | Azure says how sure it is of each word, which is the best handwriting signal we have |
| **OCR match rate** | Only one of three AND conditions, and the three-engine average, Tesseract included | A third of the text score, as the agreement of the best text with the engine closest to it; today's match rate stays for R3 and the Image Quality list | Words two engines read alike are real words; Tesseract's noise on handwriting no longer drags an Azure-read page down |
| **Pages with under 20 tokens** | Never judged: always Yes | Judged on the image: sharpness and contrast of the inked area | A faded scrawl with 8 tokens can be unreadable |
| **Blank pages** | Yes (unless not visible) | **Not Applicable** | Readability means nothing on a blank page |
| **Output** | Yes / No and a reason | Yes / No, plus a 0–1 score, a confidence, the rule that decided, and a review flag for the uncertain band | Reviewers see the close calls; accuracy can be tuned on the score |
| **Settings** | Code constants | `resources/canon/readability_canon.json` | Thresholds tuned without a code change |
| **Image Quality list** | Mixes "not readable" and "Low, match < 50%" | Two named reasons, kept apart: **Not readable** and **Low quality** | Two different questions, two different labels |

## The readability profile

**Every page gets one readability profile, measured once after the final OCR; the rule only reads it.** The profile is stored with the result, so a reviewer sees why.

| Group | Field | What it is | Source |
| --- | --- | --- | --- |
| Page | `blank` | The page's final blank / junk verdict is Blank | Blank / junk |
| Page | `handwritten_pct`, `stage1_quality`, `final_quality` | Handwritten share; stage-1 grade; Final Quality after routing | Stage 1, routing |
| Visibility | `visible`, `p_not_visible` | The page-tag model's answer and its probability | Stage 1 (ConvNeXt) |
| Best text | `best_engine` | Azure if it read the page, else Docling, else Tesseract | OCR |
| Best text | `tokens` | Word tokens, as today: punctuation trimmed, numbers and dates dropped | Best text |
| Best text | `dictionary_share`, `shape_share` | Today's two shares, on the best text | Best text, project vocabulary + English list |
| Best text | `azure_word_conf` | Mean Azure word confidence (only when Azure read the page) | Azure result JSON |
| Best text | `word_score` | Section 4 | Computed |
| Best text | `text_score` | Section 4: word score and OCR agreement, 2 to 1 | Computed |
| Agreement | `match_rate` | Today's `final_match_rate()`: three-engine overlap after Azure, else Docling vs Tesseract | Routing |
| Agreement | `ocr_agreement`, `agreement_pair` | Word overlap of the best text with the other engine that agrees with it most (with Azure: the higher of Azure vs Docling and Azure vs Tesseract; without: Docling vs Tesseract), before the edge cap; and which pair it was | OCR texts, today's `overlap()` |
| Diagnostic | `tesseract_word_score` | Today's gibberish score on Tesseract (kept for comparison, never decides) | Tesseract |
| Image | `ink_pct` | Share of the page with ink | Page image |
| Image | `sharpness` | Variance of the Laplacian on the inked area, on a page scaled to 1,600 px height | Page image |
| Image | `contrast` | 95th minus 5th percentile of grey on the inked area (0–255) | Page image |

The two image measures are new and cheap (one pass over a scaled grey image). They are only used for pages with under 20 tokens, where text cannot be judged.

## The rule

**Rules run in order and the first that fires decides.** Thresholds are canon values; the numbers below are the starting values, to be tuned on reviewed pages (section 7).

**No is kept rare and strict.** A page is called Not readable only when the evidence is clear: the text is near-garbage (text score under 0.30) and a second signal agrees, the visibility model is very sure (0.90 or more) and the text is poor, or the ink itself is badly blurred or faded. Everything in between is **Yes with a review flag**, never No. Every No is highlighted: it always sets the review flag (`not_readable`), shows as a red **Not readable** badge in review-ui, is listed first in the chart's Image Quality list and counted in the chart summary. The weekly share of No pages goes to `metrics`; a week above `no_alert_share` (start 2% of pages) is flagged so the thresholds get checked.

### Word score and text score

On the best text, with 20 or more tokens:

- **Without Azure:** `word_score = (dictionary_share + shape_share) / 2` (today's score, on a better text).
- **With Azure:** `word_score = (dictionary_share + shape_share + azure_word_conf) / 3`. Azure's own word confidence is the strongest signal on handwriting.

**OCR agreement** is the match rate between OCR engines, measured from the best text: the word overlap (today's `overlap()`, before the edge cap) of the best text with the other engine that agrees with it most. With Azure, the higher of Azure vs Docling and Azure vs Tesseract; without Azure, Docling vs Tesseract. It is not today's `final_match_rate()`: that averages all three pairs, Tesseract included, and Tesseract's noise on handwriting would pull down exactly the pages Azure reads well.

**Text score** joins the two, the words counted twice:

`text_score = (2 × word_score + ocr_agreement) / 3`

- When only one engine has text, agreement is not measured and `text_score = word_score`; the reason says so.
- Words that look right but that no second engine confirms (agreement under 0.30) set the review flag, whatever the score: an engine may be inventing words, or the other may be failing.

### Decision table

| ID | Rule | Fires when | Is Readable | Confidence | Review |
| --- | --- | --- | --- | --- | --- |
| **R0** | Blank page | The final blank / junk verdict is Blank | **Not Applicable** (NULL in the database, "Not Applicable" in the CSV and the UI) | The Blank verdict's own confidence | — |
| **R1** | Readable by its text | 20+ tokens and `text_score` ≥ 0.60 | **Yes** | 0.70–0.99, by how far the text score is above 0.60; at most 0.70 when the model or the engines disagree | Yes, when the visibility model said not visible ("model disagrees") or OCR agreement is under 0.30 ("engines disagree") |
| **R2** | Not visible, no good text | The visibility model says not visible with p ≥ **0.90**, and the text is poor: under 20 tokens, or `text_score` under **0.35** | **No** | 0.70–0.99, by how far p(not visible) is above 0.90 and the text score below 0.35 | Yes, always (every No is highlighted) |
| **R3** | Not readable by its text | 20+ tokens, `text_score` under **0.30**, and Final Quality Low **or** OCR agreement under **0.30** | **No** | 0.70–0.99, by how far the text score is below 0.30 | Yes, always |
| **R4** | Too little text, poor image | Under 20 tokens, ink on the page, and `sharpness` under **30** **or** `contrast` under **30** | **No** | 0.55–0.80, by how blurred or faded the ink is | Yes, always |
| **R5** | Nothing above | — | **Yes** | 0.50–0.70, by the text score (or the image when under 20 tokens); 0.50 when nothing was measured | Yes, when `text_score` is **0.30**–0.60, OCR agreement is under 0.30, or Final Quality is Low with match rate under 0.50 |

**Missing values** lean to Yes, as today: an unknown visibility is not "not visible", a missing match rate or agreement is not "under 0.50" (the text score is then the word score), an image that could not be measured fails no image test. A rule that cannot be evaluated is skipped and named as `not measured` in the record.

### Readability score

One number from 0 to 1 for the review UI and for tuning: the `text_score` when the page has 20+ tokens; otherwise `min(1, sharpness / 120, contrast / 120)` from the image. It explains the verdict; it does not replace the rules.

### Readability confidence

How sure the pipeline is of its Yes / No, from 0 to 1. Each rule turns its **margin** (how far the page is past the rule's threshold, 0 to 1) into a confidence inside its own band. Bands and thresholds are canon values.

| Rule | Margin | Confidence |
| --- | --- | --- |
| R0 | — | The Blank verdict's own confidence (1.0 for a measured blank, 0.97 for a placeholder, at most 0.90 on a short page) |
| R1 | min(1, (text\_score − 0.60) / 0.30) | 0.70 + 0.29 × margin; at most 0.70 when the model or the engines disagree |
| R2 | Mean of min(1, (p\_not\_visible − 0.90) / 0.10) and the text margin (1 under 20 tokens, else min(1, (0.35 − text\_score) / 0.35)) | 0.70 + 0.29 × margin |
| R3 | min(1, (0.30 − text\_score) / 0.30) | 0.70 + 0.29 × margin |
| R4 | min(1, max((30 − sharpness) / 30, (30 − contrast) / 30)) | 0.55 + 0.25 × margin |
| R5 | Text: (text\_score − 0.30) / 0.30; under 20 tokens: min(1, (min(sharpness, contrast) − 30) / 60) | 0.50 + 0.20 × margin; 0.50 when nothing was measured |

- **Any confidence under 0.70 sets the review flag**, so the flag and the confidence never disagree.
- **Score and confidence are different numbers.** The score says how readable the page looks; the confidence says how sure the Yes / No is. Text score 0.62 gives score 0.62 and confidence 0.72 (a narrow Yes); text score 0.05 gives score 0.05 and confidence 0.99 (a sure No).
- **Calibrated in shadow:** per confidence band (0.50–0.60, 0.60–0.70, …) the share of pages the reviewer agreed with goes to `metrics`; a band that is over- or under-confident has its floor moved in canon.

### Reason

Stored with every verdict, as today, now with the rule ID and both parts of the text score: `R1: text score 0.67 (Azure words 0.72, agreement 58%; model said not visible)`, `R3: text score 0.27 (words 0.30, agreement 20%)`, `R5: text score 0.58, engines disagree (agreement 15%)`, `R4: 8 tokens, sharpness 22`.

## Outputs, display and accuracy

**Is Readable stays a Yes / No column; three small columns explain it, and the full profile goes with the routing detail.** Computed where it is today (stage 5, `_store_route`, the second call after Azure), but after the pass-2 blank / junk verdict is known for R0. Until then, R0 is applied when the Final step copies the value (stage 15).

| Place | Field | Content |
| --- | --- | --- |
| `ocr_quality_results` | `is_readable` (BOOLEAN) | Yes / No; NULL for Not Applicable (R0) and older runs |
| `ocr_quality_results` | `readability_score` NUMERIC(5,4) (new) | Section 4 |
| `ocr_quality_results` | `readability_confidence` NUMERIC(5,4) (new) | How sure the Yes / No is (section 4); for Not Applicable, the Blank verdict's confidence |
| `ocr_quality_results` | `readability_rule` VARCHAR(8) (new) | `R0`–`R5`: tells Not Applicable apart from an older run's NULL |
| `ocr_quality_results` | `readability_review_required` BOOLEAN (new) | The review band of section 4 |
| `ocr_quality_results.quality_detail.routing` | `readability` | The whole profile, the rule, the reason, and each rule's `no` / `fired` / `not measured` |
| `imaging_final` | `is_readable`, `readability_rule`, readability\_confidence | Copied by stage 15 |
| Final CSV | `is_readable` and readability\_confidence (appended at the end, in that order) | The client columns before it stay as they are, `is_visible` included |
| Cosmos page doc / staging | `quality.readability`; rows of `05_ocr_final2.json` | Same fields, Cosmos names |

New columns follow CLAUDE.md §4–§5: `schema.sql`, a dated migration, `migrate.sql`, the changelog, every reader and writer.

### review-ui

| Data | Shown as |
| --- | --- |
| `is_readable` true | **Yes** (confidence beside it, e.g. 0.77; score on hover) |
| `is_readable` false | **No** (confidence, score and reason) |
| `readability_rule` = R0 | **Not Applicable (blank page)** |
| Older run (no rule), `is_visible` known | Visible / Not visible, as today |
| Nothing known / stage not run | Not Available / Yet to process, as today |
| `readability_review_required` | A **Review** badge next to the value |

The confidence beside Is Readable becomes the **readability confidence**, not the visibility model's confidence; the readability score shows on hover.

**Image Quality list**, three reasons kept apart:

- `Not readable (R2 / R3 / R4: why)` when Is Readable is No;
- `Readability: review (why)` when the review flag is set;
- `Low quality, match rate NN%` when Final Quality is Low and the match rate is under 50%, whatever Is Readable says.

### Accuracy

The reviewer's Yes / No moves from `page_ground_truth.is_visible` to a new `page_ground_truth.is_readable` (migration copies it; `is_visible` is read as a fallback until every reader moves). Pages that are Not Applicable in the pipeline or Blank in the truth are not counted. Accuracy is also reported per rule, so a rule that is often wrong shows up.

## Worked examples

**Today's eight example pages, re-judged by v2: five Yes / No answers flip (three to Yes, two to No) and one becomes Not Applicable; a ninth page shows what the OCR agreement adds.** The first three columns are from today's document; the best-text and agreement values are illustrative. Text score = (2 × words + agreement) / 3.

| # | Today's inputs | Today | Best text, agreement, text score (illustrative) | v2 rule | v2 · confidence | Review |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Visible, High, match 96%, Tesseract score 0.885 | Yes | Docling, 310 tokens, words 0.90, agreement 0.96: text **0.92** | R1 | **Yes** · 0.99 | — |
| 2 | Not visible, Medium, match 80%, Tesseract 0.84 | No | Docling, 150 tokens, words 0.86, agreement 0.80: text **0.84** | R1 | **Yes** (changed) · 0.70 (capped: model disagrees) | Yes: model disagrees |
| 3a | Visible, Low, match 32%, Tesseract 0.35 (faxed handwritten note) | No | Azure, 140 tokens, shares 0.70 / 0.74, Azure word confidence 0.72: words 0.72; Azure vs Docling 0.58: text **0.67** | R1 | **Yes** (changed) · 0.77 | — |
| 3b | Same page, Azure reads it poorly | No | Azure, 140 tokens, words 0.38, agreement 0.30: text **0.35** | R5 | **Yes** (changed: not clear enough for No) · 0.53 | Yes: text score in the 0.30–0.60 band |
| 4 | Visible, Low, match 32%, Tesseract 0.66 | Yes | Azure, words 0.70, agreement 0.52: text **0.64** | R1 | **Yes** · 0.74 | — (still listed as "Low quality, match 32%") |
| 5 | Visible, Low, match 61%, Tesseract 0.275 | Yes | Azure, words 0.55, agreement 0.61: text **0.57** | R5 | **Yes** · 0.68 | Yes: text score in the 0.30–0.60 band |
| 6 | Visible, Medium, match 20%, Tesseract 0.225 | Yes | Docling, 80 tokens, words 0.30, agreement 0.20: text **0.27** | R3 | **No** (changed) · 0.73 | Yes: Not readable |
| 7 | Visible, Low, match 10%, 12 tokens | Yes | Under 20 tokens; sharpness 22 | R4 | **No** (changed) · 0.62 | Yes: Not readable (image rule) |
| 8 | Low, no match rate, settled Blank in pass 1 | Yes | — | R0 | **Not Applicable** · 1.0 (the Blank verdict's) | — |
| 9 (new) | Visible, Medium, match 18% | Yes | Docling, 200 tokens, words 0.80, Docling vs Tesseract 0.15: text **0.58** | R5 | **Yes** · 0.69 | Yes: engines disagree (agreement 15%) |

**Example 3a in words.** Tesseract makes noise of the handwriting (score 0.35), so today calls the page unreadable. Azure, which read the page because it is handwritten and Low, returns real words with good confidence. A coder can read this page through the Azure text, so it is readable.

**Example 6 in words.** Quality is Medium, so today's rule cannot say No. But the best text is mostly non-words and the engines agree on only 20% of it: nobody can read words off this page, so v2 says No.

**Example 9 in words.** The Docling text looks like words (0.80), but Tesseract finds almost none of them (15%). On words alone this page would pass R1 quietly. With the agreement in the score it lands in the review band, and the reviewer is told why: the engines disagree, so either Docling invented words or Tesseract failed, and a person should look.

## Making it scalable

**Settings in canon, a shadow period before the switch, and a learned score once there are enough reviewed pages.**

### Settings in canon

`resources/canon/readability_canon.json` holds every threshold and switch, and reloads on change like the other canon files (today's constants and the cached vocabulary go):

```json
{
  "version": 1,
  "min_tokens": 20,
  "good_text_score": 0.60,
  "poor_text_score": 0.30,
  "not_visible_text_score": 0.35,
  "not_visible_at": 0.90,
  "match_below": 0.50,
  "text_score_weights": { "word_score": 2, "ocr_agreement": 1 },
  "agreement": { "pair": "best_engine_vs_closest", "before_edge_cap": true, "disagree_below": 0.30, "low_below": 0.30 },
  "image": { "enabled": true, "sharpness_below": 30, "contrast_below": 30, "score_full_at": 120 },
  "confidence": {
    "text_rules": { "floor": 0.70, "span": 0.29, "margin_range": 0.30 },
    "image_rule": { "floor": 0.55, "span": 0.25 },
    "fallback": { "floor": 0.50, "span": 0.20, "nothing_measured": 0.50 },
    "disagree_cap": 0.70,
    "review_below": 0.70
  },
  "no_is_strict": { "always_review": true, "no_alert_share": 0.02 },
  "not_applicable_label": "Not Applicable",
  "use_azure_word_conf": true,
  "best_engine_order": ["azuredocintel", "docling", "tesseract"],
  "vocabulary": { "canon_words": true, "english_list": true }
}
```

### Shadow first

`READABILITY_LOGIC = v1 | v2 | shadow`. In `shadow`, v1 writes `is_readable` and v2's answer, rule and score go to the routing detail; a report lists every page where they differ, with the reviewer's truth where there is one. The switch to v2 waits until v2 is at least as accurate as v1 against ground truth.

### Measured per rule

Per rule and week, in `metrics`: how often it fired and how often the reviewer agreed. The thresholds in canon are tuned on the validation charts, never on test.

### A learned score later

When there are enough reviewed Yes / No pages (a few hundred No pages), a logistic regression on the profile (word score, Azure confidence, OCR agreement, match rate, quality, visibility probability, sharpness, contrast, handwritten share) gives a calibrated p(readable). It replaces only the **R5 review band** first; the clear rules R0–R4 stay, as hard rules stay in continuity. It is the same pattern as the other models: one run card, a version in the `models` record, shadow before switch.

### Tests

One synthetic case per rule plus a near-miss (no real text: generated noise, a word list, a blurred and a faded test image); a test that missing values lean to Yes; a test that a page with 150 good tokens is never No because of the visibility model alone; a test that agreement pairs the best engine with its closest engine, not the three-engine average; a test that good words with agreement under 0.30 always carry a review flag.

## What to give Claude Code

**Give it this design as a file** (`Training/reference/claude_code_handoff_2026-11-22/5_readability/READABILITY_V2_LOGIC.md`), today's code (`core-pipeline/modules/ocr/readability.py`, `stage_final2.py`, `routing.py`) and the prompt below. Claude Code cannot open this page.

**Build order** (one branch per step; v1 stays the default until step 5):

1. **Profile.** `readability.profile(page)` measures every field of section 3: best text and its two shares, Azure word confidence from the Azure JSON, OCR agreement (best text vs its closest engine, from today's overlap()) and the text score, today's Tesseract score as a diagnostic, sharpness and contrast. Stored in `quality_detail.routing.readability`. No verdict changes.
2. **Canon.** `resources/canon/readability_canon.json`; today's constants and the cached vocabulary move there, with reload on change.
3. **v2 rules** (R0–R5, score, reason, review flag) behind `READABILITY_LOGIC = v1 | v2 | shadow`; R0 applied in stage 15 when the blank / junk verdict is final.
4. **Schema and display.** New columns (`readability_score`, `readability_rule`, `readability_confidence, readability_review_required`, `page_ground_truth.is_readable`) through `schema.sql`, a dated migration, `migrate.sql` and the changelog; `is_readable` and readability\_confidence appended to the final CSV; review-ui shows Not Applicable, the confidence, the score, the Review badge and the three Image Quality reasons; accuracy reads the new truth column with the old one as fallback.
5. **Shadow and switch.** Run shadow on the reviewed charts; list every v1 vs v2 difference with ground truth; switch the default to v2 when it is at least as accurate; rewrite `docs/specials/` readability page from the code.
6. **Later:** the learned score for the R5 band (section 7), when enough reviewed No pages exist.

**Done when**

- Step 1 changes no `is_readable` value.
- The shadow report lists every difference (page ids, v1 reason, v2 rule and reason, truth), and v2 is not less accurate than v1 on reviewed pages.
- No page with 20+ tokens and a text score of 0.30 or more is No, and every No carries a review flag.
- Blank pages are Not Applicable and are left out of accuracy. Every Yes / No has a confidence, and every confidence under 0.70 has a review flag.
- Each rule has a firing test and a near-miss test; `python -m pytest tests/ -q` passes.
- No page text in logs; the profile holds numbers and shares only.

**Prompt for Claude Code**

```markdown
Read Training/reference/claude_code_handoff_2026-11-22/5_readability/READABILITY_V2_LOGIC.md (the new design),
then core-pipeline/modules/ocr/readability.py, stage_final2.py (_store_route) and routing.py (route,
after_docintel, overlap, three_way_overlap, final_match_rate), review-ui/frontend/src/ImagingPanel.tsx (fmtReadability, imageQualityRows),
review-ui/frontend/src/accuracy.ts and review-ui/backend/app/store/rows.py.

Implement the design's "Build order", one branch per step. Before each step, show me what exists and your plan,
and wait for my OK. Keep READABILITY_LOGIC=v1 as the default until I approve the shadow report in step 5.

Rules: thresholds only in resources/canon/readability_canon.json; schema changes follow CLAUDE.md sections 4-5
(schema.sql, dated migration, migrate.sql, CHANGELOG, every reader and writer); the final CSV only gains
is_readable and readability_confidence at the end; no page text in logs; python -m pytest tests/ -q passes after every step.

After each step, report: pages whose Is Readable changed (count and page ids, by rule), the rule counts on the
reviewed charts, accuracy of v1 and v2 against ground truth, and which "Done when" checks pass.
```
