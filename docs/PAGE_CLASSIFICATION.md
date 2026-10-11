# Page classification — how inference works

What happens to each page of a chart between OCR and the reviewer's screen:
which text is read, which models run, who wins, and how the Final answer is
reached. The names are in [PAGE_TAXONOMY.md](PAGE_TAXONOMY.md);
the code is `core-pipeline/stages/lib/page_classify/`.

> Status, 2026-10-11: implemented and unit-tested (35 tests in
> `tests/test_page_classify.py`). No trained BERT model is installed yet, so
> today the pipeline classifies by keywords alone and says so. Training lives
> in `training/` (see `training/README.md`).

---

## 1. The names

Every page gets three labels, all from `keyword-canon/page_taxonomy.json`:

| Name | What it is | Count |
|---|---|---|
| `page_type` | The family. Decides Codable / Non-Codable / Discharge | 49 |
| `page_subtype` | The kind of page inside the family. Every page type has a generic sub-type with its own name | 190 |
| `model_type` | What BERT predicts: the sub-type for a Progress Note, otherwise the page type | 58 |

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
| From | `core-pipeline/models/page-family/` | `keyword-canon/page_keyword_canon.json` |
| Reaches | only the model types it was trained on (21 today) | every one of the 190 sub-types |
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

### 4.4 Level 4 — codability

From the page type, in the taxonomy. Never predicted.

### 4.5 Blank, junk and duplicate pages

Not classified. They are `non_codeable`, keep their junk label as the sub-type
(`decided_by = blank_junk`), and a label that is a taxonomy sub-type (Invoice)
also gives its page type.

**Written:** `page_classification` — `page_type`, `page_subtype`, `model_type`,
`classification_category`, `decided_by`, `needs_review`, `bert_model_type`,
`bert_confidence`, `keyword_page_subtype`, `keyword_score`, `keyword_margin`,
`keyword_title_hit` — and `imaging/<chart>_codeable.csv`.

---

## 5. Across pages: the continue decision (`continuity` stage)

Groups pages into documents from their own values only (see
[CONTINUITY.md](CONTINUITY.md)): printed pagination first, then repeated
banner/footer and IDs, then title and signature; progress notes stay open
until their signature or the next encounter.

Two things continuity records for the classifier:

| | Meaning |
|---|---|
| `link_strength` | **strong** only when every link from the document's first page to this one is printed evidence — page numbering (`Page k of N` → `k+1 of N`) or a "continued" marker. Anything else is **weak** |
| `start_confirmed` | the document's first page was really seen: it has a keyword title hit, or its counter says page 1 |

A page counter whose total is above **20** is ignored: it numbers a whole fax
or export, not one document.

---

## 6. The Final answer (`imaging_final` stage)

### 6.1 Page type — level 3 rules, first that applies

"Per-page" is the Extracted answer. The document's type is its first page's
Extracted type.

| # | Rule | When | Final |
|---|---|---|---|
| 1 | `continuation` | not the first page, **strong** link, start confirmed, per-page Progress Note with **no title of its own**, document is **not** a Progress Note | the document's page type; sub-type = the keyword sub-type if it fits that type with score ≥ 3, else the first page's sub-type |
| 2 | `embedded_in_document` | not the first page, strong link, start confirmed, per-page Laboratory Data / Radiology Report, document **is** a Progress Note | Progress Note / Laboratory Data (or Radiology Report) |
| 3 | `embedded_same_page` | per-page Laboratory Data / Radiology Report and the keyword model saw a Progress Note title on the same page | the same, without needing page order |
| 4 | `possible` | rule 1 or 2 would apply but the link is weak or the start unconfirmed | keep the Extracted answer, **flag for review** |
| 5 | `no_change` | everything else — including an untitled Progress Note that continues a Progress Note, or continues nothing | keep the Extracted answer |

A type changes only on printed evidence and only when the document's real
first page was seen. Weaker evidence flags the page; it never changes it.

Codability is read again from the Final page type, so a lab page inside a
Progress Note is Codable, and a stand-alone lab page is not.

### 6.2 Date of service

After the page type: the document's first page's DOS, else its first dated
page's. A continuation page with no date shows Extracted DOS = NA and Final
DOS = the document's.

**Written:** `imaging_final` — `page_type`, `page_subtype`, `model_type`,
`codability`, `classification_category`, `page_type_source`
(`page` / `continuation` / `embedded`), `continuation_rule`, `needs_review`,
`dos_from`, `dos_to`, `dos_source`, plus every other reviewer field — and
`imaging/<chart>_final.csv`.

---

## 7. Worked examples

| Chart | Page | Extracted | Link | Final |
|---|---|---|---|---|
| A | 1 "DISCHARGE SUMMARY", Page 1 of 3 | Discharge Summary | start, confirmed | Discharge Summary |
| A | 2 untitled, reads like a note, Page 2 of 3 | Progress Note | strong | **Discharge Summary** (`continuation`) |
| A | 3 lab table, Page 3 of 3 | Laboratory Data | strong | Laboratory Data (document is not a Progress Note) |
| B | 1 "OFFICE VISIT", Page 1 of 2 | Progress Note / Office Visit | start, confirmed | Progress Note / Office Visit |
| B | 2 lab results, Page 2 of 2 | Laboratory Data / Chemistry | strong | **Progress Note / Laboratory Data** (`embedded_in_document`) |
| C | 1 "DISCHARGE SUMMARY" | Discharge Summary | start, confirmed | Discharge Summary |
| C | 2 untitled, same banner, no page numbers | Progress Note | weak | Progress Note, **review** (`possible`) |

---

## 8. Settings and where they live

| What | File (all reload on change) |
|---|---|
| Names, codability, embedded pairs | `keyword-canon/page_taxonomy.json` |
| Keyword terms and weights | `keyword-canon/page_keyword_canon.json` |
| Ladder thresholds, level 3 rules | `keyword-canon/page_arbitration.json` |
| Continuity signals, page-counter limit | `keyword-canon/continuity_canon.json` |
| The model | `core-pipeline/models/page-family/` (`PAGE_FAMILY_MODEL_DIR`) |

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

1. **Train BERT** and copy it into `models/page-family/` — then re-measure the
   ladder: how often each step decides, and how accurate each is.
2. **Tune the thresholds** on the validation split. Now: 0.50, or 0.25 with a 0.10
   lead; Unknown below 0.10 (set 2026-10-11 after retraining).
3. **Prune the keyword canon** — written from general knowledge, untuned; run a
   term report on the labelled text.
4. **Text normalisation** between Azure and Tesseract text.
5. **Encounter type tier 1** lost two entries with no taxonomy equivalent
   (home health visit note, skilled nursing visit note).
