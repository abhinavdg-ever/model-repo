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

1. **Rules R1–R8** run in order; the first that fires decides. R5.2
   (DistilBERT p(junk) < 0.05 → Main) is checked right after R1, so nothing can reverse it.
2. If none fires, **R9 — the vote** of two voters (DistilBERT and the keyword canon)
   decides, and then the **vetoes** may turn a Junk back into Main.
3. After the verdict, **duplicates** are found among the Main pages.

It runs **twice**:

| | Pass 1 (stage 3) | Pass 2 (stage 7) |
|---|---|---|
| Text | Tesseract (preliminary OCR) | Final OCR: Azure (Final 2), else Docling (Final 1); **plus the junk words only Tesseract read** |
| Pages | Printed, not low quality | Every page pass 1 did not junk, plus handwritten / mixed / low-quality pages (never judged in pass 1) |
| Word positions | none | yes (for R6 and the header / footer rule) |
| May junk via | R1, R2, R3, R7, and the vote only when **JJ with both strong** | every rule |
| Otherwise | Main, "deferred to final OCR" | final verdict |

**Visual (R3)** — every page the page-tag model calls `visual` is first checked for **card**
wording (Junk / Others if found, whatever its length; vendor / insurer wording only adds weight). Without it, a page under
**20** words (canon `visual.max_words`) is kept; a visual page with more text goes on through
R4–R9 like any other page. A visual page under 20 words is decided in pass 1 and skipped in pass 2. A page the HW stage found **no ink**
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
| clinical | section_headings | 2 | 8 | | chief complaint, reason for visit, HPI, assessment, plan, allergies, immunizations — counted when the line starts with one followed by `:` / `-` / the line end, **or anywhere in a heading line of ≤ 6 words** ("Allergies and Adverse Reactions"; `heading_line_max_words`) |
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
| junk | **id_card** (visual pages only) | 4 | 8 | Others | card #, UCard, member ID, group number, copay, insurance card, driver's license, DL # (no pharmacy wording) |
| junk | placeholder | 6 | 6 | Others | intentionally left blank |
| demographic | identity | 1 | 3 | | patient name, DOB, sex, MRN |
| demographic | registration | 2 | 6 | | face sheet, registration, guarantor |

**Hard junk groups** (in bold): record_request, roi_vendors, invoice, id_card — retrieval
and billing paperwork, and insurance / ID cards and driver's licences, that a face sheet
never carries. They matter for the demographic rules below: a card prints a name and a
DOB, and those must not rescue it as a face sheet. That is why card wording is a **junk**
group and not part of demographic (demographic is KEEP evidence). Two card terms (8) make
a strong JUNK ballot. **id_card counts only on pages the page-tag model calls `visual`**
(canon `document_type`), under the Visual word limit or over it — in the R3 card check and
in the vote; on any other page card wording adds nothing. It also carries insurance-card
wording (member ID, group number, copay, member services…) and card-number patterns:
`Group # …`, `Member ID: W123456789`, `PCP $25`,
`EXP 01/31/2027`, `Card # …`, `DL # …`. Those insurance words stay neutral on every other page.
A second visual-only group, **insurers** (Aetna, Humana, UnitedHealthcare, BCBS…; weight 2,
cap 4, not hard), is **not** card evidence: it adds weight to R3 and the keyword ballot only.

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
| Document type `visual` | page-tag model (`ocr_quality_results.document_type`); Visual only with < 20 words | R1, R3 |
| Handwritten area % | page-tag model | handwritten veto (> 30%) |
| Provider Signature = Yes | KV extraction (`provider_signature.csv`), pass 2 | R4 |
| Word positions (lines) | Final OCR JSON, pass 2 | R6, header / footer rule |
| Ink below the short band | the page image, pass 2, short pages only | R6 |

---

## 3. The ladder — first rule that fires decides

