# Member Name and DOB: End-to-End Logic

As built on 2026-11-19 (DOB post-processing added the same day). Stage 9 `member_verify`. Code: `core-pipeline/modules/member/`
(`stage.py` the stage, `engine.py` the per-page logic, `extractors/` rules and GLiNER,
`rules/` the decisions, `name_filter.py`). Words and lists: `resources/canon/member_*.json`.
How the Yes / No is shown and scored: review-ui `memberMatch.ts`, `accuracy.ts`.

In one line: for every page, find the **manifest member's** name, DOB and member ID on the page;
the page is **verified** when the name matches and a DOB or ID backs it, **wrong member** when
only other people's names are on it; enough wrong-member pages **reject** the chart.

---

## 1. What goes in

| Input | Where from |
|---|---|
| **The member** (name, first / middle / last, DOB, member ID) | `manifest_member_list`, joined on `record_id = chart_list.chart_name`. Loaded only by db-utilities. Several rows: the most complete one (has an ID, then a DOB, then a middle name, then the lowest id) |
| **Page text** | the best OCR: Final OCR 2 (Azure) → Final OCR 1 (Docling) → Tesseract. Tesseract is not used on a handwritten, mixed or uncertain page, or a low-quality one |
| **Key / value rows** | stage 7 (kv_extract) `staging/extraction.json`: per page, the `name`, `dob` and `member_id` rows with their sentence, value, model score and accepted flag |
| **Blank / junk verdict** | the Final blank / junk flag of each page |

**Name mode.** First, middle and last all present → 3-word mode. First and last → 2-word
mode. Only `member_name` filled → it is split (2 words: first / last; 3+: first / second /
last). No usable name → no page can verify (`manifest_name_incomplete`).

**Pages read.**

| Page | Read? | Counts for the chart? |
|---|---|---|
| Main | yes | yes |
| Blank or Junk (since 2026-11-04) | yes, same logic | no: stored `not_verified`, no Yes / No of its own |
| Duplicate | no: copies the values of the page it duplicates in Final | no |
| No manifest row for the chart | no | chart = needs review, `manifest_missing` (2026-11-10) |

## 2. Finding the name (first that finds one wins)

**A. Rules over the whole page.** Every word that touches a part of the member's name anchors a
search 5 words either side for the best span of name words:

- A part matches when it is the **same word** (any case), the **same initial**, a known
  **nickname** of it (91 groups in `member_name_variants_canon.json`, e.g. Bill / William), a
  two-word part written as one ("Le Blanc" / "LeBlanc"), or a hyphenated surname split by OCR
  ("Miller-Cook").
- **Any order**: "Smith, John" and "John Smith" are the same.
- A span is broken by a number or a form label between the name's words.
- **No spelling tolerance in the pipeline**: one wrong OCR letter is not a match.

| Mode | Match (name OK) | Not a match |
|---|---|---|
| 2-word | both full (**BOTH_FULL**); one full + the other's initial (**INITIAL**) | one full + another full word; anything else |
| 3-word | all three full (**ALL_FULL**); two full, no extra words (**TWO_FULL**) | two full + an extra word; anything else |

**B. GLiNER (only if A found nothing).** Short sentences around name labels (Patient Name,
Member Name, Subscriber Name, Name …) are read by GLiNER for "person" (threshold 0.25). Hits that
are generic words, ages or under 2 characters are dropped; 2–3 name words are kept. The first
hit that matches the member wins, else the highest-scoring one.

**C. The kv_extract rows (2026-11-01 order).**

1. a name row that is the member, or whose text contains the member's name;
2. else the rules run on each name row's sentence; the shortest hit wins;
3. else, if the name so far is not the member, the first accepted name row that looks like a person.

**D. Clean-up.** Words before the member's first and after its last matched part are trimmed.
A value that is an age, or not a person's name and not the member, becomes N/A. "Looks like a
person" (`member_name_filter_canon.json`, 2026-10-28): no digits, at most 4 words, no
run-together words, no word from the never-in-a-name list, not only labels / common words.

## 3. Finding the DOB

