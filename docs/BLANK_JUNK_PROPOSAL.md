# Blank / Junk — Methodology

Status: **IMPLEMENTED (2026-10-15)** in `core-pipeline/stages/lib/blank_junk/method.py`, called by
the blank/junk stage in both passes. Written 2026-10-14; R4 narrowed on 2026-10-15 (§6); TF-IDF removed and confidence
rebuilt from the evidence on 2026-10-17 (§8, §10).
Canon: `core-pipeline/stages/lib/keyword-canon/blank_junk_context_canon.json`.

---

## 1. Principle

A clinical page that is junked loses billable content and cannot be recovered
downstream. A junk page that is kept only costs a reviewer a few seconds.
So:

- Every tie, abstention and missing signal resolves to **KEEP**.
- JUNK needs **evidence**: DistilBERT and the keywords agreeing, DistilBERT alone
  with the keywords silent, or the keywords alone at a strong score (§8), and
  no veto.
- A small set of deterministic rules decide before any vote, so obvious pages
  never depend on a model.
- Every vote is written to the database, so any verdict can be explained and
  thresholds can be tuned from data.

---

## 2. Verdicts

| Verdict | Meaning | Subtypes |
|---|---|---|
| `blank` | No content | — |
| `junk` | Non-clinical page | Invoice, Cover Page, Record Request/Transmittal, Instructions, Letter/Fax, Others |
| `duplicate` | Same content as a kept page nearby | — |
| `not_blank_junk` | Main page, kept | — |

A junk verdict always carries a subtype. Others is the fallback; NULL is never
written.

---

## 3. Inputs

| Signal | Source | Pass 1 | Pass 2 |
|---|---|---|---|
| Text | Tesseract prelim (pass 1); Azure DocIntel, else Docling/RapidOCR (pass 2) | yes | yes |
| Line coordinates | `final2.pagesMeta[].lines[].polygon`, else `final1.words[].polygon` | no | yes |
| Ink map | Page image (same ink measure the HW stage uses, `page_marks`) | yes | yes |
| Page type | `ocr_quality_results.document_type`: printed / handwritten / form / **visual** / blank / uncertain, and `handwritten_area_pct`, page-tag model (`convnext_page_tags`) | yes | yes |
| Signature | KV extraction (`kv_extract`, seq 56): Provider Signature = Yes. Runs after final OCR and **before** pass 2 (seq 60) | no | yes |
| DistilBERT | 8-label page model (KEEP, BLANK, 6 × JUNK_*) | yes | yes |
| Keyword context | `blank_junk_context_canon.json` | yes | yes |

**Visual.** Read from one place only: `ocr_quality_results.document_type = 'visual'`
(CSV: `document_type` in `imaging/<chart>_hw_printed.csv`). This is the same value
review-ui shows as Type. No extra confidence cut-off is applied: the page-tag model
already turns a weak call into `uncertain`. There is no proxy. A page without
page-tag output (`method` other than `convnext_page_tags`) is simply not Visual. Its
row records `visual_source = "none"`, so a run on the old 2-class checkpoint
shows in the data. The four charts under `review-ui/data/folders/` were run that
way and carry no Visual tags.

A Visual page is decided in **pass 1** (R8) and **pass 2 is skipped** for it.

---

## 4. Context canon (the JSON)

`blank_junk_context_canon.json` holds lists of words. It reloads on change,
like the other canons. The words are sorted into three **contexts**, and each
context is split into **groups** of similar words:

| Context | Groups | What a high score means |
|---|---|---|
| **clinical** | section_headings, assessment_language, vitals, exam_findings, diagnosis_coding, conditions, medications_orders, labs_results, imaging_procedures, **patient_education**, signature | The page is part of a medical record |
| **junk** | fax_cover, cover_page, cover_stamps (Accept / Unaccept), record_request, roi_vendors, invoice, placeholder | The page is retrieval or billing paperwork; each group names the junk subtype |
| **demographic** | identity, registration | A face sheet or registration page: administrative-looking, but a chart page |
| **neutral** | fax, phone, page, confidential, PHI, and all insurance wording | Never scored: it appears on every kind of page |

