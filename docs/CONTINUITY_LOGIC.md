# Document continuity — logic as implemented

Which document each page belongs to, and the values a document's pages share. What the
code does today (the weighted revamp, 2026-10-18). The design, the measurement behind
the weights and the encounter-type half still to build:
[`CONTINUITY_ENCOUNTER_REVAMP.md`](CONTINUITY_ENCOUNTER_REVAMP.md).

| | |
|---|---|
| Engine | `core-pipeline/stages/lib/continuity/engine.py` (`assign`, `_gate`, `_weighted`, `document_finals`) |
| Signals | `core-pipeline/stages/lib/continuity/signals.py` |
| Stage | `continuity`, seq 87 — after `page_subtype` and `dos_extract`, before `imaging_final` |
| Settings | `stages/lib/keyword-canon/continuity_canon.json` (version 3, reloads on change) |
| Writes | `page_continuity_results` (incl. `score`, `review_required`), `imaging/<chart>_continuity.csv` |
| Final values | written by `imaging_final` (seq 100) into `imaging_final` / `<chart>_final.csv` |
| Database changes | [`schema/changes_2026-10-18.sql`](../schema/changes_2026-10-18.sql) §4 (`decided_by` gains `weighted`) |

---

## 1. The question

For every page: **does it continue the previous page's document, start a new one, or is
it unclear?** The pages of one document then share the first page's **Final** page type
and **Final** DOS. Each page keeps its own Extracted values.

Getting it wrong is asymmetric: merging two encounters is a clinical error that reaches
coding; splitting one is a nuisance a reviewer catches. Unclear pages are therefore
**flagged**, not guessed.

---

## 2. Inputs per page

| Input | From |
|---|---|
| Text | the page's OCR text (Final 2 → Final 1 → Tesseract) |
| Line positions | Final 2 JSON line polygons, else Final 1 words grouped into lines, else plain text lines |
| Section headers | the OCR JSON (`section_headers`, matched to `section_header_canon.json`) |
| Own DOS (From, To) | `dos_extraction_results` — the Extracted DOS |
| Page type (L1), sub-type, full path, confidence | `page_classification` (axes) |
| Title on the page | page classification (`axes.title_of`, else `keyword_title_hit`) |
| Printed page number "N of M" | `additional_page_details` (key/value stage), else read from the header / footer bands |
| Signed | `provider_signature.csv` `signature_present = y`, or a signature block in the text |
| Signed in tail | a signature block in the page's **last third** of lines |
| Skipped | blank / junk / duplicate (final verdict) |

Bands: header = a line whose top is above **10%** of the page height; footer = a line
whose bottom is below **92%**. Without positions: the first 4 and last 3 lines.

---

## 3. The decision

### 3.1 Skipped pages

Blank, junk and duplicate pages belong to no document. A **blank or junk** page also
**ends** the open document (a physical separator); a **duplicate** does not (a scanning
artefact) and takes its original's document and values.

### 3.2 Gates — the first that applies decides

| # | Gate | Result | `decided_by` |
|---|---|---|---|
| 1 | First page of the chart | new | `first_page` |
| 2 | A blank or junk page came just before | new | `blank_junk` |
| 3 | Own DOS **equal** to the open document's (From and To) | **continue** | `same_dos` |
| 4 | Own DOS, **different** | **new** | `new_dos` |
| 5 | Printed page numbers follow on: "3 of 5" → "4 of 5" | **continue** | `pagination` |
| 6 | The previous page was printed "page N of N" | new | `page_number_end` |

**DOS outranks pagination**: a different date is a different encounter, whatever the
footer says. **Pagination never starts a document**: it confirms a continue (5) or closes
one (6) — a misread digit must not split a document.

### 3.3 Weighted evidence — an undated page no gate decided

```
score = Σ weight × value        every value in [0, 1]

score >= +4     continue         confidence medium   decided_by weighted
score <= -4     new document     confidence medium   decided_by weighted
otherwise       unknown          confidence low      review_required, stays in the open document
```

