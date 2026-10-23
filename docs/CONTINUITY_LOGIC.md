# Document continuity — where one document ends and the next begins

What the code does, with every number and the file that holds it. The method and its
reasoning come from *How we decide where one document ends and the next begins*
(Document_Splitting.md, 2026-10-20); this file describes it as built. Paths are relative
to `core-pipeline/`.

| | |
|---|---|
| Engine | `stages/lib/continuity/engine.py` (`assign`, `score_gap`, `_final_pass`) |
| Measurements | `stages/lib/continuity/signals.py` (furniture, tail / head blocks, TF-IDF, the curves) |
| Stage | `stages/lib/continuity/stage.py` — `continuity`, seq 87, after `page_subtype` and `dos_extract`, before `imaging_final` |
| Settings | `stages/lib/keyword-canon/continuity_canon.json`, version 4 (reloads on change) |
| Inputs | `staging/extraction.json` first (§3), then the DB (DOS, page type, blank / junk) and the OCR JSON |
| Writes | `page_continuity_results` (the original columns plus `confidence`, one row per content or junk page), `<chart>/staging/continuity.json` (version 2: documents, then per page both gaps in plain words and the Final values; every score in `continuity_detail.json`), `imaging/<chart>_continuity.csv` (every page) |
| Database | one new column, `confidence` (0–1); on an existing database `schema/changes_2026-10-19.sql` (removed; `git show 173a6cb:schema/changes_2026-10-19.sql`) §8 widens `decided_by` and §9 adds `confidence` |

---

## 1. Why it matters

The pages of a chart arrive in order with nothing saying where one document stops.
Four later decisions depend on the grouping:

- **Date of service.** A labelled date is on about 18% of pages; the rest inherit their
  document's.
- **Page type.** A lab table inside a progress note is part of the note.
- **Codability.** The same questionnaire is codeable inside a note and not on its own.
- **Encounter type.** Decided once per document (`encounter` stage, seq 90).

**The two mistakes are not equal.** Joining two documents merges two visits, and
nothing downstream can see it. Splitting one leaves a fragment a reviewer notices. So
**when the evidence is balanced, the gap joins**, and a later pass can still split.

---

## 2. Gaps, not pages

Between any two consecutive pages there is a **gap**, and the gap is what is decided.
It gets one **break score**: a plain sum of points, positive toward a break, negative
toward joined.

- **Break Before** of a page = the score of the gap before it.
- **Break After** of a page = the score of the gap after it.
- Break After of page 4 and Break Before of page 5 are **the same number**, so the two
  pages can never disagree.
- The first page of the chart always starts a document; the last always ends one.

Every gap draws on three kinds of evidence, kept as separate **sub-scores**:

| Kind | Read from | Example |
|---|---|---|
| **ending** | the earlier page alone | it finishes with a signature |
| **opening** | the later page alone | it carries a title |
| **pair** | the two compared | the same header line on both |

---

## 3. Where each fact comes from: `extraction.json` first

The key/value extraction runs once after OCR and stages what it found per page in
`<chart>/staging/extraction.json`. Continuity reads it first (`stage.py`):

| Fact | From the staging | Fallback |
|---|---|---|
| **How the date was found** (DOS trust) | the tier of the staged `dos` row that read the page's Extracted DOS (best tier wins) | `dos_trust.default` (3) |
| **Printed page number** | the selected `page_no` row (`page_no`, `page_total`) | `additional_page_details`, then "page N of M" in the header / footer bands |
| **Signed anywhere** | any accepted `electronic_signature` row | `provider_signature.csv` `signature_present = y`, or a signature phrase in the text |
| **Signed in the closing lines** | an accepted `electronic_signature` row in the `footer` region | a signature phrase in the page's last third of lines |
| **Visit number** | accepted `member_id` rows whose key is an encounter-type ID (FIN, CSN, account, visit — `member_id/id_types.json`) | `Visit / Encounter / FIN / CSN / Account #` in the text |
| **Section headings** | accepted `heading_heron` rows of kind `section` | the OCR JSON's matched `section_headers` (both are used) |

