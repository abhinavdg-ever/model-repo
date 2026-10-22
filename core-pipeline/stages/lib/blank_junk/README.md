# Blank / junk / duplicate

**The pipeline decides with `method.py`** (stage 3 and 7, `stage.py`): rules R1–R8, then
DistilBERT and the keyword canon vote (R9). Words, weights, thresholds and the
short-page geometry live in `../keyword-canon/blank_junk_context_canon.json`. The method, with
worked examples, is `docs/BLANK_JUNK_LOGIC.md`; the summary is `docs/LOGIC.md` §3.

Duplicates: Main and junk pages (never blank), against a neighbour within ±25 pages; a
Main page is always the original of a junk copy. Similarity is the
average of the word-sequence match (SequenceMatcher over words, autojunk off) and the
word-count match; ≥ 98% or
text wholly contained in it is a duplicate, 95–98% a partial duplicate that stays Main. Pages
under 300 characters never compared; the longer (or earlier) page is kept.

The rest of this file describes the older regex classifier (`classify_junk.py` and the
detector modules) and the former post-model rules (`postprocess.py`). The stage no longer
calls either; they remain for the standalone script below.

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
| **Duplicate** | Yes (Duplicate) | the original's | Main and junk pages (never blank): ≥ 98% similar (average of word-sequence + word-count match) to a ±25 Main neighbor, or text wholly contained in it; pages under 300 chars never compared; longer (or earlier) page kept. 95–98% is Partial: the page stays Main |
| **Main** | No | Not Available | Everything else |

### Priority order
Blank → Cover Page → **clinical Main** → Letter/Fax → Invoice → Record Request → Instructions → Others → Main  
(then Duplicate is applied across the chart)

## Former post-model rules (no longer run by the stage)

`postprocess.py`, formerly after the model or the regex fallback, both passes, in order:
(1) model Blank/Junk with ≥ 2 clinical section headings → Main; (2) model Blank
with text and no word "blank" → Junk (no sub-type); (3) pass 2 only: < 20 words,
not signed, not a clinical note → Junk / Others; (4) Provider Signature = Yes, or
< 20 words with the word "signature" → Main; (5) forced: fax (letter_fax plus
the word "fax") or record request ≥ 3 times → Junk of that sub-type; only
Provider Signature = Yes overrules it, and no other category forces junk; (6) absolute, last: Optum +
Datavant ≥ 3 times in all → Junk / Record Request, no override. Then, at the end
of pass 2 across the chart: (7) a Visual page (page-tag model) ruled Blank or Junk
with the previous or next page kept → Main, unless (5) or (6) applies.
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