| Signal | Weight | Value | Status |
|---|---|---|---|
| `section_forward` | **+10.0** | the next page's sections follow the previous page's in note order (§4.3), 0..1 | fitted |
| `page_type_change` | **−7.0** | (1 − same-type probability) × confidence in the new type (§4.4) | estimated |
| `cur_has_title` | **−6.0** | the page carries a title for its page type | estimated |
| `same_visit_number` | +5.4 | both pages print the same visit / encounter / FIN / CSN number | fitted |
| `page_fill` | +5.1 | how far down the previous page's body text reaches (§4.2) | fitted |
| `header_line_repeats` | +4.7 | a header line (≥ 12 characters) printed on both pages | fitted |
| `prev_signed` | −4.4 | the previous page is signed anywhere | fitted |
| `body_cosine_char` | +3.7 | character tf-idf similarity of the previous page's tail and this page's head (§4.1) | fitted |
| `full_path_same` | +3.0 | the same page type at every level (L1 / L2 / L3) | estimated |
| `footer_line_repeats` | +2.8 | a footer line printed on both pages | fitted |
| `body_cosine_word` | +1.0 | word tf-idf similarity of the same blocks | fitted |
| `cur_opens_note` | +0.5 | the page opens on Chief Complaint / HPI | fitted |
| `prev_signed_in_tail` | −0.4 | the previous page's signature is in its tail | fitted |

`fitted` = measured on 170 labelled pairs from 15 charts (balanced accuracy 0.79 ± 0.19,
split by chart); `estimated` = specified after the measurement, to fit before trusting.
The `measurement` block in the canon carries the provenance.

### 3.4 The abstain band

A page scoring between −4 and +4 is **unknown**: it stays in the open document (leaving a
page in place is the recoverable error; starting a spurious document fragments an
encounter) and is flagged `review_required`. Never widen the band to lower the abstain
rate — those are the pages most likely to be wrong.

### 3.5 Signature terminator

A previous page with a **signature block in its tail** closes its document even when the
score kept it open (`decided_by signature`). Gates 3 and 5 (same DOS, page numbers) still
win, because they decide first.

---

## 4. The signals in detail

### 4.1 Text running from one page into the next

1. **Strip the furniture**: lines of ≥ 12 characters printed on both pages (patient
   banner, footer template) are removed — they appear on every page of the chart and
   would swamp the body. (They are what `header_line_repeats` / `footer_line_repeats`
   score.)
2. **Tail and head blocks**: the previous page's last body lines, walking back in groups
   of three lines until 20 words; the next page's first body lines, forward, likewise.
3. **TF-IDF cosine** on those blocks — character 3–5 grams within words, and word 1–2
   grams; `min_df` 2, sublinear tf; fitted on **every block of the chart** (never per
   pair). Normalised: `min(1, cosine / 0.25)` for characters, `/ 0.15` for words.

### 4.2 Page fill

Where the previous page's body ends: the bottom of its last line **starting above the
footer band**, divided by the footer top (0.92), clipped to 1.

| Fill | Value |
|---|---|
| ≤ 0.60 | 0 — the document probably ended there |
| 0.60 – 0.92 | graded: (fill − 0.60) / 0.32 |
| ≥ 0.92 | 1 — the text runs on |

Without line positions: 1 when the page has ≥ 28 lines or ≥ 1,800 characters, else 0;
when that text proxy swings a weighted decision, the page is flagged for review.

### 4.3 Section order

Note order (`section_order` in the canon, each step mapped to `section_header_canon.json`
names): chief complaint → HPI → review of systems → past history → social → family →
medications → allergies → vitals → physical exam → labs → imaging → problem list →
assessment / diagnosis → plan → patient instructions → follow-up.

`section_forward = clip((first section of this page − last section of the previous) / steps × 4, 0, 1)`.
Moving backwards counts 0 (no evidence either way).

### 4.4 Page type change

