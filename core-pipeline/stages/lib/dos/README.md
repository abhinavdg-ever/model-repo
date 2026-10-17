# DOS extraction

One row per page, in the DB and in `imaging/<chart>_dos.csv`. Per page, the
first rule that finds a date wins:

1. **Key/value.** The date the key/value extractor selected for the page.
   Spaced slashes (`05 / 20 / 2025`) are merged into one OCR word first, so
   they count as plain dates.
2. **Edge keyword.** A DOS label that starts within the first or last 20 words
   of the page, with a date right after it (only filler words such as `at`,
   `on`, `time` in between). An encounter label (Encounter Date, Visit Date,
   Date of Service, DOS, Service Date, Appointment Date, ...) wins; otherwise
   Admission Date is From and Discharge Date is To; a discharge date alone is
   both. A written range after an encounter label ("DOS: a - b") keeps both ends.
   Read on the final OCR text first; on a page with no Final2 text, then on
   the Tesseract text (not for handwritten / low-quality pages), because
   Docling can drop the top banner.
2b. **Key/value pattern fallback.** Only when 1 and 2 found nothing: a date
   with spaces only (`06 12 24`) right after a DOS key, 0.30;
   `match_type` `kv_pattern`.
3. **Azure OpenAI**, when `DOS_LLM_ENABLED`, on pages matching `clinical_cues`.
4. **Key/value, low confidence.** When the model chose no date, the date its rules
   accepted, at the model's own (low) probability; `match_type` `kv_low`.
5. Otherwise the page has no date of service.

Each page keeps its own date here (the Extracted DOS). The Final DOS — the
document's first page's on every page, or `DOS_DEFAULT_DATE` for a stand-alone
non-visit document — is set by continuity / `imaging_final`. More detail in
[`docs/LOGIC.md` § Date of service](../../../../docs/LOGIC.md#6-date-of-service).

Labels, `edge_words`, the allowed filler words, the confidences and
`DOS_DEFAULT_DATE` live in `../keyword-canon/dos_canon.json` and reload on change.

## CSV columns

```csv
chart_name,page_name,page_number,dos_from,dos_to,dos_from_iso,dos_to_iso,doc_dos_from,doc_dos_to,doc_dos_from_iso,doc_dos_to_iso,match_type,final_dos,keyword,confidence,extraction_method
```

`match_type` is `edge_keyword`, `edge_keyword_tesseract`, `admit_discharge_pair`, `kv`, `llm` or
`no_date_found`. `confidence` is fixed per rule (edge 0.95, key/value 0.90,
LLM 0.70; 0 when nothing was found). The `doc_dos_*` columns are the page's
own date, or `DOS_DEFAULT_DATE` when it has none.

## Debugging a pick

`DOS_DEBUG=true` writes `<chart>/debug/<chart>_dos_candidates.csv`: every DOS
label found at a page's edges, its date, which edge, and `chosen`.
