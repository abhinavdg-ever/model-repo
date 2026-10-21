# Page Sequencing — logic as implemented

What the code does today, with every number and the file that holds it. Part 1 is the
pipeline stage that suggests an order. Part 2 is how review-ui shows that order, reads the
ground truth, and scores the two against each other.

| | |
|---|---|
| Stage | `core-pipeline/stages/lib/sequencing/stage.py` (`page_sequencing`, pass 1, seq 95) |
| Engine | `core-pipeline/stages/lib/sequencing/detect.py` (`compute_sequence_assignments`) and `engine/` |
| Settings | `stages/lib/sequencing/config.py`; `SEQUENCING_CROSS_ENCODER` in `core-pipeline/config.py` (default **false**) |
| Output | `page_sequencing_results` (DB), `imaging/<chart>_sequencing.csv` |
| Ground truth | `page_ground_truth.page_sequence` (text: `3` or `J2`) |
| Review UI | `review-ui/frontend/src/ImagingPanel.tsx` (`SequencingTable`) |

Sequencing **suggests** an order. It never renames, moves or reorders a file.

---

## Part 1 — the pipeline stage

### 1.1 Summary

1. **Which pages take part.** Blank, junk and duplicate pages (the final blank/junk
   verdict) are set aside. So are near-blank pages (§1.3).
2. **Page markers.** Each page's printed page number ("Page 3 of 12" and similar) is found
   (§1.4).
3. **Streams.** If the markers show more than one paginated document mixed together, the
   chart is split into streams and each stream is ordered on its own (§1.5).
4. **Order within a stream.** Pages with a trusted marker swap places among themselves by
   marker number. Every other page keeps its scan position (§1.6).
5. **Cover pages last.** A fax cover or transmittal on scan page 1 moves to the end (§1.7).
6. **Renumber.** Positions are renumbered 1, 2, 3 … with no gaps (§1.8).

### 1.2 Input text

| | |
|---|---|
| Text | `best_page_text`: Final OCR 2 (Azure), else Final OCR 1 (Docling), else Tesseract. Tesseract is never used for handwritten, mixed or low-quality pages |
| Printed page number | `additional_page_details.printed_page_no` / `printed_page_total`, written by the key/value stage. When present it **replaces** every text pattern for that page |
| Set aside | Final blank/junk verdict in `blank`, `junk` or `duplicate` (`BJ_EXCLUDE`). These pages get no text and are marked `skipped` for this stage |

### 1.3 Near-blank pages

`page_heuristics.is_near_blank`. A page that passed blank/junk is still set aside here when:

| Condition |
|---|
| No text at all |
| ≤ **12** words **and** ≤ **80** characters |
| ≤ **3** words **and** < **40** characters |

A set-aside page gets `sequence_method = classified_page`, confidence **0.30**, review flag
on, and a position **after** every ordered page.

### 1.4 Page markers

`engine/marker_detector.py`. Each page's header is its first 5 non-empty lines and its
footer is its last 5.

**Patterns**, tried in this order (first match wins in each place):

| Pattern | Example | Confidence |
|---|---|---|
| `page_x_of_y` | Page 3 of 12 | 1.0 |
| `page_x_slash_y` | Page 3/12 | 1.0 |
| `pg_x_of_y` | Pg. 3 of 12 | 1.0 |
| `pg_x` | Pg 3 | 0.85 |
| `p_dot_x_of_y` | p. 3 of 12 | 1.0 |
| `x_of_y` | 3 of 12 | 0.92 |
| `x_slash_y` | 3/12 | 0.92 |
| `page_x` | Page 3 | 0.85 |
| `dash_x_dash` | - 3 - | 0.85 |
| `footer_plain_x` (footer only) | a line holding only `3` | 0.78 |

- **Where:** the footer and header are searched with every pattern. The body is searched
  only with the four "of / slash" patterns that say page, pg or p.
- **Bad numbers are dropped.** A page number of 0, or one larger than its total, is
  dropped. One exception: `Page 1003 of 12` is read as page 3. When the number is larger
  than the total, the last digits are tried.
- **The key/value stage's printed number wins.** When present, it is the page's only
  candidate. It scores 1.0 with a total and 0.85 without one.

**Choosing one marker per page:**

1. Markers that carry a total ("of N") are grouped into **families** by N.
2. The **dominant family** is the one with at least 2 pages and at least 2 distinct page
   numbers. Ties are broken by most pages, then most "page"-worded markers, then most
   distinct numbers.
   - The dominant family is trusted even when N is far larger than the upload, because a
     partial upload of a bigger record still orders correctly by those numbers.
3. A page outside that family keeps a **lone** marker only when all of these hold:
   - the marker literally says page, pg or p;
   - its total matches the dominant family's total, or there is no dominant family;
   - its total is ≤ max(2 × pages, pages + 20).
4. Bare numbers ("1/25", "1 of 3", footer digits) count **only** as part of the dominant
   family. On their own they are usually dates or doses.

