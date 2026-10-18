# Page classification — how inference works

What happens to each page of a chart between OCR and the reviewer's screen:
which text is read, which models run, who wins, and how the Final answer is
reached. The names are in [PAGE_TAXONOMY.md](PAGE_TAXONOMY.md);
the code is `core-pipeline/stages/lib/page_classify/`.

> Status, 2026-10-11: implemented and unit-tested (35 tests in
> `tests/test_page_classify.py`). No trained BERT model is installed yet, so
> today the pipeline classifies by keywords alone and says so. Training lives
> in `training/` (see `training/README.md`).
>
> 2026-10-16: the v24 **page axes** (L1, L2, L3, specialty, codeability)
> decide `page_type` / `page_subtype` — §4.6. The ladder of §4.1–4.3 still
> runs, for its raw `bert_*` / `keyword_*` columns only. The old single-head
> BERT is retired (backup in `models/page-family-backup/2026-10-16/`);
> `models/page-family/page-axes/` holds a placeholder until the page-axes
> model is trained, and the v24 keyword canon decides alone until then.

---

## 1. The names

Every page gets three labels, all from `keyword-canon/page_taxonomy.json`:

| Name | What it is | Count |
|---|---|---|
| `page_type` | The family. Decides Codable / Non-Codable / Discharge | 51 |
| `page_subtype` | The kind of page inside the family. Every page type has a generic sub-type with its own name | 191 |
| `model_type` | What BERT predicts: the sub-type for a Progress Note, otherwise the page type | 60 |

Sub-type names are unique, so a sub-type always implies its page type. Two
extra pairs exist only after the continuation rules: **Progress Note /
Laboratory Data** and **Progress Note / Radiology Report**.

---

## 2. The order of work

Each step reads only what earlier steps wrote, so nothing feeds back into
itself.

```
OCR (Final2, Final1, Tesseract)
  │
  ├─ kv_extract      key/value extraction: printed DOS, page number, signature, headings
  ├─ page_subtype    EXTRACTED page type — per page: BERT + keywords + ladder   (levels 1, 2, 4)
  ├─ dos_extract     each page's own DOS — the key/value date when there is one;
  │                  else, as backup, the best regex candidate (≥ 0.75, DOS_MIN_SCORE);
  │                  else the LLM on clinical pages. The Extracted sub-type decides
  │                  default-date pages (demographics, injections) and non-encounter
  │                  pages (face sheet, med list, allergies…)
  ├─ continuity      which document each page belongs to — reads the Extracted type and own DOS
  ├─ encounter_type  per visit, from the Extracted type and own DOS
  └─ imaging_final   FINAL page type — continuation rules on continuity's documents (level 3, 4)
                     then FINAL DOS — carried over the same documents
```

**Extracted** = the models' answer for the page alone.
**Final** = what the continuation logic says, given the document the page sits in.
review-ui shows both side by side.

---

## 3. The text

At inference the classifier reads the page's best text, as plain text:

1. **Final2** (Azure Document Intelligence), else
2. **Final1** (Docling + RapidOCR), else
3. **Tesseract** (prelim) — not for handwritten, mixed or low-quality pages.

BERT is trained on Tesseract text (faster to produce). The Azure-vs-Tesseract
difference is accepted for now; normalising the text so the two look alike is
planned post-processing.

---

## 4. Per page: the Extracted answer (`page_subtype` stage)

### 4.1 Two predictors, independent of each other

| | BERT (`bert.py`) | Keyword model (`keywords.py`) |
|---|---|---|
| Predicts | `model_type` + confidence (softmax) | `page_subtype` + score, margin, title hit |
| From | `core-pipeline/models/page-family/bert/` | `keyword-canon/page_keyword_canon.json` |
| Reaches | only the model types it was trained on (21 today) | every one of the 191 sub-types |
| Good at | pages without a clear title | page kinds with few training images |

