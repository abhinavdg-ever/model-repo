# Document continuity — break scores

Which document each page belongs to, and the values a document's pages share.

> **Status (2026-10-19): design agreed, engine not yet rebuilt.** This file describes the
> break-score method the user asked for on 2026-10-19. The code still runs the
> gate + weighted ladder of 2026-10-18 (summarised in §13 so it can be compared). The
> weights below keep the fitted values of that revamp, sign-flipped so that **positive
> means break**; signals marked *new* are estimated and must be fitted before trusting.
> Background and measurement: [`CONTINUITY_ENCOUNTER_REVAMP.md`](CONTINUITY_ENCOUNTER_REVAMP.md).

| | |
|---|---|
| Engine | `core-pipeline/stages/lib/continuity/engine.py` |
| Signals | `core-pipeline/stages/lib/continuity/signals.py`, `layout.py`, `signature.py` |
| Runs | at four checkpoints (§6): blank_junk pass 1 (seq 30), blank_junk pass 2 (60), page_subtype (75), `continuity` (87, final) |
| Settings | `stages/lib/keyword-canon/continuity_canon.json` (reloads on change) |
| Writes | `page_break_scores` (every checkpoint), `page_continuity_results` (final), `imaging/<chart>_continuity.csv` |
| Final values | written by `imaging_final` (seq 100) |
| Shown | review-ui: Break Before / Break After, the tag, and the basis of each score |

---

## 1. The idea

Every page gets two scores:

- **Break Before:** how strongly the evidence says a new document starts *at* this page.
- **Break After:** how strongly the evidence says the document *ends* after this page.

The tag (**New / Continue / Last / Standalone**) is worked out from the two scores. It is
never stored as a fact on its own. Break After of page *i* and Break Before of page *i+1*
describe the same gap, so they are the **same number**, shown from both pages. Change the
evidence on one gap and the tags on both sides follow; they cannot disagree.

Nothing is final until the last checkpoint. Each checkpoint **recomputes** every score
from all the evidence available at that moment (never adjusting the previous result), so a
re-run gives the same answer and a later stage can undo an earlier break.

Getting it wrong is asymmetric: merging two encounters is a clinical error that reaches
coding; splitting one is a nuisance a reviewer catches. Unclear gaps are **flagged**, not
guessed.

---

## 2. The gap score

```
break(i → i+1) = Σ weight × value        every value in [0, 1]; positive = break

  ending evidence   read on page i alone      (how page i ends)
+ opening evidence  read on page i+1 alone    (how page i+1 starts)
+ pair evidence     comparing the two pages   (do they belong together)
```

| Gap score | Gap state |
|---|---|
| ≥ **+4** | **break** |
| ≤ **−4** | **joined** |
| between | **unclear**: treated as joined (the page stays in the open document, the recoverable error) and flagged `review_required` |

Never widen the band to lower the unclear rate: those are the gaps most likely to be
decided wrongly.

Two fixed ends: the chart's first page has Break Before = **break** and its last page has
Break After = **break**. Nothing else is hard: DOS and printed page numbers are heavy
weights, not gates, because OCR misreads "2 of 3" and DOS extraction makes mistakes.

### 2.1 Ending evidence (page i, part of its Break After)

| Signal | Weight | Value | Status |
|---|---|---|---|
| `page_fill` | −5.1 | how far down the body text reaches (§4.2); a full page pulls toward joined, an empty bottom adds nothing | fitted |
| `bottom_ink` | −3.0 | C1 only, before line positions: the same measure from the image's last ink row (§4.2) | *new* |
| `prev_signed` | +4.4 | the page is signed anywhere | fitted |
| `signed_in_tail` | +8.0 | a signature block in the last third of the lines (replaces the old "signature terminator" override) | *new* |
| `page_n_of_n` | +8.0 | prints "page N of N": the last page | *new* |
| `continued_marker` | −6.0 | ends with "continued", "cont'd", "(over)", "see next page" | *new* |
| `cut_mid_sentence` | −2.0 | the last body line ends without terminal punctuation, mid-word, or in a hyphen | *new* |

### 2.2 Opening evidence (page i+1, part of its Break Before)

