# Date of Service (DOS) — logic as implemented

This document describes what the code does today. Every rule cites
`file:line` (paths are relative to the repo root). Where the code and another
doc disagree, the code is described and the disagreement is listed in §10.

---

## 1. Summary

The `dos_extract` stage gives every page that is not blank, junk or a
duplicate **its own** date of service: the **Extracted DOS**. The first rule
that produces a date wins:

1. A DOS label within the first or last 20 words of the page, with a date right
   after it. This rule reads the final OCR text first, then the Tesseract text.
2. The date that the key/value extractor (`kv_extract`) selected.
3. Azure OpenAI, when it is enabled and the page looks clinical.
4. A date the key/value **rules** accepted but its model did not, when the
   model chose no date for the page (best model score first; `match_type`
   `kv_low`, stored as method `kv`).
5. No date.

Rules 1–3 have a fixed confidence: 0.95, 0.90 and 0.70, and 0 for rule 5.
Rule 4's confidence is the model's own probability for that date (below its
DOS threshold of 0.85, so it reads low), or `kv_low` (0.50) in `dos_canon.json`
when the staging has no score (written before `model_score` was staged: then
a row the rules scored, not `*_shared`, counts as rule-accepted). The stage writes one `dos_extraction_results` row per eligible page and
`imaging/<chart>_dos.csv`. Later stages set the **Final DOS**:

- `continuity` groups pages into documents, and the Extracted DOS is the main
  grouping signal.
- `imaging_final` gives every page of a document its first page's DOS.
- A stand-alone document made only of non-visit page types gets
  `DOS_DEFAULT_DATE` (2222-02-02).

review-ui scores DOS against ground truth as follows:

| Result | Condition |
|---|---|
| **Match** | Every labelled date (From, To) equals the Final DOS. |
| **Partial** | The dates equal only the Extracted DOS. |
| **No Match** | The dates equal neither. |

---

## 2. Where it runs and on which pages

| Fact | Code |
|---|---|
| Stage 11 of the 15-entry `STAGE_CHAIN`. It runs after `kv_extract` (its rule 2 input), `blank_junk:2` and `page_subtype`, and before `continuity` and `imaging_final`. | `core-pipeline/orchestrator/runner.py:49-72` (DOS at `:66`) |
| Registry row `('dos_extract', 1, 80, 'Date-of-Service Extraction', TRUE)`. | `schema/v1.sql:124` |
| Pages whose **final** blank/junk flag is `blank`, `junk` or `duplicate` are marked `skipped` (reason `blank_junk`) and get no DOS row. | `core-pipeline/stages/lib/dos/stage.py:202-208`, `core-pipeline/stages/_support.py:29`, `:135-155` |
| All other pages in `ctx.todo` are eligible (`eligible = set(ctx.todo)` after the skip). | `core-pipeline/stages/lib/dos/stage.py:208` |
| The chart text is rebuilt with a `===== <page_name> =====` block for **every** page. Ineligible pages get an empty body, so page order is kept. | `core-pipeline/stages/lib/dos/stage.py:119-165` |
| Text for each eligible page is chosen in the order Final2 (Azure DI) → Final1 (Docling) → Tesseract. JSON payloads are unwrapped to `content` / `markdown`. | `core-pipeline/stages/_support.py:301-341` |
| Tesseract text is never used when `document_type` is not `printed` / `visual`, or when `quality_tag = 'low'`. | `core-pipeline/db/__init__.py:818`, `:896-908` |
| A separate Tesseract text (used by the rule-1 fallback) is passed only for pages with **no Final2 text**, with the same Tesseract restriction. | `core-pipeline/stages/lib/dos/stage.py:155-161` |
| The page splitter recognises `===== name =====` markers. It falls back to `----- Page N` markers, and then to a single page `1.jpg`. | `core-pipeline/stages/lib/dos/dos_logic.py:106-147` |
| If a page's cleaned text is exactly `UNACCEPT`, the loop **stops**: that page and every later page get no hit. Those eligible pages then receive empty rows from the catch-all loop. | `core-pipeline/stages/lib/dos/dos_logic.py:475-480`, `core-pipeline/stages/lib/dos/stage.py:296-313` |

---

## 3. Step-by-step extraction

### 3.1 Configuration (`dos_canon.json`)

The settings are loaded from `core-pipeline/stages/lib/keyword-canon/dos_canon.json`
by `_build_profile` (`core-pipeline/stages/lib/dos/dos_logic.py:68-101`). The
file is reloaded when its mtime changes (`core-pipeline/stages/lib/canon_store.py`).