**Keyword scoring** (the canon's `how_to_match`):

* whole words / phrases only, case-insensitive; the longest overlapping match wins;
* title term in the first 12 lines = **3** and a *title hit*; elsewhere = **2**;
  a one-word title term counts as a title only as a heading on its own line;
* body term = **1**; ambiguous term = **0.5**, and only once another term of the
  same sub-type matched;
* a page type scores its best sub-type; the result is the top page type, its
  best sub-type, the score, the margin over the next page type, and the title hit.

### 4.2 Level 1 — page type (the ladder; first step that applies wins)

| # | Step | When | Winner | Review |
|---|---|---|---|---|
| 1 | `agreement` | both give the same page type | both | no |
| 2 | `bert_high` | BERT ≥ **0.50** — or BERT ≥ **0.25** with a lead ≥ **0.10** over its second choice | BERT | no |
| 3 | `keyword_only_class` | the keyword page type is one BERT was not trained on, keyword score ≥ **3** with a title hit | keywords | no |
| 4 | `keyword_title` | keyword title hit and margin ≥ **2** | keywords | no |
| 5 | `bert_medium` | BERT ≥ **0.25** | BERT | yes |
| 6 | `unknown` | a BERT model is loaded and BERT < **0.10** | page type **Unknown** | yes |
| 7 | `top3_agreement` | a page type in both models' top 3 — the best combined rank, ties to BERT's order | both | yes |
| 8 | `keyword_body` | keyword score ≥ 3 | keywords | yes |
| 9 | `bert_low` | nothing above | BERT | yes |
| — | `no_prediction` | neither model answered | **Unknown** | yes |

Steps 3–4 are "the keyword model, when it is clear": below 25% it wins when it
has a title. Below 10% with no clear keyword the page is Unknown; between 10%
and 25% the two models' top 3 page types are compared.

With no BERT model installed, steps 3, 4 and 8 do the work (every class counts
as untrained; the Unknown step needs a model), and a weak keyword hit falls to
`keyword_body` for review.

### 4.3 Level 2 — sub-type inside the winning page type

* **Progress Note:** BERT's model type when BERT said Progress Note; else the
  keyword sub-type if it is a Progress Note sub-type; else `Progress Note`.
* **Anything else:** the keyword sub-type if it belongs to that page type and
  scored ≥ 3; else the generic sub-type (the page type's own name).

### 4.3a Page type confidence

`page_classification.confidence` (the codeable CSV's `confidence`, review-ui's
Page Type confidence) follows the ladder step that decided the page type —
`page_arbitration.json` → `confidence`. `b` is BERT's probability; `k` is the
keyword model's confidence: 0.60 with a title hit (0.30 without), plus 0.05
per point of margin over the next page type, capped at 0.90 (0.60).

| Step | Confidence |
|---|---|
| `agreement` | 1 − (1−b)(1−k), capped at 0.99 |
| `bert_high`, `bert_medium`, `bert_low`, `unknown` | b |
| `keyword_only_class`, `keyword_title`, `keyword_body` | k |
| `top3_agreement` | the lower of b and k |
| `no_prediction` | none |

A step that flags the page for review is capped at 0.49. Blank / junk pages
keep the blank/junk confidence. `bert_confidence` still holds BERT's raw
probability.

### 4.4 Level 4 — codability

From the page type, in the taxonomy. Never predicted.

### 4.5 Blank, junk and duplicate pages

Not classified. They are `non_codeable`, keep their junk label as the sub-type
(`decided_by = blank_junk`), and have no page type — not even when the label
is also a canon page type (Invoice).

### 4.6 Page axes, v24 — the page type written

On the same pages and text, `axes.py` gives the v24 axes, and they are the
page's classification:

| Levels decided | `page_type` | `page_subtype` | `model_type` |
|---|---|---|---|
| L1 only | L1 | L1 | L1 |
| L1, L2 | L1 | L2 | L1 > L2 |
| L1, L2, L3 | L1 (L2) | L3 | L1 > L2 > L3 |
| none | Unknown | Unknown | — (needs review) |

e.g. *Lab Report/Result (Radiology)* / *CT Scan Report*; *Lab Report/Result* /
*Radiology*; *Progress Note* / *Progress Note*. `classification_category`
comes from the **canon's** codeability — never the model's — (`discharge` →
`discharge_summary`; `needs_review` → `non_codeable` with `needs_review` set), `confidence` is
L1's, and `decided_by` is `axes:` plus who decided L1 / L2 / L3
(`axes:model/model/canon`). The ladder's page type is no longer written.

Each level is the page-axes model's when it clears its gate, otherwise the v24
keyword canon's (`keyword-canon/page_canon_v24.json`, the same matcher as §4.1
restricted to the entries under the levels already decided). Gates are in
`page_arbitration.json` → `axes`:

| Level | Model decides when | Otherwise |
|---|---|---|
| L1 | top L1 ≥ **0.40**, ahead of the second by ≥ 0.10, not the folded `OTHER` (`route = model`) | the canon's best L1, keyword confidence ≥ **0.50** |
| L2 | the model decided L1 and L2 ≥ **0.40** | the canon's best L2 under that L1, ≥ **0.50** |
| L3 | never — the model's L3 is kept in `axes.prediction` only | the canon's best L3 under that L1 > L2, ≥ **0.50** |
| Specialty | not Unknown / Others, ≥ 0.70, ahead of the second by ≥ 0.20, trained on ≥ 10 pages | the specialty of the canon entry behind the deepest canon-decided level, if it has one |
| Codeability | never — kept in `axes.prediction` only | the tag of the canon page type behind the decided path (below); `needs_review` when its rule is `context` / `source` |

Keyword confidence is §4.3a's `k`. Under Progress Note there is no L2 or L3:
its L2 is the content section, from section tagging. `axes.sources` records
who decided L1 / L2 / L3 / specialty / codeability (`model`, `canon` or `-`),
e.g. `model/model/canon/-/model`. Without a model the canon decides every
level and `axes.route` is null; `/health` → `page_axes_model` says why.

**The canon page type behind the path.** After the levels are decided, the
canon's best keyword match among its entries under L1 [> L2 [> L3]] (else the
entry for exactly that path) is the page's `axes.canon_page_type`: e.g.
*Telephone Encounter* under Progress Note. Its tag is the codeability, its
specialty the fallback specialty, and its name the most specific name the
stages below can match.

