# DOS extraction

One row per page, in the DB and in `imaging/<chart>_dos.csv`. Per page, two
states:

1. **Key/value.** Among the dates the extractor accepted, grouped by the tier it
   gave them (`staging/extraction.json`), the first group in the page kind's
   order (`kv_priority` in `dos_canon.json`). The kind is the page
   classification L1 (`page_kinds`):
   - **discharge** (Discharge Summary): Admit + Discharge pair → service → weak
     → dt/dd → header/footer
   - **lab** (Lab Report): collected → received → Report Date → service →
     weak → dt/dd → header/footer → Admit + Discharge pair
   - **other**: service → weak → dt/dd → header/footer → Admit + Discharge pair

   Within a group the highest score (model, else rules). Only weak keys have a
   floor: score above 0.3 (`weak_min_score`). Its confidence is that score.
   Spaced slashes (`05 / 20 / 2025`) are merged into one OCR word first, so they
   count as plain dates.
2. **Edge keyword**, only when 1 has no date. A DOS label that starts within
   the first or last 20 words of the page, with a date right after it (only
   filler words such as `at`, `on`, `time` in between). An encounter label
   (Encounter Date, Visit Date, Date of Service, DOS, Service Date, ...) wins;
   otherwise Admission Date is From and Discharge Date is To; a discharge date
   alone is both. A written range after an encounter label ("DOS: a - b")
   keeps both ends. Never a date right after `D:`, `T:`, `DD:`, `DT:`, `TD:`,
   `TT:`, Dictated or Transcribed. Read on the final OCR text first; on a page
   with no Final2 text, then on the Tesseract text (not for handwritten /
   low-quality pages), because Docling can drop the top banner. Confidence 0.50.

On an "other" page with neither: the dictated date (`D:`, `DD:`, Dictated),
else the transcribed one, at 0.30. Otherwise the page has no date of service.

The Admit + Discharge pair: From = admit, To = the discharge on or after it,
one alone is both; on a discharge page an edge label fills a missing side;
`source` `admit_discharge`.
A Printed / Print Date label's date is never a DOS.

Each page keeps its own date here (the Extracted DOS). The Final DOS — the
document's first page's on every page, or `DOS_DEFAULT_DATE` for a stand-alone
non-visit document — is set by continuity / `imaging_final`. Full detail in
[`docs/DOS_LOGIC.md`](../../../../docs/DOS_LOGIC.md).

Labels, `edge_words`, the allowed filler words, the confidences and
`DOS_DEFAULT_DATE` live in `../keyword-canon/dos_canon.json` and reload on change.

## CSV columns

```csv
chart_name,page_name,page_number,dos_from,dos_to,dos_from_iso,dos_to_iso,doc_dos_from,doc_dos_to,doc_dos_from_iso,doc_dos_to_iso,match_type,final_dos,keyword,confidence,extraction_method,source
```

`source` is `kv`, `edge`, `edge_tesseract`, `admit_discharge`, `dictation` or `none`. `match_type` is `kv`,
`edge_keyword`, `edge_keyword_tesseract`, `admit_discharge_pair` or
`no_date_found`. `extraction_method` is `kv` for a key/value date, else `rules`.
The `doc_dos_*` columns are the page's own date, or `DOS_DEFAULT_DATE` when it
has none.

## Debugging a pick

`DOS_DEBUG=true` writes `<chart>/debug/<chart>_dos_candidates.csv`: every DOS
label found at a page's edges, its date, which edge, and `chosen`.