| Key | Value | Use |
|---|---|---|
| `DOS_DEFAULT_DATE` | `2222-02-02` | Fills the doc-level columns when a page has no date. It is also the Final DOS of a non-visit document. It is validated as `%Y-%m-%d` (`dos_logic.py:81`). |
| `edge_words` | 20 | Size of the top and bottom edge windows. |
| `label_reach_chars` | 40 | The date must **start** within this many characters after the label. |
| `label_gap_words` | at, on, of, from, is, time, date, dt | The only letter-words allowed between a label and its date. |
| `llm_words` | 60 | Number of words at the top and bottom that are sent to the LLM. |
| `confidence` | edge 0.95, kv 0.9, llm 0.7 | Fixed confidence for each rule. |
| `labels.encounter` | encounter date, date of encounter, visit date, date of visit, date of service, dates of service, service date, service dates, dos, appointment date, appt date, exam date, date of exam, ed visit date, arrival date, presentation date, service period, certification period, cert period | Class `encounter`. |
| `labels.admit` | admit date, admission date, date of admission, date of admit, admitted on, admitted, dos from | Class `admit` (From). |
| `labels.discharge` | discharge date, date of discharge, discharged on, discharged, dos to | Class `discharge` (To). |
| `clinical_cues` | Regexes such as chief complaint, HPI, discharge summary, progress note, operative note, … | Gates the LLM. |
| `discharge_cues` | discharge summary/note/date…, admission date, hospital course, date of admission/discharge | Switches the LLM prompt to ask for admission and discharge dates. |

The label regex has these properties:

- Phrases are lower-cased and sorted longest first, so `dos from` beats `dos`
  at the same position (`dos_logic.py:71-76`).
- Words inside a phrase are joined with `\s*`, so OCR-glued text such as
  `dateofservice` still matches (`dos_logic.py:63-65`).
- The phrase must not touch a letter on either side (`(?<![a-z])…(?![a-z])`,
  case-insensitive) (`dos_logic.py:77-80`).
- A label's class is looked up from its letters only. An unknown spelling
  defaults to `encounter` (`dos_logic.py:59-60`, `:268`).

### 3.2 Date regex and parsing (`DATE_RE`)

`core-pipeline/stages/lib/dos/dos_logic.py:161-194`

| Shape | Example | Notes |
|---|---|---|
| `YYYY-M-D` / `YYYY/M/D` | `2024-03-14` | The same separator must be used both times. |
| `M/D/YY(YY)` / `M-D-YY(YY)` | `3/4/24`, `03-14-2024` | The same separator must be used both times. A 2-digit year below 50 becomes 20xx; 50 or above becomes 19xx (`:173-177`). |
| `Month D(st/nd/rd/th), YYYY` | `January 28, 2025` | The year must have 4 digits. |
| `D(th) Month YYYY` | `28 Jan 2025` | The year must have 4 digits. |

- The match must not be preceded by a digit, `/` or `-` (`(?<![\d/-])`), and
  must not be followed by a digit (`:163`, `:168`).
- `parse_date_match` returns ISO, or `None` when the date is not a real calendar
  day, for example `02/30/2024` (`:180-194`).
- **There is no other validation in rules 1 and 3.** No rule checks for future
  dates, a minimum year, or the DOB. A date such as `DOS: 3/4/2099` was
  confirmed to be accepted as `2099-03-04`. The DOB and plausible-year filters
  exist only inside the key/value extractor (rule 2, §3.4). The only thing that
  stops a label from taking a DOB is the label-reach and gap-word rule (§3.3).

### 3.3 Rule 1 — edge keyword

**Edge spans.** A "word" is a whitespace token that contains a letter or a
digit, so table pipes and bullets are not counted. The two spans are the first
20 words and the last 20 words. On a short page the spans overlap
(`dos_logic.py:213-227`).

**Finding labels** (`edge_dates`, `dos_logic.py:238-275`):

1. Every label match that **starts** inside an edge span is collected,
   including overlapping matches. This is how `Office Visit  Date of Service`
   yields both `visit date` and `date of service` (`:230-235`, `:251`). The same
   start offset is never counted twice (`:252-254`).
2. The date must be found by `DATE_RE.search` within the next 40 characters
   after the label (`:255-258`).
3. Every letter-word between the label and the date must be in
   `label_gap_words`. Otherwise the label is dropped (`:259-261`). Example:
   `Visit Date: | DOB: 01/02/1960` produces no DOS, because `DOB` is not a gap
   word.
