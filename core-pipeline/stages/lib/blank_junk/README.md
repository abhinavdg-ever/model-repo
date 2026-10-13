# Junk / blank page classification

Classifies pages from **existing preliminary OCR** (`ocr/<chart>_prelim.txt`) — does **not** re-OCR.

## Categories

| Classification | Blank or Junk | Page Type | What matches |
|----------------|---------------|-----------|--------------|
| **Blank** | Yes (Blank) | Not Available | Empty OCR or &lt; 5 alphanumeric chars; “intentionally blank” |
| **Cover Page** | Yes (Junk) | Cover Page | Accept / Unaccept / Cover page(s) / Discharge Summary with **&lt; 20 words** |
| **Main (clinical)** | No | Not Available | Progress note / HPI / meds / vitals / … — **short-circuits before junk rules** so a fax confidentiality footer cannot flip them |
| **Letter/Fax** | Yes (Junk) | Letter/Fax | Cover letter, fax transmission / facsimile / fax cover; confidentiality notice only when paired with fax language. |
| **Invoice** | Yes (Junk) | Invoice | invoice, amount due, unit price, superbill, remittance, **revenue reconciliation**, … (not bare “price”; weak phrases need ≥2 hits) |
| **Record Request/Transmittal** | Yes (Junk) | Record Request/Transmittal | request letter; records↔request (any order); attached; transmittal/transmitted; Urgent Request for Records; Your Records requested |
| **Instructions** | Yes (Junk) | Instructions | “what to send”, provide documentation, please send, … |
| **Others** | Yes (Junk) | Others | Table of contents; **&lt; 20 words** and not a signature page; **gibberish Tesseract OCR** (many no-vowel tokens / junk symbols) |
| **Duplicate** | No | Not Available | Main pages only: ≥98% similar OCR text to a ±2 Main neighbor, or text wholly contained in it; pages under 300 chars never compared; longer (or earlier) page kept; UI Yes at 100% / May Be at 98–<100% |
| **Main** | No | Not Available | Everything else |

### Priority order
Blank → Cover Page → **clinical Main** → Letter/Fax → Invoice → Record Request → Instructions → Others → Main  
(then Duplicate is applied across the chart)

## After the model

`postprocess.py`, after the model or the regex fallback, both passes, in order:
(1) model Blank/Junk with ≥ 2 clinical section headings → Main; (2) model Blank
with text and no word "blank" → Junk (no sub-type); (3) pass 2 only: < 20 words,
not signed, not a clinical note → Junk / Others; (4) Provider Signature = Yes, or
< 20 words with the word "signature" → Main; (5) forced: one junk keyword
category (fax/letter, invoice, record request, instructions, table of contents)
≥ 3 times → Junk of that sub-type; only Provider Signature = Yes overrules it.
The regex fallback has no short-page rule of its own: short pages are judged by
(3) only, so pass 1 never junks a page for being short.
Settings in `../keyword-canon/junk_keywords_canon.json`.

## Run

```bash
cd 02-imaging-pipeline/junk-classification
python classify_junk.py
```

```bash
cp output/junk_classification.csv ../../05-imaging-ui/data/pipeline/
```
