# Document continuity

Which document each page belongs to, and the values a document's pages share.

> Status, 2026-10-10: implemented and tested (19 tests in `tests/test_continuity.py`,
> full suite green apart from 4 failures that predate this work). Dry-run on the
> four local charts only. Not yet run against a live Postgres or a full batch.

---

## 1. What it is for

A chart is a stack of pages from many documents: progress notes, labs, face
sheets, letters. Most fields are extracted per page, but a page usually only
makes sense as part of its document. The second page of an office visit has no
title and often no date of service, yet its page type and DOS are the visit's.

Continuity answers two questions:

1. **Grouping.** Does this page continue the previous page's document, or start
   a new one?
2. **Carrying.** For every page in a document, what is its Final DOS (the
   document's first dated page's), and does its page type change? Page type
   follows the continuation rules in [PAGE_CLASSIFICATION.md](PAGE_CLASSIFICATION.md)
   §6 — an untitled Progress-Note-looking page continuing another document
   takes its type; lab or radiology inside a Progress Note becomes Progress
   Note / Laboratory Data — and is otherwise the page's own.

Each page keeps its own values too. review-ui shows both:

| | Extracted | Final |
|---|---|---|
| First page of a visit | Progress Note (SOAP Note), 2024-03-28 | Progress Note (SOAP Note), 2024-03-28 |
| Page 2, no title, no date | Progress Note, NA | Progress Note, 2024-03-28 |
| Page 3, a lab table inside the visit | Laboratory Data, 2017-11-14 | Progress Note / Laboratory Data, 2024-03-28 |

---

## 2. Where it runs

```
… → page_subtype → dos_extract → continuity → encounter_type → page_sequencing → imaging_final
```

| | |
|---|---|
| Stage | `continuity`, pass 1, seq 87 |
| Code | `core-pipeline/stages/lib/continuity/` (`engine.py`, `layout.py`, `signature.py`, `stage.py`) |
| Settings | `core-pipeline/stages/lib/keyword-canon/continuity_canon.json` (reloads on change) |
| Reads | page type (`page_classification`), page-level DOS (`dos_extraction_results`), blank/junk verdict, OCR text, the Final2/Final1 OCR JSON, the key/value staging (printed page number), the signature CSV |
| Writes | `page_continuity_results`, `imaging/<chart>_continuity.csv` (grouping only) |
| Carried by | the `imaging_final` stage (last in the chain), into `imaging_final` / `<chart>_final.csv` |

It runs after page type and DOS because it reads both (to judge progress notes)
and carries both (as the Final values).

---

## 3. Inputs per page

| Input | Source | Used for |
|---|---|---|
| Lines with vertical position | Final2 JSON line polygons; else Final1 words grouped into lines; else plain text lines | header and footer bands |
| Section headers + position | `section_headers` in the OCR JSON (matched canonical name, normalised top) | titles, visit-opening headers |
| Printed page number | key/value staging (`page_no`), else regex on the bands | tier 1 |
| Page type + family | `page_classification.page_subtype` ("Family (Type)") | progress-note rule, Final page type |
| Own DOS | `dos_extraction_results.date_of_service_from/to` (page level only) | next-encounter rule, Final DOS |
| Signed | extraction `signature_present`, or a signature block in the text | closes a note; tier 3 |
| Skipped | blank / junk / duplicate (final verdict) | passed over, no document |

**Final1 vs Final2.** Different OCR engines on the same image (Docling+RapidOCR
locally, Azure Document Intelligence billed). Positions agree (53688890 page 10:
the banner at 4.8–6.4% height in both); text and line grouping differ. Final2 is
preferred because it returns lines in reading order; Docling's markdown is not
top-to-bottom, so its "first lines" are not the page header.

---

## 4. The decision, page by page

Each page is judged against the previous non-skipped page.

### Tier 1 — printed pagination (decides alone)

