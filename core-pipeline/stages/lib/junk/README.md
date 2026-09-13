# Junk / blank page classification

Classifies pages from **existing preliminary OCR** (`ocr/<chart>_prelim.txt`) — does **not** re-OCR.

## Categories

| Classification | Blank or Junk | Page Type | What matches |
|----------------|---------------|-----------|--------------|
| **Blank** | Yes (Blank) | Not Available | Empty OCR or &lt; 5 alphanumeric chars; “intentionally blank” |
| **Cover Page** | Yes (Junk) | Cover Page | Accept / Unaccept / Cover page(s) / Discharge Summary with **&lt; 20 words** |
| **Letter/Fax** | Yes (Junk) | Letter/Fax | Cover letter, fax transmission / facsimile / fax cover |
| **Invoice** | Yes (Junk) | Invoice | invoice, amount due, unit price, superbill, remittance, **revenue reconciliation**, … (not bare “price”) |
| **Record Request/Transmittal** | Yes (Junk) | Record Request/Transmittal | request letter; records↔request (any order); attached; transmittal/transmitted; Urgent Request for Records; Your Records requested |
| **Instructions** | Yes (Junk) | Instructions | “what to send”, provide documentation, please send, … |
| **Others** | Yes (Junk) | Others | Table of contents; **&lt; 20 words** and not a signature page; **gibberish Tesseract OCR** (many no-vowel tokens / junk symbols) |
| **Duplicate** | No | Not Available | Same OCR fingerprint as an earlier page (valid content OK) |
| **Main** | No | Not Available | Everything else |

### Priority order
Blank → Cover Page → Letter/Fax → Invoice → Record Request → Instructions → Others → Main  
(then Duplicate is applied across the chart)

## Run

```bash
cd 02-imaging-pipeline/junk-classification
python classify_junk.py
```

```bash
cp output/junk_classification.csv ../../05-imaging-ui/data/pipeline/
```