**Downstream, in v24 names.** Every stage after this one reads v24 names
from the row (`axes.names_of`: canon page type, L3, L2, L1, most specific
first; `axes.family_of`: the L1):

* continuity compares pages on their L1 and takes "own title" from a v24 canon
  title (`axes.title_l1`); `continuity_canon.json` → `default_final_dos` lists
  v24 names (Demographics, Patient Demographics, Immunization, Problem List…).
* encounter type tier 1 takes the page's most specific name listed in
  `encounter_canon.json` → `tier1_page_types` (canon page types such as
  Telephone Encounter, Office Visit; L1 / L2 such as Operative Note), checked
  against `page_canon_v24.json` at load.
* the Final rules (§6.1) agree on the L1 and use v24 names for the inside-note
  and embedded rules (`page_arbitration.json` → `inside_progress_note`).
* review-ui splits `Lab Report/Result (Radiology) (CT Scan Report)` into its
  three names before scoring against ground truth (semantic similarity, so v1
  ground-truth labels still score).

The model is `train_axes.py`'s output folder in `models/page-family/page-axes/`
(`PAGE_AXES_MODEL_DIR`). Its inference code is copied into
`page_classify/axes_model.py`; `tests/test_page_axes.py` fails when the copy
drifts from the trainer. Pick the gates from a trained model with
`training/bert-training/tune_thresholds.py`.

**Written:** `page_classification` — `page_type`, `page_subtype`, `model_type`,
`classification_category`, `decided_by`, `needs_review`, `bert_model_type`,
`bert_confidence`, `keyword_page_subtype`, `keyword_score`, `keyword_margin`,
`keyword_title_hit`, `axes` (JSONB: route, l1 / l2 / l3, specialty,
codeability, each with its confidence; sources; canon_page_type; the model's
raw prediction) — and `imaging/<chart>_codeable.csv` (axes columns appended at
the end).

---

## 5. Across pages: the continue decision (`continuity` stage)

