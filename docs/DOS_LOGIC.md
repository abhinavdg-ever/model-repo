# Date of Service (DOS) — logic as implemented

This document describes what the code does today, step by step, with every
number and the file that holds it. Paths are relative to `core-pipeline/`
unless they start with `review-ui/`, `schema/` or `docs/`.

---

## 1. Summary: the date hierarchy

Each page that is not blank, junk or a duplicate gets **its own** date of
service, the **Extracted DOS**. The first step that gives a date wins.

| Step | Applies to | Date | Confidence | `source` |
|---|---|---|---|---|
| 0. Admit / Discharge | discharge pages | Admit = From, Discharge (on or after it) = To; one alone is both. Key/value dates first, an edge label fills a missing side (§4.1) | highest score of the dates used, else 0.50 | `admit_discharge` |
| 1. Key/value | every page | the tier order below (§3.6) | that date's score | `kv` (`admit_discharge` for the pair) |
| 2. Edge keyword | only when steps 0–1 have no date | a DOS label in the first or last 20 words of the page (§4) | 0.50 | `edge` / `edge_tesseract` |
| 3. Dictated, then Transcribed | "other" pages only, when steps 0–2 have no date | the date after `D:` / `DD:` / Dictated, else after `T:` / `DT:` / `TD:` / `TT:` / Transcribed | 0.30 | `dictation` |
| — | nothing found | no date | 0 | `none` |

**Page kind** = the page classification **L1** (`page_subtype` stage, seq 75,
runs before DOS; `page_kinds` in `dos_canon.json`). Text on the page does not
decide it.

| Kind | L1 |
|---|---|
| discharge | Discharge Summary |
| lab | Lab Report/Result (pre-v24 page type: Laboratory Data) |
| other | anything else, or no classification |

**Step 1 tier order** (`kv_priority` in `dos_canon.json`). Candidates are the
DOS dates the extractor accepted, grouped by the **tier the extractor gave
them** in `staging/extraction.json`. The first group with a date wins; within
it, the highest score (the model's, else the rules'), then page order. A tier
not listed for the page's kind is not a candidate.

| Kind | Order |
|---|---|
| discharge | Admit + Discharge pair (step 0) → service → weak → dt / dd → header / footer |
| lab | collected → received → Report Date → service → weak → dt / dd → header / footer → Admit + Discharge pair |
| other | service → weak → dt / dd → header / footer → Admit + Discharge pair |

- **service** = DOS, Visit Date, Encounter Date, Order Date, … (§3.1);
  **weak** = Date, Service, Chief Complaint, … (Report Date is weak too, except
  on lab pages where it is its own group); **dt / dd** = the weak keys in
  `weak_last_keys`; **header / footer** = the `keyless` tier: a date in the
  header or footer band with no label before it.
- **Score floor:** only weak keys (Report Date and dt / dd included) need a
  score above `weak_min_score` (**0.3**). Every other tier counts at any score.
- **The Admit + Discharge pair at the end** of lab and other pages keeps a stay
  on a page the classifier did not call a discharge summary. Once the page type
  is corrected, a re-run of `dos_extract` puts it first.

**Never a DOS:** print dates (Printed, Print Date, …), a date right after
`D:` / `T:` / `DD:` / `DT:` / `TD:` / `TT:` / Dictated / Transcribed except in
step 3, the member's DOB, years before 2000 or after next year, and
spaces-only dates (`06 12 24`). There is no LLM.

Later stages set the **Final DOS**: `continuity` groups pages into documents,
and `imaging_final` gives every page of a document its first page's DOS, or
the placeholder `2222-02-02` for a stand-alone non-visit document (§6).

```
OCR words ──► merge spaced dates ──► KV extraction (rules + model) ──► staging/extraction.json
                                                                              │
page text ─────────────────────────────────────────────► DOS stage ◄─────────┘
                                                          0. admit/discharge (discharge pages, by L1)
                                                          1. KV date by tier order for the page kind
                                                          2. edge keyword (0.50)
                                                          3. dictated / transcribed (other pages, 0.30)
                                                              │
                                                              ▼
                                  dos_extraction_results + imaging/<chart>_dos.csv  (Extracted)
                                                              │
                                                 continuity ──► imaging_final  (Final)
```

---

## 2. Which pages, and which text