4. If the date is not a real day, the label is dropped (`:262-264`).

**Choosing the page's date** (`edge_page_date`, `dos_logic.py:302-334`). Hits
are ordered top edge first, then bottom edge, and by position within each edge.

| Case | Result | Code |
|---|---|---|
| Any `encounter` hit | The first one gives From. To = the end of a written range directly after it, else From. | `:313-318` |
| Range after an encounter date | Allowed separators: `-`, `–`, `—`, `to`, `thru`, `through`, within 12 characters after the date. The end must be at least the start. | `:278-289` |
| `admit` and a `discharge` that is not earlier than the admit date | From = admit, To = discharge. The keyword is `Admit+Discharge`. | `:319-329` |
| `admit` only, or the only discharge is before the admit | From = To = admit. | `:330` |
| `discharge` only | From = To = discharge. | `:331-333` |

- **Text order** (`dos_logic.py:483-492`): rule 1 runs on the final-text page
  first. If that finds nothing and the page has Tesseract text (§2), it runs
  again on the Tesseract text. Only the Tesseract text's own edge words are
  read. A hit from that second run is marked `source = "edge_tesseract"`.
- **Ranges written without spaces are not read.** In
  `DOS: 03/14/2024-03/20/2024`, the end date is preceded by `-`, so the
  `DATE_RE` look-behind rejects it. Confirmed result: From = To = `2024-03-14`.
  The spaced form `03/14/2024 - 03/20/2024` keeps both ends.

### 3.4 Rule 2 — key/value date

The stage reads the `dos` field from the extraction staging (`ensure_staging`).
If no staging exists, it runs the extraction first. When the extraction weights
are missing, the staging is empty and rule 2 never fires
(`core-pipeline/stages/lib/extraction/stage.py:365-384`).

For each page, every **selected** `dos` row becomes
`{iso, iso_to, raw, tier, keyword}` (`core-pipeline/stages/lib/dos/stage.py:212-239`):

- `iso` is `canonical_date(dos_from or value)`. Rows whose result is not a
  10-character ISO date are dropped (`:223-226`).
- `iso_to` is kept only when it is a valid ISO date later than `iso` (`:228-232`).