Groups pages into documents from their own values (see
[CONTINUITY_LOGIC.md](CONTINUITY_LOGIC.md)): the same DOS continues and a
different DOS starts a new document; an undated page continues unless the
printed page numbers, a signature, a first-page title, a sure page-type change or
an early text end say the previous document ended.

Two things continuity records for the classifier:

| | Meaning |
|---|---|
| `link_strength` | **strong** only when every link from the document's first page to this one is printed evidence — page numbering (`Page k of N` → `k+1 of N`) or a "continued" marker. Anything else is **weak** |
| `start_confirmed` | the document's first page was really seen: it has a keyword title hit, or its counter says page 1 |

A page counter whose total is above **20** is ignored: it numbers a whole fax
or export, not one document.

---

## 6. The Final answer (`imaging_final` stage)

### 6.1 Page type — agreed across the document

"Per-page" is the Extracted answer. Across the pages of one continuity
document every page takes the **first page's family (L1)**; each page keeps
its own names below it.

**The agreed family** (`agree_document_type`), first that applies:

| Basis | When | Agreed family |
|---|---|---|
| `first_page` | the first page has an L1 (not Unknown) | the first page's L1 |
| `vote` | the first page is Unknown, when one L1 holds ≥ 60% of the confidence weight (`document_agreement.vote_min_share`) | the pages' L1s, weighted by page-type confidence; a tie goes to the first page's |
| — | no clear majority | none: every page keeps its own, flagged for review (`no_agreement`) |

**Each page** (`decide_final`):

| `continuation_rule` | When | Final |
|---|---|---|
| `agreed` | the page's L1 is the agreed family | kept as is (*Lab Report/Result (Laboratory)* stays under a *Lab Report/Result (Radiology)* first page) |
| `agreed_first_page` / `agreed_vote` | its L1 differs | page type = the agreed L1; sub-type = the page's own page type (an Unknown page: the agreed L1). `agreed_vote`, or a weak link, flags the page for review. `page_type_source = continuation` |
| `no_agreement` | the vote has no clear majority | kept as is, **flagged for review** |
| `embedded_same_page` | page in no multi-page document, of an `embedded_families` L1 (Lab Report/Result), headed by a Progress Note canon title | Progress Note / its own page type |
| `no_change` | page in no multi-page document | kept as is |