**Conflicts** (`find_marker_conflicts`): when pages carry different totals, or two pages
claim the same number, those pages lose their marker for ordering and are flagged for review.

### 1.5 Streams — several documents in one chart

`engine/stream_detector.py`. This step runs only when pages carry markers.

1. Marked pages are grouped by total.
2. **Small groups are dropped.** When the largest group has ≥ **3** pages, a group of ≤ 2
   pages that is also smaller than a third of the largest is dropped.
3. **Too few markers.** If only 1–2 markers remain on a chart of ≥ **5** pages, all of
   them are dropped and the chart is one stream in scan order.
4. **Repeated page numbers.** Inside a group, two pages with the same page number are
   compared by a SHA-256 of their lowercased, space-normalised text (text of ≥ **30**
   characters only):
   - same hash → a **duplicate**: the later page is removed and placed at the end;
   - different text → the pages start separate **streams**. Each later page joins the
     stream whose last page is nearest in scan order.
5. A page with no marker is an **orphan**. Orphans join the stream when there is only one
   stream. Otherwise they are ordered separately after the streams.
6. Streams are numbered `split_001`, `split_002` … by their first scan page. Every stream
   after the first in a group is a **conflict** stream, and all its pages are flagged for
   review.

With several streams, the order is: the streams one after another, then the orphans, then
the duplicates, then the set-aside pages.

### 1.6 Order within a stream

`engine/sequence_builder.py` and `engine/order_merge.py`.

**The order itself** (`unified_page_order`):

- Marker pages are reordered **within the scan slots they already occupy**. The set of
  scan positions held by marked pages stays fixed, and those pages are placed into the
  slots by ascending marker number.
- Unmarked pages keep their scan position.
- Fewer than 2 marked pages leaves pure scan order.

> Marker numbers and scan positions are different scales: a partial upload can number
> 2–90, and per-note counters restart. That is why marked pages are never sorted against
> the scan positions of unmarked pages.

**Method and confidence** (written for every page; they do **not** change the order):

| `sequence_method` | When | Confidence | Review |
|---|---|---|---|
| `explicit_marker` | Trusted marker whose total = pages in the stream | marker's | no |
| `explicit_marker_reference` | Trusted marker, any other total | marker's, at most 0.90 | no |
| `cross_encoder` | Cross-encoder on (§1.9) and the page had no marker | the chain score; 0.60 when there is none | when score < **0.50** |
| `header_group` | Shares a header or footer with another unmarked page (Jaccard > **0.70**) | 0.90 when the group has a marker, else 0.75 | when in a marker conflict |
| `original_order` | Anything else | 0.30 | yes |
| `classified_page` | Set aside (§1.3) | 0.30 | yes |
| `duplicate_page` | Removed as a duplicate (§1.5) | 0.30 | yes |

### 1.7 Cover pages last

`engine/cover_order.py`, `page_heuristics.is_cover_page`. This applies only to scan
page **1**. Header, footer and text are read together.

| Condition | Cover? |
|---|---|
| Matches *cover (page/sheet)*, *fax cover*, *transmittal*, *confidentiality notice*, *patient / medical record from* | yes |
| Near-blank (§1.3) | no |
| < **80** words and *fax*, *cover* or *enclosure* appears, with < **120** words | yes |

A cover page moves to the end, and every page after it moves up one.

### 1.8 What is written

The final positions are renumbered 1, 2, 3 … in order.