The rest comes from the database and the OCR files:

| Fact | From |
|---|---|
| Date of service | `dos_extraction_results` (the DOS stage's Extracted DOS) |
| Page type, full path, confidence, title | `page_classification` (axes: family = L1, names = the full path; `axes.title_of`) |
| Blank / junk / duplicate, junk subtype and confidence | `v_page_blank_junk_final` |
| Line positions | Final 2 JSON line polygons, else Final 1 words grouped into lines, else text lines |

Header band: a line whose top is above **10%** of the page; footer band: bottom below
**92%** (`bands`).

---

## 4. Every signal and its points

Points are **1, 2, 3 or 5** only (`points` in the canon): 5 decisive, 3 strong,
2 moderate, 1 weak. + pushes toward break, − toward joined.

### 4.1 Decisive — 5

| Signal | Points | When |
|---|---|---|
| `different_dos` | **+5 / +3 / +2** | the page's date differs from the open document's date |
| `same_dos` | **−5 / −3 / −2** | the page's date equals the open document's |
| `pagination_follows` | **−5** | "page 3 of 5" followed by "page 4 of 5" |

**Date points depend on how the date was found** (`dos_trust`); the lower of the two
dates' trust counts:

| DOS tier | Points |
|---|---|
| `service`, `admit`, `discharge` (a proper label) | **5**, decisive |
| `weak`, `received`, `printed`, `collected` (a keyword near the edge; lab collection dates kept off the decisive tier) | 3 |
| `keyless` | 2 |
| not explained by the staging | 3 |

The open document's date is its first dated content page. A junk segment owns no
date, so the last document's date stays open across it.

**Page numbers only ever join.** "Page 1 of 6" is not used to split: OCR misreads a 7
as a 1, and a wrongly split document is very hard to find later.

### 4.2 Strong — 3

| Signal | Side | Points | Measured |
|---|---|---|---|
| `signed_in_tail` | ending | +3 | difference 21 |
| `signed_anywhere` | ending | +2 | difference 26 (fires with the above: a signed closing page totals +5) |
| `last_numbered_page` | ending | +3 | "page N of N" |
| `has_title` | opening | +3 | **assumed — check first**: section headings taken for titles |
| `page_type_change` | pair | +3 × (1 − same-type probability) × confidence | assumed (§5.2) |
| `junk_run` | pair | +3 (a run of 2) / +5 (3 or more) × junk confidence | assumed (§6) |
| `says_continued` | ending | −3 | "continued", "cont'd", "(over)", "see next page" at the end |
| `sections_forward` | pair | −3 × how far forward | difference 27, no false positives |
| `same_visit_number` | pair | −3 | difference 8, no false positives |

### 4.3 Moderate — 2

| Signal | Side | Points | Measured |
|---|---|---|---|
| `page_fill` | ending | −2 .. +2, the curve in §5.1 | join side difference 34, the strongest measured |
| `text_start` | opening | −1 .. +2, §5.4 | **unmeasured: recorded only** |
| `same_header_line` | pair | −2 | difference 31 (normalised matching should beat it) |
| `wording_char` | pair | −2 × similarity | difference 27, after removing furniture (§5.3) |
| `same_junk_kind` | pair | −2 | both junk, the same subtype |

### 4.4 Weak — 1

| Signal | Side | Points | Note |
|---|---|---|---|
| `blank_between` | pair | +1 | a blank page in the gap |
| `same_footer_line` | pair | −1 | difference 8: one footer template is printed across unrelated documents |
| `wording_word` | pair | −1 × similarity | not independent of `wording_char`, so it carries less |
| `cut_mid_sentence` | ending | −1 | only when `wording_char` fires: worthless alone (difference 3) |
| `starts_mid_thought` | opening | −1 | only when `wording_char` fires (difference 5 alone) |
| `same_page_type` | pair | −1 | the full type path, not just "both Progress Notes" |

---

## 5. The scaled signals

### 5.1 Where the text ends (`page_fill`)

From the bottom of the earlier page's last body line (lines starting in the footer band
do not count), as a fraction of the page height:

