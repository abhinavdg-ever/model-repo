# Blank / Junk Logic

As built on 2026-11-19. Code: `core-pipeline/modules/blank_junk/` (`method.py` the ladder,
`stage.py` the short page and duplicates, `card_rules.py` cards, `pn_headers.py` Progress Note
headers). Numbers: `resources/canon/blank_junk_context_canon.json`; the Progress Note header list:
`resources/canon/progress_note_headers.json` (both editable, reload on change).

Each page gets one verdict: **Main**, **Blank**, or **Junk / subtype** (Cover Page, Cards,
Letter/Fax, Record Request, Instructions, Others). An invoice is a Main page (page type Invoice).

Terms:

- **Electronic Signature** = kv_extract found a provider signature on the page
  (`provider_signature_results`, `signature_present = y`). Words such as "signed" alone never count.
- **Previous / next page** = the nearest page before / after that is not blank.
- **Progress Note header** = a line (or stage-6 section header) of at most 6 words holding one of
  the 36 sections' words, matched loosely: "Orders:", "Orders Placed", "Plan of Care", "Treatment
  Plan", "Diagnosis" / "Diagnoses", "Assessment & Plan", "HPI", "ROS", "Vitals" …. A sentence that
  only mentions a word ("we will plan …") never counts.

The 36 sections (`progress_note_headers.json`): Visit details · Chief complaint · History of present
illness · Review of systems · Past medical history · Problem list · Past surgical history · Family
history · Social history · Social needs (SDOH) · OB/GYN history · Medications · Allergies ·
Immunizations · Preventive care · Screening tools · Vitals · Physical exam · Mental status exam · Lab
results · Imaging and diagnostics · Pathology · Assessment / impression · Visit diagnoses ·
Assessment and plan (combined) · Plan · Orders · Prescriptions and refills · Medication changes ·
Referrals · Procedures done in the visit · Medications given in the visit · Follow-up and next
appointment · Disposition · Patient instructions and education · Medical decision making and time.

---

## Pass 1: on the Tesseract text, rules only (unchanged)

The first rule that matches decides.

| # | Check | Verdict | Confidence |
|---|---|---|---|
| 1 | No ink (stage 1); stage 1 says blank and the OCR has < 15 characters; no text | Blank | 100% |
| 2 | The word "blank" in < 30 characters; "Intentionally left blank" on a page of ≤ 25 words | Blank | 95% |
| 3 | A cover mark (Accept, Unaccept, Cover) or "End of Report" on a page of < 10 words | Junk / Cover Page | 95% |
| 4 | < 15 characters | Junk / Others | 95% |
| — | Anything else | Main for now; pass 2 decides | 40% |

## Pass 2: on the best Final OCR text

**Carried forward.** A page pass 1 called Blank or Junk keeps it; pass 2 does **not** run pass 1's
text rules again (2026-11-19). Exception: a **handwritten (or mixed) page or a bad-quality (Low)
page** that pass 1 never judged gets pass 1's rules once here, on the Final OCR (Tesseract could not
read it). Form and visual pages do not. Measured blanks (no ink, no text) are always checked.

Then, the first rule that matches decides:

| # | Rule | Check | Verdict |
|---|---|---|---|
| 0 | Stage 1 Blank, header only | Stage 1 says Blank, all text in the top 15% header band, no ink below (2026-11-17) | Blank |
| 1 | **Contents page** | "Index" / "Contents" / "Table of Contents" as a heading **in the top third of the page** (2026-11-20, user), **followed by** 3+ entries with page numbers or dates: a line ending in a page number (dot leaders, spaces, or a table cell such as Docling's "\| Progress Note \| 12 \|"), "Page 3" / "Pages 4-7", or a page number on a line of its own. Checked **before** the keeps, because its entries read like a medication list. The next page carrying the list on is Junk too | Junk / Others |
| 2 | Fax cover sheet | "Fax" plus 2+ routing boxes: Urgent, For Review, Please Comment, Please Reply, Please Recycle, As Requested (even when signed). On a page that is not short, "Fax" must be a **section header** (2026-11-19) | Junk / Letter/Fax |
| 3 | **Short page** | see below: decided by S1–S6, else straight to the classifier (rule 8) | see below |
| 4 | Keeps | Visual page under 20 words that is not a card; > 30% handwritten; Electronic Signature not repeated in the same spot on the previous / next page (and p(junk) < 0.95); lab result; medication list (3+ doses); **clinical content**: 2+ ICD-10 codes, a Social History header, or **2+ Progress Note sections** (2026-11-19) | Main |
| 5 | **Card** | on a printed, visual or uncertain page; see below | Junk / Cards |
| 6 | **Fax letter** | "Fax" as a **section header** (2026-11-19, user: a mention of Fax, or a fax number in a letter, is not junk) + a letter's closing (Sincerely, Yours truly, Regards, Respectfully …) and no Electronic Signature; or "Fax" in a section header and no signature. **Never** on a page with note content (a Progress Note header, an ICD-10 code or a medication list): "Fax to:" inside a note is not junk (2026-11-19) | Junk / Letter/Fax |
| 7 | Stage 1 Blank fallback | Stage 1 typed it Blank and nothing above kept it (2026-11-17) | Blank |
| 8 | **Classifier** | The DistilBERT service: JUNK with p(keep) < 0.99 → Junk / its subtype; BLANK → Junk / Others; else Main. A model Invoice is a card only on a card near miss that no form / letter / length guard stopped and no note content (2026-11-19: an anesthesia note read as an invoice was becoming Cards) | its verdict |
| ✓ | **p(keep) 99% check** (2026-11-19, user) | On a page that is **not short**, a rule that would file it **Junk / Letter/Fax or Junk / Cover Page** (rules 2 and 6) stands only while DistilBERT's p(keep) is **under 99%**. At 99% or more the page stays **Main** (rule `KEEP99`). Other junk rules (contents page, cards, the short-page rules, R1 / R2 no-text rules, the classifier) are not overruled. Canon `keep_veto_at` 0.99 | Main |
| 9 | Invoice post-processing | unchanged: a dollar table or invoice wording → Main (Invoice page); a terms page → Junk / Others; a care provider's page with no invoice wording → Main, not an invoice | |

### Rule 3: the short page (2026-11-19)

**Short** = no text line in the middle of the page (25%–75% of the height) and no ink there when
measured: the text sits at the top, at the bottom, or both (canon `contained_page.top_fraction`
0.25, `bottom_fraction` 0.75). A page with no line positions is short only when its content ends in
the top 25%. A page over 30% handwritten, or a visual page, is not judged here (its own keeps apply).

The first that matches decides:

| # | Check | Verdict |
|---|---|---|
| S1 | The **previous page ends in a Progress Note header**: one of its last 3 headers sits in its bottom third. This page carries that section on | Main |
| S2 | The page has a **Progress Note header and 3+ other words** | Main |
| SF | A **fax routing block**: a fax number ("Fax: (330) 555-0142") and a To / Attn / From line, and no note content (Progress Note header, ICD-10 codes, doses). Checked **before S3**, so a provider named on it ("To: Dr Roe") does not keep it (2026-11-19, user) | Junk / Letter/Fax |
| S3 | An **Electronic Signature**, or a **provider name** (kv_extract), **not in the same spot** (same region and the same name or signature sentence) on the previous / next page: a provider line printed on every page does not count. No provider position known: a provider the previous / next page does not have | Main |
| S4 | Its content is **measurements**: glucose / vitamin / lab values with units, doses, ICD-10 codes | Main |
| S5 | **98%+ of its words, in the same order**, are on the previous or next page | Junk / Others |
| S6 | **5 new words or fewer** (not on the previous / next page): all header words → Junk / Others; otherwise → Main | Junk / Main |
| — | More than 5 new words and nothing above | the classifier (rule 8) decides |

### Rule 5: cards (`card_rules.py`)

Unchanged guards: words from two card groups; no form or letter words; ≤ 150 words; text over ≤ 75%
of the page; the word "card" and a name, **unless the evidence is strong**. Changes on 2026-11-19:

- **Strong evidence needs neither the word "card" nor a name**: 3+ insurance groups (Member ID,
  Group Number, Payer ID, RxBIN / RxPCN / RxGRP, payer name, plan words such as PPO, PCP $0),
  3+ back-of-card lines, a Medicare / SSN / passport / green card / military ID, any licence or
  state ID, or a barcode. (A "UCard" front was missed: "card" inside "UCard" is not the word.)
- **Licence back**: 3 of Class, Restrictions, Endorsements, Corrective Lenses, Donor, DLN, with no
  title, is a licence's back.
- **Back of a card**: "If found, please return", "Please return to", "Customer Service:",
  "Pharmacist help desk / use", plus the earlier lines (For Members, Submit claims to, TTY 711 …).
- **Barcode**: the page image is read with `zxing-cpp` (PDF417 on licences, QR and 1-D codes on
  cards; ~1 MB, no model) on printed / visual / uncertain pages of ≤ 150 words. A barcode with any
  card word or licence field makes it a card.

### Confidence (2026-11-19)

The model's raw probability for the verdict's class (Main p(keep), Blank p(blank), Junk p(junk)),
**whichever rule decided** (no fixed 0.30 any more); without the model 40%. Measured blanks 100%;
pass 1 text rules 95%.

## Confidence and codability (2026-11-19)

- **Confidence** = DistilBERT's probability for the class the page was filed under: Main →
  p(keep), Blank → p(blank), Junk → p(junk). Except:
  - a **rule that flips the model** (the model's top class is Keep and a rule files it Junk /
    Blank, or the model says Junk / Blank and a rule keeps it Main): **40%**;
  - a **short page decided by the short rules** (S1–S6, SF): **60%**;
  - no model (pass 1): the rule's own value (pass-1 rule 95%, measured blank 100%, else 40%).
- **Floor**: no confidence, on any field, is shown below **20%** (a rule that overrides a sure
  model used to read 0.7%).
- **Codability**: a Blank or Junk page is **Non-Codable** in Final, with the Blank / Junk
  confidence.

## Duplicates (after the verdict)

Built 2026-11-11 from the user's `Duplicate_Logic.md`. Code: `dup_rules.py` (the rules),
`stage.py` (rows), `continuity/duplicates.py` (the date-of-service block). Settings: canon
`duplicate_logic` (defaults in `dup_rules.DEFAULTS`). One chart at a time; Main pages only.

1. **Prepare.** Header and footer lines are left out: print stamps, "Page x of y", fax lines,
   and header / footer lines (first 4, last 3) found on 3 or more pages. Only the body is
   compared. The score of a pair is the overlap of their distinct words.
2. **Matches.** Each page against its 3 most similar earlier pages. The earlier page is the
   original.
3. **Hard blocks.** Nothing overrides them:

| # | Block | Never a duplicate when |
|---|---|---|
| 1 | Date of service | both are Progress Note pages and their documents' Final DOS differ (applied in continuity) |
| 2 | Length | word counts differ by more than 10% or more than 20 words |
| 3 | Dates and numbers | any differing word is a date, time or number |
| 4 | Signature | the signature blocks differ (signed vs not, or another signer / date) |
| 5 | Short signed page | either page is signed with under 25 words (even if the images match) |
| 6 | Image | either page has under 25 words, or is a form or handwritten, and the page images do not match |

4. **Exact.** The same text (stray single letters and punctuation aside), at any distance.
5. **Within 3 pages** only an exact match counts.
6. **Runs, more than 3 pages apart, in the same order.** Rule A: a page next to a duplicate of
   the run that matches ≥ 98% is a duplicate too, until the run stops growing. Rule B: three
   pages in a row averaging ≥ 95%, none below 90%, are all duplicates.
7. **Conflicts.** Two partial (not exact) matches to one original: neither is a duplicate,
   both are flagged `Review: Conflict` (reason `duplicate_review: Conflict`). Several exact
   copies are all duplicates.
8. **Labels.** **Yes (to page N)** (flag duplicate); **Partial (to page N)** (stored as Copy of N,
   reason `duplicate_content_of_page_id:N`, a normal Main page): **any 100% match that is not a
   duplicate** -- word for word but stopped by a hard block, or the same distinct words not word
   for word (2026-11-15). N is always the earliest copy and links to that page in review-ui.
   "Page 8 out of 11" is pagination, stripped like "Page 8 of 11" (2026-11-15).

Per page, `blank_junk.json` records label, duplicate, copy_of, rule (Exact / Rule A / Rule B),
score, blocked_by and review.

Pass 1 marks exact copies within 3 pages only (pages under 25 words wait for pass 2).
Continuity applies block 1: a blocked exact duplicate becomes Copy of N, a blocked Rule A / B
duplicate a plain Main page; a later continuity run restores it when the dates agree.

Page images match when 90% of their ink (cropped to where the ink is, 96 x 96) lies on the
other's, one pixel of give (`image_min_match`). Not yet calibrated on real charts.

## Continuity

Off for now: continuity no longer turns a junk page inside a Progress Note back into Main.

## Debugging file: the page x page similarity matrix

Every blank / junk pass writes `staging/duplicate_similarity_pass<N>.csv` (2026-11-15): one row per
page with `words` and `distinct_words`, then one column per page (`p1`, `p2` …). A cell is the share of
the **row** page's distinct body words (the duplicate check's body text: header / footer lines and print
stamps out) found on the **column** page. So a page contained in another is 100% one way only. Numbers
only, no page text. Pass 1 reads Tesseract, pass 2 the Final OCR.