`kv_page_date` then takes the **first** valid item. Its confidence is the fixed
0.90 (it does **not** use the extractor's own score). A range keeps its end
date (`core-pipeline/stages/lib/dos/dos_logic.py:340-351`).

How the extractor chooses its date, in brief
(`core-pipeline/stages/lib/extraction/dos/extract.py`):

- **Key tiers** are listed in `extraction/dos/keys.json`:
  service = collected > admit = discharge > received > weak > printed > keyless.
  The best tier with a valid date wins, and a `collected` date wins inside the
  service tier (`extract.py:36-39`, `:366-383`).
- **Plausible year:** the year must be between 2000 and next year (`:42`, `:88-90`).
- **DOB filter:** dates equal to a trusted DOB value on the page are blocked
  (`:344-352`, `:93-112`).
- **Keyless dates:** header and footer dates without a key are used only when
  no keyed date was accepted. Dates followed by a clock time, lines containing
  fax/print/signed words, and page labels are skipped (`:298-341`, `:439-440`).
- **Admit and discharge on one page:** both are selected (`:378-381`). The DOS
  stage builds a separate single-date item for each and keeps only the
  **first**, so the discharge date is dropped there. The extractor's own
  record summary pairs them into a range instead
  (`extraction/dos/output.py:52-59`).

### 3.5 Rule 3 — LLM

See §4. The result has confidence 0.70, keyword `""` and source `llm`
(`dos_logic.py:495-505`).

### 3.5a Rule 4 — key/value low-confidence fallback

`StagedPage.rule_fallback("dos")` (`extraction/staging.py`) returns the DOS
rows the rules accepted (`rule_accepted`) when no row is selected, ordered by
`model_score`. `dos/stage.py` passes them as `kv_fallback`;
`kv_page_date(..., source="kv_low")` takes the first. The extraction pipeline
stages `rule_accepted` and `model_score` on every row the trained model
re-decides (`pipeline.apply_model_selection`).

### 3.6 Output row and `match_type`

`_row` (`core-pipeline/stages/lib/dos/dos_logic.py:419-449`) builds one row per
page:

| Field | Value |
|---|---|
| `dos_from` / `dos_to` | `MM-DD-YYYY` (`iso_to_mdy`, `:357-359`), or `""` |
| `dos_from_iso` / `dos_to_iso` | ISO date, or `""` |
| `doc_dos_*` | The page's own date, or `DOS_DEFAULT_DATE` when there is none. **Nothing is carried over from other pages here.** |
| `match_type` | `no_date_found`. Otherwise `admit_discharge_pair` whenever `is_pair` (an admit+discharge pair, **and also** an encounter range or a kv range). Otherwise `edge_keyword` / `edge_keyword_tesseract` / `kv` / `llm` / `kv_low` (`:414-416`, `:426-431`). |
| `final_dos` | `dos_from` ISO, or `""` |
| `confidence` | The rule's confidence, or `0.0` when there is no date |

---

## 4. LLM pass

| Question | Answer | Code |
|---|---|---|
| Is it enabled? | `DOS_LLM_ENABLED` defaults to **true**. It is effectively on only when `AZURE_OPENAI_ENDPOINT` is set **and** auth is usable: `AZURE_OPENAI_AUTH=key` needs a key; `entra` needs `azure-identity`; `auto` needs a key, or else `azure-identity`. | `core-pipeline/config.py:150-154`, `:172-191` |
| Client | `get_azure_openai_client()` returns `None` when the endpoint or credentials are missing. The stage logs a warning and falls back to regex only. | `core-pipeline/stages/lib/dos/azure_llm.py:81-126`, `core-pipeline/stages/lib/dos/stage.py:86-116` |
| On which pages does it run? | Only when rules 1 and 2 found nothing, a client exists, and the page text matches `clinical_cues`. | `core-pipeline/stages/lib/dos/dos_logic.py:405-407`, `:495` |
| Which text does it receive? | The page's final text (never the Tesseract fallback text). The snippet is the first 60 and last 60 `\w+` words, tagged `[TOP]` / `[BOTTOM]`. When the two halves are the same, only one copy is sent. | `dos_logic.py:516-552` |
| Prompt | Strict-parser instructions; the discharge mode is used when `discharge_cues` match; dates must be MM-DD-YYYY; reply with JSON `{"dos_from","dos_to"}` or nulls; today's date is included. Settings: `temperature 0`, `max_tokens 80`, `timeout 30`. The model is the `AZURE_OPENAI_DEPLOYMENT` value (default `gpt-4o-mini`). | `dos_logic.py:553-611`, `azure_llm.py:129-130` |
| Retries | `call_with_retry` with `AZURE_RETRY_ATTEMPTS` (5), `AZURE_RETRY_BASE_DELAY` (1.0) and `AZURE_RETRY_MAX_DELAY` (30.0). | `dos_logic.py:583-612`, `core-pipeline/config.py:89-91` |
| Parsing the answer | Code fences are stripped. `NO_VISIT_DATE_FOUND` with no `{` means none. Otherwise `json.loads`. A null `dos_from` means none. Each value goes through `normalize_date` (`DATE_RE`). An invalid `dos_to` is replaced by `dos_from`. **Any exception** returns `None` and prints a warning. | `dos_logic.py:613-634`, `:362-368` |
| Merging | `to_iso_date` is applied. If `dos_from` does not parse, the page has no date. `to` falls back to `from`. **Nothing checks** that To ≥ From, that the date is not in the future, or that it is not the DOB. | `dos_logic.py:502-505`, `:371-382` |
| `extraction_method` | Run-level value: `rules+llm` when a client exists, else `rules`. Per-page override: `llm` when the LLM supplied the date, `kv` when key/value did. So an edge-keyword page in an LLM-enabled run is stamped `rules+llm`. | `core-pipeline/stages/lib/dos/stage.py:77-83`, `:197-198` |

`/health` reports `dos_llm_enabled` (`core-pipeline/api/main.py:747`).

---

## 5. Page level vs document level, and the Final value

| Level | Where it lives | Rule |
|---|---|---|
| **Extracted** (page) | `dos_extraction_results.date_of_service_from/to` | The page's own date (§3). Never carried from another page. |
| `*_doclevel` | `dos_extraction_results.date_of_service_*_doclevel` | The page's own date, else `2222-02-02` (`dos_logic.py:422-425`). This is **not** a document-wide value, despite the schema comment at `schema/v1.sql:496`. |
| **Final** | `imaging_final.dos_from/to`, `dos_source` | The document's first page's date (see below). |

**Continuity grouping** (`core-pipeline/stages/lib/continuity/engine.py:274-313`):

- A page **with** an Extracted DOS continues the open document when its
  `(from, to or from)` equals the document's DOS (`same_dos`). Otherwise it
  starts a new document (`new_dos`) (`:292-296`, `:86-88`).
- A page **without** a DOS continues the open document unless one of the
  document-end checks fires (signature, page N of N, title of another type,
  page-type change, text ending early) (`:297-303`).
- Blank and junk pages break the document; duplicate pages do not (`:283-286`,
  `core-pipeline/stages/lib/continuity/stage.py:110-140`).
- A document's DOS is set from the page that opens it (`engine.py:305-307`).
  An undated opener has DOS `("","")`, so the next dated page always opens a
  new document. As a result, every page of a document either has no date of
  its own or has the opener's date.

**The Final DOS** is computed by `document_finals`
(`core-pipeline/stages/lib/continuity/engine.py:399-426`) and written by
`imaging_final` (`core-pipeline/stages/lib/imaging_final/stage.py:143-217`):

- **Default case.** If **every** page of the document is a `default_final_dos`
  page type or sub-type (`continuity_canon.json` → `default_final_dos.page_types`:
  Patient Demographics, Patient Information, Patient Registration, Growth
  chart, Problem List, Immunizations, Immunization list; matched
  case-insensitively), then Final From = To = `DOS_DEFAULT_DATE`, read from
  `dos_canon.json` (`engine.py:139-141`, `:417-421`, `:429-432`).
- **Otherwise**, Final From = the opener's `dos_from`, and Final To = the
  opener's `dos_to`, or its `dos_from` if `dos_to` is empty (`:422-426`).
  An undated opener gives an empty Final DOS.
- **`dos_source`** is set as follows (`imaging_final/stage.py:185-189`, `:217`):

  | Value | When |
  |---|---|
  | `document` | The page belongs to a continuity document and the Final From is non-empty. |
  | `page` | The page is in no document (blank or junk) and has its own date. Blank and junk pages have no DOS row, so in practice this is empty. |
  | NULL | No Final date. |
- **Duplicates** copy `dos_from` / `dos_to` from the page they duplicate and
  get `dos_source = 'duplicate'` (`imaging_final/stage.py:61-68`, `:229-237`).
- The `encounter_type` stage reads the doc-level columns and treats
  `DOS_DEFAULT_DATE` as no date (`core-pipeline/stages/lib/encounter/stage.py:109`,
  `:149-151`; `core-pipeline/stages/lib/encounter/encounter_classify.py:196-209`).

---

## 6. Storage

### `dos_extraction_results` (`schema/v1.sql:487-527`)

The table has one row per page (`UNIQUE (page_id)`) and is written with
`ON CONFLICT (page_id) DO UPDATE` (`core-pipeline/db/__init__.py:1059-1113`).

| Column | Written from | Code |
|---|---|---|
| `date_of_service_from/to` | `dos_from_iso` / `dos_to_iso`, or NULL | `core-pipeline/stages/lib/dos/stage.py:265-266` |
| `date_of_service_from/to_doclevel` | `doc_dos_*_iso`: the page's own date or `2222-02-02` | `stage.py:267-268` |
| `dates` | `_date_rows(hit)`, renamed to column vocabulary in `upsert_dos` | `stage.py:168-193`, `db/__init__.py:1079-1088` |
| `date_count` | `GENERATED ALWAYS AS (jsonb_array_length(dates)) STORED` | `schema/v1.sql:511-512` |
| `extraction_method` | `rules` / `rules+llm` / `llm` / `kv` (§4). The v1 CHECK allows only the first three (§10). | `stage.py:77-83`, `schema/v1.sql:514-516` |
| `confidence` | 0.95 / 0.90 / 0.70 / **0.0** for a page with no date found | `dos_logic.py:446` |

How rows are written:

- An eligible page that got a hit is written from that hit
  (`stage.py:254-273`).
- An eligible page without a hit, for example after `UNACCEPT`, is written with
  all dates NULL, `dates = []`, `confidence = NULL` and the run-level method
  (`stage.py:296-313`).

**`dates` JSONB shape.** The array has at most one element, because `dos_from`
is a single value today. It is empty when the page has no date.

```json
[{"seq": 1,
  "date_of_service_from": "2024-03-14",
  "date_of_service_to":   "2024-03-14",
  "source_keyword":       "Date of Service",
  "confidence":           0.95}]
```

`source_keyword` is the hit's `keyword`. When the keyword is empty (always the
case for the LLM), it falls back to `match_type`. It is truncated to 100
characters (`stage.py:189`).

