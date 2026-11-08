# Blank / Junk Logic

As built on 2026-11-08. Code: `core-pipeline/modules/blank_junk/` (`method.py` the ladder,
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
| 7 | Card | An ID or insurance card. The model's p(keep) ≥ 0.99 keeps it | Junk / Cards |
| 8 | Fax | "Fax" in a section header, and rule 4 did not keep the page | Junk / Letter/Fax |
| 9 | Contents page | "Index" / "Contents" / "Table of Contents" as a heading, plus 3+ lines with page numbers | Junk / Others |
| 10 | **Short page** | Content ends in the top 20% of the page. See below | see below |
| 11 | Classifier | The model says Invoice: a money amount **and** an **Invoice Date** or **Due Date** with a date | Main (page type Invoice / Invoice Page) |
|    |  | The model says Invoice, without both | Junk / Others |
|    |  | The model says Invoice, but the card rule nearly matched (or the model says insurance ID) | Junk / Cards |
|    |  | The model says JUNK otherwise (and p(keep) < 0.99) | Junk / its subtype |
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

## Duplicates (pass 2, after the verdict)

These checks compare only Main pages. The result is **Duplicate**, **Partial**, or
nothing; Repeated Content is off.

- **Duplicate:** the two pages are about the same length (the shorter at least 90% of the
  longer), and either the shorter page is contained in the other, ≥ 98% of the words overlap,
  or fewer than 8 distinct words differ.
- **Partial:** 95–98% overlap, or fewer than 15 distinct words differ. Length is not checked,
  so a page inside a much longer one is a Partial at most.

The later page is always the duplicate. Two pages are never matched when:
- their dates of service, or any date written on them, differ;
- a signed page (rule 4) is apart from its look-alike (another Main page between them).

Pass 1 marks only exact copies up to 3 pages apart.

## Continuity

Off for now: continuity no longer turns a junk page inside a Progress Note back into Main.
