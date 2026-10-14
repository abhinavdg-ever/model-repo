# Blank / Junk — Methodology

Status: PROPOSAL (2026-10-14), written from scratch. Nothing here is implemented.
Canon: `core-pipeline/stages/lib/keyword-canon/blank_junk_context_canon.json`.

---

## 1. Principle

A clinical page that is junked loses billable content and cannot be recovered
downstream. A junk page that is kept only costs a reviewer a few seconds.
So:

- Every tie, abstention and missing signal resolves to **KEEP**.
- JUNK needs **agreement**: at least two independent methods saying JUNK and
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
| TF-IDF | `tfidf_flat.joblib`, p(junk) | yes | yes |
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
| **clinical** | section_headings, assessment_language, vitals, exam_findings, diagnosis_coding, conditions, medications_orders, labs_results, imaging_procedures, signature | The page is part of a medical record |
| **junk** | fax_cover, cover_page, cover_stamps (Accept / Unaccept), record_request, roi_vendors, invoice, placeholder | The page is retrieval or billing paperwork; each group names the junk subtype |
| **demographic** | identity, registration | A face sheet or registration page: administrative-looking, but a chart page |
| **neutral** | fax, phone, page, confidential, PHI, and all insurance wording | Never scored: it appears on every kind of page |

Two lists are deliberately absent:

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

## 5. The three voters

Each voter returns **JUNK**, **KEEP** or **ABSTAIN**, a strength
(**strong** / **weak**) and, for JUNK, a subtype. The voters are independent:
none reads another's output.

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

### 5.2 TF-IDF (lexical statistics)

| Condition | Vote | Strength |
|---|---|---|
| p_junk ≥ 0.85 | JUNK | strong |
| 0.60 ≤ p_junk < 0.85 | JUNK | weak |
| p_junk ≤ 0.15 | KEEP | strong |
| 0.15 < p_junk ≤ 0.40 | KEEP | weak |
| 0.40 < p_junk < 0.60 | ABSTAIN | — |

Subtype: none of its own. It agrees or disagrees on JUNK, and the subtype comes
from the others (§8).

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

"—" means below threshold. When J and C both hit, the keyword voter says KEEP
and the page is flagged for review (§9). The best junk group is the group with
the highest score; a tie goes to the group with the higher weight.

The keyword voter **abstains** when it has no evidence; it does not vote KEEP.
A page with no hits is decided by the two models.

---

## 6. Precedence ladder

Rules run top to bottom. The first rule that fires decides, and nothing below
it runs. Rules R1–R8 are deterministic; R9 is the vote.

| # | Rule | Verdict | Overrides |
|---|---|---|---|
| **R1** | **Blank.** No ink above the blank threshold (`blank_page` / `faint_marks_only` from the HW stage), **or** fewer than 15 non-space OCR characters and the page is not Visual | `blank` | everything |
| **R2** | **Placeholder.** A `placeholder` term ("intentionally left blank") and 25 words or fewer | `junk` / Others | everything below |
| **R3** | **Signature.** Provider Signature = Yes from KV extraction (`kv_extract`, seq 56, runs before pass 2), or the clinical `signature` group hits in the body | `not_blank_junk` | R4–R9 |
| **R4** | **DistilBERT force.** DistilBERT p(junk) **> 0.85** (Σ JUNK_* + BLANK), pass 2. Nothing below can overrule it: not the clinical veto, not strong clinical, not the vote | `junk` / DistilBERT's top junk label | R5–R9 |
| **R5** | **Short page.** Content ends in the top **15%** of page height (§7). Fires **even when the page carries a clinical heading**, and **whatever the neighbouring pages are**: no adjacency check | `junk` / Others | R6–R9, including the clinical veto |
| **R6** | **Junk counter-veto.** record_request + roi_vendors ≥ 7 (J⁺ from retrieval terms alone) and not C⁺ | `junk` / Record Request/Transmittal | R7–R9 |
| **R7** | **Strong clinical.** C⁺ (score ≥ 8) | `not_blank_junk` | R8–R9 |
| **R8** | **Visual.** `document_type = 'visual'`. Decided in **pass 1**; pass 2 is skipped for these pages | `not_blank_junk`, unless invoice / record_request / roi_vendors ≥ 7, then `junk` with that group's subtype | R9 |
| **R9** | **Vote** (§8) | per §8 | — |