### `imaging/<chart>_dos.csv`

Columns: `chart_name, page_name, page_number, dos_from, dos_to, dos_from_iso,
dos_to_iso, doc_dos_from, doc_dos_to, doc_dos_from_iso, doc_dos_to_iso,
match_type, final_dos, keyword, confidence, extraction_method`
(`core-pipeline/stages/lib/dos/stage.py:54-71`).

- The CSV has one row per eligible page with a hit (`:275-294`).
- With `DOS_DEBUG=true`, `<chart>/debug/<chart>_dos_candidates.csv` lists every
  edge label, its date, its edge (`top`, `bottom`, `tesseract_top`,
  `tesseract_bottom`) and whether it was `chosen` (`:74`, `:316-322`;
  `dos_logic.py:490-491`, `:507-508`).

### `imaging_final` (`schema/v1.sql:695-724`)

The table holds `dos_from` / `dos_to` (DATE) and `dos_source`, which must be
one of `page`, `document` or `duplicate` (§5).

---

## 7. How review-ui compares DOS to ground truth

**Inputs**

- In Postgres mode:
  - Extracted values come from `dos_extraction_results`: `dosFrom`, `dosTo`,
    `docDosFrom`, `docDosTo`, `dosConfidence`
    (`review-ui/backend/app/adapters/postgres/repository.py:882-904`).
  - Final values come from `imaging_final`: `finalDosFrom`, `finalDosTo`
    (`:1013-1031`).
  - Ground truth comes from `page_ground_truth.dos_from/dos_to`, which are
    free text (`schema/v1.sql:316-317`).
  - The accuracy report reads the same columns through
    `review-ui/backend/app/services/accuracy_cache.py:59-124`.
