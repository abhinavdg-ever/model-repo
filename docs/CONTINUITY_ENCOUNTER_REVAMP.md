# Document continuity and encounter type — the weighted revamp

Replaces the first-match-wins ladder in `stages/lib/continuity/engine.py` with
weighted evidence, adds semantic page-to-page continuity, and extends the
encounter type stage. Written as one specification: everything needed to build
it, with no prior knowledge of the conversations behind it.

| | |
|---|---|
| Replaces | `continuity_canon.json` §`document_end` ladder, `engine.py` `assign` |
| Extends | `encounter_canon.json`, `encounter_classify.py` |
| Stage order | unchanged — `continuity` seq 87, after `page_subtype` and `dos_extract`, before `imaging_final` seq 100 |
| New config | `continuity_canon.json` gains `weights`, `decision`, `semantic` |
| Schema change | `page_continuity_results.score` and `.review_required` come back into use |

---

## Contents

1. [Why change a working ladder](#1-why-change-a-working-ladder)
2. [Vocabulary](#2-vocabulary)
3. [Inputs](#3-inputs)
4. [The signals, and what each is worth](#4-the-signals-and-what-each-is-worth)
5. [Semantic continuity](#5-semantic-continuity)
6. [Page fill](#6-page-fill)
7. [Section progression](#7-section-progression)
8. [The decision](#8-the-decision)
9. [Grouping, position and link strength](#9-grouping-position-and-link-strength)
10. [Encounter type](#10-encounter-type)
11. [The complete config](#11-the-complete-config)
12. [How the weights were fitted](#12-how-the-weights-were-fitted)
13. [Validation](#13-validation)
14. [Failure modes](#14-failure-modes)
15. [Build order](#15-build-order)

---

## 1. Why change a working ladder

The current engine decides by first match across eleven ordered checks. It is
clear, auditable and fast, and three of its properties are worth keeping
exactly as they are: DOS outranks pagination, pagination never splits a
document, and a blank or junk page ends the open document while a duplicate
does not.

Three properties are worth changing.

**Evidence does not accumulate.** A page with four weak continue signals and
one weak new signal is decided entirely by whichever check sits higher in the
ladder. Check 9 (page type changed) fires and the page is new, regardless of
the repeated header, the text running to the bottom, and the section order
moving forward. Those three together are worth more than the one, and the
ladder has no way to say so.

**There is no abstain state.** Check 11 is `continue_page` — a silent default.
Every page that no rule could decide is recorded as a confident continue, and
`review_required` was retired because nothing ever set it. On the measured
corpus roughly a quarter of pairs have no decisive evidence either way. Those
are pages a reviewer should see, and today they are indistinguishable from
pages the engine was sure about.

**Text continuity is not used at all.** Whether the prose on page N runs into
page N+1 is the most intuitive signal in the problem and the engine does not
look at it. Measured properly it is worth +28 on 99% coverage — the
highest-coverage real signal available.

The revamp keeps the gates, replaces the ladder below them with a weighted sum,
and reinstates the abstain band.

---

## 2. Vocabulary

| Term | Means |
|---|---|
| **Page** | One scanned image, its OCR text and its line positions |
| **Pair** | Two consecutive non-skipped pages in one chart. The unit of decision |
| **Document** | A run of pages forming one clinical document. What `continuity` assigns |
| **Visit** | What the encounter stage groups: pages sharing a date of service |
| **Chart** | Everything received for one patient in one request |
| **Skipped** | A blank, junk or duplicate page. Belongs to no document |
| **Furniture** | Template text the EMR reprints on every page: patient banner, footer |
| **Body** | What is left of a page after furniture is removed |
| **Lift** | P(signal fires given continuation) − P(signal fires given new document) |
| **Coverage** | How often a signal can speak at all, either way |

**Document versus visit.** `continuity` produces documents. The encounter stage
groups pages into visits by date, which may span several documents. The two are
different groupings over the same pages and both are needed.

**Getting it wrong is asymmetric.** Merging two encounters is a clinical error
that propagates into coding. Splitting one encounter is a nuisance a reviewer
catches. Every threshold should fail toward splitting, and genuinely ambiguous
boundaries should be flagged rather than guessed.

---

## 3. Inputs

Everything the component needs already exists in the pipeline.

| Input | Source | Used for |
|---|---|---|
| Page text | `ocr_results`, Final2 → Final1 → Tesseract | all text signals |
| Line positions | Final2 JSON polygons → Final1 words grouped → plain lines | page fill, bands |
| Own DOS (From, To) | `dos_extraction_results` — the Extracted date | the DOS gate |
| Page type, sub-type, per-level confidence | `page_classification.axes` (page axes v24) | page-type change |
| Title on the page | `axes.title_of`, else `keyword_title_hit` | first-page marker |
| Printed page number | key/value `page_no`, else the header/footer bands | pagination gate |
| Signature | `provider_signature.csv` `signature_present = y`, else a signature block in the text | document end |
| Section headers | `section_header_canon.json` | section progression |
| Blank / junk / duplicate | `v_page_blank_junk_final` | skipping |

**Bands.** With line positions: header is a line whose top is above `head_top`
(0.10 of page height), footer is a line whose bottom is below `foot_bottom`
(0.92). Without: the first 4 and last 3 lines, and a quarter of the lines each
way for page numbers. A line starting below `foot_bottom` is excluded from
where the page's text ends.

**Skipped pages.** Blank, junk and duplicate pages belong to no document. A
blank or junk page **ends** the open document — it is a physical separator. A
duplicate does not — it is a scanning artefact and says nothing about
boundaries. A duplicate inherits the document, page type, DOS and encounter
type of the page it duplicates.

---

## 4. The signals, and what each is worth

Measured on 170 pairs with pagination ground truth, drawn from 15 charts and
985 consecutive pairs across 1,011 OCR'd pages. `CONT` and `NEW` are the
proportion of true continuations and true new-document starts on which the
signal fires.

```
signal                                CONT   NEW   lift  coverage  disposition
-------------------------------------------------------------------------------
page fills to the bottom               90%   56%   +34      99%    weight  +5.1
header line repeats verbatim           50%   20%   +31     100%    weight  +4.7
tf-idf word cosine, body               62%   34%   +28      99%    weight  +1.0
section order moves forward            27%    0%   +27      31%    weight +10.0
tf-idf char cosine, body               54%   27%   +27      99%    weight  +3.7
previous page signed anywhere           6%   32%   -26     100%    weight  -4.4
previous page signed in tail            4%   24%   -21     100%    weight  -0.4
body jaccard > 0.05                    46%   27%   +19      99%    subsumed
next page opens on CC/HPI              10%   20%    -9      31%    weight  +0.5
footer line repeats                    10%    2%    +8     100%    weight  +2.8
same visit / encounter number           8%    0%    +8      low    weight  +5.4
credential line in tail                 4%   10%    -6     100%    dropped
header similarity > 0.5                23%   27%    -4     100%    dropped
footer similarity > 0.5                31%   27%    +4     100%    dropped
previous page runs on (no full stop)   98%   95%    +3      99%    dropped
next page starts lowercase              5%    0%    +5      99%    dropped
same accession number                   0%    0%     0     never   kept
same order number                       0%    0%     0     never   kept
page type change, confidence-weighted    -     -     -        -    weight  -7.0 est
full path unchanged                      -     -     -        -    weight  +3.0 est
page has its own title                   -     -     -        -    weight  -6.0 est
```

Five readings worth taking.

**Where weight and lift disagree, the regression is reporting redundancy.**
`body_cosine_word` has +28 lift but only +1.0 weight because
`body_cosine_char` captures nearly the same information and got there first.
`prev_signed_in_tail` has −21 lift and −0.4 weight for the same reason. Neither
should be removed: they contribute when the other is absent.

**`section_forward` carries the largest weight on 31% coverage.** It fired on
27% of continuations and **0%** of new documents — zero false positives on the
labelled set. High precision, honest abstention. That is the right profile for
a heavily weighted feature.

**Two syntactic tells were measured and dropped.** "Previous page ends without
terminal punctuation" scored 98% against 95%; "next page starts lowercase"
scored 5% against 0%. Nearly every OCR page runs on, because pages end in
tables and banners rather than sentences. They are listed here so a future
implementer does not rediscover and re-add them.

**The three estimated weights are estimates.** Page type change, full-path
agreement and title presence were specified after the measurement run. They
carry `status: estimated` in the config and must be fitted before being
trusted.

**Accession and order numbers never fired on this corpus.** They are kept
because they cost nothing to evaluate and are decisive when present; the zero
reflects this sample, not their value.

---

## 5. Semantic continuity

Whether the text on one page runs into the next. New to the pipeline, and the
implementation detail below is what makes it work rather than measure zero.

### 5.1 Strip the furniture first

```python
def strip_furniture(prev_lines, cur_lines, min_chars=12):
    repeated = {l for l in prev_lines if len(l) >= min_chars} & \
               {l for l in cur_lines  if len(l) >= min_chars}
    return ([l for l in prev_lines if l not in repeated],
            [l for l in cur_lines  if l not in repeated],
            repeated)
```

An EMR reprints the patient banner and footer template on every page of the
*chart*, not every page of the *document*. Those lines therefore appear on both
pages of every pair, continuation or not. They contribute identical similarity
to both classes and swamp the body text that carries the signal.

Measured both ways on the same 170 pairs:

```
                                       CONT    NEW    lift
raw tail block vs raw head block
  word overlap > 0.10                   16%    15%     +1    no signal
  char 4-gram > 0.15                    11%    12%     -1    no signal
  sequence ratio > 0.30                 13%    17%     -4    no signal

furniture stripped, body only
  tf-idf word cosine > 0.05             62%    34%    +28    strong
  tf-idf char cosine > 0.10             54%    27%    +27    strong
  jaccard > 0.05                        46%    27%    +19    moderate
```

The removed lines are not discarded — they become the furniture signal, worth
+31 on their own. Subtract the furniture from one test and score it in another.

### 5.2 The tail and head blocks

Take the last paragraph of the previous page's body. If it holds fewer than 20
words, keep walking backwards through paragraphs until 20 words are
accumulated. The head block is the mirror image, forwards from the top of the
next page's body.

```python
def tail_block(body_lines, nth_word=20):
    paras = paragraphs(body_lines)        # blank-line blocks, else 3-line runs
    if not paras:
        return ""
    if len(paras[-1].split()) >= nth_word:
        return paras[-1]
    acc, out = 0, []
    for p in reversed(paras):
        out.insert(0, p)
        acc += len(p.split())
        if acc >= nth_word:
            break
    return " ".join(out)
```

OCR rarely preserves paragraph breaks. The fallback — grouping consecutive
lines in threes — is crude but necessary; do not assume `\n\n` exists.

**Coverage: 99% of pairs yield a tail block of at least 20 words.**

### 5.3 The two cosines

```
char_wb, 3-5 grams, min_df=2, sublinear_tf     robust to OCR character damage
word,    1-2 grams, min_df=2, sublinear_tf     robust to word reordering
```

Fit the vectoriser **on the whole corpus of blocks**, not per pair. Inverse
document frequency is what down-weights ubiquitous clinical vocabulary, and it
needs the corpus to compute. Fitting per pair makes idf meaningless.

Normalise for the weighted sum by dividing by the operating threshold and
clipping: `min(1.0, cosine / 0.15)` for word, `/ 0.25` for char.

---

## 6. Page fill

The strongest measured signal, and the pipeline already has what it needs.
Final1 carries Docling's layout dump; Final2 carries line polygons. No new OCR
is required.

```
page_height        from the page geometry
last_body_bottom   the bottom of the last line whose top is ABOVE foot_bottom
footer_top         page_height * foot_bottom                    (0.92)
fill               last_body_bottom / footer_top, clipped to 1.0
```

Excluding lines that start inside the footer band is what stops a page-number
line setting `fill` to 1.0 everywhere. The current implementation already does
this.

**Two thresholds are in play and they are not the same number.**

```
today    page_end_max = 0.60    text ending above 60% means the document ENDED
measured fill >= 0.92           text reaching 92% means the page CONTINUES
```

Between 0.60 and 0.92 the current ladder says nothing and falls through to
`continue_page`. That dead zone covers a large share of pages and is currently
a silent default on the strongest signal in the problem. Grade it instead:

```
fill <= 0.60           value 0.0      strong evidence the document ended
0.60 < fill < 0.92     value (fill - 0.60) / 0.32
fill >= 0.92           value 1.0      strong evidence it continues
```

This is the clearest single gain from moving to weights. Measure the
interpolated version against the two step functions before committing.

Without layout the fallback is a text proxy — `line_count >= 28 or
char_count >= 1800`. It scored +34 and is still the weakest version of the best
signal, because line count cannot distinguish 30 short table rows ending
halfway down from 30 prose lines reaching the footer. Pages with
`layout_source = text` should be flagged.

---

## 7. Section progression

A clinical note is written in a conventional order. A page opening on an early
section after a page that ended on a late one is a new note.

```
order:  chief_complaint, history_of_present_illness, review_of_systems,
        past_medical_history, social_history, family_history,
        medications, allergies, vitals, physical_exam,
        lab_results, imaging_results, problem_list, visit_diagnosis,
        plan, patient_instructions, follow_up, signature

direction = clip((min_rank(cur) - max_rank(prev)) / len(order) * 4, -1, 1)
```

Positive means the next page follows the previous one in note order. Section
headers come from `section_header_canon.json` and are already written to
`<chart>_continuity.csv`.

---

## 8. The decision

Three layers: skip handling, gates, then the weighted sum. The first that
applies wins.

### 8.1 Skips and gates

```
0   skipped page
        blank or junk    -> no document, AND ends the open one
        duplicate        -> no document, does NOT end anything

1   first page of the chart                          -> NEW        first_page
2   a blank or junk page came immediately before     -> NEW        blank_junk
3   own DOS equal to the open document's (From, To)  -> CONTINUE   same_dos
4   own DOS, a different one                         -> NEW        new_dos
5   pagination follows on (N -> N+1, same total)     -> CONTINUE   pagination
6   previous page was "page N of N"                  -> NEW        page_number_end
```

**DOS outranks pagination.** A different date of service means a different
encounter whatever the page numbers say; an EMR can print one continuous footer
across a multi-day admission summary. Check 3 holds even after a signature —
a signed page followed by a page carrying the same date is the same visit.

**Pagination never splits.** It confirms a continue, closes a document, or
marks a first page as confirmed. It never starts one. The reason is OCR: a "1"
misread from a damaged "7" would split a document mid-way, and splits caused by
a single misread character are the hardest errors to find afterwards.
Confirming a continue costs nothing when wrong, because the other signals still
vote.

One parameter to revisit: `max_total` is 20 today, so "page 4 of 47" is
rejected and long documents lose the signal entirely. The band restriction
already removes most false matches from body ratios, so the cap can probably
rise. Measure the false-match rate at 20, 50 and 300 before changing it.

### 8.2 The weighted sum

Reached only when no gate fired — that is, an undated page with no decisive
pagination.

```
score = Σ (weight_i × value_i)        values in [0,1]

score >= +4     CONTINUE        confidence medium   decided_by weighted
score <= -4     NEW_DOCUMENT    confidence medium   decided_by weighted
otherwise       UNKNOWN         confidence low      review_required = y
```

```
signal                        weight    status
-----------------------------------------------
section_forward                +10.0    fitted
page_type_change                -7.0    estimated
cur_has_title                   -6.0    estimated
same_visit_number               +5.4    fitted
page_fill (graded)              +5.1    fitted
header_line_repeats             +4.7    fitted
prev_signed                     -4.4    fitted
body_cosine_char                +3.7    fitted
full_path_same                  +3.0    estimated
footer_line_repeats             +2.8    fitted
body_cosine_word                +1.0    fitted
cur_opens_note                  +0.5    fitted
prev_signed_in_tail             -0.4    fitted
-----------------------------------------------
grouped CV balanced accuracy    0.790 +/- 0.186   (5 folds, split by chart)
```

### 8.3 Page type change as a graded feature

The current ladder's rule is sound and should be kept as the source of the
value, converted from a boolean to a magnitude:

```
same_type_probability =
    the classifier's confidence in this page's type when that type is the
    previous page's L1; otherwise 1 - that confidence

type_change_strength = (1 - same_type_probability) * confidence_in_new_type
value fed to the sum  = -type_change_strength, clipped to [0,1]
```

| Previous | This page | Confidence | Same-type probability | Today | Graded value |
|---|---|---|---|---|---|
| Progress Note | Lab | 90% | 10% | new | −0.81 |
| Progress Note | Lab | 60% | 40% | continue | −0.36 |
| Progress Note | Progress Note | 95% | 95% | continue | −0.05 |

The 60% case is the one that changes. Today it contributes nothing; graded, it
contributes a moderate push toward new that other signals can outvote.

**Score agreement at the full path, not L1.** "Progress Note follows Progress
Note" holds for long runs across different encounters, so L1 agreement alone
over-merges. As a *split* rule L1 is safe, which is why today's ladder is fine;
as a *continue* signal it is too weak to use.

### 8.4 The abstain band

A page scoring between −4 and +4 stays in the open document and is flagged. It
does not start a new one: leaving a page in the current document is the
recoverable error, starting a spurious document fragments an encounter.

This reinstates `page_continuity_results.score` and `.review_required`, both of
which were retired when nothing set them. On the measured corpus the soft-signal
system ran at 100% precision, 76% recall, 24% abstain. Never widen the band to
reduce the abstain rate — the abstained pages are the ones most likely to be
wrong.

---

## 9. Grouping, position and link strength

```python
doc_n = 0
for i, page in enumerate(chart_pages):          # sorted, skips excluded
    if i == 0 or verdict.relation == "new_document":
        doc_n += 1
    page.document_seq = doc_n
    page.document_id  = f"{chart_id}:doc-{doc_n}"
```

**The chart prefix is mandatory.** A counter that restarts per chart collides
across charts and merges unrelated documents. This has happened once: 623 pages
produced 9 documents and looked plausible until counted.

### 9.1 Position and the signature terminator

```
len(doc) == 1   -> single       index 0 -> first
index len-1     -> last         otherwise -> continue
```

A page carrying a signature in its tail closes its document even when the
weighted score kept it open, unless pagination or a shared DOS explicitly
contradicts. The signature was the strongest new-document evidence measured at
−26, and an attestation is semantically the end of a clinical document.

### 9.2 Link strength

```
link            strong   joined by same_dos or pagination
                weak     joined by the weighted sum
link_strength   strong   only while EVERY link back to the first page is strong
                weak     otherwise
```

Keep the existing definition. Add one value: a page joined across the abstain
band gets `link = unknown`, and `link_strength` becomes `weak` from there on.
A document whose chain contains an abstention is one a reviewer should be able
to find.

### 9.3 Review flags

```
review_required = y when:
    relation == unknown
    or a run of more than 25 pages with no pagination anywhere
    or layout_source == text on a page where page_fill decided
```

The second catches the characteristic failure: a footer template repeating
across unrelated documents and binding them into one implausible group. A
19-page group formed entirely on footer similarity has occurred in this corpus
and was a false positive.

### 9.4 Final values

Unchanged. Every page of a document takes the first page's page type, DOS and
encounter type. A stand-alone document whose every page is a `default_final_dos`
type — Patient Demographics, Patient Registration, Growth chart, Problem List,
Immunization — takes `DOS_DEFAULT_DATE` 2222-02-02. One such page inside a
visit takes the visit's date.

---

## 10. Encounter type

One answer per visit, stamped on every page: Outpatient (F2F), Outpatient
(Tele), Inpatient or Home.

### 10.1 Ranked evidence, not a weighted sum

This is the one place in the component where weighting is the wrong tool.
Continuity is a balance-of-evidence question, so it sums. Encounter type is an
authority question: one discharge summary settles it, and no volume of weak
hints should outvote it. The existing tier design is correct and stays.

```
tier 1      a page type that exists in one setting only        decides alone
tier 2      text naming the setting                            decides when tier 1 is empty
tier 3      hints found in several settings                    breaks ties only
negatives   remove the tier 2/3 finding they name              never touch tier 1
context     logged, never scored
```

Each finding is recorded **once per visit** however often its words appear.
Twenty occurrences of "telehealth" is one finding. This is what stops a
boilerplate footer dominating.

```
tier 1, one setting              that setting                     0.95
tier 1, two settings             more tier 3 hints, then priority 0.70  conflict=y
tier 1 empty, tier 2 one         that setting                     0.80
tier 2, two settings             as above                         0.60  conflict=y
nothing in tiers 1-2             see 10.3
```

Priority on a tie: Home 10, Tele 20, F2F 30, Inpatient 40.

### 10.2 Visits

A visit is a run of consecutive pages sharing a date — the document-level DOS,
else the page-level DOS. `DOS_DEFAULT_DATE` is the absence of a date, not a
date. The same date forty pages later is a second visit.

Where `continuity` has run, prefer its documents as the visit unit: the pages
of one document share one answer by construction, and the document grouping
already encodes evidence the date alone does not.

### 10.3 The default, and its guard

Today a visit with nothing in tiers 1 or 2 is left empty with
`reason=no_setting_evidence`. The change is to default it to **Outpatient
(Tele)**, because a wrongly-claimed in-person visit is the more expensive
error — with one guard.

```
no telehealth evidence
   AND vital signs present
   AND physical examination findings present
        -> Outpatient (F2F)     confidence 0.50
           reason: vitals and exam findings, no telehealth marker

otherwise, nothing in tiers 1-2
        -> Outpatient (Tele)    confidence 0.30
           reason: defaulted, no setting evidence
           defaulted: y
```

You cannot take a blood pressure over video. Vitals and examination findings
sit in **tier 3**, not tier 2, because they also appear in inpatient and
home-visit documentation and must not decide alone — but together, in the
absence of any telehealth marker, they are enough to overturn the default.

**Flag every defaulted answer.** A default is a guess and must be
distinguishable in the output from an evidenced determination. Without the
flag, a reviewer cannot tell which Tele labels to trust, and the default
becomes invisible in the accuracy numbers.

### 10.4 Keep the loader validation

The catalog loader already refuses a file where a setting is unknown, a tier 1
id is not a page type id, a phrase sits in two tiers or under two settings, a
context phrase also scores, or a negative cancels a phrase that does not exist.
Extend the same discipline to the new continuity weights: refuse a config whose
weights name a signal the extractor does not produce, or whose `decision` band
is inverted.

---

## 11. The complete config

`continuity_canon.json` keeps `bands`, `pagination`, `document_end` and
`default_final_dos`. Three blocks are added.

```jsonc
{
  "version": 3,

  "measurement": {
    "comment": "Provenance for the weights. A config that carries its own
                provenance tells the next reader how much to trust it.",
    "n_pairs": 985, "n_labelled": 170, "n_continue": 129, "n_new": 41,
    "n_charts": 15, "ground_truth": "explicit pagination on both pages",
    "cv_balanced_accuracy": 0.790, "cv_std": 0.186, "cv_split": "GroupKFold by chart_id",
    "fitted": "2026-10-18",
    "known_bias": "documents printing pagination are longer and more
                   template-driven; furniture signals may be overstated and
                   prose signals understated"
  },

  "semantic": {
    "furniture_min_chars": 12,
    "tail_nth_word": 20,
    "char": { "analyzer": "char_wb", "ngram": [3,5], "min_df": 2, "norm_divisor": 0.25 },
    "word": { "analyzer": "word",    "ngram": [1,2], "min_df": 2, "norm_divisor": 0.15 }
  },

  "page_fill": {
    "comment": "Graded between the two existing anchors rather than a step at
                either. page_end_max stays as the lower anchor.",
    "ended_at": 0.60, "continues_at": 0.92, "graded": true
  },

  "section_order": [ "chief_complaint", "history_of_present_illness", "..." ],

  "weights": {
    "section_forward":     { "w":  10.0, "lift":  0.27, "coverage": 0.31, "status": "fitted" },
    "page_type_change":    { "w":  -7.0, "lift":  null, "coverage": null, "status": "estimated" },
    "cur_has_title":       { "w":  -6.0, "lift":  null, "coverage": null, "status": "estimated" },
    "same_visit_number":   { "w":   5.4, "lift":  0.08, "coverage": null, "status": "fitted" },
    "page_fill":           { "w":   5.1, "lift":  0.34, "coverage": 0.99, "status": "fitted" },
    "header_line_repeats": { "w":   4.7, "lift":  0.31, "coverage": 1.00, "status": "fitted" },
    "prev_signed":         { "w":  -4.4, "lift": -0.26, "coverage": 1.00, "status": "fitted" },
    "body_cosine_char":    { "w":   3.7, "lift":  0.27, "coverage": 0.99, "status": "fitted" },
    "full_path_same":      { "w":   3.0, "lift":  null, "coverage": null, "status": "estimated" },
    "footer_line_repeats": { "w":   2.8, "lift":  0.08, "coverage": 1.00, "status": "fitted" },
    "body_cosine_word":    { "w":   1.0, "lift":  0.28, "coverage": 0.99, "status": "fitted" },
    "cur_opens_note":      { "w":   0.5, "lift": -0.09, "coverage": 0.31, "status": "fitted" },
    "prev_signed_in_tail": { "w":  -0.4, "lift": -0.21, "coverage": 1.00, "status": "fitted" }
  },

  "decision": {
    "continue_at":  4.0,
    "new_at":      -4.0,
    "comment": "The band between is deliberate. Never widen it to reduce the
                abstain rate: the abstained pairs are the ones most likely to
                be decided wrongly."
  },

  "signature_terminator": { "enabled": true },

  "review_triggers": {
    "unknown_relation": true,
    "long_run_without_pagination": 25,
    "page_fill_from_text_proxy": true
  }
}
```

---

## 12. How the weights were fitted

### 12.1 Ground truth

No hand-labelled boundaries exist. Ground truth is derived from explicit
pagination: where both pages of a pair carry "Page N of M", the relation is
known with certainty.

```
985 consecutive pairs
170 with pagination ground truth        17%
129 continuations, 41 new documents
15 charts
```

### 12.2 The bias, stated

Documents that print pagination are longer and more template-driven than
documents that do not. A two-page fax of a handwritten note has no pagination
and is not represented. So furniture signals may be overstated, prose signals
understated, and the 41 new-document examples are few — the new-document
weights rest on them. This is the largest known weakness in the fit and is
addressed by hand-labelling.

### 12.3 Procedure

```
features    each normalised to [0,1]
model       LogisticRegression(class_weight='balanced', C=1.0, max_iter=2000)
validation  GroupKFold(n_splits=5, groups=chart_id)
metric      balanced accuracy
scaling     coefficients / max(|coefficients|) * 10
```

**Split by chart, never by page.** Pages within one chart share letterhead,
patient banner and footer template. A page-level random split puts both halves
of the same document in train and test, and the furniture signals then look far
stronger than they are. This is the difference between a believable number and
one that collapses in production.

### 12.4 Refitting

Refit when hand-labelled boundaries exist, when the three estimated weights are
measured, or when the corpus grows by half. **Always update the `measurement`
block alongside the weights** — a weight whose provenance is unknown is a
weight nobody can audit.

The measurements above were taken with standalone reimplementations of the
extractors, not the pipeline's own outputs. Before adopting the weights in
production, re-measure against real DOS, real signature flags, real line
polygons and real page-type confidences. Expect the DOS and page-type signals
to strengthen considerably — the pipeline's DOS stage is far richer than the
minimal extractor used here, and the page-type feature carries confidence that
the measured boolean did not. The *ranking* of the top signals should survive;
the *weights* will move.

---

## 13. Validation

### 13.1 What to measure

```
pairwise          precision and recall on continue, separately on new_document
                  abstain rate
                  confusion against the pagination-labelled subset

document level    boundary F1
                  over-merge rate: documents spanning two true documents
                  over-split rate: true documents broken apart
                  document length distribution

encounter type    accuracy per type
                  share of Tele answers that were defaulted rather than evidenced
```

### 13.2 Acceptance

```
over-merge rate                 < 2%      the expensive error
abstain rate                    < 30%
precision on new_document       > 95%
pagination-subset accuracy      > 95%     it must reproduce its own ground truth
defaulted Tele share            reported, not bounded
```

Optimise for over-merge even at the cost of the others.

### 13.3 Sanity checks that have each caught a real bug

- **Document count.** 623 pages producing 9 documents was the missing chart
  prefix on `document_id`.
- **Longest group.** A 19-page group with no pagination anywhere was a
  footer-template false positive.
- **Length histogram.** A spike at exactly 1 page means fragmenting; a long
  tail means merging.
- **Per-chart counts.** A 715-page chart yielding 3 documents, or 715, is a bug
  in opposite directions.

---

## 14. Failure modes

| Failure | Cause | Detection | Mitigation |
|---|---|---|---|
| Everything merges | `document_id` missing the chart prefix | document count absurdly low | prefix it; assert uniqueness |
| A blank page splits a document | blank/junk verdict not read | blank pages present in the pair stream | read `v_page_blank_junk_final` first |
| Footer template merges documents | `foot_repeat` firing across documents of one EMR | long runs with no pagination | review flag at 25; footer weighted below header |
| Semantic similarity measures zero | furniture not stripped | raw cosine lift near zero | §5.1 |
| Weights look great, production poor | page-level train/test split | CV far above production | split by `chart_id` |
| `fill` pinned at 1.0 everywhere | footer-band lines included in `last_body_bottom` | fill distribution not bimodal | exclude lines starting below `foot_bottom` |
| Everything splits at every page | section headers empty, so the +10 feature never fires | section coverage near zero | check `section_header_canon` output first |
| Spanish pages all become new documents | OCR language | Spanish pages scoring zero on every lexicon | Final2 Azure handles this; verify |
| Abstain rate near zero | the band collapsed, or gates firing on everything | `decided_by` distribution | inspect; a 0% abstain rate is a bug, not a triumph |

---

## 15. Build order

1. **Extract the signals** — `signals.py` against the pipeline's own values, not
   reimplementations. Run the measurement harness and confirm the lift table
   reproduces. If it does not, the extraction differs and nothing downstream
   will behave as documented.
2. **Hand-label boundaries on 300 pages**, chosen to include short documents
   and documents without pagination. This removes the §12.2 bias and is the
   only way to validate the 41 new-document examples the weights rest on.
3. **Fit the three estimated weights** — page type change graded, full-path
   agreement, title presence — and refit the whole set.
4. **Implement the decision** — skips, gates, then the weighted sum. Verify
   against the pagination subset before anything else: the component must
   reproduce its own ground truth first.
5. **Reinstate `score` and `review_required`** in `page_continuity_results`,
   and surface the abstain band in review-ui. The flagged pages are the
   deliverable of this whole change.
6. **Grade the page fill** between 0.60 and 0.92, measured against the two step
   functions.
7. **Encounter type** — add the default and its guard, and the `defaulted`
   column.
8. **Only then** consider sentence embeddings. Tf-idf on furniture-stripped
   body text gives +28 today; an embedding model would plausibly beat it, but
   the weights must be vendored and it must be measured against this baseline
   rather than assumed better.

---

## Appendix — files

```
stages/lib/continuity/engine.py              assign, document_ended, document_finals
stages/lib/encounter/encounter_classify.py   tiers, negatives, decision
stages/lib/keyword-canon/continuity_canon.json
stages/lib/keyword-canon/encounter_canon.json
stages/lib/keyword-canon/section_header_canon.json
stages/lib/keyword-canon/dos_canon.json
docs/CONTINUITY_LOGIC.md    the ladder this replaces
docs/DOS_LOGIC.md           the date stage this consumes
docs/BLANK_JUNK_LOGIC.md    the skip verdicts this consumes
docs/PROVIDER_LOGIC.md      the signature flag this consumes
docs/LOGIC.md §7b, §8       the two stages this changes
```
