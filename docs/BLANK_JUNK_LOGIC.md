# Blank / Junk — logic as implemented

What the code does today, step by step, with every number and the file that
holds it. Paths are relative to `core-pipeline/`.

| | |
|---|---|
| Engine | `stages/lib/blank_junk/method.py` (`decide`) |
| Stage | `stages/lib/blank_junk/stage.py` (pass 1 and pass 2) |
| Settings | `stages/lib/keyword-canon/blank_junk_context_canon.json` (reloads on change) |
| Debug | `<chart>/staging/blank_junk.json`: every page's ballots, scores, rule and reason, both passes |
| Output | `blank_junk_classification` (DB), `imaging/<chart>_junk.csv` |

---

## 1. Summary

Every page gets one verdict: **Main**, **Blank**, or **Junk / subtype**.

1. **Rules R1–R8** run in order; the first that fires decides.
2. If none fires, **R9 — the vote** of two voters (DistilBERT and the keyword canon)
   decides, and then the **vetoes** may turn a Junk back into Main.
3. After the verdict, **duplicates** are found among the Main pages.

It runs **twice**:

| | Pass 1 (stage 3) | Pass 2 (stage 7) |
|---|---|---|
| Text | Tesseract (preliminary OCR) | Final OCR: Azure (Final 2), else Docling (Final 1); **plus the junk words only Tesseract read** |
| Pages | Printed, not low quality | Every page pass 1 did not junk, plus handwritten / mixed / low-quality pages (never judged in pass 1) |
| Word positions | none | yes (for R5 and the header / footer rule) |
| May junk via | R1, R2, R6, R8, and the vote only when **JJ with both strong** | every rule |
| Otherwise | Main, "deferred to final OCR" | final verdict |

**Visual** = the page-tag model says `visual` **and** the page has fewer than **20** words
(canon `visual.max_words`); a visual page with more text is judged like any other page.
A Visual page is decided in pass 1 (R8) and skipped in pass 2. A page the HW stage found **no ink**
on (`blank_page` / `faint_marks_only`) counts as printed for this: pass 1 judges it (R1:
Blank) and final OCR skips it. (Before 2026-10-18 it was skipped by pass 1 as
"handwritten", sent to final OCR, then skipped by pass 2 for having no text: never Blank.) Only pass 1's
survivors (and the handwritten pages) are sent to Final OCR, so pass 1 is cautious.

---

## 2. The evidence

### 2.1 Keyword scores (C, J, D)

Three scores from the canon: **C** clinical, **J** junk, **D** demographic.

- A group's score = **weight × unique terms found**, capped at the group's cap. A
  term repeated counts once. The context score is the sum of its groups.
- Matching is case-insensitive on whole words / phrases; the longest match wins.
- **Neutral** words (fax, phone, tel, letterhead and insurance boilerplate) never count.
- **Header / footer rule (pass 2):** a junk term in the header band (top **12%**) or
  footer band (bottom **8%**) counts only when the body has a junk term too, so a fax
  banner alone does not make a clinical page junk.
- **Tesseract junk words (pass 2):** junk terms that only the Tesseract text had
  (a vendor name in a logo the final OCR dropped, e.g. "Optum") are added as body
  hits, on printed, not-low-quality pages. Listed in `votes.kw.tesseract_only`.
  C and D read the final OCR only.

| Context | Group | Weight | Cap | Junk subtype | Examples |
|---|---|---|---|---|---|
| clinical | section_headings | 2 | 8 | | chief complaint, reason for visit, HPI, assessment, plan |
| clinical | assessment_language | 2 | 6 | | patient presents with, complains of, denies |
| clinical | vitals | 1 | 3 | | blood pressure, heart rate, pulse ox |
| clinical | exam_findings | 1 | 3 | | HEENT, lungs clear, RRR |
| clinical | diagnosis_coding | 2 | 4 | | ICD-10, CPT, HCC |
| clinical | conditions | 1 | 4 | | diabetes, hypertension |
| clinical | medications_orders | 1 | 3 | | mg, tablet, by mouth |
| clinical | labs_results | 1 | 3 | | reference range, result, specimen |
| clinical | imaging_procedures | 2 | 4 | | radiology report, x-ray, MRI |
| clinical | patient_education | 2 | 6 | | patient education, after visit summary |
| clinical | signature | 3 | 6 | | electronically signed by, attestation |
| junk | fax_cover | 2 | 6 | Letter/Fax | fax cover sheet, number of pages |
| junk | cover_page | 2 | 6 | Cover Page | cover sheet, table of contents |
| junk | cover_stamps | 2 | 4 | Cover Page | ACCEPT / UNACCEPT (at a line start) |
| junk | **record_request** | 3 | 9 | Record Request/Transmittal | request for records, release of information |
| junk | **roi_vendors** | 4 | 8 | Record Request/Transmittal | Optum, Datavant, Ciox |
| junk | **invoice** | 3 | 9 | Invoice | invoice number, bill to, amount due |
| junk | **id_card** | 4 | 8 | Others | card #, UCard, RxBIN / RxPCN / RxGRP, for pharmacist, rx claims, insurance card, driver's license, DL # |
| junk | placeholder | 6 | 6 | Others | intentionally left blank |
| demographic | identity | 1 | 3 | | patient name, DOB, sex, MRN |
| demographic | registration | 2 | 6 | | face sheet, registration, guarantor |