| Fact | Where |
|---|---|
| The stage is `dos_extract`. It runs after `kv_extract`, the second blank/junk pass and `page_subtype`, and before `continuity` and `imaging_final`. | `orchestrator/runner.py` (`STAGE_CHAIN`) |
| Pages whose final blank/junk flag is `blank`, `junk` or `duplicate` are skipped (reason `blank_junk`) and get no DOS row. | `stages/lib/dos/stage.py` (`run`), `stages/_support.py` (`BJ_EXCLUDE`) |
| Every other page is eligible and gets exactly one row. | `stages/lib/dos/stage.py` |

**Key/value extraction reads the OCR words** (text plus box) from the page
JSON. **The edge keyword reads the page text**, chosen in this order:

| Text | Used when |
|---|---|
| Final2 (Azure Document Intelligence) | the page has it |
| Final1 (Docling) | no Final2 |
| Preliminary (Tesseract) | neither, and never for a handwritten, mixed or low-quality page |

For a page with **no Final2 text**, the edge keyword also tries the Tesseract
text when the chosen text gave nothing, because Docling can drop the top
banner. Same handwritten / low-quality restriction. Code:
`stages/lib/dos/stage.py` (`_combined_text`), `stages/_support.py`
(`best_page_text`), `db/__init__.py` (`page_blocks_prelim`).

### 2.1 Spaced dates are merged before anything reads the page

`extraction/util/geometry.py` (`words_from_page` → `merge_spaced_dates`)
joins consecutive OCR words that together form exactly a numeric date into one
word, its box the union of theirs:

| OCR words | Becomes |
|---|---|
| `05` `/` `20` `/` `2025` | `05/20/2025` |
| `05/` `20/` `2025` | `05/20/2025` |
| `05` `/20` `/2025` | `05/20/2025` |
| `BP` `120` `/` `80` | unchanged (two numbers, not a date) |
| `WBC` `10` `12` `20` | unchanged (no slashes) |

The words must be on one line, left to right, with each gap under one line
height. Every extractor (DOS, DOB, signature date) sees the merged date. A
date with spaces only (`06 12 24`) is **never** read: without slashes it
cannot be told apart from lab values, counts or phone numbers.

---

## 3. Step 1 — key/value extraction

Runs once per chart in the `kv_extract` stage and saves every candidate to
`<chart>/staging/extraction.json`. Code: `stages/lib/extraction/dos/extract.py`.
Labels: `stages/lib/extraction/dos/keys.json`.

### 3.1 DOS labels and tiers

OCR words are matched to these labels. The tier decides which date the rules
prefer when a page has several (rank 0 is best).

| Tier | Rank | Labels |
|---|---|---|
| service | 0 | DOS, D.OS, Date of Service, Dates of Service, Service Date(s), Service Date/Time, Service Period, Certification Period, Visit, Visit Date, Date of Visit, Visit Note, Encounter, Encounter Date, Exam Date, Date of Exam, Treatment Date, Appt Date, Appt. Date/Time, Office Visit on, Clinic Visit, Date of Consultation, Initial Visit, Document Date, Order Date, Registration Date, Reg Dt/Tm, Est Dt/Tm, OccDt, Date of Admission, Admit Date/Date of Service, DIAS |
| collected | 0, and wins over the other service labels | Collection Date, Collection Date/Time, Collected, Collected Date, Collected On, Date Collected, Specimen Collected, Coll Date |
| admit | 1 | Admit, Admitted, Admit Date, Admission Date, Admission Date/Time, Admit Inpatient Date/Time, Start Time |
| discharge | 1 | Disch, Discharged, Discharge Date, Discharge Date/Time, End Time |
| received | 2 | Received, Received Date, Received Date/Time, Received On, Date Received, Specimen Received |
| weak | 3 | Date, Report Date, Service, Chief Complaint, Chief Complaint/Reason For Visit, PSFH/ROS Updated Date |
| printed | 4 | Printed, Printed On, Printed Date, Print Date, Date Printed |
| keyless | 5 | no label: a date in the header or footer |

`DD` and `DT` are not labels: they are dictation / transcription stamps.

**Print dates are never a DOS.** A `printed`-tier label (Printed, Print Date, …)
still reads its date, so no other key and no keyless header/footer rule can take
that date, but the pair is rejected (`source` `printed_not_dos`, not accepted).

### 3.2 What counts as a date