| `page_sequencing_results` | |
|---|---|
| `original_page_number` | scan page number (review-ui's Original Sequence) |
| `seq` | the suggested position (review-ui's Algo. Sequence) |
| `sequence_method`, `confidence`, `review_flag` | §1.6 |

The CSV `imaging/<chart>_sequencing.csv` adds `stream_id` and `ocr_source` (`final2` /
`final1` / `prelim`).

### 1.9 The cross-encoder is off

`SEQUENCING_CROSS_ENCODER` defaults to **false**, and
`artifacts/cross_encoder_mini_lm.onnx` is not in the repo (only the tokenizer is).

When it is on and the model is present, the cross-encoder works like this:

1. It scores each unmarked page's last 15 lines against every other unmarked page's first
   15 lines, for pages within **12** scan pages of each other. There is a budget of
   **2,500** pairs per chart, and over budget it does nothing.
2. It builds a chain greedily:
   - the start page is the one least likely to be a continuation (a "cont." in the header
     counts against it);
   - each next page is the best-scoring one.

The chain is passed to the order merge but **not used by it**, so today it changes only
`sequence_method` and `confidence`, never the order. The same is true of header groups.

---

## Part 2 — review-ui: Algo. Sequence, ground truth, accuracy

The Sequencing tab in a chart shows S.No, File, Original Sequence, Algo. Sequence and
Ground Truth.

### 2.1 Two series: Main and J

Every page is in one of two series, each running 1, 2, 3 … on its own:

| Series | Pages | Written as |
|---|---|---|
| Main | content pages | `1`, `2`, `3` … |
| J | blank and junk pages | `J1`, `J2`, `J3` … |

A **complete (100%) duplicate** is in neither series.

### 2.2 Algo. Sequence (`algoSequences`)

| Page | Shown as |
|---|---|
| Complete duplicate (similarity 100%) | `Skipped` |
| Blank, junk, or a partial duplicate | `J1`, `J2` … in page order |
| Any other page | the stage's `seq` (§1.8) |
| Sequencing not run | Yet to Process |

### 2.3 Ground truth (`groundTruthSequences`)

`page_ground_truth.page_sequence` is read as text: `3.0` and `03` become `3`; `j 2` becomes
`J2`.

The client's sheet often numbers **every** page 1, 2, 3 …, junk included. Each value is
read like this:

1. A page is **J** when its value starts with `J`, or its ground truth has `blank_page` =
   Yes, or its ground truth has `junk_page` = Yes.
2. J pages are renumbered **J1, J2 …** by their stored number, with page number breaking
   ties.
3. Every other page with a number is renumbered **1, 2, 3 …** the same way. This closes
   the gaps the junk pages leave.
4. A value that is neither a number nor `J<n>` is shown as written.

A chart already saved in that form looks the same as it is stored.

> The loader (`core-pipeline/jobs/ground_truth_load.py`) also stores a blank / junk page's
> sequence as `J<n>` when the sheet says Blank, Junk **or Invoice**. It keeps the sheet's
> number and does not renumber. review-ui counts only `blank_page` and `junk_page`, so an
> invoice page loaded before that change, still holding a plain number, is counted as Main.

### 2.4 Marks on the Ground Truth column

Marks are shown only outside editing, on pages whose ground truth is `<n>` or `J<n>`.
Complete duplicates get no mark.

| Ground truth | Algo. | Mark |
|---|---|---|
| J | blank / junk (J) | ✓ |
| J | Main | ✗ Main in Algo. |
| Main | blank / junk (J) | ✗ Junk in Algo. |
| Main | Main, no break | ✓ |
| Main | Main, break | ✗ Break |
| Main | Main, no Algo. number | no mark |

**Breaks** (`sequenceBreaks`) are found among the pages that are Main on both sides and
have both numbers:

1. Rank those pages in ground-truth order.
2. Walk them in Algo. order.
3. A page **breaks** when its ground-truth rank is not one more than the previous page's.
   For the first page, it breaks when it is not rank 0.

A break marks where the Algo. order jumps, not every page that is out of place. Moving one
page elsewhere usually costs two breaks: one where it is taken from, one where it lands.
Swapping two neighbours costs three, as the example shows.

**Example.** The ground truth runs A B C D. The Algo. gives A C B D.

| Algo. order | Ground-truth rank | Expected | Mark |
|---|---|---|---|
| A | 0 | 0 | ✓ |
| C | 2 | 1 | ✗ Break |
| B | 1 | 3 | ✗ Break |
| D | 3 | 2 | ✗ Break |

That is 3 breaks out of 4 pages, so Accuracy = 25.0%.

### 2.5 Accuracy

```
Accuracy = 1 − breaks / pages
```

| | |
|---|---|
| breaks | every ✗: Main-page breaks plus junk mismatches in either direction |
| pages | every page with a mark (✓ or ✗) |

It is shown above the table, for example `Accuracy = 65.1% (15 breaks / 43 pages)`, and only
when at least one page is marked.

### 2.6 Editing the ground truth

The **Edit** button sits in the Ground Truth column header.

- **Starting values.** Edit starts from the values as shown (§2.3), not as stored.
- **Allowed values.** Each value must be a whole number from 1 or `J1`, `J2` …. A blank
  value clears the page.
- **Each series must be complete.** Main must run 1–n and J must run J1–Jn, with no gap
  and no repeat. Save stays disabled until they do.
- **Copy Original** fills the column from scan order: Main pages 1, 2 … and blank / junk
  pages J1, J2 …. Complete duplicates are left blank.
- **Copy Algo.** fills it from the Algo. Sequence. `Skipped` and Yet to Process keep the
  draft value.
- **What Save writes.** Save writes every page whose value differs from what is
  **stored**. A chart still stored as 1, 2, 3 across junk pages therefore shows a count on
  Save as soon as Edit opens. Saving rewrites it into the J form.
- **Audit.** Each write is a `manual_entry` with the user who saved it.

---

## Known gaps

- **Only markers move pages.** Header groups and the cross-encoder change method and
  confidence, not the order (§1.6, §1.9). A chart without printed page numbers comes back
  in scan order, apart from a page-1 cover.
- **Near-blank pages are Main in review-ui.** A page the stage sets aside as near-blank
  (§1.3), but that blank/junk did not mark, is placed at the end with a high `seq`.
  review-ui shows it as Main, so it can show as a break.
- **Invoices.** The loader treats Invoice = Yes as junk; review-ui does not (§2.3).