**Hard junk groups** (in bold): record_request, roi_vendors, invoice, id_card — retrieval
and billing paperwork, and insurance / ID cards and driver's licences, that a face sheet
never carries. They matter for the demographic rules below: a card prints a name and a
DOB, and those must not rescue it as a face sheet. That is why card wording is a **junk**
group and not part of demographic (demographic is KEEP evidence). Two card terms (8) make
a strong JUNK ballot.

| Threshold (`thresholds`) | Value | Name used below |
|---|---|---|
| clinical | 4 | C |
| clinical_strong | 8 | C⁺ |
| junk | 4 | J |
| junk_strong | 7 | J⁺ |
| demographic | 3 | D |

### 2.2 DistilBERT

An 8-label page model; `p(junk)` = the sum of its JUNK_* labels and BLANK,
`p(keep)` = the rest. Its top junk label gives the subtype (JUNK_INVOICE → Invoice,
JUNK_COVER_PAGE → Cover Page, JUNK_RECORD_REQUEST / JUNK_COVER_REQUEST → Record
Request/Transmittal, JUNK_INSTRUCTIONS → Instructions, JUNK_LETTER_FAX /
JUNK_FAX_TRANSMISSION → Letter/Fax, anything else → Others). Not loaded ⇒ it abstains
and `/health` shows `engine: keywords`.

### 2.3 Other inputs

| Input | From | Used by |
|---|---|---|
| No ink | HW stage (`hw_method` blank_page / faint_marks_only) | R1 |
| Document type `visual` | page-tag model (`ocr_quality_results.document_type`); Visual only with < 20 words | R1, R8 |
| Handwritten area % | page-tag model | handwritten veto (> 30%) |
| Provider Signature = Yes | KV extraction (`provider_signature.csv`), pass 2 | R3 |
| Word positions (lines) | Final OCR JSON, pass 2 | R5, header / footer rule |
| Ink below the short band | the page image, pass 2, short pages only | R5 |

---

## 3. The ladder — first rule that fires decides

| Rule | Condition | Verdict | Pass | Confidence |
|---|---|---|---|---|
| **R1** | No ink; or (not Visual) 0 characters; or the word "blank" in < **30** characters | Blank | 1, 2 | 1.0 |
| **R1** | Not Visual, < **15** characters | Junk / Cover Page when a cover term is there, else Junk / Others | 1, 2 | 1.0 |
| **R2** | "Intentionally left blank" (placeholder group) on ≤ **25** words | Junk / Others | 1, 2 | 0.99 |
| **R3** | Provider Signature = Yes, or a signature phrase — **skipped when the ballots are JJ** (the vote decides; `votes.r3_skipped = "JJ"`) | Main | 2 | 0.95 |
| **R4** | DistilBERT p(junk) **> 0.85** — not when C ≥ 4 or D ≥ 3 (then the vote decides; `votes.r4_skipped`) | Junk / DistilBERT's subtype | 2 | evidence |
| **R5** | Short page (§4), unless **both** voters say KEEP (KK) **and** DistilBERT's p(keep) > **0.8** | Junk / Others | 2 | evidence |
| **R6** | record_request + roi_vendors ≥ **7** and not C⁺ | Junk / Record Request/Transmittal | 1, 2 | evidence |
| **R7** | C⁺ (clinical ≥ 8) | Main | 1, 2 | evidence |
| **R8** | Visual page (`visual`, < **20** words) → Main, unless record_request + roi_vendors + invoice + id_card ≥ **7** → Junk / that group's subtype | Main or Junk | 1 | evidence |
| **R9** | The vote (§5) | | 1, 2 | evidence |

---

## 4. R5 — the short page

For a page whose only content is a header, stamp line or vendor banner at the top.
Pass 2 only (needs word positions). Settings: `short_page` in the canon.

**Step 1 — where does the content end?** (`content_bottom`)
Take the OCR lines, ignoring:
- lines in the bottom **6%** (`ignore_footer_band`),
- running stamps (`ignore_line_patterns`): "Page 3 of 7", a bare page number,
  "Printed: 10/30/2025", a fax transmission line, "Confidential…".

The lowest remaining line's bottom edge, as a share of the page height, is where the
content ends.