| Text ends at | Value | Reading |
|---|---|---|
| ≥ 92% | **−2** | the page ran out of room: continues |
| 80% – 92% | −2 .. 0 | tapering off |
| 65% – 80% | 0 .. +2 | rising into the band |
| 50% – 65% | **+2** | finished, with room for a signature |
| 35% – 50% | +2 .. 0 | falling away |
| < 35% | **0** | too little on the page to judge (a spillover tail or a failed print) |

The peak is worth only +2, so a form page ending in the band can be outvoted. With a
signature in the closing lines it adds up to near-decisive, which is right.

**Without line positions** a rough proxy is used: −2 when the page has ≥ 28 lines or
≥ 1,800 characters, else nothing. The positive band cannot be read this way. When the
proxy decides a gap, the gap is flagged.

### 5.2 How much the page type changed (`page_type_change`)

`(1 − same-type probability) × the classifier's confidence × 3`. The same-type
probability is the confidence when the two families agree, one minus it when they
differ. Below 0.05 it is not counted.

| Previous | This page | Confidence | Points |
|---|---|---|---|
| Progress Note | Lab | 90% | +2.4 |
| Progress Note | Lab | 60% | +1.1 |
| Progress Note | Progress Note | 95% | +0.1 |

### 5.3 Wording across the gap (`wording_char`, `wording_word`)

1. **Remove the furniture.** Any line of ≥ 12 characters printed on both pages
   (compared normalised, §7) is stripped: banner, letterhead, footer template. Without
   this the signal measures nothing (difference 1). The removed lines are what
   `same_header_line` and `same_footer_line` score instead.
2. **Take the ends by line.** The earlier page's **last two body lines** by position,
   extended back until 20 words; the later page's first two, forward.
3. **Compare.** Character 3–5 grams (survives OCR damage) and word 1–2 grams, TF-IDF
   fitted on every block of the chart, so words every page shares are discounted.
   Similarity = cosine / 0.25 (characters) or / 0.15 (words), clipped to 1; below 0.05
   it is not counted.

### 5.4 Where the next page's text begins (`text_start`)

How far below the header band's lines the later page's first body line starts:
≤ 3% → −1, 3–12% → −1 .. 0, 12–25% → 0 .. +2, > 25% → +2. **Unmeasured, so it is
computed and stored (`recorded.text_start`) but not added to the score**, until
`text_start.apply` is set after a run has measured it.

---

## 6. Blank pages, junk and duplicates

| Kind | Treatment |
|---|---|
| **Blank** | Transparent. The gap is scored between the page before and the page after, plus `blank_between` +1. The blank page has no scores of its own and is shown as **"Blank (in Doc 3)"** when that gap joined, **"Blank"** between documents. |
| **Single junk page** | Transparent and adds nothing: the gap is scored between the content pages either side. It belongs to the document before it (an invoice stapled to the back of a note), keeps its own junk page type, and gets no document number for Final values. |
| **Run of two or more junk pages** | Scored like content and broken off at both ends by `junk_run`: +3 for two pages, +5 for three or more, × the run's mean junk confidence. Three or more with a scaled value ≥ **4.5** is decisive. The run becomes its own **junk group**, numbered with the documents: **"New (Junk 4)" … "Last (Junk 4)"**. Inside it, `same_junk_kind` −2 holds it together. Blanks and duplicates between junk pages do not break a run. A junk group is no visit: its **DOS and encounter type read "Skipped"**. |
| **Duplicate** | Takes its original's document and adds no gap. |