| Step | Logic |
|---|---|
| Rules | Every token equal to the manifest's **4-digit year** anchors; the year with the 2 numbers before or after it must be the member's day and month in any of D-M-Y, M-D-Y, Y-M-D, Y-D-M. Leading zeros ignored; **day and month swapped is accepted**. No 2-digit years, no month names. |
| GLiNER (if the rules missed) | sentences around Date of Birth / DOB / Birth Date, labels "date" and "date of birth"; the hit must be the member's date |
| kv_extract rows | a DOB row whose sentence holds the member's date → the member's DOB; else the first accepted DOB row that matches (month names allowed here); **else the first accepted DOB row even if it is another date** |

The DOB is **not told apart from other dates by its label** in the rules: the member's date
anywhere on the page counts.

**Post-processing across the chart (2026-11-19, user; `dob_filter.py`).** After every page has
been read and **before any page is verified**, only DOBs with a **year after 2000** can be dropped:

| # | Check | Result |
|---|---|---|
| 1 | The manifest has a DOB and the page's DOB year is **more than 10 years** from it | dropped |
| 2 | Otherwise: the page's DOB is not the chart's **majority DOB** (the one date most pages read, strictly more than any other, on 2+ pages) | dropped |

The member's own (manifest) DOB and dates up to 2000 are never dropped. A dropped DOB reads
**Not Found**, its key source says why (`dropped: …`), it has no confidence, and it can no
longer back the name in the page verdict. Main pages and blank / junk pages are counted
separately (they are read in two runs).

## 4. Finding the member ID

The manifest ID, exact (any case) with no letter or digit touching it; else GLiNER around ID
labels (Member ID, Subscriber ID, Medicaid ID, MRN, Insurance ID …); else the kv_extract ID row
that matches; **else the first accepted ID row even if it is another ID**.

## 5. The page verdict

**Verified** (`page_verified = true`):

| Mode | Name result | Also needs |
|---|---|---|
| 2-word | BOTH_FULL | a DOB **or** a member ID on the page |
| 2-word | INITIAL | a DOB **and** a member ID |
| 3-word | ALL_FULL or TWO_FULL | a DOB **or** a member ID |
| any | anything else | never verified |

The DOB / ID check is **presence only**: because of the kv_extract fall-backs in 3 and 4, a DOB
or ID that is not the member's can still satisfy it (see 9).

**Wrong member**: the page is not verified, at least one person's name was read on it (GLiNER
people, run again on a failed page, plus the kv_extract names), and **none** of them is the
member. Without GLiNER the kv_extract names still feed this, so wrong member is possible with
NER off.

**Page status**: Verified, else Wrong member, else Not verified.
**Other names** (2026-10-27): on a page whose name is not the member, the other people's names
found on it.

## 6. The chart decision

| Condition | Status | Reason |
|---|---|---|
| no page checked (all blank / junk) | skipped | all_blank_junk |
| wrong-member pages ≥ the threshold | **failed (Reject)** | wrong_member_threshold |
| at least one verified page | verified (Accept) | pages_verified |
| no usable manifest name | needs review | manifest_name_incomplete |
| otherwise | needs review | no_page_verified |

**Reject threshold** (2026-10-28): 10% of the chart's pages (at least 1, at most 5), doubled:
**between 2 and 10 wrong-member pages**. `|ner_disabled` is added to the reason when GLiNER is off.
The chart's "confidence" is an **agreement rate**: pages agreeing with the decision / pages checked.

## 7. Extracted vs Final

Final member values are the **page's own** values: not carried across a document like page
type, DOS or encounter. A duplicate copies its original's values. Blank and junk pages keep
their member values (2026-11-14). So Extracted and Final show the same member value.

## 8. Marking Yes / No

**The pipeline stores no per-field Yes / No.** It stores the values and the page status. The
**(Yes) / (No)** next to Member Name, DOB and ID is worked out in review-ui (`memberMatch.ts`):