| Signal | Weight | Value | Status |
|---|---|---|---|
| `cur_has_title` | +6.0 | the page carries a title for its page type | estimated |
| `page_one` | +6.0 | prints "page 1 of M" | *new* |
| `cur_opens_note` | −0.5 | opens on Chief Complaint / HPI (fitted small and joining; kept as measured) | fitted |
| `starts_mid_sentence` | −2.0 | the first body line starts in lowercase or mid-list | *new* |

### 2.3 Pair evidence

| Signal | Weight | Value | Status |
|---|---|---|---|
| `different_dos` | **+15.0** | both pages carry their own DOS and From or To differs | *new* (was gate 4) |
| `same_dos` | **−10.0** | this page's own DOS equals the open document's | *new* (was gate 3) |
| `pagination_follows` | **−10.0** | "3 of 5" → "4 of 5" | *new* (was gate 5) |
| `section_forward` | −10.0 | sections move forward in note order (§4.3) | fitted |
| `page_type_change` | +7.0 | (1 − same-type probability) × confidence in the new type (§4.4) | estimated |
| `same_visit_number` | −5.4 | the same visit / encounter / FIN / CSN number on both | fitted |
| `header_line_repeats` | −4.7 | a header line (≥ 12 characters) printed on both | fitted |
| `body_cosine_char` | −3.7 | character tf-idf similarity, tail → head (§4.1) | fitted |
| `full_path_same` | −3.0 | the same page type at every level | estimated |
| `footer_line_repeats` | −2.8 | a footer line printed on both | fitted |
| `body_cosine_word` | −1.0 | word tf-idf similarity | fitted |
| `kind_change` | +8.0 | one page is Main and the other Junk; value = the junk verdict's confidence (§3) | *new* |
| `same_junk_group` | −4.0 | both Junk with the same junk group (invoice → invoice) | *new* |
| `blank_between` | +1.0 | one or more blank pages sit in the gap (§3.2) | *new* |

`different_dos` (+15) outweighs `pagination_follows` (−10) as before: a different date is
a different encounter whatever the footer says (+5 → break). Same DOS plus page numbers
following on is −20, a very strong join.

`fitted` = measured on 170 labelled pairs from 15 charts (balanced accuracy 0.79 ± 0.19,
split by chart); the `measurement` block in the canon carries the provenance.

### 2.4 The basis

Every score is stored with its parts, so a reviewer sees *why*:

```
Break Before  +11.7   break    page_type_change +5.7, cur_has_title +6.0, page_fill(prev) −0.0
Break After    −0.9   unclear  page_fill −5.1, page_type_change +4.2  → review
```

The top four contributions by size go into `evidence`; all of them are in the JSON.

---

## 3. Main, junk and blank pages

### 3.1 Junk pages form their own segments

Junk pages are no longer dropped from continuity. They are scored with the same method and
grouped into **junk segments** that get the same four tags: a three-page records request is
`New (Junk J1)`, `Continue (Junk J1)`, `Last (Junk J1)`.

- The step between Main and Junk is `kind_change` (+8 × junk confidence): a confident junk
  page after a note breaks, but a weakly called one does not cut a note in half.
- If blank_junk pass 2 reverses a junk call, the next checkpoint re-scores the gap and the
  note joins up again.
- DOS signals are not used between junk pages.

### 3.2 Blank pages are compared across

Duplex scans put blank backs inside documents. A blank page is **transparent**: the gap is
scored between the page before it and the page after it, plus `blank_between` (+1.0).

- The blank page has no Break Before / After of its own and no tag.
- It is shown **inside** the document when the gap across it is joined ("Blank, in Doc 3"),
  and **between** documents when it is a break.

### 3.3 Duplicates

As before: a duplicate takes its original's document, tag and values, and adds no gap.

---

## 4. The signals in detail

### 4.1 Text running from one page into the next

1. **Strip the furniture**: lines of ≥ 12 characters printed on both pages (patient
   banner, footer template) are removed; they are what `header_line_repeats` /
   `footer_line_repeats` score.
2. **Tail and head blocks**: the previous page's last body lines, walking back in groups of
   three lines until 20 words; the next page's first body lines, forward, likewise.
