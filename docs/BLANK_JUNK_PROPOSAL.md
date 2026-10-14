# Blank / Junk — Current Logic and Proposed Redesign

Status: PROPOSAL for review (2026-10-14). Nothing here is implemented yet.
Sections 1-3 describe the code as it is; sections 4-14 are the proposed design.

---

## 1. Summary

- The repo's blank/junk model code (`core-pipeline/stages/lib/blank_junk/model/src/`) was last synced from b_jnk v3 on 2026-10-12 (commit c274b09). The `b_jnk_v3` zip of 2026-10-14 is **newer**: most model-side differences are improvements the repo does not have yet.
- The repo adds its own layer on top of the model: two passes, a TF-IDF safety net, seven post-model rules, duplicate detection.
- Several of those post-model rules patch false-junk cases that v3 now fixes inside the model.
- The proposal: standardise on three voters (DistilBERT, TF-IDF, a keyword-context voter driven by an editable canon), decide by 2-of-3 with a clinical veto, keep pass 1 conservative, and record every vote.

---

## 2. Current logic (repo)

### 2.1 Passes and inputs

| | Pass 1 | Pass 2 |
|---|---|---|
| Runs after | Preliminary OCR (Tesseract) | Final OCR 1 + 2, section headers, kv_extract |
| Text | Tesseract | Azure DocIntel, else Docling/RapidOCR (prelim not used) |
| Pages | Printed only; handwritten and quality_tag=low skipped | Pages pass 1 did not call blank / junk / duplicate |
| Docling page object | no | only when final2 is empty and final1 is used |

### 2.2 Model decision (vendored b_jnk package)

1. **Route:** empty or structurally empty text → BLANK; picture but no text (Docling only) → KEEP + review.
2. **DistilBERT** (8 labels: KEEP, BLANK, six JUNK_*), probabilities bucketed into KEEP / BLANK / JUNK.
3. **Retention:** a clinical image is always KEEP; a demographic page is KEEP + review unless the model is at least 0.95 sure of a junk label and that label's trigger phrase is on the page.
4. **Keep veto:** p(KEEP) > 0.10 → KEEP + review.
5. **Confidence floor:** best label below 0.85, or page below 0.70 → KEEP + review.
6. **Word rules:** invoice, shell (cover / letter-fax) and end-of-record phrases force JUNK + review.
7. **Subtype:** single trigger wins; otherwise the model's pick if among the triggers, else main purpose; short / gibberish → Others.
8. **TF-IDF safety net (repo only):** BERT says KEEP but TF-IDF p(junk) ≥ 0.60 and not a clinical image → JUNK + review.
9. **Fallbacks:** TF-IDF only when BERT / torch is missing; regex classifier when no model loads.

### 2.3 Clinical-image recognition

Text patterns only (`model/src/preprocessing/protocol.py:112-123`); any match anywhere on the page keeps it and skips the word rules and the safety net:

| Pattern | Example |
|---|---|
| wound + photo / image / tracking / care | Wound care photo |
| dermatology(-ic) + photo / image / lesion | Dermatology lesion |
| lesion + photo / image / capture | Lesion capture |
| endoscopy / endoscopic | the word alone |
| colonoscopy / colonoscopic | the word alone |
| retinal + scan / image / photo | Retinal scan |
| fundus + photo / image / exam | Fundus exam |
| ophthalmolog… | the word alone |
| clinical + photo / image / imaging | Clinical imaging |
| OCT + nerve / mac / scan | OCT scan |
| slit lamp | Slit lamp |

Weak spot: endoscopy, colonoscopy and ophthalmology match on their own, so a fax cover from "XYZ Endoscopy" is always kept. v3 requires photo / image / picture for endoscopy and colonoscopy, and allows the bare specialty name only on pages of 60 words or fewer.

### 2.4 Post-model rules (repo only, both passes, in order)