| Rule | Condition | Verdict | Pass | Confidence |
|---|---|---|---|---|
| **R1** | No ink; or (not Visual) 0 characters; or the word "blank" in < **30** characters | Blank | 1, 2 | 1.0 |
| **R1** | Not Visual, < **15** characters | Junk / Cover Page when a cover term is there, else Junk / Others | 1, 2 | 1.0 |
| **R5.2** | DistilBERT p(junk) **< 0.05**, checked right after R1, so **no later rule can reverse it** (not R2, R3, R4, R6, R7, R8 or the vote) | Main | 1, 2 | evidence |
| **R2** | "Intentionally left blank" (placeholder group) on ≤ **25** words | Junk / Others | 1, 2 | 0.99 |
| **R3** | Visual page (`document_type = visual`, any length): **first**, card evidence (an id_card term or card-number pattern) is required; with it, card + roi_vendors + insurers + record_request + invoice ≥ **4** → **Junk / Others**. Vendors and insurers alone never fire it. Else < **20** words → Main. Else the rules below and the vote | Junk / Others, Main, or on | 1, 2 | evidence |
| **R4** | Provider Signature = Yes, or a signature phrase — **skipped when the ballots are JJ** (the vote decides; `votes.r4_skipped = "JJ"`) | Main | 2 | 0.95 |
| **R5.1** | DistilBERT p(junk) **> 0.85** — not when C ≥ 4 or D ≥ 3 (then the vote decides; `votes.r5_skipped`) | Junk / DistilBERT's subtype | 2 | evidence |
| **R6** | Short page (§4), unless **both** voters say KEEP (KK) **and** DistilBERT's KEEP is **strong** (p(keep) ≥ 0.85) | Junk / Others | 2 | evidence |
| **R7** | record_request + roi_vendors ≥ **7** and not C⁺, unless both voters say KEEP (KK) | Junk / Record Request/Transmittal | 1, 2 | evidence |
| **R8** | C⁺ (clinical ≥ 8) | Main | 1, 2 | evidence |
| **R9** | The vote (§5) | | 1, 2 | evidence |

---

## 4. R6 — the short page

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

| `ink_fraction` | Meaning | R6 |
|---|---|---|
| < **0.5%** (`body_ink_max_fraction`) | nothing real below | **Junk / Others** |
| ≥ 0.5% | content below | does not fire; the next rules decide |

For scale: a page of body text is 5–15% ink; a signature or stamp is roughly
0.2–0.5%; a speck is under 0.01%.

`votes.short_page` records `content_bottom`, and for a short page `ink`,
`ink_fraction` and `ink_limit`. `"no_geometry"` = no word positions;
`"ink": "not_measured"` = the image could not be read (R6 then does not fire).

A Provider Signature (R4) is checked before R6: a signed short page is kept, unless JJ.

**Both KEEPs, with DistilBERT sure, undo a short page.** When DistilBERT and the keywords
both vote KEEP (KK) and DistilBERT's KEEP is **strong** (p(keep) ≥ `voters.bert.keep_strong`,
0.85), R6 is skipped (`votes.short_page.skipped = "KK, DistilBERT strong KEEP"`) and the
later rules / the vote decide. One KEEP, or KK with a weak DistilBERT KEEP, is not enough:
R6 rules the short page Junk / Others.

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

- **R1 = 1.0, R2 = 0.99, R4 = 0.95** (observations).
- Otherwise, from these signals, each a probability that the verdict is right:
  - the rule's own value: R3 0.90, R6 0.95, R7 0.97, R8 0.95;
  - DistilBERT: p(junk) for a Junk verdict, p(keep) for Main (left out under 8 words,
    except in R5.1 / R5.2);
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
| 2 | R6: short (14.5% ≤ 15%), ink 0.23% < 0.5%, not KK | **Junk / Others (R6)**, confidence ≈ 0.99 (R6 0.95, DistilBERT 0.71, keywords 0.4, all agree) |

Without R6 it would still be junk: keywords J weak (Optum from Tesseract; D 3 does not
make them K because roi_vendors is hard), DistilBERT J weak → JJ → Junk; handwriting
3.6%, so no veto.

