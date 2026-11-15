# Blank / Junk Logic

As built on 2026-11-15. Code: `core-pipeline/modules/blank_junk/` (`method.py` the ladder,
`stage.py` the short page and duplicates). Numbers: `resources/canon/blank_junk_context_canon.json`.

Each page gets one verdict: **Main**, **Blank**, or **Junk / subtype**. The subtypes are
Cover Page, Cards, Letter/Fax, Record Request, Instructions and Others. There is no
Junk / Invoice. Every rule is in the ladder below; nothing runs after it.

Two terms used throughout:

- **Electronic Signature** = kv_extract found a provider signature on the page. Stage 7
  (kv_extract) writes `staging/extraction.json`, and from the same results one signature row
  per page (`imaging/<chart>_provider_signature.csv` and `provider_signature_results`). Blank /
  junk reads `signature_present = y`, and the signature's region and sentence, from that row.
  Words such as "signed" or "signature" in the text never count on their own.
- **Previous / next page** = the nearest page before / after that is **not blank**
  (no text, or no ink in stage 1). Blank pages in between are skipped.

---

## Pass 1: on the Tesseract text, rules only

The first rule that matches decides.

| # | Check | Verdict |
|---|---|---|
| 1 | No ink; stage 1 says blank **and** the OCR has < 15 characters (a page typed blank with text on it goes on down the ladder, 2026-11-15); no text; or the word "blank" in < 30 characters | Blank |
| 2 | "Intentionally left blank" on a page of ≤ 25 words | Blank |
| 3 | A cover mark (Accept, Unaccept, Cover) on a page of < 10 words | Junk / Cover Page |
| 4 | "End of Report" on a page of < 10 words | Junk / Cover Page |
| 5 | < 15 characters | Junk / Others |
| — | Anything else | Main for now. Pass 2 decides |

## Pass 2: on the best Final OCR text

The first rule that matches decides.