1. At least 2 clinical headings at line start → Main (0.9).
2. Model said BLANK, page has text and no "blank" word → Junk with **no subtype**.
3. Pass 2 only: Main page under 20 words, not signed, not a clinical note → Others (0.9).
4. Signed, or under 20 words containing "signature" → Main (0.9).
5. Forced junk: letter/fax phrases (incl. "fax") or record-request phrases hit at least 3 times → that subtype (0.95); skipped when signed.
6. Vendor junk: Optum / OptumCare / Datavant at least 3 times → Record Request (0.99), absolute.
7. End of pass 2: a Visual page next to a kept page → Main, unless rule 5 or 6 applies.

### 2.5 Duplicates

Kept (Main) pages only, within ±2 positions, normalised text of at least 300 characters; a match at SequenceMatcher ≥ 0.98 or when one text is contained in the other (stored as 1.0). The longer page is kept (earlier on a tie); chains resolve to the original.

### 2.6 Written to the database

`blank_junk_classification`: `blank_junk_flag` (blank / junk / duplicate / not_blank_junk), `junk_subtype` (Invoice, Cover Page, Record Request/Transmittal, Instructions, Letter/Fax, Others, or NULL), `duplicate_of_page_id`, `ocr_source`, `confidence`, `reason`, `pass_no`, `is_final`. There is **no review column**: the model's review flag survives only as words inside `reason`.

---

## 3. Findings

### 3.1 b_jnk_v3 zip vs repo

| What | v3 zip | Repo | Verdict |
|---|---|---|---|
| TF-IDF safety net | none | p(junk) ≥ 0.60 → Junk | repo's deliberate addition |
| Demographic override for Invoice | needs a billing/card regex match | any trigger may override | both deliberate, different fixes |
| Fax stamps on lab pages as triggers | ignored | counted | v3 newer (repo junks faxed labs) |
| Rule veto: p(keep) ≥ 0.95 and ≥ 3 clinical words cancels word rules | yes | no | v3 newer |
| Admin pages (retrieval paperwork, header-only, contents, placeholder, C-CDA metadata) → Junk | yes | no | v3 newer |
| Always-keep: forms, clinical letters, signature pages, labs, dense values | yes | no | v3 newer |
| Bare endoscopy / ophthalmology keeps any page | short pages only | any page | v3 newer |
| "billing address" not billing; "birth date" demographic | yes | no | v3 newer |
| Two passes, duplicates, post-model rules | n/a | yes | repo pipeline design |

### 3.2 Bugs in the repo

1. **Visual rescue undoes forced junk.** A page pass 1 force-junked gets no final OCR, so pass 2 sees empty text; the "forced junk stays junk" check cannot fire, and the page turns Main next to a kept page.
2. **Faxed clinical pages become Letter/Fax in pass 1 with no way back.** "Fax:" letterhead + fax header + "this facsimile" footer reach 3 hits; pass 1 never knows the page is signed, and a junk page gets no final OCR.
3. **The pass-2 re-keep of signed pass-1 junk never fires** on a fresh run (same empty-text cause).
4. **Silent degradation:** a missing provider-signature CSV switches the signature protections off with nothing recorded in the data.
5. **A corrupt BERT checkpoint falls back to regex**, even when TF-IDF is available.
6. **Junk with NULL subtype** shows "Junk" in DB mode, "Not Available" in Local mode, and the CSV calls it "Main".
7. **Stale docs:** `docs/LOGIC.md` still describes the regex classifier; `PLAN.md` is referenced but missing; CLAUDE.md says quality_tag must not drive logic, yet pass 1 filters on it.

---

## 4. Proposed design — overview

```
Page
 ├─ 0. Route (no vote): empty → BLANK; "intentionally left blank" → JUNK;
 │                      Visual → KEEP (skip pass 2)
 ├─ 1. Voters: DistilBERT · TF-IDF · Keyword context (canon)
 ├─ 2. Hard rules (current rules that still apply) override
 ├─ 3. Decide: 2-of-3 with clinical veto; ties → KEEP
 ├─ 4. Post-processing (pass 2): short top-only page, blank-with-text
 ├─ 5. Duplicates (±5 pages)
 └─ 6. Record every vote (votes JSONB) + review flag
```