Two lists are deliberately absent:

- **Patient education counts as clinical.** Handouts and instruction sheets
  ("Consumer Information Use and Disclaimer", "after visit summary", "call your
  doctor if") are part of the record. Page classification has a Patient
  Education page type, but it runs after blank/junk, so a junked handout never
  reaches it. Two `patient_education` hits make the page C, so DistilBERT cannot
  force it to junk (R4) and the vote keeps it.
- **No `instructions` junk group.** Patient instructions (discharge, medication,
  after-visit) are clinical pages. The Instructions subtype can still come from
  DistilBERT, but no word list pushes a page toward it.
- **No `insurance` group.** Insurance wording sits on clinical notes, face
  sheets and retrieval letters alike, so it tells us nothing. It is neutral.

### 4.1 How a page gets a score

Each group has two numbers.

- **Weight:** how much one hit of that group is worth. It reflects how sure the
  word makes us.
- **Cap:** the most the group can add in total, however many times its words
  appear.

| Weight | Meaning | Example |
|---|---|---|
| 1 | Common or ambiguous: suggests, never proves | "diabetes", "mg", "dob": also appear in request letters and forms |
| 2 | Typical of the context | "Assessment:" at line start, "fax cover sheet", "face sheet" |
| 3 | Nearly decisive | "request for medical records", "invoice number", "electronically signed by" |
| 4 | Decisive on its own | An ROI vendor name: Datavant, Ciox, Optum |
| 6 | Placeholder only | "intentionally left blank" |

**The calculation, in three steps:**

1. **Count unique hits.** Each distinct word or phrase of the group counts
   **once**, however often it appears. "Datavant" on every line of a
   letterhead is 1 hit; "Datavant" and "Ciox" are 2. Where phrases overlap,
   the longest wins: "fax cover sheet" is one hit, not also a hit for
   "fax cover".
2. **Group score** = weight × unique hits, cut down to the cap if it is higher.
3. **Context score** = the group scores of that context added together.

The page ends up with three numbers: **clinical**, **junk** and
**demographic**. Each is then compared with its threshold:

| Threshold | Value | Name used below |
|---|---|---|
| clinical ≥ 4 | 4 | **C**: clinical evidence present |
| clinical ≥ 8 | 8 | **C⁺**: clearly a clinical note |
| junk ≥ 4 | 4 | **J**: junk evidence present |
| junk ≥ 7 | 7 | **J⁺**: clearly retrieval / billing paperwork |
| demographic ≥ 3 | 3 | **D**: demographic page |

**Worked example 1 — a progress note.**

| Group | Unique hits on the page | Weight | Weight × hits | Cap | Group score |
|---|---|---|---|---|---|
| section_headings | "HPI:", "Assessment:", "Plan:" = 3 | 2 | 6 | 8 | 6 |
| conditions | "diabetes" (3 times, counts once), "hypertension" = 2 | 1 | 2 | 4 | 2 |
| medications_orders | "metformin", "lisinopril", "daily", a dose like "500 mg" (the dose pattern counts once) = 4 | 1 | 4 | 3 | **3** (capped) |
| | | | | **clinical** | **11 → C⁺** |

**Worked example 2 — a record-request letter that lists diagnoses.**

| Group | Unique hits | Weight | Score |
|---|---|---|---|
| record_request | "request for medical records", "dates of service requested" = 2 | 3 | 6 |
| roi_vendors | "Datavant" (twice, counts once) = 1 | 4 | 4 |
| | | **junk** | **10 → J⁺** |
| conditions | "diabetes", "CKD" = 2 | 1 | 2 |
| | | **clinical** | **2: below C** |

**Why unique hits.** A word repeated in a letterhead, a footer or a fax
banner on every line says nothing new after the first time. Counting it once
keeps repetition from building a score.

**Why the cap matters.** A medication list naming 20 different drugs has 20
unique hits; without a cap it would score 20 and look like the strongest
clinical page in the chart. With the cap of 3, one kind of word can never
reach a threshold alone. For
example, conditions max 4 < C⁺ at 8. A page reaches C⁺ only with evidence from
**several different groups**: headings, plus conditions, plus medications.
Junk works the same way. One vendor name gives at most 8 points; a vendor plus
request wording is J⁺. The fax-cover group is capped at 6, so a fax cover alone
is J but never J⁺.

**Three refinements:**

- **`line_start`** (section headings only): "Plan" counts only at the start of
  a line, alone or followed by `:` or `-`. Inside a sentence ("we plan to…")
  it does not count.
- **Patterns** catch value-bearing evidence: `BP 128/76`, `10 mg`, `$45.00`. A
  form label "BP: ____" does not match; a measured BP does.
- **Zones** (pass 2 only): the header is the top 12% of the page, the footer
  the bottom 8%. Junk words there count only if the body also has a junk hit.
  A fax banner across the top of a lab report does not make it junk.

## 5. The two voters

Each voter returns **JUNK**, **KEEP** or **ABSTAIN**, a strength
(**strong** / **weak**) and, for JUNK, a subtype. The voters are independent:
neither reads the other's output. TF-IDF was a third voter until 2026-10-17; it
was removed because it was not accurate enough (on chart 53688890 it decided
no verdict and only added split votes).

### 5.1 DistilBERT (semantic)

The raw softmax over the 8 labels is used, not the vendored decision rules.
Those rules embed their own vetoes, which would count the same evidence twice.

```
p_keep  = P(KEEP)
p_junk  = Σ P(JUNK_*) + P(BLANK)        # BLANK on a page with text is junk
top_junk_label = argmax over JUNK_* (BLANK → Others)
```

| Condition | Vote | Strength |
|---|---|---|
| fewer than 8 words of text | ABSTAIN | — |
| p_junk ≥ 0.85 | JUNK | strong |
| 0.60 ≤ p_junk < 0.85 | JUNK | weak |
| p_keep ≥ 0.85 | KEEP | strong |
| 0.50 ≤ p_keep < 0.85 | KEEP | weak |
| otherwise | ABSTAIN | — |

Subtype: `top_junk_label`.

### 5.3 Keyword context (canon)

| J | C | D | Vote | Strength | Subtype |
|---|---|---|---|---|---|
| J⁺ | — | — | JUNK | strong | best junk group |
| J | — | — | JUNK | weak | best junk group |
| J / J⁺ | — | D | JUNK if the junk comes from record_request / roi_vendors / invoice, else KEEP (weak) | as above | best junk group |
| any | C | any | KEEP | weak | — |
| any | C⁺ | any | KEEP | strong | — |
| — | — | D | KEEP | weak | — |
| — | — | — | ABSTAIN | — | — |

"—" means below threshold. When J and C both hit, the keyword voter says KEEP. The best junk group is the group with
the highest score; a tie goes to the group with the higher weight.

The keyword voter **abstains** when it has no evidence; it does not vote KEEP.
A page with no hits is decided by DistilBERT.

---

## 6. Precedence ladder

Rules run top to bottom. The first rule that fires decides, and nothing below
it runs. Rules R1–R8 are deterministic; R9 is the vote.

| # | Rule | Verdict | Overrides |
|---|---|---|---|
| **R1** | **Blank.** No ink above the blank threshold (`blank_page` / `faint_marks_only` from the HW stage), **or**, on a page that is not Visual, 0 non-space OCR characters or the word "blank" in fewer than 30. **Tiny page:** otherwise fewer than 15 characters (not Visual) → junk, Cover Page when a Cover Page term is there, else Others | `blank` / junk | everything |
| **R2** | **Placeholder.** A `placeholder` term ("intentionally left blank") and 25 words or fewer | `junk` / Others | everything below |
| **R3** | **Signature.** Provider Signature = Yes from KV extraction (`kv_extract`, seq 56, runs before pass 2), or one of the clinical `signature` group's phrases ("electronically signed by", "attestation" …; a credential at a line end alone is not enough) | `not_blank_junk` | R4–R9 |
| **R4** | **DistilBERT force.** DistilBERT p(junk) **> 0.85** (Σ JUNK_* + BLANK), pass 2, **unless the canon finds clinical (C, ≥ 4) or demographic (D, ≥ 3) evidence** — then the page goes on down the ladder and the vote decides (`votes.r4_skipped`) | `junk` / DistilBERT's top junk label | R5–R9 |
| **R5** | **Short page.** Content ends in the top **15%** of page height (§7). Fires **even when the page carries a clinical heading**, and **whatever the neighbouring pages are**: no adjacency check | `junk` / Others | R6–R9, including the clinical veto |
| **R6** | **Junk counter-veto.** record_request + roi_vendors ≥ 7 (J⁺ from retrieval terms alone) and not C⁺ | `junk` / Record Request/Transmittal | R7–R9 |
| **R7** | **Strong clinical.** C⁺ (score ≥ 8) | `not_blank_junk` | R8–R9 |
| **R8** | **Visual.** `document_type = 'visual'`. Decided in **pass 1**; pass 2 is skipped for these pages | `not_blank_junk`, unless invoice / record_request / roi_vendors ≥ 7, then `junk` with that group's subtype | R9 |
| **R9** | **Vote** (§8) | per §8 | — |

Why this order:

- **DistilBERT force (R4).** Above 85% the model is trusted outright, except on
  a page the canon calls clinical or demographic. Run on the local charts, the
  unrestricted rule junked a Patient Demographics face sheet and a BCG
  medication-administration page (DistilBERT p(junk) 0.98–1.0 on both), so on
  2026-10-15 it was narrowed: those pages go to the vote, where the clinical
  and demographic vetoes apply. Only blank, placeholder and signature sit above
  R4. A signed page stays kept, for the same reason it is exempt from the
  short-page rule (next point).
- **Signature (R3) above DistilBERT force (R4) and short page (R5).** In risk adjustment a diagnosis is
  only valid on a signed, credentialed note. A page holding only "Electronically
  signed by Dr X, MD" at the top is the attestation of the previous page's
  note. Junking it can invalidate that date of service. This is the **only**
  exemption from R5.
- **Short page (R5) above the clinical veto.** As requested: a lone
  "Assessment and Plan" heading or a header block with nothing beneath it is
  not codable content.
- **R6 above strong-clinical (R7) only below C⁺.** A request letter lists
  diagnoses and dates ("please send records for diabetes, CKD"), which gives a
  moderate clinical score. A real note reaches C⁺ and survives R6.
- **Visual (R8)** has little text, so the text voters are unreliable on it.
  It is kept by default; only very strong retrieval or invoice wording junks
  it. A logo-heavy fax cover can be tagged Visual.

---

## 7. Short page (R5) — exact definition

Pass 2 only, because it needs line coordinates and Provider Signature from KV extraction (which runs just before pass 2).

1. **Page height H:** `pagesMeta.height` (DocIntel), else `final1.height`.
   Normalise every polygon to top-left origin, since Docling headers may be
   bottom-left.
2. **Lines:** DocIntel lines; else Docling words grouped into lines (same
   baseline ± half the median word height).
3. **Drop** lines that start in the bottom 6% (page numbers, footers) and lines
   matching `ignore_line_patterns` **anywhere in the line**: "Page 2 of 5" /
   "Pg 14 of 17", a bare number, "Printed: <date>", fax transmission stamps,
   "Confidential…". A running footer such as `MRN: 023466 Printed: 10/30/2025
   8:10 AM Pg 14 of 17` at 92% of the height is a stamp, not content.
4. **content_bottom** = max bottom-y of the remaining lines ÷ H.
5. **Ink check:** the band from 17% to 94% of the height must hold no ink
   above `body_ink_max_fraction` (1e-4, the same blank cut-off as the HW stage).
   This catches a picture, a handwritten block or a stamp that OCR did not read.
6. **Fire** when `content_bottom ≤ 0.15` and the ink check passes and R3 did
   not fire.

**Not considered:**

- Clinical headings on the page.
- The previous and next page.
- Word count: a page that ends in the top 15% is short by construction.

| Page | Result |
|---|---|
| "PROGRESS NOTE — Patient: J Doe DOB 01/02/1950", nothing below | JUNK / Others |
| "Assessment and Plan:" alone at the top | JUNK / Others |
| Header block, then a wound photo mid-page (no text) | Kept: ink in the body band |
| Header block, then "Page 3 of 7" at the bottom | JUNK / Others: footer ignored |
| "Electronically signed by A. Smith, MD 03/14/24" alone at the top | Kept by R3 |

---

## 8. How the two voters decide (R9)

### 8.1 Counting

Each voter casts one ballot: J (JUNK), K (KEEP) or A (ABSTAIN), written
DistilBERT first: `JA` = DistilBERT junk, keywords abstain. Strong and weak are
kept on every ballot.

### 8.2 Decision table

| # | Ballots | Verdict | Notes |
|---|---|---|---|
| 1 | J J | **JUNK** if at least one J is strong; else **KEEP** | Two leanings are not agreement |
| 2 | J A | **JUNK**, DistilBERT's subtype | DistilBERT alone, keywords have no evidence either way |
| 3 | A J | **JUNK** when the keyword junk score ≥ 7 (`voters.keyword.junk_alone_min`); else **KEEP** | Keywords alone, only at a strong score. DistilBERT abstains under 8 words, when not loaded, or between its thresholds |
| 4 | J K, K J | **KEEP** | The voters disagree |
| 5 | K K | **KEEP** | |
| 6 | K A, A K | **KEEP** | |
| 7 | A A | **KEEP** | Nothing could judge the page |

In pass 2 a DistilBERT p(junk) above 0.85 has already been decided by R4 unless
the page is clinical or demographic, and then the keywords vote KEEP. So row 2
in pass 2 is in practice DistilBERT at 0.60–0.85.

### 8.3 Vetoes applied to a JUNK result from the table

Applied in order to rows 1–3. Any veto turns the result into **KEEP**; the
confidence then comes out low by itself (§8.5).

| Veto | Condition | Effect |
|---|---|---|
| **Clinical veto** | C (clinical ≥ 4) | KEEP |
| **Demographic veto** | D and the junk score does not come from record_request / roi_vendors / invoice | KEEP (face sheets, registration) |
| **Handwritten veto** | `handwritten_area_pct` **> 30%** (page-tag model) | KEEP. A page that is mostly handwriting is clinical far more often than admin, and OCR on it is weak. A printed form with a few handwritten fields (≤ 30%) is not protected |

C⁺ never reaches R9 (R7 kept it). So the clinical veto here covers the
moderate band, score 4–7.

### 8.4 Why this table

- **A KEEP against a JUNK always keeps** (row 4). If either method sees a
  clinical or demographic page, the other saying junk is not enough.
- **Two weak JUNKs are not agreement** (row 1, both weak).
- **One voter may junk when the other is silent** (rows 2 and 3): abstention is
  not a KEEP vote. The keywords alone need a strong score, because a junk word
  or two appears on clinical pages too.

### 8.5 Confidence written to the row

The confidence is the evidence for the verdict, not a fixed number. Each
signal is a probability that the verdict is right:

| Signal | Junk verdict | Main verdict | Used when |
|---|---|---|---|
| DistilBERT | p(junk) | p(keep) | it scored the page (loaded, ≥ 8 words; R4 always) |
| Keywords | junk score / 10 | max(clinical, demographic) / 10 | the keyword ballot is J or K; pointing the other way it counts as 1 − that |
| Rule | R5 0.95 · R6 0.97 · R7 0.95 · R8 0.90 | same | R5–R8 |

```
all signals agree      confidence = 1 − Π(1 − p)      two agreeing signals reinforce
any disagreement       confidence = mean(p)           a contested call comes out low
no signal              confidence = 0.50
R1 1.00 · R2 0.99 · R3 0.95                            observations, fixed
```

A signal agrees when its probability is ≥ 0.5; the keywords agree when their
ballot points the verdict's way. The signals are staged as `confidence_from`.

| Page | DistilBERT | Keywords | Verdict | Confidence |
|---|---|---|---|---|
| Fax cover, both junk | 0.80 | 0.60 | Junk | 1 − 0.2 × 0.4 = 0.92 |
| JA, weak DistilBERT | 0.70 | — | Junk | 0.70 |
| AJ, invoice score 9 | — | 0.90 | Junk | 0.90 |
| JK, clinical 4 | p(keep) 0.20 | 0.40 | Main | mean = 0.30 |
| Handwritten veto on JA | p(keep) 0.30 | — | Main | 0.30 |
| Note, both keep | 0.98 | 0.60 | Main | 0.99 |

The numbers are uncalibrated: DistilBERT's softmax is overconfident. Once
reviewed pages are available, fit a temperature on them so 0.80 means right
about 80% of the time.

---

## 9. Subtype

When the verdict is JUNK (R9), the subtype is chosen in this order:

1. The keyword voter's best junk group, if the keyword voter voted JUNK.
2. Else DistilBERT's `top_junk_label`, if BERT voted JUNK.
3. Else Others.

Mapping: fax_cover → Letter/Fax · cover_page → Cover Page · record_request,
roi_vendors → Record Request/Transmittal · invoice → Invoice · placeholder →
Others · cover_stamps → Cover Page. There is no instructions word list, so
Instructions comes only from DistilBERT's label.

---

## 10. Low confidence, never "no verdict"

This is client-facing, so every page gets one verdict: blank, junk (with a
subtype), duplicate or Main. There is no separate review flag and no cap. A
contested call — a veto, a split vote, a weak single voter, a missing model —
gets a low confidence from §8.5 by itself. Filter on low confidence to see the
pages worth a second look.

---

## 11. Pass 1 and pass 2

| | Pass 1 (Tesseract) | Pass 2 (final OCR) |
|---|---|---|
| Pages | All pages. Visual is **decided here** (R8). Handwritten and uncertain pages are deferred | Everything pass 1 deferred; **Visual pages skipped** |
| Rules | R1, R2, R6, R7, R8 | R1–R9 in full (R8 never reached: Visual is already decided) |
| DistilBERT > 0.85 | Not forced (no Provider Signature yet); counts as a strong JUNK ballot | **Forced JUNK** (R4) unless signed |
| Vote | Only **row 1 with both strong** and no veto is final; J A and A J wait for pass 2 | Full table |
| Everything else | Deferred: stays Main, gets final OCR, decided in pass 2 | Final |

Pass 1 only removes pages it is certain about, which saves the stage-5 bill. A
page that pass 1 junks never gets final OCR, so pass 1 must never be wrong
cheaply. DistilBERT force (R4), short page (R5) and signature (R3) wait for pass 2 because they need
coordinates and Provider Signature, which KV extraction writes just before
pass 2.

---

## 12. Degraded runs

| Missing | Behaviour | Recorded |
|---|---|---|
| DistilBERT | Its ballot is A. Only row 3 (keywords ≥ 7) can junk in the vote | `"note": "unavailable"` on the DistilBERT ballot; `/health` → `engine: keywords`; no DistilBERT signal in `confidence_from` |
| Canon fails to load | Last good version (canon_store); first load raises | — |
| Page-tag model | No page is Visual; R8 never fires | `visual_source = "none"` |
| KV extraction (Provider Signature) | R3 uses the canon `signature` group only | `signature_source = "canon_only"` |
| Handwritten area (old 2-class model) | Handwritten veto does not apply | `hw_area = "none"` |
| Coordinates (pass 2 text only) | R5 does not run | `short_page = "no_geometry"` |

---

## 13. Duplicates

Run after the verdict, on kept pages only. The method is unchanged:
normalised text of at least 300 characters, SequenceMatcher ≥ 0.98 or one text
contained in the other, keep the longer page. The window grows to ±5 pages,
and the comparison only runs when the two lengths are within 10% of each other.

---

## 14. Recording

The database row is unchanged: `blank_junk_flag`, `junk_subtype`,
`confidence` (§8.5) and `reason` (the deciding rule, e.g.
`R9 vote JK (DistilBERT, keywords) row 4 -> Main`). No schema change.

Every ballot is staged on disk beside the key/value extraction's
`extraction.json`, at `<chart>/staging/blank_junk.json`. Each pass replaces its
own entries and keeps the other pass's:

```json
{"version": 1, "chart_name": "53688890", "updated_at": "2026-10-15T10:12:03+00:00",
 "pages": {"24.jpg": {"pass_2": {
   "pass": 2, "rule": "R9",
   "bert":  {"vote": "JUNK", "strength": "strong", "p_junk": 0.98, "subtype": "Record Request/Transmittal",
             "label": "JUNK_RECORD_REQUEST"},
   "kw":    {"vote": "KEEP", "strength": "weak", "p_junk": 0.0, "clinical": 5, "junk": 0,
             "demographic": 1, "junk_group": null, "hits": {"clinical.section_headings": ["PLAN"]}},
   "ballots": "JK", "by_voter": {"bert": "J", "kw": "K"},
   "row": 4, "r4_skipped": "clinical",
   "confidence_from": {"bert": 0.02, "keywords": 0.5, "combined": "disagree"},
   "visual_source": "none", "signature_source": "kv_extract", "hw_area": "none",
   "short_page": {"content_bottom": 0.415},
   "verdict": "Main", "confidence": 0.26, "reason": "R9 vote JK (DistilBERT, keywords) row 4 -> Main"}}}}
```

`ballots` is one letter per voter, always in the order DistilBERT, keywords (K = Keep, J = Junk, A = Abstain), so it says who cast which ballot.

`visual_source` is `none` when the page-tag model did not tag the page,
`signature_source` is `canon_only` when the KV signature output was missing,
`short_page` is `no_geometry` when the page had no word boxes, and a model that
was not loaded shows `"note": "unavailable"` on its ballot. A duplicate keeps
the votes of its verdict, plus a `duplicate` entry.

---

## 15. Checked on the local charts (2026-10-15)

Implemented directly, without the shadow-mode step. Run on the three local
charts that have Final OCR (84 pages), against the old verdicts in their
`junk.csv`:

| Result | Pages |
|---|---|
| Same verdict as before | 80 |
| R4 junk, old Main — header / footer only (`Printed … Pg 8 of 10`) | 1 (53688890 p28) |
| R4 junk, old Main — tail of a note (care team, confidentiality footer) | 1 (53688890 p16) |
| Patient-education handouts (fall prevention, first-aid kit): R4 junk until the `patient_education` canon group was added; now Main (clinical 6, confidence 0.60) | 2 (60306165 p27, p29) |
| Face sheet and BCG administration page | kept after R4 was narrowed (vote, confidence 0.60) |

Still to do: compare against `page_ground_truth` (`blank_page`, `junk_page`)
per voter and per rule from `staging/blank_junk.json`, with the number that matters being
clinical pages marked junk; and check R5 by hand on charts run with the
page-tag model.

---

## 16. Open points

1. `advantmed` is listed as an ROI vendor (its own retrieval cover sheets).
   Remove it if Advantmed letterhead appears on clinical pages.
2. Should R5 also run in pass 1 using ink alone? It would save stage-5 cost on
   short pages, but it would lose the signature exemption.
3. DistilBERT force (R4) keeps signed pages. Should a signed page above 85% be
   junked too? Should R4 also run in pass 1, saving the stage-5 bill but before
   Provider Signature is known?