- In Local mode, values are read from `imaging/<chart>_dos.csv`. The ISO
  columns are preferred, and an empty To becomes From
  (`review-ui/backend/app/adapters/local/repository.py:73-85`).

**Per-date mark** (`dateMark`, `review-ui/frontend/src/groundTruth.ts:155-169`)

| Ground truth | Pipeline | Mark |
|---|---|---|
| Empty or a "pending" word: `na`, `n/a`, `not found`, `not sure`, `yet to process`, `skipped`, … (`:41-51`) | — | Not labelled. No bit is produced and the date is not scored. |
| A date that cannot be parsed, or a section that is not processed, or no pipeline date | — | `unknown` |
| A date | Same year, month and day | `match` |
| A date | Shares at least one of year, month or day | `partial` |
| A date | Shares none | `mismatch` |

Accepted date shapes (`groundTruth.ts:119-142`):

- ISO
- `M/D/YY(YY)` or `M-D-YY(YY)`
- `Month D, YYYY`
- `D Month YYYY` (also with `.`, `/` or `-` between parts)

Ordinal suffixes and `Sept` are normalised. A 2-digit year counts as 20xx when
it is at most (current year mod 100) + 1, otherwise 19xx.

**Page verdict** (`review-ui/frontend/src/accuracy.ts:253-268`, `:311-321`, `:335`)

1. Each labelled date (From, To) gets a grade: `yes` for a match, `maybe` for a
   partial, otherwise `no` (`:239-243`). `dosVerdict` returns `yes` only when
   **every** labelled date is `yes`, and `no` otherwise. A partial on a single
   date therefore counts as wrong (`:253-259`).
2. The verdict is computed twice:
   - against the Final DOS: `finalFrom = finalDosFrom || dosFrom`, and
     `finalTo = finalDosTo || finalDosFrom || dosTo` (`:311-320`);
   - against the Extracted DOS (`dosFrom` / `dosTo`).
3. `finalOrExtracted` combines the two: Final `yes` gives **Match** (1).
   Otherwise, Extracted `yes` gives **Partial** (0.5). Otherwise the result is
   **No Match** (0) (`:263-268`, metric text `:46-49`).

**Display (Imaging panel)**

- **DOS From / DOS To "Extracted" column.** This column shows the
  key/value staging's `dos` value when there is one. Only otherwise does it
  show the stored `dos_extraction_results` value
  (`review-ui/frontend/src/ImagingPanel.tsx:143-155`, `:1153-1181`, `:1241`).
- **"Processed" (Final) column.** This column shows `finalDosFrom/To`, else
  the Extracted value (`:169-184`).
- **Doc Summary.** Each document row shows its first page's
  `finalDosFrom/To` (`:1600-1626`). The per-page summary shows a page with no
  date as `2222-02-02` at 80% (`:43-74`).