**Step 2 — is it short?** Content ends at or above **15%** of the height
(`top_fraction`).

**Step 3 — is there anything below that OCR did not read?** A picture, handwriting, a
signature or a stamp is not text, so the image is measured: in the band from **17% to
94%** of the height (`body_ink_band`), the share of pixels that are dark ink strokes
(paper texture and faint pencil do not count) = `ink_fraction`.

**Step 4 — decide.**

| `ink_fraction` | Meaning | R5 |
|---|---|---|
| < **0.5%** (`body_ink_max_fraction`) | nothing real below | **Junk / Others** |
| ≥ 0.5% | content below | does not fire; the next rules decide |

For scale: a page of body text is 5–15% ink; a signature or stamp is roughly
0.2–0.5%; a speck is under 0.01%.

`votes.short_page` records `content_bottom`, and for a short page `ink`,
`ink_fraction` and `ink_limit`. `"no_geometry"` = no word positions;
`"ink": "not_measured"` = the image could not be read (R5 then does not fire).

A Provider Signature (R3) is checked before R5: a signed short page is kept, unless JJ.

**Both KEEPs, with DistilBERT sure, undo a short page.** When DistilBERT and the keywords
both vote KEEP (KK) and DistilBERT's p(keep) is above **0.8**
(`short_page.undo_bert_keep_above`), R5 is skipped (`votes.short_page.skipped = "KK,
DistilBERT p(keep) 0.91 > 0.8"`) and the later rules / the vote decide. One KEEP, or KK
with DistilBERT at 0.8 or below, is not enough: R5 rules the short page Junk / Others.

---

## 5. R9 — the vote

### 5.1 The two ballots

Each voter casts **J** (junk), **K** (keep) or **A** (abstain), strong or weak.

**DistilBERT** (`voters.bert`) — abstains on a page under **8** words or when not loaded:

| p | Ballot |
|---|---|
| p(junk) ≥ 0.85 | J strong |
| p(junk) ≥ 0.60 | J weak |
| p(keep) ≥ 0.85 | K strong |
| p(keep) ≥ 0.50 | K weak |
| otherwise | A |

**Keywords**, first that applies:

| Scores | Ballot |
|---|---|
| C ≥ 8 | K strong |
| C ≥ 4 | K weak |
| J ≥ 4 and D ≥ 3 and the best junk group is **not** hard | K weak (a face sheet with a fax banner) |
| J ≥ 7 | J strong |
| J ≥ 4 | J weak |
| D ≥ 3 | K weak |
| otherwise | A |

### 5.2 The table

Ballots are written DistilBERT first (`JA` = DistilBERT junk, keywords abstain).

| Row | Ballots | Verdict |
|---|---|---|
| 1 | **J J** | **Junk, whatever the strength** |
| 2 | J A | Junk |
| 3 | A J | Junk when J ≥ **7** (`voters.keyword.junk_alone_min`), else Main |
| 4 | J K, K J | Main |
| 5 | K K | Main |
| 6 | K A, A K | Main |
| 7 | A A | Main |

**Pass 1** acts only on row 1 with **both ballots strong**; every other page stays Main
with "(pass 1: deferred to final OCR)".

### 5.3 The veto — a Junk from the vote turns back to Main

| Veto | Condition | `votes.veto` |
|---|---|---|
| Handwritten | handwritten area **> 30%** of the page | `handwritten` |

There is no clinical or demographic veto: the keyword ballot already covers them. With
C ≥ 4, or D ≥ 3 without hard junk wording, the keywords vote **K**, and a vote with a K
in it (rows 4–6) is never Junk. (Removed 2026-10-18: they could never fire.)

### 5.4 The subtype

The keywords' best junk group's subtype when they voted J, else DistilBERT's, else Others.

| Subtype | Code |
|---|---|
| Invoice | 2 |
| Cover Page | 3 |
| Record Request/Transmittal | 5 |
| Instructions | 6 |
| Others | 7 |
| Letter/Fax | 8 |

(1 = Blank, 4 = Duplicate, 0 = Main.)

---

## 6. Confidence

The evidence for the verdict, never a fixed cap (`_confidence`).

- **R1 = 1.0, R2 = 0.99, R3 = 0.95** (observations).
- Otherwise, from these signals, each a probability that the verdict is right:
  - the rule's own value: R5 0.95, R6 0.97, R7 0.95, R8 0.90;
  - DistilBERT: p(junk) for a Junk verdict, p(keep) for Main (left out under 8 words,
    except in R4);
  - keywords: J ÷ 10 when they voted J; max(C, D) ÷ 10 when they voted K; flipped
    (1 − that) when their vote is against the verdict.
