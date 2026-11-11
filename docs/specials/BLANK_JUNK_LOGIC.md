# Blank / Junk Logic

As built on 2026-11-11. Code: `core-pipeline/modules/blank_junk/` (`method.py` the ladder,
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
| 1 | No ink, stage 1 says blank, no text, or the word "blank" in < 30 characters | Blank |
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
| 4 | **Signature** | An Electronic Signature here, **and** none on the previous or next page, **and** the model's p(junk) < 0.95 | Main |
| 5 | Lab result | One word from each group: **Laboratory / Lab Result(s) / Lab Report / Lab Test(s)** (not "Lab" alone) · **Reference Range** (Ref Range, Reference Interval) · **Value(s)** | Main |
| 6 | Clinical content | 2+ ICD-10 codes (I11.0, E11.9…), or a Social History header | Main |
| 7 | Card | An ID or insurance card: words from two card groups, the word "card", no form or letter words, ≤ 150 words, text over ≤ 75% of the page. A name (a Name / Cardholder label, or the name kv_extract found), not needed on strong evidence: 3+ card groups, 3+ back-of-card lines, or a Medicare / other ID card's own lines (a card's back has no name). Section headers do not block. The model's p(keep) ≥ 0.99 keeps it | Junk / Cards |
| 8 | Fax | "Fax" in a section header, and rule 4 did not keep the page | Junk / Letter/Fax |
| 9 | Contents page | "Index" / "Contents" / "Table of Contents" as a heading, plus 3+ lines with page numbers | Junk / Others |
| 10 | **Short page** | Content ends in the top 20% of the page. See below | see below |
| 11 | Classifier | The model says JUNK, but p(keep) ≥ 0.99 | Main |
|    |  | The model says Invoice or insurance ID, and it is a card (the card rule nearly matched, or the model's insurance-ID label) | Junk / Cards |
|    |  | Otherwise Invoice, with a money amount **and** an **Invoice Date** or **Due Date** with a date | Main (page type Invoice / Invoice Page) |
|    |  | Otherwise Invoice | Junk / Others |
|    |  | The model says JUNK of another kind | Junk / its subtype |
|    |  | The model says BLANK | Junk / Others |
|    |  | Otherwise | Main |

### Rule 10: the short page

Rule 10 applies only to a page that got past rules 1–9. A page kept by the signature, lab or
clinical rules is never junked here. The first check that matches decides.

| # | Check | Verdict |
|---|---|---|
| a | **The signature carries on from the page before.** The previous page ends in its Electronic Signature (kv_extract places it in the footer, or its signature sentence is in that page's last paragraph). This page has the provider's name or credentials (kv_extract), the signer's name, credentials (MD, DO, NP…), or a date | Main |
| b | **Fewer than 5 distinct words** that are not on the previous or next page | Junk / Others |
|   | …unless those words are only "Diagnosis" / "Visit Diagnosis" | Main |
| — | Otherwise | the classifier decides (rule 11) |

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