- **Downloads.** Downloads use `docDos*`, else the page's own date, else
  `2222-02-02` at 0.8 (`review-ui/backend/app/services/imaging_csv.py:40-73`;
  `ImagingPanel.tsx:76-87`).

---

## 8. Config, env vars and keyword files

| Setting | Default | Effect | Code |
|---|---|---|---|
| `DOS_LLM_ENABLED` | `true` (still requires endpoint and auth) | Master switch for rule 3. | `core-pipeline/config.py:187-191` |
| `AZURE_OPENAI_ENDPOINT` | — | Required for rule 3. | `config.py:102`, `azure_llm.py:101-102` |
| `AZURE_OPENAI_AUTH` | `auto` | One of `key`, `entra` or `auto`. | `azure_llm.py:36-66` |
| `AZURE_OPENAI_API_KEY` | — | Key auth. | `azure_llm.py:95` |
| `AZURE_OPENAI_API_VERSION` | `2024-08-01-preview` | Client API version. | `azure_llm.py:97-99` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o-mini` | `model=` for the call. | `azure_llm.py:129-130` |
| `AZURE_RETRY_ATTEMPTS` / `_BASE_DELAY` / `_MAX_DELAY` | 5 / 1.0 / 30.0 | LLM retry policy. | `config.py:89-91` |
| `DOS_DEBUG` | `false` | Writes the candidates CSV. | `config.py:195` |
| `keyword-canon/dos_canon.json` | see §3.1 | Labels, windows, gap words, confidences, LLM cues, default date. Reloads on change. | `dos_logic.py:96-101` |
| `keyword-canon/continuity_canon.json` → `default_final_dos.page_types` | 7 types | Which stand-alone documents get the default Final DOS. | `continuity/engine.py:111-133` |
| `extraction/dos/keys.json`, `key_blocklist.json` | — | Key tiers for rule 2. | `extraction/dos/extract.py:432-436` |
| Truthy flag values | `1`, `true`, `yes`, `on` | — | `config.py:150-154` |

The legacy stand-alone CLI `core-pipeline/stages/lib/dos/extract_dos.py` is
not part of the pipeline. It uses `DOS_USE_LLM` / `--llm` (`:198`), reads
`*_final1.txt` and then `*_prelim.txt` (`:88-104`), and writes a 12-column CSV
(`:34-47`). Its `.env.example` documents `DOS_USE_LLM`, not `DOS_LLM_ENABLED`.

---

## 9. Worked example

Rules 1–4 below were checked by running `detect_dos_per_page` with the LLM
off. Steps 5 and 6 follow from the code in §5 and §7, under the stated
assumptions. Chart pages:

| Page | Text (excerpt) | Key/value staging |
|---|---|---|
| `1.jpg` | `Progress Note` / `Date of Service: 03/14/2024` / `Chief complaint…` | — |
| `2.jpg` | `Plan continues` | — |
| `3.jpg` | `lab` | selected `Collected` → `2024-03-15` |

1. **1.jpg.** `Date of Service` starts in the top 20 words, and the date
   follows within 40 characters with no gap words. It is an encounter label
   with no range after the date. Result: From = To = `2024-03-14`,
   `match_type = edge_keyword`, confidence 0.95.
2. **2.jpg.** There is no label and no kv date. With the LLM off, the result is
   `no_date_found`, confidence 0.0, and the doc-level columns are `2222-02-02`.
3. **3.jpg.** There is no edge label. Rule 2 gives `2024-03-15`,
   `match_type = kv`, confidence 0.90, keyword `Collected`, and
   `extraction_method = kv`.
4. **Rows written.**
   - 1.jpg: `dates = [{"seq":1,"date_of_service_from":"2024-03-14","date_of_service_to":"2024-03-14","source_keyword":"Date of Service","confidence":0.95}]`,
     `extraction_method = rules`.
   - 2.jpg: `dates = []`, `date_of_service_from = NULL`, `*_doclevel = 2222-02-02`.
5. **Continuity.** Assume 1.jpg is unsigned and no document-end check fires.
   - 1.jpg opens document 1.
   - 2.jpg has no DOS, so it continues document 1.
   - 3.jpg has a different DOS, so it opens document 2 (`new_dos`).
   - Final DOS: pages 1–2 get `2024-03-14` (`dos_source = document`), and page
     3 gets `2024-03-15`.
6. **Scoring.** Suppose the ground truth for 2.jpg says From = `03/14/2024`.
   - Final `2024-03-14` matches, so the result is **Match**.
   - If the ground truth for 3.jpg says `03/16/2024`: Final and Extracted are
     both `2024-03-15`. The day differs, so the date is marked `partial`, which
     `dosVerdict` grades `no`. The result is **No Match**.

---

## 10. Discrepancies

| # | Where | Says | Code does |
|---|---|---|---|
| 1 | `schema/v1.sql:514-516` (the `extraction_method` CHECK) | Allows only `rules`, `llm`, `rules+llm` | `stage.py:81-82` writes `kv` for rule-2 pages. On a database built from `v1.sql` alone, this upsert violates the CHECK. The constraint is widened only by `schema/v2.sql:155-162` or `schema/temp_changes_to_db.sql:15-20`. This contradicts CLAUDE.md §5 ("V2 is genuinely optional"). `docs/LOGIC.md` §6 "Writes" does not mention `kv`. |
| 2 | `docs/LOGIC.md` §6 rule table (row 2) | Rule 2 → `match_type = kv` | A kv **range** (`iso_to > iso`) and an encounter range after `DOS: a - b` are both labelled `admit_discharge_pair` (`dos_logic.py:428-429`). |
| 3 | `docs/LOGIC.md` §6 rule 1 ("A written range … keeps both ends") | Every range keeps both ends | Only a range with a space, `to`, `thru` or `through` keeps its end. `a-b` with no spaces loses its end because of the `DATE_RE` look-behind `(?<![\d/-])` (`dos_logic.py:163`, `:287`). |
| 4 | `docs/LOGIC.md` §6 rule 1 ("not for handwritten / low-quality pages") | Handwritten or low quality | The Tesseract fallback is blocked for any `document_type` other than `printed` / `visual`, including `form`, `uncertain` and `blank`, as well as for `quality_tag = low` (`db/__init__.py:818`, `:896-908`). |
| 5 | `core-pipeline/orchestrator/runner.py:63-64` (comment) | "DOS reads the Extracted sub-type for default-date and non-encounter pages" | `dos_extract` never reads `page_classification`. Page types matter only in continuity and `imaging_final` (`continuity/engine.py:417-421`). |
| 6 | `schema/v1.sql:496` (comment) | `*_doclevel` is "the chart-wide DOS this page rolled into" | `*_doclevel` is the page's own date, or `2222-02-02` (`dos_logic.py:422-425`). It is never carried from another page. `docs/LOGIC.md` §6 describes this correctly. In §8 ("document-level DOS, else its page-level DOS"), the "document-level" value is therefore the page's own date or the default. |
| 7 | `core-pipeline/stages/lib/extraction/dos/extract.py:10-11`, `output.py:52-59` | Admit + discharge on one page become a range | That holds in the extractor's record summary only. The DOS stage takes the first selected kv item, so the discharge date is dropped (`dos/stage.py:222-239`, `dos_logic.py:343-350`). |
| 8 | `docs/LOGIC.md` §6 (rule 4: "Nothing … confidence 0") | Confidence 0 | That is true for pages processed by the loop (stored `0.0`). Pages after an `UNACCEPT` page, or otherwise missed by the loop, are written with `confidence = NULL` and no `dates` (`stage.py:296-313`). |
| 9 | `core-pipeline/stages/lib/dos/.env.example:1-2`, `extract_dos.py` | `DOS_USE_LLM`, LLM "OFF by default" | The pipeline stage uses `DOS_LLM_ENABLED`, which is **on** by default when Azure OpenAI is configured (`config.py:187-191`). |
| 10 | review-ui DOS "Extracted" column vs accuracy | — | The panel shows the key/value staging's DOS when there is one (`ImagingPanel.tsx:150-151`, `:1241`). Accuracy and the ground-truth bits score the stored `dos_extraction_results` value (`accuracy.ts:290-291`). When rule 1 won, the displayed Extracted value can therefore differ from the scored one. |
| 11 | `schema/v1.sql:691` (comment) | Final DOS is "the document's first dated page's" | It is the **first page's** DOS (`continuity/engine.py:422-426`). In practice the result is the same: an undated opener cannot be followed by a dated page in the same document (`engine.py:292-296`). In that case the Final DOS is empty rather than a later page's date. |

Unclear from code alone: whether `ctx.todo` can exclude completed pages
during an API run. The API always reprocesses (CLAUDE.md §9), but the CLI
`--resume` path (`force=False`) could leave some eligible pages out of `todo`.
Such pages keep their previous DOS rows.