Searched only in the header and footer bands, and only with the word `page` /
`pg`, so a date like `26/24` is not "page 26 of 24".

| Previous | Current | Result |
|---|---|---|
| N of M | N+1 of M | continue |
| anything (not the same) | 1 of M | new document |
| N of M | anything else of any total | new document ("pagination resets") |

### Tier 2 — page furniture (adds to a score)

| Signal | Weight |
|---|---|
| A line (≥ 12 chars) repeated in the header band of both pages | +5 |
| A line repeated in the footer band | +2 |
| Header bands look alike (similarity > 0.5) | +2 |
| Footer bands look alike | +2 |
| Same accession number | +6 |
| Same order / visit / encounter number | +5 |
| Same MRN | +1 |

### Tier 3 — structure (adds to the score)

| Signal | Weight |
|---|---|
| A **matched** section header in the top 15% of this page (a title) | −6 |
| A signature on the previous page | −3 |
| "continued" / "cont'd" near the top | +6 |

Unmatched header candidates do not count as titles — any bold line
("Respiratory:") is a candidate.

### Score → relation

| Score | Relation | What happens |
|---|---|---|
| ≥ 5 | continue | joins the open document |
| ≤ −2 | new_document | opens a new document |
| between | **unknown** | opens its own document, `review_required = true` — nothing is copied onto it |

### Bands

| | With positions | Without |
|---|---|---|
| Header | lines whose top < 10% of page height | first 4 lines |
| Footer | lines whose bottom > 92% | last 3 lines |
| Pagination search | header + footer bands | first and last quarter of the lines |

---

## 5. Progress notes

A document whose first page's family is `progress_note` gets extra rules.

**Stays open** through pages the signals cannot decide (`unknown` → continue,
`decided_by = progress_note`).

**Closes:**

1. **After the signature page.** That page is the note's last. The next page is
   judged normally (and the previous-signature −3 applies).
2. **Before a page the signals or pagination call a new document.**
3. **Before the next encounter:** a page whose own DOS differs from the note's
   **and** that carries a visit-opening section header. This overrides
   continuing pagination.

Visit-opening headers (`encounter_headers` in the canon): Office Visit, Office
visits, Initial office note, Progress Note(s), History and Physical, Admission
Note, Consultation (Note), Consult Note, Reason for Visit, Annual Wellness Visit,
Encounter visit, Patient Encounter, Encounter Form, Emergency visit records,
Follow-up Visits, Telephone Encounter / Visit, Visit summary, Home Health /
Skilled Nursing visit notes, Chief Complaint, CC, and a few more.

**Why both a new DOS and a header.** Measured on the local charts:

* *Pagination is a print job, not an encounter.* 53688890 prints five office
  visits as "page 1 of 17 … 17 of 17". A new date has to be able to split it.
* *A new date alone is too eager.* Continuation pages carry stray old dates —
  a 2017-11-14 problem-list date inside a 2025 note (60307605 pages 7, 17, 21),
  labelled only "Date". Splitting on it breaks the note.