---

## 7. Header and footer lines are matched normalised

Every run of digits and every date is replaced by a placeholder before an **exact**
comparison, so a page number or a print time cannot make an identical banner differ:

```
raw         Smith, John | MRN 004812 | Printed 03/28/24 | Page 3 of 7
normalised  smith, john | mrn # | printed <date> | page # of #
```

Fuzzy matching was measured and is noise (difference −4 for headers, 4 for footers), so
the match stays exact.

---

## 8. The decision

### 8.1 Every gap is decided

| Score | Decision |
|---|---|
| > 0 | **break** |
| < 0 | **joined** |
| = 0 | **joined, flagged** (`decided_by tie`) |

There is no "don't know": every page needs a document to inherit its date, type and
encounter from.

### 8.2 Decisive signals

- **A 5-point signal settles the gap** when no other 5 opposes it: the gap takes its
  sign whatever the rest says (`decided_by decisive`, confidence high). The other
  signals are still computed and stored.
- **Two decisive signals disagree** (page numbers run on, but the dates differ):
  **the different date wins** and the gap breaks, at medium confidence, flagged
  (`decisive_conflict`). A date is a fact about the care; a page number is a fact about
  the printing.
- **Forced by rule** (2026-10-23, canon `continuity_canon.json`), after the score:
  - a **Demographics** page beside another kind of page: always a break (`demographics_break`);
  - a **visit heading** (Reason for Visit / Appointment, Chief Complaint, HPI…) in the page's top band: always a break, a new Progress Note — unless the printed page N of M runs on (`visit_headings.decisive`);
  - a **Visual** page that is not junk: always joins the page before it (`visual_join`).

### 8.3 Confidence

| Field | How |
|---|---|
| `confidence_level` | `|score|` ≥ 5 high, 2–4 medium, 0–1 low; decisive high; decisive conflict medium |
| `confidence` (0–1) | `P(break) = 1 / (1 + exp(−(score − b) / a))`, confidence = `|2P − 1|`; × **0.6** when the three sub-scores point opposite ways; at least 0.9 for a decisive gap. **a = 2, b = 0 until fitted** on the labelled gaps |

A low-confidence side of a page adds a **"?"** to its label: `Continue? (Doc 3)`.

### 8.4 When the sub-scores disagree sharply

Two patterns, at ±4 (`conflict_rules.at`):

| Pattern | Sub-scores | Usual cause | Right answer |
|---|---|---|---|
| **A** finished, but still matching | ending ≥ +4 and pair ≤ −4 | a signed note followed by an addendum from the same visit | break |
| **B** new-looking page, continuing content | opening ≥ +4 and pair ≤ −4 | a section heading taken for a title | join; record it as a title misfire |

Either one **drops the confidence one level and flags the gap**. The answer itself is
changed only when `conflict_rules.apply` is true. It is **false** for now, because the
document says to count how often each pattern appears on a run before wiring them in.
The pattern is stored as `recorded.conflict`.

### 8.5 What is flagged (`review_required`)

- low confidence (`confidence < 0.25`, or level low);
- a score of exactly 0;
- two decisive signals disagreeing;
- conflict pattern A or B;
- the page-fill proxy decided the gap;
- a final-pass split;
- every page of a document longer than **25** pages with no printed page number
  anywhere (a footer template binding unrelated documents).

---

## 9. The final pass: whole documents

After every gap is decided, one more pass looks at whole documents (`_final_pass`).

**Split first.** A document holding two or more **different dates read from a label**
(trust 5) splits at each date change (`final_pass: split: two dates in one document`,
flagged). This catches a document that spans two visits while every single gap looked
like a join.

**Then merge** neighbours, but only across a **weak** break: confidence low or score
≤ 2, never a decisive break, and never between two documents with different dates.
These run up to **3** times, because one merge can reveal another:

| Rule | Merge two neighbouring documents when |
|---|---|
| `same_date` | both are dated and the dates are equal |
| `continued_across` | the first one's last page ends "continued" |
| `same_type_untitled` | both have the same page type (full path) and neither starts with a title |
| `orphan_page` | a one-page undated document sits between two documents of the same type, both breaks weak: it joins the one after |
| `junk_rejoin` | two junk groups of the same subtype |

**Last, a Progress Note runs on** (`progress_note_runs_on`). A document whose first page is
a Progress Note (`final_pass.progress_note_families`) keeps taking the documents after it
until one of these ends it:

- a **new date read from a label** (trust 5) on the next document's first page;
- a **signature** on the note's last page (anywhere on it, or in its closing lines);
- a **decisive break** (a different labelled date, a run of three or more junk pages);
- a **junk group**.

So a lab table, a questionnaire or an untitled page after a note stays in the note. Unlike
the merges above, this one also crosses a strong break such as a title.

A merged gap keeps its score; it records `final_pass: merged: <rule>` and the page's
`decided_by` becomes `final_pass`. Each rule can be switched off in `final_pass.rules`.

---

## 10. What comes out

### 10.1 Tags and labels

| Break Before | Break After | Tag | Label |
|---|---|---|---|
| break | not | **new** | `New (Doc 3)` |
| not | not | **continue** | `Continue (Doc 3)` |
| not | break | **last** | `Last (Doc 3)` |
| break | break | **standalone** | `Standalone (Doc 3)` |

Junk groups read `New (Junk 4)` and so on: documents and junk groups share **one
numbering**, so `document_seq` carries both. A blank page reads `Blank (in Doc 3)` or
`Blank`. The tag is derived from
the document's pages after the final pass, never stored on its own. The legacy
`position` column is still written (new → first, standalone → single).

### 10.2 `page_continuity_results`: the original columns

No new columns. One row per content or junk page:

| Column | Holds |
|---|---|
| `document_seq` | the document or junk group (one numbering) |
| `seq` | page within it |
| `position` | the tag: `first` = New, `continue`, `last`, `single` = Standalone |
| `relation` | `new_document` for its first page, else `continue` |
| `score` | the **Break Before** |
| `decided_by` | the gap before: `chart_start`, `decisive`, `decisive_conflict`, `sum`, `tie`, `conflict_a`, `conflict_b`, `final_pass`; a transparent page `attached` |
| `confidence` | the gap before's confidence, 0–1 (`|2p − 1|` on the curve, §8); 1 on the chart's first page; added 2026-10-20 beside `score` |
| `confidence_level` | the gap before's (high / medium / low) |
| `evidence` | the gap before in words, e.g. `+8 break, high (signed_in_tail +3, last_numbered_page +3, has_title +3, signed_anywhere +2)` |
| `link_strength` | strong while every link back to the first page is a 5-point join (page numbers or the same labelled date) |
| `start_confirmed` | the first page has a title or prints "page 1" |
| `review_required` | §8.5 |

Blank and duplicate pages have no row. `imaging_final` gives a document's Final values to
its **content** pages only: junk pages keep their own, whatever their `document_seq`.

### 10.3 `<chart>/staging/continuity.json`: every score

Written beside `extraction.json` and `blank_junk.json` on every run, and kept after the chart
finishes:

