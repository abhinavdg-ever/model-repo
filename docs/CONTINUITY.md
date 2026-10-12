# Document continuity

Which document each page belongs to, and the values a document's pages share.

> Status, 2026-10-12: grouping rewritten to the date-of-service rules in §4.
> Final DOS is each page's own (no carrying); Final page type is the first
> page's. Tested in `tests/test_continuity.py`; not yet re-run on a live batch.
> §9–§11 describe the earlier tiered engine and are kept as history.

---

## 1. What it is for

A chart is a stack of pages from many documents: progress notes, labs, face
sheets, letters. Most fields are extracted per page, but a page usually only
makes sense as part of its document. The second page of an office visit has no
title and often no date of service, yet its page type and DOS are the visit's.

Continuity answers one question: does this page continue the previous page's
document, or start a new one? The Final page type of every page in a document
is its first page's (the sub-type can differ), and so are its Final DOS and
encounter type. A stand-alone document of non-visit page types takes the
default DOS 2222-02-02.

| | Extracted | Final |
|---|---|---|
| First page of a visit | Progress Note (SOAP Note), 2024-03-28 | Progress Note (SOAP Note), 2024-03-28 |
| Page 2, no title, no date | Progress Note, NA | Progress Note, 2024-03-28 |
| Page 3, a lab table inside the visit | Laboratory Data, 2024-03-28 | Progress Note / Laboratory Data, 2024-03-28 |

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

Each page is judged against the open document (blank/junk/duplicate pages are
passed over; a blank or junk page in between ends the document).

| Page | Result | `decided_by` |
|---|---|---|
| Has a DOS equal to the open document's (From and To) | continue — same-DOS pages are always one document | `same_dos` |
| Has a different DOS | new document | `new_dos` |
| No DOS, previous page signed | new document | `signature` |
| No DOS, printed page number follows the previous page's (N → N+1 of M) | continue, confirmed — overrides the checks below | `pagination` |
| No DOS, previous page printed "page N of N" (its last) | new document | `page_number_end` |
| No DOS, looks like a first page (a keyword title for another page type) | new document | `first_page_marker` |
| No DOS, page type changes and the classifier is sure | new document | `page_type` |
| No DOS, previous page's text ends mid-page | new document | `page_end` |
| No DOS, otherwise | continue | `continue_page` |

**Page type change.** The chance that this page is the previous page's type is
the classifier's confidence when the two types agree, and one minus it when
they differ. At or below `document_end.max_same_type_probability` (10%), with
the classifier above `min_new_type_confidence` (70%) on the new type, the
document ends: a Lab page after a Progress Note at 90% confidence (10%) ends
it; at 60% (40%) it continues.

**Mid-page ending.** The bottom of the previous page's last line above the
footer band (a line starting below 92% is footer). Above
`document_end.page_end_max` (60%) of the page height, the document ended there.
Needs line positions (Final2 polygons, else Final1 words); a text-only page
gives no signal.

**Printed pagination** is read (header/footer bands, or the key/value page
number) and recorded as `page_no` / `page_total`. On an undated page,
consecutive numbers confirm a continue page and "page N of N" ends the
document; page 1 confirms a document's start. Page numbers never split a
document.

---

## 5. Progress notes

No special rules any more: a progress note is grouped like any other document.

---

## 6. Final values

| Final | Rule |
|---|---|
| Final page type | the first page's page type; the sub-type can differ (PAGE_CLASSIFICATION.md §6) |
| Final DOS | the first page's DOS on every page. A stand-alone document whose every page's type or sub-type is Patient Demographics, Patient Information, Patient Registration, Growth chart, Problem List, Immunizations or Immunization list (`continuity_canon.json` → `default_final_dos`) takes `DOS_DEFAULT_DATE` (2222-02-02); such a page inside a visit's document takes the visit's DOS |
| Final encounter type | the first page's |

A document's DOS (review-ui Doc Summary, "Start Page 1 – End Page 3") is its
first page's.

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
| Page type | `page_type`, `page_subtype`, `model_type`, `codability`, `classification_category`, `page_type_source`, `continuation_rule` | **the document's first page's type** (a ≥60% confidence-weighted vote only when the first page is Unknown; else each page's own, flagged); the sub-type can differ — see PAGE_CLASSIFICATION.md §6.1 |
| Date of service | `dos_from`, `dos_to`, `dos_source` | **the document's first page's** (`source = document`; the default for a stand-alone non-visit document), else the page's own |
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


### review-ui

Page Type, Page Subtype, Codable and DOS show Extracted (own) vs Final (from
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

## 9. History: how the tiered engine's weights were chosen

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

## 10. History: dry run of the tiered engine

| Chart | Pages | Documents | Notes |
|---|---|---|---|
| 53688890 | 29 | 8 | the 17-page printout splits into its 5 visits (2024-03-28, 06-03, 10-03, 2025-03-27, 09-24) plus a page with 2024-10-03 + "Reason for Visit" |
| 60307605 | 30 | 8 | five paginated 5-page visits; stray 2017 dates stay inside their notes |
| 60306165_250126_1043 | 42 | 7 | 40-page printout, split by pagination resets and one next-encounter |
| 60140965.tif | 13 | 3 | no pagination; signals + progress-note rule; page 12's stray 2025-07-23 stays in its note |

---

## 11. History: open questions on the tiered engine

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