3. **TF-IDF cosine** on those blocks: character 3–5 grams within words, word 1–2 grams;
   `min_df` 2, sublinear tf; fitted on every block of the chart. Normalised
   `min(1, cosine / 0.25)` for characters, `/ 0.15` for words.

### 4.2 Page ending: fill and bottom space

Where the body ends: the bottom of the last line **starting above the footer band**,
divided by the footer top (0.92), clipped to 1. Bottom space = 1 − fill.

| Fill | Value |
|---|---|
| ≤ 0.60 | 0: the text stopped early, no pull toward joined |
| 0.60 – 0.92 | graded: (fill − 0.60) / 0.32 |
| ≥ 0.92 | 1: the text runs on |

Sources, best first:

| Checkpoint | Source |
|---|---|
| C2+ | Final 2 line polygons, else Final 1 words grouped into lines |
| C1 | `bottom_ink`: the corrected page image's last row with ink above the footer band (works on handwriting, needs no OCR boxes) |
| none | text proxy: 1 when ≥ 28 lines or ≥ 1,800 characters, else 0; flagged when it swings a gap |

Bottom space only counts together with how the next page opens: an empty bottom followed
by a titled page is a break; an empty bottom followed by a page continuing the same
sections (the short last page of a long note is the common case) stays joined.

### 4.3 Section order

Note order (`section_order` in the canon): chief complaint → HPI → review of systems →
past history → social → family → medications → allergies → vitals → physical exam → labs →
imaging → problem list → assessment / diagnosis → plan → patient instructions → follow-up.

`section_forward = clip((first section of this page − last section of the previous) / steps × 4, 0, 1)`.
Moving backwards counts 0.

### 4.4 Page type change

| Previous | This page | Confidence | Same-type probability | Value | × +7 |
|---|---|---|---|---|---|
| Progress Note | Lab | 90% | 10% | 0.81 | +5.7 |
| Progress Note | Lab | 60% | 40% | 0.36 | +2.5 |
| Progress Note | Progress Note | 95% | 95% | 0.05 | +0.3 |

---

## 5. The tag

Each side of a page is **break** or **not** (joined or unclear):

| Break Before | Break After | Tag |
|---|---|---|
| break | not | **New** |
| not | not | **Continue** |
| not | break | **Last** |
| break | break | **Standalone** |

Shown as `New (Doc 3)`, `Continue (Junk J1)`, and so on. A page with an unclear side carries
`review_required` and a "?" in review-ui (`Continue? (Doc 3)`).

Documents are numbered in page order: `Doc n` for Main segments, `Junk Jn` for junk
segments.

| Field | Meaning |
|---|---|
| `start_confirmed` | the New page has a title or prints "page 1" |
| `link_strength` | `strong` while every gap back to the New page is ≤ −10, else `weak` |

---

## 6. Checkpoints: nothing is final early

| # | Runs at | Evidence it adds | Writes |
|---|---|---|---|
| **C1** | blank_junk pass 1 (seq 30) | Tesseract text: titles, "N of M", header/footer repeats, signature phrases, cosines; `bottom_ink`; pass-1 verdicts (Main / Junk / Blank) | `page_break_scores` |
| **C2** | blank_junk pass 2 (60) | Final 1 / Final 2 line positions (exact `page_fill`, cut / mid-sentence), section headers, KV page numbers, pass-2 verdicts | `page_break_scores` |
| **C3** | page_subtype (75) | page type, confidence, full path, canon title | `page_break_scores` |
| **Final** | `continuity` (87) | Extracted DOS, signature flags; tags fixed, Final values carried | `page_break_scores` + `page_continuity_results` |

- **Every checkpoint recomputes** from scratch with the signals present; a missing signal
  is left out, not counted as 0 (`evidence` records which were available).
- **Each checkpoint keeps its own row**, so review-ui can show how a page's scores moved
  (C1 −2.1 → C2 +6.3 → Final +6.3: "the line positions showed the text stopped at 40%").
- Only the Final checkpoint feeds Final values and `imaging_final`. Earlier checkpoints are
  provisional; no stage reads them as decisions today.