**A signed ROI letter (67992141 p7)** — Provider Signature = Yes, vendor wording,
DistilBERT junk: the ballots are JJ, so R4 is skipped and the vote makes it Junk /
Record Request (before, R4 kept it Main).

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
| R5.1 force junk | p(junk) > 0.85 | canon `voters.bert.force_junk_above` |
| R5.2 force keep | p(junk) < 0.05 | canon `voters.bert.force_keep_below` |
| A J needs | J ≥ 7 | canon `voters.keyword.junk_alone_min` |
| R6 short page | content ends ≤ 15% | canon `short_page.top_fraction` |
| R6 ink band | 17%–94% of the height | canon `short_page.body_ink_band` |
| R6 ink limit | 0.5% | canon `short_page.body_ink_max_fraction` |
| R6 footer ignored | bottom 6% | canon `short_page.ignore_footer_band` |
| R3 card check | an id_card hit, and id_card + roi_vendors + insurers + record_request + invoice ≥ 4 | canon `visual` (`card_group`, `veto_junk_groups`, `veto_min_score`) |
| Visual word limit | < 20 words | canon `visual.max_words` |
| Handwritten veto | > 30% | canon `handwritten_veto` |
| R1 tiny / "blank" | < 15 / < 30 characters | `method.py` |
| R2 placeholder | ≤ 25 words | `method.py` |
| Hard junk groups | record_request, roi_vendors, invoice, id_card | `method.py` `HARD_JUNK_GROUPS` |
| R6 undo | KK with DistilBERT's KEEP strong (p(keep) ≥ 0.85) | canon `voters.bert.keep_strong` |
| Duplicates | ≥ 98%, ±5 pages, ≥ 300 characters | `classify.py` |

## 10. Changes, 2026-10-17 / 18

- TF-IDF removed; two voters (DistilBERT, keywords).
- **R3 is skipped when the ballots are JJ** (a signed ROI / transmittal letter is junk).
- **JJ is Junk whatever the strength** (was: one ballot had to be strong). Pass 1 still
  needs both strong.
- **Pass 2 keywords also count the junk terms only Tesseract read** (`tesseract_only`).
- **R5 records its ink** (`ink`, `ink_fraction`, `ink_limit`).
- **R5 ink limit 0.01% → 0.5%**; **short page 15% → 20%**, ink band 17% → 22% start.
- **KK with a strong DistilBERT KEEP undoes a short page** (`short_page.skipped`); one KEEP does not.
- **Both KEEPs (KK) overrule R6** (`votes.r6_skipped = "KK"`): retrieval wording no longer
  junks a page both voters keep.
- **A heading line (≤ 6 words) containing a section-heading word counts it** —
  "Allergies and Adverse Reactions" scores `allergies`; a long sentence does not.
- **New hard junk group `id_card`** (insurance / ID cards, driver's licences), counted on
  visual pages only, also in the Visual (R8) veto.
- **Short page back to 15%** (ink band from 17%), 2026-10-19.
- **The clinical and demographic vetoes are gone** (the keyword ballot covers them);
  the handwritten veto stays.

## 11. Changes, 2026-10-19 — Visual moves to R3

- **The Visual rule is R3** (was R8, after strong clinical). Old R3–R7 are now R4–R8:
  R4 signature, R5 DistilBERT, R6 short page, R7 retrieval, R8 C⁺. Skip keys follow
  (`r4_skipped` = JJ on a signature, `r5_skipped` = C / D on DistilBERT, `r7_skipped` = KK
  on retrieval). Entries in §10 use the old numbers.
- **Card check first, on every visual page** whatever its length: a card term or pattern
  is required; vendor / insurer / retrieval / invoice wording adds weight (total ≥ **4**; was
  ≥ 7, any hard group, only under 20 words) → **Junk / Others**. It comes before the
  signature, DistilBERT, short page and C⁺ rules.
- **id_card** gains insurance-card wording and card-number patterns; pharmacy wording
  (RxBIN / PCN / RxGRP, for pharmacist, rx claims) is removed — not card evidence; new visual-only
  **insurers** group (weight 2), not card evidence.

## 12. Changes, 2026-10-19 — DistilBERT sure it is not junk keeps the page

- **R5 is split in two: R5.1** = the junk force (p(junk) > 0.85, as before), **R5.2** = the
  keep force below. `votes.rule` and the reason say `R5.1` / `R5.2`.

- **R5.2: p(junk) < 0.05 → Main** (`voters.bert.force_keep_below`), in **both passes**.
  The user trusts the model here: it is checked right after R1 and before R2–R4 and R6–R9,
  so no other rule can reverse it: not the placeholder, the Visual card check, the short
  page, retrieval wording or the vote. R1 still comes first: with no ink or almost no text
  the model has nothing to read.
- Its confidence is the usual evidence (§6): DistilBERT's p(keep), reinforced or averaged
  with the keyword ballot.