| Field | (Yes) when | Otherwise |
|---|---|---|
| Member Name | the value is the manifest member: titles dropped, any order, nicknames, hyphen merge; 3-part name: 2 full words enough; 2-part: 1 full + 1 initial; **one wrong letter allowed in words of 5+ letters**; any extra word → No | **(No)**: another person |
| Member DOB | the exact same day as the manifest DOB (M/D/YYYY read as month / day; **no swap**) | **(No)** |
| Member ID | the manifest ID; a list shows matching IDs first | **(No)** |
| nothing read | **Not Found** (no Yes / No) | |

**Ground truth** for each field is **Yes / No / Not Found** (Skipped or empty is saved as Not
Found, 2026-11-14).

**Accuracy (Accuracy View, `accuracy.ts`).** The pipeline's call per field: nothing read →
Not Found; the member → Yes; anyone else → No. It matches the ground truth when the calls are
equal (Not Found = Not Found matches). Name and DOB are scored; member ID is not. Pages either
side calls blank / junk, or a duplicate, are not scored.

## 9. Known differences and gaps

1. **Pipeline vs review-ui matching.** review-ui allows one wrong letter in a 5+ letter name and
   no day / month swap; the pipeline allows no wrong letter but does accept a swapped day / month.
   A page can therefore be Verified in the pipeline while review-ui shows (No), or the reverse.
2. **Verification on another DOB / ID.** The DOB / ID check is presence only and the kv_extract
   fall-back can fill a non-matching DOB or ID, so a page with the member's name and someone
   else's date can verify. The 2026-11-19 post-processing removes the recent wrong dates (after
   2000); an older wrong date, or another ID, still counts.
3. **The DOB is any occurrence of the member's date**, not only a date labelled DOB.
4. **Excel Compare scores differently.** It compares ground-truth Yes / No with whether the
   pipeline has *any* value. Ground truth "No" with another person's name read is a mismatch
   there and a match in the Accuracy View.
5. The stage's docstring says "no NER → no wrong member"; the kv_extract names make it possible.

Points 1, 2 and 4 are decisions to make, not yet changed.

## 10. Confidence

Three separate numbers, name / DOB / ID (2026-11-19, user):

| Case | Confidence |
|---|---|
| a key / value row KV Extract **accepted and selected** (2026-11-20, user: no other row counts) reads the member value: its value (a name: every word, any order; a DOB: the same date), else its sentence; the highest such row | that row's **rule score × LightGBM model_score** (rule score 0: model_score alone; no model: 0.60) |
| found by searching the whole page (the rules, GLiNER) and no KV row reads it | **0.60** (`rules_find`) |
| **Member ID** (2026-11-19, user) | the **mean** of every ID row KV Extract **accepted and selected**: the IDs the Member ID cell lists |
| nothing read | none |

The page column `member_extraction_results.confidence` is the lowest of the three. Under 0.50 →
the field is in `review_fields`. Full rule: `CONFIDENCE_LOGIC.md`.

## 11. Where it is stored

| Output | Holds |
|---|---|
| `member_extraction_results` (one row per page) | extracted name / DOB / ID, source per field (rule based / ner), key source, page_status, page_verified, confidence, the manifest values, matched member id (verified pages), other_names |
| `member_verification_summary` (one row per chart) | final_status, decision_reason, document_decision (accept / reject), agreement rate, matched member id |
| `imaging/<chart>_member_extraction.csv`, `_member_verification.csv` | the same, rebuilt from the database |
| `imaging_final.confidences` | `member_name`, `member_dob`, `member_id` |

## 12. Settings

| Setting | Default | Effect |
|---|---|---|
| `MEMBER_NER_ENABLED` | true | GLiNER on; off or weights missing → rules and kv_extract only |
| `MEMBER_NER_MODEL_ID` | gliner_medium | which GLiNER checkpoint |
| `MEMBER_NER_MODELS_PATH` | core-pipeline/models/ner | where the checkpoints are |
| `member_keywords_canon.json` | | name / DOB / ID labels, ignore words, labels, non-name words |
| `member_name_filter_canon.json` | | "looks like a person" lists |
| `member_name_variants_canon.json` | | nickname groups (also used by review-ui) |