---

## 7. Review flags (`review_required`)

| Flag | When |
|---|---|
| Unclear | Break Before or Break After fell in the band (§2) |
| Long run without page numbers | a document of more than **25** pages where no page prints a number — every page of it |
| Text-proxy fill | page fill came from the text proxy and moved the gap across a threshold |
| Flip at Final | the Final tag differs from C3's and the deciding signal is weak (\|contribution\| < 4) |

---

## 8. What a document shares: the Final values

Unchanged (`document_finals`), applied to Main documents only:

| Final value | Rule |
|---|---|
| Page type | the New page's (each page keeps its own sub-type) |
| DOS | the New page's, on every page |
| DOS of a stand-alone non-visit document | every page a `default_final_dos` type → **2222-02-02**; such a page inside a visit takes the visit's DOS |
| Encounter type | the New page's |

Junk segments carry no Final values; each junk page keeps its own junk subtype.

---

## 9. Encounter type and the label

### 9.1 Visits come from the Final segments

The encounter stage (seq 90) runs after the Final checkpoint and reads only
`page_continuity_results`, never an earlier checkpoint.

| Segment | Encounter type |
|---|---|
| **Main document** (`Doc n`) | one visit: the tier evidence (LOGIC.md §8) is gathered over every page from its New page to its Last page, and the answer is stamped on all of them |
| **Junk segment** (`Junk Jn`) | none: junk is never a visit; `encounter_type` stays empty, `reason = junk` |
| **Blank page in a document** | takes its document's answer in `imaging_final` (it is not read for evidence) |
| **Blank page between documents** | none |
| **Duplicate** | its original's, as before |

The tiers, negatives and confidences are unchanged. The only change is the unit: a visit
is a Main segment as the break scores drew it, and a junk page can no longer be pulled into
a visit or start one.

### 9.2 When the document itself is unsure

Encounter type inherits the document's doubt instead of hiding it:

| Situation | Effect |
|---|---|
| A gap inside the document is **unclear** (§2) and the pages on each side of it would answer **different settings** on their own | `conflict = y`, `review_required`, `reason = unclear_break`; the visit's answer is kept (the joined reading) and the alternative is in the evidence |
| The document's `link_strength` is `weak` and the answer came from tier 3 tie-breaking only | confidence drops one bucket (0.70 → 0.60) |
| `continue_applied = y` on a page whose Break Before is unclear | the page is listed in the visit's evidence as "joined across an unclear break" |

So a reviewer who sees `Continue? (Doc 3)` also sees whether that doubt would change the
encounter type.

### 9.3 The label

One label vocabulary from the tag, used by `page_continuity_results`, the CSVs, `imaging_final`
and review-ui:

| Tag | Main | Junk | Unclear side |
|---|---|---|---|
| new | `New (Doc 3)` | `New (Junk J1)` | `New? (Doc 3)` |
| continue | `Continue (Doc 3)` | `Continue (Junk J1)` | `Continue? (Doc 3)` |
| last | `Last (Doc 3)` | `Last (Junk J1)` | `Last? (Doc 3)` |
| standalone | `Standalone (Doc 3)` | `Standalone (Junk J1)` | `Standalone? (Doc 3)` |
| blank | `Blank (in Doc 3)` / `Blank` | — | — |

Replaces `First / Continue / Last / Single (Doc n)` (`position_label` in `engine.py`).

`encounter_label` stays what it is, the setting's display name from `encounter_canon.json`
(`Outpatient (F2F)`, `Outpatient (Tele)`, `Inpatient`, `Home`). review-ui shows the two side by
side: `New (Doc 3) · Outpatient (F2F)`. Junk pages show only their segment label.

---

## 10. Storage

New table (add, never drop):