* The true new visits all had a date found by an encounter label ("Office Visit
  on") and a matched "Office Visit" header. The stray dates had neither.

After a signature closes a note, the document still remembers it began as a
progress note, so the next-encounter check keeps working inside a long printout.

---

## 6. Final values

Per document:

| Final | Rule |
|---|---|
| Final page type | the first page's page type |
| Final DOS | the first page's DOS; if it has none, the first page in the document that has one (the evidence says "Final DOS from page N") |

A continuation page with no date shows Extracted DOS = NA and Final DOS = the
first page's. A page outside any document (blank/junk/duplicate) has no Final
values.

---

## 7. Outputs

### `page_continuity_results` (v1.sql)

| Column | |
|---|---|
| `document_seq` | document number within the chart |
| `seq` | page number within the document |
| `position` | single / first / continue / last |
| `relation` | new_document / continue / unknown |
| `decided_by` | first_page / pagination / signals / progress_note |
| `confidence_level` | high (pagination, first page) / medium (signals, progress note) / low (unknown) |
| `score` | tier 2+3 score when signals decided |
| `review_required` | true for unknown |
| `evidence` | e.g. `page 7->8`, `next encounter: Office Visit on 2024-10-03 (page 7->8)`, `repeated header line; same mrn` |

The printed page number and section headers are stored per page in
`additional_page_details` (written by the key/value stage; moved from v2.sql to
v1.sql).

### `imaging/<chart>_continuity.csv`

The same per page, plus the page's own `page_type`, `dos_from`, `dos_to`, the
printed `page_no` / `page_total` used, the `section_headers` names, and
`layout_source` (final2 / final1 / text).

### `imaging_final` (v1.sql) and `imaging/<chart>_final.csv`

Written by the last stage, `imaging_final` (seq 100), one row per page, from
every stage's stored output. The Final value of every reviewer field:

| Group | Columns | Final is |
|---|---|---|
| Member | `member_name`, `member_dob`, `member_id` | the page's own (member stage) |
| Quality & orientation | `printed_or_handwritten`, `handwritten_area_pct`, `is_visible`, `quality_tag`, `orientation_angle`, `tilt_angle`, `mirrored` | the page's own (stage 1) |
| Classification | `blank_junk_flag`, `is_duplicate` | the page's own |
| Document | `document_seq` | from continuity |
| Page type | `page_type`, `page_subtype`, `model_type`, `codability`, `classification_category`, `page_type_source`, `continuation_rule` | the page's own, unless a continuation rule changes it (`source = continuation` / `embedded`) — see PAGE_CLASSIFICATION.md §6 |
| Date of service | `dos_from`, `dos_to`, `dos_source` | **the document's first dated page's** (`source = document`), else the page's own |
| Encounter | `encounter_type` | the page's own (already one per visit) |
| Provider | `provider_name`, `signature_present` | the page's own (key/value staging) |
| Sequence | `seq` | suggested sequence |
| Duplicate | `duplicate_of_page_id` | the page this one duplicates (duplicates only) |

**Duplicates.** Every stage after blank/junk skips a duplicate page, so it has
no values of its own. It takes the member, document, page type (all columns),
DOS, encounter type and provider of the page it duplicates, with
`page_type_source` / `dos_source` = `duplicate`. It keeps its own quality,
orientation, blank/junk flag and `seq` (`COPIED_FROM_REFERENCE` in
`imaging_final/stage.py`).

The carry rule lives in one function, `continuity.engine.document_finals`, used
by both stages.

### review-ui

Page Type, Page Subtype, Codeable and DOS show Extracted (own) vs Final (from
`imaging_final`). A "Document" row shows `Document 3 · continue — page 7->8 of
17`. Runs from before these stages still display through the old `finalDos` /
`dosMatch = span` path.

### Existing database

```bash
psql "$DATABASE_URL" -f schema/temp_changes_to_db.sql   # safe to re-run
```

---

## 8. What it replaced

Three mechanisms that each decided "continues" their own way and could disagree:

| Was | Where | Now |
|---|---|---|
| MiniLM + "page ends mid-sentence" tagger, CSV columns `continues_previous` / `continue_reason` | `stages/lib/continuation/` (deleted) | continuity |
| DOS progress-note span (`span_page_types`, `span_override_score`, `match_type = span`, `final_dos = note date`) | `dos_logic.py`, `dos_canon.json` | every page keeps its own date; continuity carries the Final DOS |
| Page-type span (`continue` on canon entries) and fill-between (`fill_between_families`) | `codeable_classify.py`, `codeable_canon.json` | every page classified on its own; continuity carries the Final page type |

Kept: the family `span` flag (it only ranks progress notes and discharge over
Demographics on a single page) and `span_break_score` (a score floor for
model-named families). Both are per-page.

---

## 9. How the weights were chosen

80 page pairs from four charts, labelled by printed pagination (55 continuations,
25 new documents), scored with pagination hidden.

| Signal | On continuations | On new documents |
|---|---|---|
| Repeated header line (top 10%) | 75% | 28% |
| Repeated footer line | 53% | 28% |
| Matched title in the top 15% | 11% | 20% |
| Previous page signed | 11% | 40% |
| "continued" marker | 4% | 4% |
| Identity tokens | 0% | 0% |

* The repeated header is the strongest signal but fires on 28% of new
  documents: separate documents from one EMR reprint the same patient banner.
  So it cannot reach `continue_at` alone.
* Errors were weighted: a wrong **merge** copies another document's page type
  and DOS onto a page, so it counted three times a wrong **split**.

| Weights | New docs wrongly merged | Continuations wrongly split | Left unknown |
|---|---|---|---|
| Prototype (`continuity.json`) | 11 / 25 | 3 / 55 | 15 |
| Chosen | 6 / 25 | 1 / 55 | 25 |

* Sentence-level continuity is not used. From the prototype's corpus: "previous
  page ends without punctuation" 28% on continuations vs 29% on new documents;
  "next page starts lowercase" 91% vs 93%.

---

## 10. Dry run on the local charts

| Chart | Pages | Documents | Notes |
|---|---|---|---|
| 53688890 | 29 | 8 | the 17-page printout splits into its 5 visits (2024-03-28, 06-03, 10-03, 2025-03-27, 09-24) plus a page with 2024-10-03 + "Reason for Visit" |
| 60307605 | 30 | 8 | five paginated 5-page visits; stray 2017 dates stay inside their notes |
| 60306165_250126_1043 | 42 | 7 | 40-page printout, split by pagination resets and one next-encounter |
| 60140965.tif | 13 | 3 | no pagination; signals + progress-note rule; page 12's stray 2025-07-23 stays in its note |

---

## 11. Open questions — for the brainstorm

1. **Tuning data is thin.** 80 labelled pairs, four charts, one or two EMRs.
   The prototype's corpus (366 charts in `continuity_sample.csv`) has its OCR
   elsewhere; re-tuning on it, or on ground-truth `page_sequence`, would firm up
   the weights.
2. **Same-EMR new documents.** 6 of 25 still merge on banner alone. Ideas: a
   different matched title family between the pages; the page-type family
   changing (lab → progress note) as a negative signal; a DOS change outside
   progress notes.
3. **Unknown → own document.** Conservative, but 25 of 80 pairs land there and
   are flagged. Is that review load acceptable, or should non-note unknowns
   join the open document (prototype behaviour) with the Final values withheld?
4. **Which families stay open until signed?** Only `progress_note` today.
   Discharge summaries and operative reports have the same shape.
5. **Next-encounter headers.** The list is hand-picked from the section-header
   canon. Should it come from the DOS stage instead — "this page's date came
   from an encounter label" (`dates[].source_keyword`)?
6. **Final DOS when the first page has none.** Currently the first dated page in
   the document. Alternatives: the most common date in the document; the
   encounter-label date only.
7. **Pages out of order.** 53688890 page 15 (2024-10-03) sits after the
   2025-03-27 visit. Continuity groups in file order; sequencing suggests a
   reorder but they do not talk to each other yet.
8. **Final page type for a different-family page inside a document** (a lab
   table inside a visit becomes Final "Progress Note"). Right for coding? Or
   should some families (labs, imaging) keep their own type?

## 12. Open questions on `imaging_final`

1. **Member values** come from `member_extraction_results`. review-ui's
   Member rows prefer the key/value staging's value when it has one, so the
   two can differ on a page where they disagree.
2. **Provider credentials** are not split out; `provider_name` is stored as
   written (review-ui splits the credential suffix for display).
3. **Encounter type** stays per page. It is already decided per visit (pages
   sharing a DOS); should it follow the Final DOS instead of the page's own?