Why this order:

- **DistilBERT force (R4).** Above 85% the model is trusted outright.
  Only blank, placeholder and signature sit above it. A signed page stays kept,
  for the same reason it is exempt from the short-page rule (next point).
  Because of R4, a DistilBERT JUNK ballot that reaches the vote (R9) is always
  weak (0.60–0.85); its strong JUNK band in §5.1 is used only in pass 1.
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
   matching `ignore_line_patterns`: "Page 2 of 5", a bare number, fax
   transmission stamps, "Printed on …", "Confidential…".
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

## 8. How the three voters agree (R9)

### 8.1 Counting

Each voter casts one ballot: J (JUNK), K (KEEP) or A (ABSTAIN). Strong and
weak are kept on every ballot. With three voters there are ten possible
ballot sets.

### 8.2 Decision table

`s` = strong, `w` = weak. "C" = the keyword clinical score is at or above 4.

| # | Ballots | Verdict | Review | Notes |
|---|---|---|---|---|
| 1 | J J J | **JUNK** | no; yes if any J is weak or C | Unanimous |
| 2 | J J K | **JUNK** only if both J are strong **and** the K is weak; else **KEEP** | yes | One dissent. A strong KEEP blocks |
| 3 | J J A | **JUNK** if at least one J is strong; else **KEEP** | yes if any J is weak | Two agree, one silent |
| 4 | J K K | **KEEP** | yes if the J is strong | Majority keep |
| 5 | J K A | **KEEP** | yes | Split, no majority |
| 6 | J A A | **KEEP** | yes if the J is strong | One voice is not agreement |
| 7 | K K K | **KEEP** | no | |
| 8 | K K A | **KEEP** | no | |
| 9 | K A A | **KEEP** | no | |
| 10 | A A A | **KEEP** | yes | Nothing could judge the page |

### 8.3 Vetoes applied to a JUNK result from the table

Applied in order to rows 1–3. Any veto turns the result into **KEEP + review**.

| Veto | Condition | Effect |
|---|---|---|
| **Clinical veto** | C (clinical ≥ 4) | KEEP + review. Even 3 strong JUNK ballots are blocked |
| **Demographic veto** | D and the junk score does not come from record_request / roi_vendors / invoice | KEEP + review (face sheets, registration) |
| **Handwritten veto** | `handwritten_area_pct` **> 30%** (page-tag model) | KEEP + review. A page that is mostly handwriting is clinical far more often than admin, and OCR on it is weak. A printed form with a few handwritten fields (≤ 30%) is not protected |

C⁺ never reaches R9 (R7 kept it). So the clinical veto here covers the
moderate band, score 4–7.

### 8.4 Why two-of-three, and why strength matters

- **Independence.** DistilBERT reads meaning, TF-IDF reads word statistics, and
  the canon reads curated phrases. They fail differently: BERT is fooled by
  long letters, TF-IDF by vocabulary overlap, and keywords by OCR noise.
  Agreement between two of them is much rarer by chance than either alone.
- **A strong KEEP is a veto in all but name** (row 2). If any method is
  confident the page is clinical, two other methods saying junk is not enough.
- **Two weak JUNKs are not agreement** (row 3 with both weak, and row 2). Weak
  means "leaning". Two leanings plus a silent third stays KEEP and goes to
  review.
- **Abstention is not a KEEP vote.** It removes the voter. That is why row 6
  (one J, two silent) keeps the page: the only evidence is one voter.

### 8.5 Confidence written to the row