```json
{"version": 1, "chart_name": "…", "written_at": "…", "canon_version": 4,
 "documents": [{"document": 1, "kind": "main", "pages": ["1.jpg", "2.jpg"],
                "dos_from": "2024-03-28", "dos_to": "2024-03-28", "page_type": "Progress Note"},
               {"document": 2, "kind": "junk", "pages": ["6.jpg", "7.jpg"],
                "dos_from": "Skipped", "dos_to": "Skipped"}],
 "pages": [{"page_name": "2.jpg", "kind": "main", "document": 1, "label": "Continue (Doc 1)",
            "tag": "continue", "break_before": -8, "break_after": -8,
            "decided_by": "decisive", "confidence": 0.96, "confidence_level": "high",
            "review_required": false, "dos_from": "", "dos_trust": null, "printed": [2, 3],
            "signed": false, "signed_in_tail": false, "title": false, "page_type": "…",
            "visit_ids": [],
            "gap_before": {"score": -8, "decision": "joined", "decided_by": "decisive",
                           "confidence": 0.96, "level": "high",
                           "ending": {}, "opening": {}, "pair": {"pagination_follows": -5, "…": 0},
                           "sub_scores": {"ending": 0, "opening": 0, "pair": -8},
                           "recorded": {"text_start": -1}, "flags": [], "final_pass": null},
            "gap_after": {"…": "…"}}],
 "encounters": [{"seq": 1, "document": 1, "page_family": "Progress Note",
                 "pages": ["1.jpg", "2.jpg"], "dos_from": "2024-03-28", "dos_to": "2024-03-28",
                 "encounter_type": "outpatient_f2f", "encounter_label": "Outpatient (F2F)",
                 "confidence": 0.8, "decided_by": "tier2", "reason": "",
                 "matched_keyword": "office visit", "conflict": false, "contenders": []}]}
```

`encounters` is added at the end by the `encounter_type` stage (it runs after continuity):
one entry per encounter (a continuity document), with the one decision every page of it
carries. Review-ui's Doc Summary builds from it, and derives the encounters from
`document_seq` as before when a chart has no staged `encounters`.

The CSV (`<chart>_continuity.csv`) lists every page (blank and duplicate too) with the
label, both scores, the confidence, `kind`, `segment`, `dos_trust` and `layout_source`; a
junk group's DOS reads `Skipped`.

### 10.4 What a document shares

Unchanged (`document_finals`, applied by `imaging_final`):

| Final value | Rule |
|---|---|
| Page type | the document's first page's (each page keeps its own sub-type) |
| Date of service | the first dated page's (the first page's when it is dated), on every page |
| A stand-alone non-visit document | every page a `default_final_dos` type (demographics, registration, growth chart, problem list, immunizations): **2222-02-02** |
| Encounter type | one per document (the encounter stage reads `document_seq`) |

Junk pages keep their own values; a junk group's DOS and encounter type read **Skipped** in
review-ui's Doc Summary.

---

## 11. Worked example

An eight-page chart, as the engine scores it (the same pages are easy to rebuild in a
test). Pages 1–3 are a progress note dated 2024-03-28 from a label and printed "page N of
3", with one banner and footer; page 3 is signed in its closing lines and stops 60% down.
Page 4 is a lab report with a title, stopping 40% down. Page 5 is blank. Pages 6–7 are a
records request (junk, 96% sure). Page 8 is a note dated 2024-06-03.

| Gap | Signals | Score | Decision |
|---|---|---|---|
| 1 → 2 | page numbers run on −5, same header −2, same footer −1, wording −0.6 / −0.9, cut mid-sentence −1, starts mid-thought −1 | **−11.5** | joined, decisive |
| 2 → 3 | page numbers run on −5, same header −2, same footer −1 | **−8** | joined, decisive |
| 3 → 4 | signed in the closing lines +3, signed +2, "page 3 of 3" +3, text ends in the band +2, page 4 has a title +3 | **+13** | break, high |
| 4 → 6 (blank 5 between) | a junk run of two starts +2.9 (3 × 0.96), blank between +1, page 4's text ends at 40% +0.7 | **+4.6** | break, medium: page 6 opens Junk 3 |
| 6 → 7 | same junk kind −2, same header −2, wording −2 / −1, cut −1, mid-thought −1 | **−9** | joined, high |
| 7 → 8 | the junk run ends +2.9 | **+2.9** | break, medium |