**Codability** comes from the canon (the page's own category). A page that
takes the agreed family, or whose canon rule is `context` (e.g. *Clinical
Summary*), takes the **document's** codability — its first page of that
family. Inside a Progress Note, a page carrying one of
`inside_progress_note.non_codeable_names` (**Laboratory**, Medication Record…)
**stays Non-Codable**; radiology does not (`page_arbitration.json` →
`inside_progress_note`).

### 6.2 Date of service

The document's first page's DOS (2222-02-02 for a stand-alone non-visit
document). A continuation page with no date shows Extracted DOS = NA and Final
DOS = the first page's.

**Written:** `imaging_final` — `page_type`, `page_subtype`, `model_type`,
`codability`, `classification_category`, `page_type_source`
(`page` / `continuation` / `embedded`), `continuation_rule`, `needs_review`,
`dos_from`, `dos_to`, `dos_source`, plus every other reviewer field — and
`imaging/<chart>_final.csv`.

---

## 7. Worked examples

| Chart | Page | Extracted | Link | Final |
|---|---|---|---|---|
| A | 1 "DISCHARGE SUMMARY", Page 1 of 3 | Discharge Summary | start, confirmed | Discharge Summary (`agreed`) |
| A | 2 untitled, reads like a note, Page 2 of 3 | Progress Note | strong | **Discharge Summary / Progress Note** (`agreed_first_page`) |
| A | 3 lab table, Page 3 of 3 | Laboratory Data | strong | **Discharge Summary / Laboratory Data** (`agreed_first_page`) |
| B | 1 "OFFICE VISIT", Page 1 of 2 | Progress Note / Office Visit | start, confirmed | Progress Note / Office Visit |
| B | 2 lab results, Page 2 of 2 | Laboratory Data / Chemistry | strong | **Progress Note / Laboratory Data**, Non-Codable (`agreed_first_page`) |
| C | 1 Chief Complaint, 2 lab, 3 med list, signed | Clinical Summary, Laboratory Data, Medication List | strong | **Progress Note** / Clinical Summary (Codable), / Laboratory Data and / Medication List (Non-Codable) (`progress_note`) |
| D | 1 untitled 0.40, 2–3 lab 0.90 / 0.80 | Clinical Summary, Laboratory Data ×2 | strong, start unconfirmed | **Laboratory Data** / Clinical Summary on page 1, **review** (`agreed_vote`) |

---

## 8. Settings and where they live

| What | File (all reload on change) |
|---|---|
| Names, codability, embedded pairs | `keyword-canon/page_taxonomy.json` |
| Keyword terms and weights | `keyword-canon/page_keyword_canon.json` |
| Ladder thresholds, level 3 rules | `keyword-canon/page_arbitration.json` |
| Continuity signals, page-counter limit | `keyword-canon/continuity_canon.json` |
| The old BERT | retired; backup in `core-pipeline/models/page-family-backup/2026-10-16/` (`PAGE_FAMILY_MODEL_DIR` points at a missing folder) |
| Page-axes gates | `keyword-canon/page_arbitration.json` → `axes` |
| v24 canon (axes fallback, L3) | `keyword-canon/page_canon_v24.json` — a copy written by `training/taxonomy/build_canon.py` |
| The page-axes model | `core-pipeline/models/page-family/page-axes/` (`PAGE_AXES_MODEL_DIR`) |

The three `page_*.json` files came from the spec package (now retired; its
naming rules are kept in [PAGE_TAXONOMY.md](PAGE_TAXONOMY.md)). If a JSON file
and that document disagree, the JSON wins.

**The model folder** must hold a Hugging Face sequence classifier whose
`id2label` names are all taxonomy model types. A folder with any other label
stops the stage, naming the bad labels. No folder (or no transformers / torch)
means keywords only — `/health` shows `page_family_model.ready: false` with the
reason, and every page has `bert_model_type = NULL`.

---

## 9. Deliberate differences from the reference package

| Package says | Here | Why |
|---|---|---|
| Level 3 links pages itself (step A/B: page counter, "continued", own title, previous N of N, weak signals) | Level 3 runs on the continuity stage's documents | One definition of a document. The package's counter pattern needs the word "page" (missing "pg 8 of 17"), drops counters on lines with a time (printed footers), and its weak signals include "starts lowercase / no end punctuation", measured as noise (28% vs 29%) |
| Fax/time-line guard on counters | Not used | It discarded real printed-footer counters on 60307605 |
| N > 20 counter guard | Used | It correctly ignores "of 40" export numbering (60306165) |
| The model folder must exist | Missing model → keywords only, visible in `/health` and the data | A degraded run must be visible, not fatal (CLAUDE.md §9) |
| No prediction case unspecified | `no_prediction`, page type empty, review | No catch-all class |
| Text = Tesseract with training settings | Final2 → Final1 → Tesseract at inference | Decided 2026-10-11: train fast on Tesseract, infer on the best text |

---

## 10. Open items

1. **Train the page-axes model** and copy it into `models/page-family/page-axes/`
   ([PAGE_AXES_MODEL.md](PAGE_AXES_MODEL.md)); tune its gates with
   `tune_thresholds.py`.
2. **Move the downstream name lists to v24** (§4.6, "Downstream, still in v1
   names"): continuity's default-DOS types, encounter tier 1, the Final
   embedded / inside-note rules, review-ui's ground truth.
3. **Tune the v1 ladder's thresholds** — moot while the ladder only fills its raw
   columns. Now: 0.50, or 0.25 with a 0.10 lead; Unknown below 0.10.
4. **Prune the keyword canon** — written from general knowledge, untuned; run a
   term report on the labelled text.
5. **Text normalisation** between Azure and Tesseract text.
6. **Encounter type tier 1** lost two entries with no taxonomy equivalent
   (home health visit note, skilled nursing visit note).