```
JUNK:  confidence = mean(p_junk of the voters that voted JUNK)
       (keyword p = min(1, junk_score / 10))
KEEP:  confidence = mean(1 − p_junk of the voters that voted KEEP), or 0.50 if none did
Rules R1–R8: fixed values — R1 1.00, R2 0.99, R3 0.95, R5 0.95, R6 0.97, R7 0.95, R8 0.90; R4 = DistilBERT's p(junk)
```

---

## 9. Subtype

When the verdict is JUNK (R9), the subtype is chosen in this order:

1. The keyword voter's best junk group, if the keyword voter voted JUNK.
2. Else DistilBERT's `top_junk_label`, if BERT voted JUNK.
3. Else Others.

Flag for review when keyword and BERT both voted JUNK with **different**
subtypes and both were strong.

Mapping: fax_cover → Letter/Fax · cover_page → Cover Page · record_request,
roi_vendors → Record Request/Transmittal · invoice → Invoice · placeholder →
Others · cover_stamps → Cover Page. There is no instructions word list, so
Instructions comes only from DistilBERT's label.

---

## 10. Review flag

Review is set when:

- the table in §8.2 sets it,
- any veto in §8.3 fired,
- J and C both hit,
- subtypes disagree (§9),
- a voter was unavailable (§12).

---

## 11. Pass 1 and pass 2

| | Pass 1 (Tesseract) | Pass 2 (final OCR) |
|---|---|---|
| Pages | All pages. Visual is **decided here** (R8). Handwritten and uncertain pages are deferred | Everything pass 1 deferred; **Visual pages skipped** |
| Rules | R1, R2, R6, R7, R8 | R1–R9 in full (R8 never reached: Visual is already decided) |
| DistilBERT > 0.85 | Not forced (no Provider Signature yet); counts as a strong JUNK ballot | **Forced JUNK** (R4) unless signed |
| Vote | Only **row 1 with all three strong** and no veto is final | Full table |
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
| DistilBERT | Its ballot is A. Two voters remain, so JUNK needs both, and both strong | `votes.bert = "unavailable"`, review |
| TF-IDF | Same | `votes.tfidf = "unavailable"`, review |
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

New column `blank_junk_classification.votes JSONB`:

```json
{
  "rule": "R9",
  "bert":  {"vote": "JUNK", "strength": "strong", "p_junk": 0.91, "label": "JUNK_LETTER_FAX"},
  "tfidf": {"vote": "JUNK", "strength": "weak",   "p_junk": 0.68},
  "kw":    {"vote": "KEEP", "strength": "weak", "junk": 6, "clinical": 5, "demographic": 0,
            "junk_group": "fax_cover", "hits": {"clinical.section_headings": ["assessment", "plan"]}},
  "ballots": "JJK", "row": 2, "veto": "clinical",
  "visual_source": "page_tags", "signature_source": "kv_extract", "hw_area_pct": 4.2,
  "short_page": {"content_bottom": 0.41},
  "review": true
}
```

Schema change per CLAUDE.md §5: edit `schema/v1.sql`, readers and writers,
docs, and the WHAT CHANGED block in `v2.sql`. review-ui shows `review` and the
ballots on the page panel.

---

## 15. Evaluation before switching

1. **Shadow mode:** compute and write `votes`, but leave `blank_junk_flag` as it
   is today.
2. Compare against `page_ground_truth` (`blank_page`, `junk_page`) for each
   voter, each rule and the ensemble.
3. The number that matters is **clinical pages marked junk**. Tune canon
   weights and thresholds until it is close to zero, then report junk recall.
4. Check R5 separately: every page it fires on, by hand, on at least 3 charts
   run with the page-tag model.
5. Switch the flag to the new verdict.

---

## 16. Open points

1. `advantmed` is listed as an ROI vendor (its own retrieval cover sheets).
   Remove it if Advantmed letterhead appears on clinical pages.
2. Should R5 also run in pass 1 using ink alone? It would save stage-5 cost on
   short pages, but it would lose the signature exemption.
3. DistilBERT force (R4) keeps signed pages. Should a signed page above 85% be
   junked too? Should R4 also run in pass 1, saving the stage-5 bill but before
   Provider Signature is known?