| # | Rule | Check | Verdict |
|---|---|---|---|
| 1 | Blank | The pass 1 rules 1–5 again | as in pass 1 |
| 2 | Visual | A visual page under 20 words that is not a card | Main |
| 3 | Handwritten | More than 30% handwritten | Main |
| 3b | Fax cover sheet | The word **Fax** and 2+ of its routing boxes: **Urgent, For (Your) Review, Please Comment, Please Reply / Reply ASAP, Please Recycle, As Requested** — even when signed (a cover sheet lists doctors) (2026-11-15) | Junk / Letter/Fax |
| 4 | **Signature** | An Electronic Signature here, **and** not the same signature in the same spot on the previous or next page (2026-11-15), **and** the model's p(junk) < 0.95 | Main |
| 5 | Lab result | One word from each group: **Laboratory / Lab Result(s) / Lab Report / Lab Test(s)** (not "Lab" alone) · **Reference Range** (Ref Range, Reference Interval) · **Value(s)** | Main |
| 5b | Medication list | 3+ different doses (a number + mg, mcg, mL, g, units, mEq, tablets …), or a Medications / Rx / Prescription word and 2+ (2026-11-15) | Main |
| 6 | Clinical content | 2+ ICD-10 codes (I11.0, E11.9…), or a Social History header | Main |
| 7 | Card | On a page typed printed, visual or uncertain (uncertain since 2026-11-15): an ID or insurance card: words from two card groups, the word "card", no form or letter words, ≤ 150 words, text over ≤ 75% of the page. A name (a Name / Cardholder label, or the name kv_extract found), not needed on strong evidence: 3+ card groups, 3+ back-of-card lines, or a Medicare / other ID card's own lines (a card's back has no name). Section headers do not block. The model's p(keep) ≥ 0.99 keeps it | Junk / Cards |
| 8 | Fax | "Fax" in a section header, and rule 4 did not keep the page | Junk / Letter/Fax |
| 9 | Contents page | "Index" / "Contents" / "Table of Contents" as a heading, plus 3+ lines with page numbers **or dates** ("Encounter: 4/14/2026 9:15 AM", 2026-11-15). The next page carrying the list on (2+ entry lines, ≤ 120 distinct words, nothing kept it earlier) is junk too | Junk / Others |
| 10 | **Short page** | Content ends in the top 20% of the page. See below | see below |
| 11 | Classifier | The model says JUNK, but p(keep) ≥ 0.99 | Main |
|    |  | The model says Invoice or insurance ID, and it is a card (the card rule nearly matched, or the model's insurance-ID label). Not a near miss on a hospital / clinic page without the word "card" (a Patient Abstract: Account Number, copay), which goes on to the invoice checks (2026-11-15) | Junk / Cards |
|    |  | Otherwise Invoice, with a **table holding a dollar amount** (Final OCR 1 / Docling markdown `\| … \| $25.00 \|`, else the Final OCR text, an HTML `<td>$…</td>` too). Terms wording cannot overrule it (2026-11-14) | Main (page type Invoice / Invoice Page) |
|    |  | Otherwise Invoice, on a terms page: **T&C**, **Terms and Conditions**, **Payment Terms**, **Payment Instructions**, Terms of Payment / Sale / Service, General / Standard Terms, appearing **before** the first invoice wording (Invoice, Bill To, Amount / Balance Due, Total, a $ amount) or on a page with none (Final OCR text, else Tesseract's) | Junk / Others |
|    |  | Otherwise Invoice with **no invoice wording** but a care provider's name: **Surgery Center**, Surgical Center, Ambulatory Surgery, **Hospital**, **Clinic**, Medical Center, Health Center (2026-11-15) | Main, **not** an invoice: page classification decides |
|    |  | Otherwise Invoice (invoice wording; no terms, or the terms come after the invoice, e.g. "Please refer to the Payment Terms & Conditions on next page") | Main (page type Invoice / Invoice Page) |
|    |  | The model says JUNK of another kind | Junk / its subtype |
|    |  | The model says BLANK | Junk / Others |
|    |  | Otherwise | Main |

### Rule 10: the short page

Rule 10 applies only to a page that got past rules 1–9. A page kept by the signature, lab or
clinical rules is never junked here. The first check that matches decides.

| # | Check | Verdict |
|---|---|---|
| a | **The signature carries on from the page before.** The previous page ends in its Electronic Signature (kv_extract places it in the footer, or its signature sentence is in that page's last paragraph). This page has the provider's name or credentials (kv_extract), the signer's name, credentials (MD, DO, NP…), or a date | Main |
| b | **Only a section header differs**: every word not on the previous or next page belongs to this page's own section header(s), "Visit Diagnosis" included (2026-11-15) | Junk / Others |
| c | **The section carries on from the page before.** Fewer than 5 new words, some of them not header words, and the **previous page's last section header** is Visit Diagnosis, Encounter, Summary, or any standard Progress Note header (the `clinical` list of `section_header_classes.json`: Chief Complaint, HPI, Medications, Assessment, Plan, Vitals …). Canon `contained_page.continue_after_headers` + `use_clinical_headers`, editable (2026-11-15) | Main |
| d | Otherwise **fewer than 5 distinct words** that are not on the previous or next page | Junk / Others |
| — | Otherwise | the classifier decides (rule 11) |

**A repeated signature does not count** (2026-11-15, every page): when its Electronic Signature sits in
the same spot (same region: header / mid / footer, and the same signature sentence or signer) as the
signature on the previous or next non-blank page, it is a stamp the record repeats: no signature keep
(rule 4), and check a (signature carried on) ignores a page before whose signature is such a stamp. This
replaces "the previous or next page is signed too" (2026-11-08): a neighbour signed elsewhere or by
someone else no longer stops the keep.

**A page that adds nothing** (2026-11-15), short or not: every one of its distinct words (at most 100,
canon `contained_page.repeated_only_max_words`) is on the previous or next page, e.g. page 6 of 6 holding
only the running header and the end-of-report block page 5 also has, low on the page -> Junk / Others.

An invoice page is never junked by the short page rule.

---

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
8. **Labels.** **Duplicate of N** (flag duplicate); **Copy of N** (a 100% text match a block
   stopped: a normal Main page, reason `duplicate_content_of_page_id:N`). N is always the
   earliest copy and links to that page in review-ui. There are no Partials.

Per page, `blank_junk.json` records label, duplicate, copy_of, rule (Exact / Rule A / Rule B),
score, blocked_by and review.

Pass 1 marks exact copies within 3 pages only (pages under 25 words wait for pass 2).
Continuity applies block 1: a blocked exact duplicate becomes Copy of N, a blocked Rule A / B
duplicate a plain Main page; a later continuity run restores it when the dates agree.

Page images match when 90% of their ink (cropped to where the ink is, 96 x 96) lies on the
other's, one pixel of give (`image_min_match`). Not yet calibrated on real charts.

## Continuity

Off for now: continuity no longer turns a junk page inside a Progress Note back into Main.