```sql
CREATE TABLE page_break_scores (
    id               BIGSERIAL PRIMARY KEY,
    chart_id         BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id          BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    stage_name       VARCHAR(50) NOT NULL,      -- the checkpoint
    pass_no          SMALLINT    NOT NULL,
    break_before     NUMERIC(6,2),               -- NULL on blank / duplicate pages
    break_after      NUMERIC(6,2),
    tag              VARCHAR(12) CHECK (tag IS NULL OR tag IN ('new','continue','last','standalone')),
    segment_kind     VARCHAR(10) CHECK (segment_kind IS NULL OR segment_kind IN ('main','junk')),
    segment_seq      INT,                        -- Doc n / Junk Jn
    seq              INT,                        -- position within the segment
    review_required  BOOLEAN NOT NULL DEFAULT FALSE,
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"before": {signal: contribution}, "after": {...}, "available": [...]}
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, stage_name, pass_no)
);
```

`page_continuity_results` gains `break_before`, `break_after`, `tag` and `segment_kind`. The
existing `position` / `relation` / `decided_by` columns stay and are still written
(`position`: new → first, standalone → single; `decided_by` = the largest contribution).
Readers use the new columns when present.

---

## 11. Settings (`continuity_canon.json`)

| Block | Holds |
|---|---|
| `weights` | every signal in §2.1–2.3 with `side` (ending / opening / pair), `w`, `lift`, `coverage`, `status` |
| `decision` | `break_at` +4.0, `join_at` −4.0, `strong_join` −10.0 |
| `pagination`, `bands`, `semantic`, `page_fill`, `section_order`, `default_final_dos` | as before |
| `bottom_ink` | the ink threshold and footer exclusion for C1 |
| `markers` | `continued_marker` and closing phrases |
| `review_triggers` | unclear, long run 25, text-proxy fill, flip at Final |
| `measurement` | provenance of the weights |

The loader refuses a file whose `weights` name a signal the engine does not produce, or
whose band is inverted (`join_at ≥ break_at`).

---

## 12. Worked example

| Page | Kind | Evidence | Break Before | Break After | Tag |
|---|---|---|---|---|---|
| 1 | Main | DOS 2024-03-28, title | (chart start) | −10.0 page 1 → 2 of 3 | New (Doc 1) |
| 2 | Main | "page 2 of 3" | −10.0 | −22.6 banner + footer repeat, full page, HPI → Plan | Continue (Doc 1) |
| 3 | Main | ends "Electronically signed by …" | −22.6 | **+12.4** signed in tail, signed | Last (Doc 1) |
| 4 | Main | Lab title, text stops at 40% | +12.4 | **+11.7** title on 5, page type change | Standalone (Doc 2) |
| 5 | Main | Lab after Lab, full page | +11.7 | −0.9 unclear | Continue? (Doc 3), review |
| — | Blank | no ink | — | — | Blank, in Doc 3 |
| 6 | Main | across the blank: same sections, +1 blank | −0.9 | +8.8 (Junk next, 0.96) | Last (Doc 3) |
| 7 | Junk | record request, conf 0.96 | +8.8 | −4.7 same junk group, footer repeat | New (Junk J1) |
| 8 | Junk | record request | −4.7 | +15.0 DOS 2024-06-03 on 9, kind change | Last (Junk J1) |
| 9 | Main | DOS 2024-06-03 | +15.0 | (chart end) | Standalone (Doc 4) |

---

## 13. What it replaces (the 2026-10-18 ladder)

| Then | Now |
|---|---|
| Gates decided in order (first page, blank/junk, same DOS, different DOS, pagination, page N of N) | the same facts as heavy weights; only the chart's first and last page are fixed |
| Score positive = continue | positive = break; same fitted weights, sign flipped |
| One score per page (the gap before it) | Break Before and Break After per page; the tag comes from both |
| Signature terminator override | `signed_in_tail` +8 |
| A blank or junk page always ended the document; junk had no row | blank is transparent; junk forms segments; `kind_change` weighs the junk confidence |
| Ran once at seq 87 | recomputed at four checkpoints, each kept |
| `single / first / continue / last` | `standalone / new / continue / last` (old column still written) |
| Encounter visit = a continuity document; blank / junk pages had none | visit = a Main segment; junk segments never get one; a blank page inside a document takes its answer; an unclear gap with different settings either side flags the visit (§9) |
| Label `First / Continue / Last / Single (Doc n)` | `New / Continue / Last / Standalone (Doc n)`, `(Junk Jn)`, `?` on an unclear side (§9.3) |