Principle: junking a clinical page loses billable content; keeping junk only costs review time. Every tie and abstention goes to KEEP, and a clinical hit blocks JUNK.

---

## 5. Context canon

A new `core-pipeline/stages/lib/keyword-canon/context-canon.json`, reloaded on change by `canon_store.py` like the other canons, so thresholds and terms are tuned without code changes.

```json
{
  "version": 1,
  "thresholds": { "junk": 4, "clinical": 6, "demographic": 4 },
  "contexts": {
    "clinical": {
      "assessment_plan": { "weight": 2, "terms": ["assessment and plan", "a/p", "plan of care"] },
      "vitals":          { "weight": 1, "terms": ["blood pressure", "bp", "pulse", "temp", "spo2", "bmi"] },
      "history":         { "weight": 1, "terms": ["hpi", "history of present illness", "pmh", "ros"] },
      "meds_dx":         { "weight": 1, "terms": ["medications", "diagnosis", "icd-10", "allergies"] },
      "labs":            { "weight": 1, "terms": ["result", "reference range", "hemoglobin", "a1c"] },
      "imaging":         { "weight": 2, "terms": ["wound photo", "fundus exam", "slit lamp", "oct scan"] },
      "signature":       { "weight": 2, "terms": ["electronically signed by", "signed by", "attestation"] }
    },
    "junk": {
      "fax_cover":       { "weight": 1, "zone": "body", "terms": ["fax cover", "number of pages", "this facsimile"] },
      "record_request":  { "weight": 2, "terms": ["request for records", "medical records request", "authorization to release"] },
      "invoice":         { "weight": 2, "terms": ["invoice", "amount due", "remit to", "balance due"] },
      "instructions":    { "weight": 1, "terms": ["instructions", "please complete", "return this form"] },
      "roi_vendors":     { "weight": 3, "terms": ["optum", "datavant", "ciox", "mrocorp", "healthport"] }
    },
    "demographic": {
      "identity":        { "weight": 1, "terms": ["date of birth", "dob", "mrn", "medical record number"] },
      "registration":    { "weight": 2, "terms": ["face sheet", "registration", "guarantor", "emergency contact"] },
      "insurance":       { "weight": 1, "terms": ["insurance", "subscriber", "policy number", "member id"] }
    }
  }
}
```

Rules:

- **Score = sum of weights of distinct terms hit.** "fax" twelve times counts once; repeated letterhead is what makes today's "≥ 3 fax" rule misfire.
- **Zones** (`any` / `header` / `footer` / `body`): pass 2 has line coordinates from Docling and Azure, so header and footer junk terms count only when the body is junk too. Pass 1 (Tesseract text) treats every term as `any`.
- **Provider names:** release-of-information **vendors** (Optum, Datavant, Ciox, …) belong in junk. **Physician names** belong under clinical `signature`: a doctor's name usually marks a real, signed note. The provider-signature CSV and extraction already give the provider per page.
- **Seed** from what exists: v3's always-keep patterns, the current fax / record-request / vendor regexes, and `section_header_canon.json` (curated clinical headings).

---

## 6. Keyword voter

J, C, D = junk, clinical, demographic score at or above its threshold.

| J | C | D | Keyword vote | Subtype |
|---|---|---|---|---|
| yes | no | no | JUNK | best-scoring junk group |
| yes | no | yes | JUNK (see note) | best-scoring junk group |
| yes | yes | any | KEEP | — |
| no | yes | any | KEEP | — |
| no | no | yes | KEEP | — |
| no | no | no | abstain | — |

- **No hits abstains** rather than voting KEEP; the voter only counts when it has evidence.
- **J + D:** emergency-department face sheets and registration forms carry demographics plus "authorization / release" wording and are real chart pages. Suggested: JUNK only when the junk score comes from `record_request` or `roi_vendors`; KEEP otherwise.

---

## 7. Voting