- **All agree** (each ≥ 0.5, keywords on the verdict's side): 1 − ∏(1 − p).
  **Any disagree**: the average. No signal: 0.5.

`votes.confidence_from` lists the signals and `agree` / `disagree`.

---

## 7. Duplicates

After the verdicts, among **Main** pages only (never blank / junk), each page against
its neighbours within **±5** pages, pages of **≥ 300** characters (whitespace stripped):

- **SequenceMatcher ≥ 98%** on lower-cased OCR text (only when the lengths are within
  10%), or
- one page's text **wholly contained** in the other's (confidence 1.0).

The longer page (or the earlier one) is kept; the other is Duplicate (code 4). Review
UI shows Yes at 100%, May Be below; May Be confidence = 1 + (similarity − 1) × 10
(98% → 80%, 99% → 90%).

---

## 8. Worked examples

**66945468_55331450 p25** — an Optum ROI page: content ends at 14.5%, ink below 0.23%,
"Optum" only in the Tesseract text, DOB / MRN / Sex on the page, DistilBERT 0.71.

| Pass | What happens | Verdict |
|---|---|---|
| 1 | DistilBERT J weak, keywords J weak (Optum = 4) → JJ, not both strong | Main, deferred |
| 2 | R5: short (14.5% ≤ 15%), ink 0.23% < 0.5%, not KK | **Junk / Others (R5)**, confidence ≈ 0.99 (R5 0.95, DistilBERT 0.71, keywords 0.4, all agree) |

Without R5 it would still be junk: keywords J weak (Optum from Tesseract; D 3 does not
make them K because roi_vendors is hard), DistilBERT J weak → JJ → Junk; handwriting
3.6%, so no veto.

**A signed ROI letter (67992141 p7)** — Provider Signature = Yes, vendor wording,
DistilBERT junk: the ballots are JJ, so R3 is skipped and the vote makes it Junk /
Record Request (before, R3 kept it Main).

**A progress note with a fax banner** — "Fax cover / pages including cover" in the top
12% only: those junk terms do not count (no junk in the body); C ≥ 4 → keywords K →
Main.

---

## 9. Every setting

| Setting | Value | Where |
|---|---|---|
| Clinical / strong | 4 / 8 | canon `thresholds` |
| Junk / strong | 4 / 7 | canon `thresholds` |
| Demographic | 3 | canon `thresholds` |
| Header / footer band (junk terms) | 12% / 8% | canon `zones` |
| DistilBERT J strong / weak, K strong / weak | 0.85 / 0.60, 0.85 / 0.50 | canon `voters.bert` |
| DistilBERT minimum words | 8 | canon `voters.bert.min_words` |
| R4 force | p(junk) > 0.85 | canon `voters.bert.force_junk_above` |
| A J needs | J ≥ 7 | canon `voters.keyword.junk_alone_min` |
| R5 short page | content ends ≤ 15% | canon `short_page.top_fraction` |
| R5 ink band | 17%–94% of the height | canon `short_page.body_ink_band` |
| R5 ink limit | 0.5% | canon `short_page.body_ink_max_fraction` |
| R5 footer ignored | bottom 6% | canon `short_page.ignore_footer_band` |
| R8 visual veto | hard groups ≥ 7 | canon `visual` |
| Visual word limit | < 20 words | canon `visual.max_words` |
| Handwritten veto | > 30% | canon `handwritten_veto` |
| R1 tiny / "blank" | < 15 / < 30 characters | `method.py` |
| R2 placeholder | ≤ 25 words | `method.py` |
| Hard junk groups | record_request, roi_vendors, invoice, id_card | `method.py` `HARD_JUNK_GROUPS` |
| R5 undo | KK and DistilBERT p(keep) > 0.8 | canon `short_page.undo_bert_keep_above` |
| Duplicates | ≥ 98%, ±5 pages, ≥ 300 characters | `classify.py` |

## 10. Changes, 2026-10-17 / 18

- TF-IDF removed; two voters (DistilBERT, keywords).
- **R3 is skipped when the ballots are JJ** (a signed ROI / transmittal letter is junk).
- **JJ is Junk whatever the strength** (was: one ballot had to be strong). Pass 1 still
  needs both strong.
- **Pass 2 keywords also count the junk terms only Tesseract read** (`tesseract_only`).
- **R5 records its ink** (`ink`, `ink_fraction`, `ink_limit`).
- **R5 ink limit 0.01% → 0.5%**; **short page 15% → 20%**, ink band 17% → 22% start.
- **KK with DistilBERT p(keep) > 0.8 undoes a short page** (`short_page.skipped`); one KEEP does not.
- **New hard junk group `id_card`** (insurance / ID cards, driver's licences), also in the
  Visual (R8) veto.
- **Short page back to 15%** (ink band from 17%), 2026-10-19.
- **The clinical and demographic vetoes are gone** (the keyword ballot covers them);
  the handwritten veto stays.
