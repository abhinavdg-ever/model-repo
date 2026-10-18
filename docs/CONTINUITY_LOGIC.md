# Document continuity — logic as implemented

Which document each page belongs to, and the values a document's pages share. What
the code does today.

| | |
|---|---|
| Engine | `core-pipeline/stages/lib/continuity/engine.py` (`assign`, `document_ended`, `document_finals`) |
| Stage | `continuity`, pass 1, seq 87 — after `page_subtype` and `dos_extract`, before `imaging_final` |
| Settings | `stages/lib/keyword-canon/continuity_canon.json` (reloads on change) |
| Writes | `page_continuity_results`, `imaging/<chart>_continuity.csv` |
| Final values | written by `imaging_final` (seq 100) into `imaging_final` / `<chart>_final.csv` |

---

## 1. The question

For every page: **does it continue the previous page's document, or start a new one?**
The pages of one document then share the first page's **Final** page type and **Final**
DOS. Each page keeps its own Extracted values.

| | Extracted (own) | Final (document) |
|---|---|---|
| Page 1 of a visit | Progress Note, 2024-03-28 | Progress Note, 2024-03-28 |
| Page 2, no title, no date | Progress Note, — | Progress Note, 2024-03-28 |
| Page 3, a lab table inside the visit | Laboratory Data, 2024-03-28 | Progress Note, 2024-03-28 |

---

## 2. Inputs per page

| Input | From |
|---|---|
| Own DOS (From, To) | `dos_extraction_results` — the page's Extracted DOS |
| Page type (compared as its family, the L1) and the classifier's confidence | `page_classification` (axes) |
| Title on the page (a title term for its page type) | page classification (`axes.title_of`, else `keyword_title_hit`) |
| Printed page number "N of M" | key/value staging (`page_no`), else read from the header / footer bands |
| Signed | `provider_signature.csv` `signature_present = y`, or a signature block in the text ("Electronically signed", "Signed by", "Provider signature", "Signature:" …) |
| Line positions | Final 2 JSON line polygons, else Final 1 words grouped into lines, else plain text lines |
| Skipped | blank / junk / duplicate (final verdict) |

Bands (`bands`): header = a line whose top is above **10%** of the page height;
footer = a line whose bottom is below **92%**. Without positions: the first 4 and last 3
lines, and a quarter of the lines each way for page numbers.

---

## 3. The decision — first match wins

**Skipped pages** (blank, junk, duplicate) belong to no document. A **blank or junk**
page also **ends** the open document; a duplicate does not.

| # | Check | Result | `decided_by` |
|---|---|---|---|
| 1 | First page of the chart | new | `first_page` |
| 2 | A blank or junk page came just before | new | `blank_junk` |
| 3 | The page has its own DOS **equal to the open document's** (From and To) | **continue** | `same_dos` |
| 4 | The page has its own DOS, **a different one** | **new** | `new_dos` |
| | *From here on the page has no DOS of its own* | | |
| 5 | **Printed page numbers follow on** (previous "page 3 of 5", this "page 4 of 5", same total) | **continue** — overrides 6–10 | `pagination` |
| 6 | The **previous page was signed** | new | `signature` |
| 7 | The previous page was printed **"page N of N"** (its last) | new | `page_number_end` |
| 8 | This page **looks like a first page**: a title for a page type other than the previous page's | new | `first_page_marker` |
| 9 | **The page type changes and the classifier is sure** (§3.1) | new | `page_type` |
| 10 | **The previous page's text stops early**: its last line (above the footer) ends above **60%** of the page height | new | `page_end` |
| 11 | None of the above | **continue** | `continue_page` |

### 3.1 Page type change (check 9)

*Same-type probability* = the classifier's confidence in this page's type when it is
the previous page's type (family), else 1 − that confidence. A new document starts
when it is **≤ 10%** (`max_same_type_probability`) **and** the classifier is **> 70%**
sure of the new type (`min_new_type_confidence`).

| Previous | This page | Confidence | Same-type probability | Result |
|---|---|---|---|---|
| Progress Note | Lab | 90% | 10% | new document |
| Progress Note | Lab | 60% | 40% | continue |
| Progress Note | Progress Note | 95% | 95% | continue |