| Form | Examples |
|---|---|
| Numeric | `6/28/2024`, `9/24/24`, `08-19-2024`, `2024-12-02` |
| Month name | `Jul 1, 2024`, `November 6, 2024`, `1st March 2024`, `28-Feb-2024`, `26.Nov.1953` |
| Range | `03/01/2024 - 03/05/2024`, `… to …`, `… thru …`, `… through …` — both ends kept as From / To |
| Date and time | `03/01/2024 10:15` — the time is dropped |

Date finders: `extraction/util/dates.py` (`find_dates`, `find_date_ranges`).

### 3.3 Which date belongs to a label

Tried in this order. The rule score is a model input, not the confidence
written to the CSV.

| Placement | Example | Rule score |
|---|---|---|
| Same line, after the label | `Date of Service: 03/14/2024` | 0.92 − 0.12 per word between, at least 0.55 |
| After "at", "on" or "dated" (service labels only) | `Encounter Note by Dr X at 06/12/2024` | 0.60 |
| Next line, when the label ends in `-` or `:` | `Collection Date -` then `12/13/2023` | 0.85 |
| Below the label, within 4 lines, roughly under it | a label over its value | 0.80 |
| Directly under a table column header | `Date  Location  PCP` over `12/19/2024 …` | 0.80 |

Distance: a weak, received or printed label allows **one** word between it
and its date, and reads below only when its line is a table header. Every
other label allows **six** words.

A note-type label of another field followed by a date on its line also gives
a date: `Progress Note: Dr Smith, MD 07/24/2024` gives `07/24/2024`.

### 3.4 What is eliminated

- A year before 2000 or after next year.
- A date equal to the member's DOB read on the same page.
- A date already read by another label: one date, one pair (the duplicate is
  marked `*_shared` and not accepted).

### 3.5 The rules' own pick

1. The rules keep the accepted dates of the best tier present.
2. On a lab page, a Collection date wins over Order Date and Date of Service.
3. An admit date and a discharge date are both picked (a From / To range).
4. Only when no labelled date was accepted, a plain date in the header or
   footer is added. Fax and print stamps (a date followed by a time), page
   labels and prose lines are skipped.

### 3.6 The model and the best date