| Previous | This page | Confidence | Same-type probability | Value | × −7 |
|---|---|---|---|---|---|
| Progress Note | Lab | 90% | 10% | 0.81 | −5.7 |
| Progress Note | Lab | 60% | 40% | 0.36 | −2.5 |
| Progress Note | Progress Note | 95% | 95% | 0.05 | −0.3 |

---

## 5. Review flags (`review_required`)

| Flag | When |
|---|---|
| Unknown | the score fell inside the band (§3.4) |
| Long run without page numbers | a document of more than **25** pages where no page prints a page number (a footer template may be binding unrelated documents) — every page of it |
| Text-proxy fill | page fill came from the text proxy (no line positions) and decided |

---

## 6. Links and position

| Field | Meaning |
|---|---|
| `link` | `strong` — joined by same DOS or page numbers; `weak` — by the weighted score; `unknown` — kept across the abstain band |
| `link_strength` | `strong` while every link back to the first page is strong, else `weak` |
| `position` | `single` / `first` / `continue` / `last` (review-ui: "Continue (Doc 3)") |
| `start_confirmed` | the first page has a title, or prints "page 1" |
| `score` | the weighted score (weighted / signature decisions only) |
| `evidence` | e.g. "same DOS 2024-03-14", "no DOS of its own; page 3->4 of 5", "score +10.4 (page_fill +5.1, full_path_same +3.0, footer_line_repeats +2.8, page_type_change -1.1)", "score +0.3 between -4 and +4: kept in the document, for review (…)" |

---

## 7. What a document shares — the Final values

Unchanged by the revamp (`document_finals`):

| Final value | Rule |
|---|---|
| Page type | the first page's (each page keeps its own sub-type) |
| DOS | the first page's, on every page |
| DOS of a stand-alone non-visit document | every page a `default_final_dos` type (Patient Demographics, Patient Information, Patient Registration, Growth chart, Problem List, Immunizations) → **2222-02-02**; such a page inside a visit takes the visit's DOS |
| Encounter type | the first page's |

---

## 8. Settings (`continuity_canon.json`, version 3)

| Block | Holds |
|---|---|
| `pagination`, `bands`, `default_final_dos` | as before |
| `weights` | each signal's `w`, measured `lift`, `coverage`, `status` (fitted / estimated) |
| `decision` | `continue_at` 4.0, `new_at` −4.0 |
| `semantic` | furniture 12 chars, 20-word blocks, char 3–5 / word 1–2 grams, `min_df` 2, divisors 0.25 / 0.15 |
| `page_fill` | `ended_at` 0.60, `continues_at` 0.92, graded, text proxy 28 lines / 1,800 chars |
| `section_order` | the steps and the section names that count as each; `opens_note` |
| `signature_terminator` | on; tail = the last 33% of lines |
| `review_triggers` | unknown, long run 25, text-proxy fill |
| `measurement` | provenance of the weights |
| `document_end` | kept for reference; the ladder that read it is gone |

The loader refuses a file whose `weights` name a signal the engine does not produce, or
whose band is inverted (`new_at ≥ continue_at`).

---

## 9. Worked example

| Page | Own DOS | Evidence | Decision | Doc |
|---|---|---|---|---|
| 1 | 2024-03-28 | — | first page | 1 |
| 2 | — | "page 2 of 3" after "page 1 of 3" | continue (`pagination`) | 1 |
| 3 | — | banner + footer repeat, text to the bottom, sections HPI → Plan | continue (`weighted`, +27.6) | 1 |
| 4 | — | previous page ends "Electronically signed by …" | new (`signature`) | 2 |
| 5 | — | Lab after Progress Note at 90%, a Lab title, text ended at 40% | new (`weighted`, −11.7) | 3 |
| 6 | — | text to the bottom (+5.1), but Lab after Lab at 60% with a title | unknown, flagged (`weighted`, −0.9) | 3 |
| 7 | 2024-06-03 | — | new (`new_dos`) | 4 |
| 8 | blank | no ink | skipped; ends Doc 4 | — |