```
New (Doc 1)  Continue (Doc 1)  Last (Doc 1)     pages 1-3, 2024-03-28
Standalone (Doc 2)                              page 4, lab report
Blank                                           page 5, between documents
New (Junk 3)  Last (Junk 3)                     pages 6-7, records request
Standalone (Doc 4)                              page 8, 2024-06-03
```

Page 8's date is not compared with anything: the document open before the junk run is
Doc 2, which has no date. Had Doc 2 been dated 2024-03-28, the gap would add
`different_dos` +5 (decisive). The final pass changes nothing here: every break is above
the weak line (score ≤ 2 or low confidence).

---

## 12. Settings (`continuity_canon.json`, version 4)

| Block | Holds |
|---|---|
| `points` | every signal: `side`, `points`, `scaled`, `decisive`, `status` (measured / assumed) |
| `dos_trust` | DOS tier → points (service / admit / discharge 5, weak / received / printed / collected 3, keyless 2, default 3) |
| `junk_run` | `decisive_at` 4.5 |
| `page_fill` | the curve (92 / 80 / 65 / 50 / 35%) and the text proxy (28 lines / 1,800 characters) |
| `text_start` | the curve (3 / 12 / 25%), `apply` false |
| `semantic` | furniture 12 characters, 20-word blocks, n-grams, `min_df` 2, divisors 0.25 / 0.15 |
| `section_order` | note order (chief complaint → … → follow-up) and the section names in each step |
| `decision` | high ≥ 5, medium ≥ 2, the curve's `a` 2 and `b` 0 (to fit), disagreement × 0.6, review below 0.25 |
| `conflict_rules` | ±4, `apply` false |
| `final_pass` | weak = score ≤ 2 or low confidence, 3 passes, each rule on / off, `progress_note_families` |
| `review_triggers` | long run 25 pages |
| `pagination`, `bands`, `default_final_dos`, `measurement` | as before |

The loader refuses a file whose `points` name a signal the engine does not produce, or
that leaves one out.

---

## 13. What is proven, what is assumed, and what waits

**Measured** (170 labelled gaps from 15 charts, both pages printing page numbers):
text reaching the bottom (34), same header line (31), wording with furniture removed (28,
27), sections in order (27, no false positives), signed (26) and signed at the bottom
(21), same footer (8), same visit number (8, no false positives).

**Assumed, to measure first:** title (+3), page-type change, junk runs, "continued",
same junk kind, the three decisive signals individually, the ending band, normalised
header / footer matching.

**Three things are measured and stored but do not change any answer yet.** Each is one
switch in `continuity_canon.json`:

1. **How far down the next page's text starts** (`text_start`, §5.4). The value is in
   `continuity.json` under `recorded`; it is not added to the score (`text_start.apply`).
2. **The two disagreement patterns** (§8.4). When they fire, the gap is flagged and its
   confidence drops one level, but the break / join answer stays the sum's
   (`conflict_rules.apply`).
3. **The 0–1 confidence number** (§8.3) uses two constants, `a` = 2 and `b` = 0, picked
   by hand. Fitting them on labelled gaps makes the number a better ranking; the
   decision itself does not use them.

**Built for the final run only.** The method allows re-scoring at earlier stages (after
blank / junk, after page type) for audit; only the final score feeds anything, and that
is the one the stage computes. Ink reaching the bottom of the image (an early-stage
signal) is not used: by the final run the line positions are there.

---

## 14. How to check it is working

- **Count the documents.** 623 pages making 9 documents was once a numbering bug.
- **Look at the longest document.** A long run with no page numbers is usually a footer
  template merging unrelated documents (the 25-page flag).
- **Plot document lengths.** A spike at one page means fragmenting; a long tail means
  merging.
- **Targets:** wrongly merged under 2% (the expensive error), low-confidence share under
  30%, accuracy over 95% on the gaps whose answer is known.
- **Split validation by chart, never by page**, or the weights will look better than they
  are.