### 3.2 Text stops early (check 10)

The bottom of the previous page's last line that starts above the footer (92%), as a
share of the page height. Above **60%** (`page_end_max`), that document ended there.
Needs line positions (Final 2 / Final 1); a text-only page gives no signal.

### 3.3 Printed page numbers

"Page 4 of 5", "Pg 4/5", a bare "4 of 5" (total ≤ 20, `max_total`). They **never split**
a document: they only confirm a continue page (check 5) or close a document (check 7).
"Page 1" on a document's first page marks its start as confirmed.

---

## 4. What a document shares — the Final values

`document_finals`, carried into `imaging_final` by its stage:

| Final value | Rule |
|---|---|
| **Page type** | the first page's (each page keeps its own sub-type) |
| **DOS** | the first page's, on every page |
| **DOS of a stand-alone non-visit document** | when **every** page is a `default_final_dos` type — Patient Demographics, Patient Information, Patient Registration, Growth chart, Problem List, Immunizations / Immunization list — the Final DOS is **2222-02-02** (`DOS_DEFAULT_DATE`). One such page inside a visit's document takes the visit's DOS |
| **Encounter type** | the first page's |
| Member, provider, quality, orientation, blank / junk | each page's own |

**Duplicates** take the page they duplicate's document, page type, DOS, encounter type,
member and provider (`page_type_source` / `dos_source` = `duplicate`), and keep their
own quality, orientation and blank / junk flag.

---

## 5. What is recorded per page

| Field | Meaning |
|---|---|
| `document_seq` | document number in the chart |
| `seq` | page number within the document |
| `position` | `single` / `first` / `continue` / `last` (review-ui: "Continue (Doc 3)") |
| `relation` | `new_document` / `continue` |
| `decided_by` | the check in §3 that decided |
| `confidence_level` | `high` (first page, blank/junk, DOS, page numbers, signature, page N of N) / `medium` (title, page type, page end, continue page) |
| `link` | `strong` — a continue by same DOS or page numbers; `weak` — any other continue |
| `link_strength` | `strong` while every link back to the first page is strong, else `weak` |
| `start_confirmed` | the first page has a title, or says "page 1" |
| `page_no`, `page_total` | the printed page number used |
| `evidence` | e.g. "same DOS 2024-03-28", "no DOS of its own; page 3->4 of 5", "the previous page was signed", "page type changes (Progress Note -> Lab, 10% it is still Progress Note)" |

---

## 6. Worked example

| Page | Own DOS | Other signals | Decision | Doc |
|---|---|---|---|---|
| 1 | 2024-03-28 | Progress Note title | first page | 1 |
| 2 | — | "page 2 of 3" after "page 1 of 3" | continue (pagination) | 1 |
| 3 | — | page 3 of 3, signed | continue (pagination) | 1 |
| 4 | — | previous page was "3 of 3" and signed | new (signature) | 2 |
| 5 | 2024-06-03 | — | new (new DOS) | 3 |
| 6 | blank | no ink | skipped; ends Doc 3 | — |
| 7 | — | Face Sheet | new (blank_junk) | 4 — stand-alone non-visit: Final DOS 2222-02-02 |

Final DOS: pages 1–3 = 2024-03-28; page 4 = its own first page's DOS (none);
page 5 = 2024-06-03; page 7 = 2222-02-02.

---

## 7. Every setting (`continuity_canon.json`)

| Setting | Value |
|---|---|
| `bands.head_top` / `foot_bottom` | 0.10 / 0.92 |
| `bands.head_lines` / `foot_lines` / `text_band_fraction` | 4 / 3 / 0.25 |
| `pagination.patterns`, `bare_pattern`, `max_total` | "page N of M", "pg N/M", bare "N of M"; total ≤ 20 |
| `document_end.page_end_max` | 0.6 |
| `document_end.max_same_type_probability` | 0.1 |
| `document_end.min_new_type_confidence` | 0.7 |
| `default_final_dos` | the non-visit page types in §4 |