The trained model (a LightGBM ranker; version v004 when it is on disk, else
v002 — the startup log's `KV extraction` line names the one that runs) scores
every candidate pair with a probability. It takes its best pair at **any**
score: `SELECT_THRESHOLDS = {"dos": 0.0}` in `extraction/util/config.py`,
which overrides the trained version's `thresholds.json`.

The DOS stage passes every row with a value that the rules accepted
(`StagedPage.candidates`), plus the print-key rows, to `kv_page_date` in
`dos_logic.py`. It picks by the **page kind** (the classification L1, §1) and
`kv_priority` in `dos_canon.json`:

| Page kind | L1 | Order, first group present wins |
|---|---|---|
| discharge | Discharge Summary | Admit / Discharge pair (§4.1) → service → weak → dt / dd → header/footer |
| lab | Lab Report/Result, Laboratory Data | collected → received → Report Date → service → weak → dt / dd → header/footer → Admit / Discharge pair |
| other | anything else | service → weak → dt / dd → header/footer → Admit / Discharge pair; then (after the edge keyword) Dictated → Transcribed |

Groups are the tiers the extractor wrote on each row, so the order works with
whatever `keys.json` produced the staging. Within a group the highest score
wins, then page order. A weak key's date (Report Date, dt and dd included)
needs a score above `weak_min_score` (0.3); no other tier has a floor. When the
Admit / Discharge group is the one chosen, the page takes the pair (§4.1).

Print-key dates are dropped here even when older staging still has them
accepted, and so is a header/footer date equal to a print date.

Its confidence is that row's `model_score`; without a model, its rule score;
with neither, `confidence.kv` (0.90) in `dos_canon.json`. If the extraction
weights are not installed, staging is empty and every page goes to state 2.

---

## 4. Step 2 — edge keyword

Only for a page with no key/value date. Code: `stages/lib/dos/dos_logic.py`
(`edge_dates`, `edge_page_date`). Settings: `keyword-canon/dos_canon.json`.

| Rule | Setting |
|---|---|
| Only labels that **start** in the first or last 20 words of the page. A word must contain a letter or digit, so table pipes do not count. | `edge_words` = 20 |
| The date must start within 40 characters after the label … | `label_reach_chars` = 40 |
| … with only these words between: at, on, of, from, is, time, date, dt. So `Visit Date: \| DOB: 01/02/1960` gives nothing: that date belongs to DOB. | `label_gap_words` |
| Labels match without case, across OCR-glued words (`dateofservice`), and overlapping: `Office Visit  Date of Service: 03/14/2024` finds Date of Service. | `labels` |
| A date right after `D:`, `T:`, `DD:`, `DT:`, `TD:`, `TT:`, Dictated or Transcribed is never taken and never ends a range: it is when the note was dictated or typed. | `_DICTATION_BEFORE` in `dos_logic.py` |

Labels (`labels` in `dos_canon.json`):

| Class | Labels |
|---|---|
| encounter | Encounter Date, Date of Encounter, Visit Date, Date of Visit, Date of Service, Dates of Service, Service Date(s), DOS, Appointment Date, Appt Date, Exam Date, Date of Exam, ED Visit Date, Arrival Date, Presentation Date, Service Period, Certification Period, Cert Period |
| admit | Admit Date, Admission Date, Date of Admission, Date of Admit, Admitted On, Admitted, DOS From |
| discharge | Discharge Date, Date of Discharge, Discharged On, Discharged, DOS To |

Choosing:

1. An **encounter** label wins: the first found, top edge before bottom. A
   written range right after it (`DOS: 03/01/2024 - 03/05/2024`) keeps both
   ends; a reversed range keeps only the start.
2. Otherwise an **admit** date is From and the first **discharge** date on or
   after it is To.
3. A discharge date alone is both From and To.

Text: the page's chosen text first; if that gives nothing and the page has no
Final2 text, the Tesseract text (`source` = `edge_tesseract`).

Confidence: `confidence.edge` = **0.50**.

### 4.1 Step 0 — discharge documents

`admit_discharge_date` in `dos_logic.py`, **before** the key/value order, on a
page whose classification L1 is Discharge Summary. Lab and other pages use the
same pair as their last key/value group (§3.6).

On such a page the stay's dates take priority:

| Field | Value |
|---|---|
| From | the admit date |
| To | the discharge date, if on or after the admit date |
| One of them only | both From and To |
| Where from | the highest-scoring `admit` / `discharge`-tier key/value date, else the edge Admit / Discharge label (discharge pages only) |
| Confidence | the highest model score of the key/value dates used, else 0.50 |
| `source` / `match_type` | `admit_discharge` / `admit_discharge_pair` (a range) or `admit_discharge` |

---

## 5. Output row

`dos_logic.py` (`_row`) builds one row per page:

| Field | Value |
|---|---|
| `dos_from` / `dos_to` (+ `_iso`) | the page's own date, or empty |
| `doc_dos_from` / `doc_dos_to` (+ `_iso`) | the page's own date, else `DOS_DEFAULT_DATE` (2222-02-02). Not a document-wide value despite the name. |
| `match_type` | `kv`, `edge_keyword`, `edge_keyword_tesseract`, `admit_discharge`, `dictation_date`, `admit_discharge_pair` (any From ≠ To), or `no_date_found` |
| `keyword` | the label the date came from |
| `confidence` | §1; 0 for no date |
| `source` (CSV) | `kv`, `edge`, `edge_tesseract`, `admit_discharge`, `dictation` or `none` |
| `extraction_method` | `kv` when a key/value date gave it (also for `admit_discharge`), else `rules` |

---

## 6. Page level vs document level: the Final DOS

| Level | Where | Rule |
|---|---|---|
| **Extracted** | `dos_extraction_results.date_of_service_from/to` | The page's own date (§3–§4). Never carried from another page. |
| `*_doclevel` | `dos_extraction_results.date_of_service_*_doclevel` | The page's own date, else 2222-02-02. |
| **Final** | `imaging_final.dos_from/to`, `dos_source` | The document's first page's date. |

**Continuity grouping** (`stages/lib/continuity/engine.py`,
`keyword-canon/continuity_canon.json`):

- A page **with** an Extracted DOS continues the open document when its
  From / To equal the document's, and starts a new document when they differ.
- A page **without** a DOS continues the open document unless the document
  ended: a signature on the previous page, a page that looks like a first
  page, a page-type change the classifier is sure of, or text that stops
  mid-page.
- Blank and junk pages end the document; duplicates do not.

**The Final DOS** (`document_finals` in `continuity/engine.py`, written by
`stages/lib/imaging_final/stage.py`):

- If **every** page of a stand-alone document is a `default_final_dos` type
  (Patient Demographics, Patient Information, Patient Registration, Growth
  chart, Problem List, Immunizations, Immunization list, …), its Final DOS is
  `DOS_DEFAULT_DATE` (2222-02-02) on every page, whatever the pages carry.
- Otherwise every page gets the first page's From, and its To (else its From).
  An undated first page gives an empty Final DOS.
- `dos_source`: `document` when a Final date is set, `duplicate` for a page
  copying its original, NULL otherwise.
- `encounter_type` reads the doc-level columns and treats 2222-02-02 as no
  date.

---

## 7. Storage

### `dos_extraction_results` (`schema/v1.sql`)

One row per page (`UNIQUE (page_id)`), upserted by `upsert_dos` in
`db/__init__.py`.

| Column | Value |
|---|---|
| `date_of_service_from/to` | the Extracted DOS, or NULL |
| `date_of_service_from/to_doclevel` | own date, else 2222-02-02 |
| `dates` | JSON array, at most one element: `[{"seq": 1, "date_of_service_from": "2024-03-14", "date_of_service_to": "2024-03-14", "source_keyword": "Date of Service", "confidence": 0.93}]` |
| `date_count` | generated from `dates` |
| `extraction_method` | `kv` or `rules`. `kv` needs the widened CHECK in `schema/v2.sql`; v1 alone allows `rules`, `llm`, `rules+llm`. |
| `confidence` | §1 |

An eligible page with no date is written with NULL dates, `dates = []` and
`extraction_method = rules`.

### `imaging/<chart>_dos.csv`

`chart_name, page_name, page_number, dos_from, dos_to, dos_from_iso,
dos_to_iso, doc_dos_from, doc_dos_to, doc_dos_from_iso, doc_dos_to_iso,
match_type, final_dos, keyword, confidence, extraction_method, source`
(`DOS_COLS` in `stages/lib/dos/stage.py`).

With `DOS_DEBUG=true`, `<chart>/debug/<chart>_dos_candidates.csv` lists every
edge label found, its date, its edge (`top`, `bottom`, `tesseract_top`,
`tesseract_bottom`) and whether it was chosen.

### `staging/extraction.json`

Every key/value candidate per page under `fields.dos`, with its rule score
(`score`), `rule_accepted`, `model_score` and the model's pick (`selected`).

---

## 8. How review-ui scores DOS against ground truth

- **Inputs.** Extracted values from `dos_extraction_results`, Final values
  from `imaging_final`, ground truth from `page_ground_truth.dos_from/dos_to`
  (free text).
- **Per date** (`review-ui/frontend/src/groundTruth.ts`, `dateMark`): same
  year, month and day = match; some part shared = partial; none = mismatch. An
  empty or "pending" label (na, not found, …) is not scored.
- **Page verdict** (`review-ui/frontend/src/accuracy.ts`):
  - Every labelled date must match. Against the Final DOS = **Match**; only
    against the Extracted DOS = **Partial**; neither = **No Match**.
  - The Final DOS falls back to the Extracted DOS when empty, and 2222-02-02
    counts as no date.
  - A page whose ground truth has no date expects none: none found is a Match,
    a date is a No Match (✕ on the page). A page with no ground-truth date where
    the pipeline shows only 2222-02-02 is not scored.
  - A blank / junk page — by the ground truth or by the pipeline — is never
    scored for DOS and gets no mark, even when the ground truth gives a date.
- **Imaging panel.** The Extracted column shows the staged key/value value when
  there is one, else the stored DOS; the Final column shows `finalDosFrom/To`,
  else the Extracted value.

---

## 9. Every setting

| Setting | Value | File |
|---|---|---|
| Model's own pick in staging | at any score (0.0) | `extraction/util/config.py` (`SELECT_THRESHOLDS`) |
| A weak key's date is a candidate | score > 0.3 (no floor for other tiers) | `dos_canon.json` (`weak_min_score`) |
| Tier order per page kind | §1 | `dos_canon.json` (`kv_priority`) |
| Weak keys ranked last among weak | dt, dd | `dos_canon.json` (`weak_last_keys`) |
| Page kind from the classification L1 | §1 | `dos_canon.json` (`page_kinds`) |
| Confidence of a dictated / transcribed date | 0.30 | `dos_canon.json` (`confidence.dictation`) |
| Confidence of a key/value date | its model score (rule score without a model; 0.90 with neither) | `keyword-canon/dos_canon.json` (`confidence.kv`) |
| Confidence of an edge date | 0.50 | `dos_canon.json` (`confidence.edge`) |
| Edge window | 20 words | `dos_canon.json` (`edge_words`) |
| Label-to-date distance | 40 characters | `dos_canon.json` (`label_reach_chars`) |
| Words allowed between | at, on, of, from, is, time, date, dt | `dos_canon.json` (`label_gap_words`) |
| Edge labels | encounter / admit / discharge | `dos_canon.json` (`labels`) |
| Placeholder date | 2222-02-02 | `dos_canon.json` (`DOS_DEFAULT_DATE`) |
| Key/value labels and tiers | §3.1 | `extraction/dos/keys.json`, `key_blocklist.json` |
| Valid years | 2000 to next year | `extraction/dos/extract.py` (`_MIN_YEAR`) |
| Words between label and date (KV) | 1 for weak / received / printed, else 6 | `extraction/dos/extract.py` (`_WEAK_GAP_WORDS`, `_MAX_GAP_WORDS`) |
| Non-visit document types | 7 page types | `keyword-canon/continuity_canon.json` (`default_final_dos`) |
| Candidate debug file | off | env `DOS_DEBUG` |

`dos_canon.json` reloads on change. `SELECT_THRESHOLDS`, `keys.json` and the
word merge take effect on the next `kv_extract`.

**What to re-run after a change** (API `only`, with `"skip_ocr": true` to avoid
paying for OCR again):

| Changed | Re-run |
|---|---|
| `dos_canon.json` | `["dos_extract", "continuity", "imaging_final"]` |
| `keys.json`, `SELECT_THRESHOLDS`, KV code, the word merge, the model files | `["kv_extract", "dos_extract", "continuity", "imaging_final"]` |

---

## 10. Worked examples

| Page text | Result | Why |
|---|---|---|
| `DOB: 01/02/1960  Date of Service: 03/14/2024` | 2024-03-14, `kv`, model score (e.g. 0.97) | Service label, same line; the DOB date is eliminated. |
| `Date: 06/12/2024` (model scores 0.18), nothing else | no key/value date; edge keyword next | A weak date needs a score above 0.3. |
| `Date: 03/01/2024` (0.95) and `Date of Service: 03/02/2024` (0.20) | 2024-03-02, `kv`, 0.20 | Service ranks above weak at any score. |
| L1 Lab Report/Result: `Collected: 04/02/2024`, `Received: 04/03/2024`, `Report Date: 04/04/2024` | 2024-04-02, `kv` | Lab order: Collected → Received → Report Date. |
| L1 Discharge Summary: `Visit Date: 02/03/2024`, `Admit Date: 02/01/2024`, `Discharge Date: 02/05/2024` | 2024-02-01 → 2024-02-05, `admit_discharge` | Step 0: the stay's dates win. |
| L1 Progress Note: `Admit Date: 02/01/2024`, `Discharge Date: 02/05/2024`, nothing else | 2024-02-01 → 2024-02-05, `admit_discharge` | The pair is the last key/value group on other pages (a misclassified discharge page). |
| `Print Date: 03/01/2024  Visit Date: 02/27/2024` | 2024-02-27, `kv` | A print date is never a DOS. |
| `DATE: 05 / 20 / 2025` | 2025-05-20, `kv` | Merged to `05/20/2025` before extraction. |
| `Date: 06 12 24` and nothing else | no DOS | Spaces-only dates are never read. |
| `Admission Date: 02/01/2024 … Discharge Date: 02/05/2024`, no KV date | 2024-02-01 → 2024-02-05, `admit_discharge`, 0.50 | A discharge page; edge labels give the stay. |
| Last line `Visit Date: 03/01/2024`, no KV date | 2024-03-01, `edge`, 0.50 | Encounter label in the last 20 words. |
| `D: 03/01/2024 T: 03/04/2024`, nothing else, an ordinary note | 2024-03-01, `dictation`, 0.30 | Step 3: dictated before transcribed. |
| The same on a lab report | no DOS | Step 3 is for "other" pages only. |
| Narrative, no label, no KV date | no DOS | Nothing else is tried. |