```
0. Route (no vote)
   no text / fewer than N characters        → BLANK
   "intentionally left blank" (± header)    → JUNK (Others, or a new placeholder subtype)
   Visual (quality analysis)                → KEEP; skip pass 2
1. Voters, each KEEP / JUNK (+ subtype) or abstain
   DistilBERT   BLANK with text counts as JUNK
   TF-IDF       p(junk) ≥ 0.60 → JUNK
   Keywords     table in section 6
2. Hard rules first (override the vote)
   ≥ 2 clinical headings → KEEP; signed → KEEP; vendor score ≥ threshold → Record Request
3. Decide
   JUNK  if ≥ 2 voters say JUNK and C is not hit
   KEEP  otherwise
   subtype: keyword subtype if it voted JUNK, else BERT's, else Others
   review:  a 2-1 split, or J and C both hit
```

---

## 8. Pass 1 vs pass 2

| | Pass 1 (Tesseract, printed only) | Pass 2 (final OCR) |
|---|---|---|
| Decides | BLANK; JUNK only when all 3 voters agree, or vendor / record-request is very strong | Full 2-of-3 vote |
| Uncertain | Defer: stays Main, gets final OCR | KEEP + review |
| Visual pages | KEEP, skip pass 2 | — |
| Zones | none (text only) | header / body / footer from coordinates |

A conservative pass 1 removes findings 3.2.1-3.2.3: nothing is junked on Tesseract noise and then never revisited.

---

## 9. Post-processing (pass 2)

1. **Short top-only page → JUNK (Others)** when all hold: the last text line ends in the top ~35% of the page; fewer than ~40 words; not a signature page (signature CSV or clinical `signature` group); clinical score below threshold. **Guard:** skip when the previous page was kept and this page has any clinical hit — a continuation page ("Plan: f/u 2 wks") must stay.
2. **Model said BLANK but the page has text → JUNK, subtype Others.** Never write junk with an empty subtype.

---

## 10. Duplicates

Window ±5 pages (was ±2). Run SequenceMatcher only when the two lengths are within ~10% of each other, so the cost stays flat. Everything else unchanged: kept pages only, at least 300 characters, ≥ 0.98 similar or contained.

---

## 11. Recording and review

- New column `blank_junk_classification.votes JSONB`, e.g.
  `{"bert": "JUNK_LETTER_FAX 0.91", "tfidf": 0.72, "kw": {"junk": 5, "clinical": 2, "demo": 0, "junk_group": "fax_cover"}, "review": true, "signature_source": "csv"}`
- `review` becomes visible in review-ui (chart view and Accuracy filter).
- `signature_source` = `csv` / `extraction` / `missing` makes a degraded run visible (finding 3.2.4).
- Schema change: edit `schema/v1.sql`, every reader and writer, docs, and the "WHAT CHANGED" block (CLAUDE.md §5).

---

## 12. Evaluation before switching

- Run all three voters and the ensemble in **score-only mode** (written to `votes`, flag unchanged).
- Compare against `page_ground_truth` (`blank_page`, `junk_page`): precision / recall for JUNK and BLANK per voter and for the ensemble.
- The key number: **clinical pages marked JUNK** (false junk). Tune canon thresholds until it is close to zero, then switch the flag to the vote.

---

## 13. Open questions

1. "Intentionally left blank": its own subtype, or Others?
2. J + D: always JUNK, or only when the junk comes from record-request / vendor terms?
3. Visual pages: keep with no check, or still junk when the vendor / record-request score is very high (a logo-heavy fax cover can be classed Visual)?
4. Sync `model/src` from the v3 zip first (keeping the repo's `no_trigger_labels=[]` and the safety net)?

---

## 14. Build order

1. Sync model code from the v3 zip (optional, independent).
2. Context canon + keyword voter, score-only.
3. `votes` column + recording; review flag in review-ui.
4. Evaluation against ground truth; tune thresholds.
5. Switch decisions to the vote; conservative pass 1; post-processing rules; duplicate window ±5.
6. Fix the remaining findings (3.2.5-3.2.7) and update `docs/LOGIC.md`.
