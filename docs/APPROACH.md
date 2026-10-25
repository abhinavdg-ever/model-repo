# Page classification, continuity, encounter type and page sequencing: the approach

Taxonomy **v29.2** (the version lives inside the files, as `taxonomy_version`; no file
or folder name carries a version). Written 2026-10-25.

This is the one document for how a chart's pages are classified, grouped into
documents and encounters, given an encounter type and put in reading order: what each step reads, how it
decides, what it stores, and how the models behind it are trained. The canon each step
uses is reproduced in full in the [appendix](#appendix-the-canon-taxonomy-v292),
rendered from the JSON files.

Every step produces two values, and both are kept:

| | Decided from | Changes with neighbours? | Example |
|---|---|---|---|
| **Extracted** | the page alone | never | a page that is wholly a pathology result → Lab Report > Pathology Report |
| **Final** | the document (or encounter) the page sits in | yes | that page inside an office note → Progress Note > Lab and Imaging Results |

Status markers used below: **[built]** in the code (the page-axes model itself still
to be trained and copied in); **[to build]** agreed design, not yet written;
**[decide]** waiting on a client decision.

---

## 1. The order of work

```
 per page                                         per document / encounter
 ───────────────────────────────────────────      ───────────────────────────────────
  OCR (Tesseract, RapidOCR, Azure)
  blank / junk / duplicate
  member verification
  PAGE CLASSIFICATION — Extracted ─────┐
     best 5 paths, canon, Others       │
  DATE OF SERVICE — Extracted          ├──▶  CONTINUITY: where documents begin and end
  ENCOUNTER TYPE — Extracted ──────────┘        (gaps scored from the best paths, 
     rules + encounter model                     encounter change, layout, dates, ...)
                                                    │
                                                    ▼
                                             PAGE CLASSIFICATION — Final
                                             DATE OF SERVICE — Final
                                             ENCOUNTER TYPE — Final (encounter vote)
                                                    │
                                                    ▼
                                             sequencing, imaging_final
```

**Change from today [to build]:** the encounter stage runs after continuity and does
both jobs. It splits in two: the page's own vote (Extracted) moves **before**
continuity, so a change of encounter type can count toward a break; the encounter vote
(Final) stays after it.

---

## 2. The taxonomy

* **17 L1**: Progress Note, Lab Report, Operative Note, Procedure Note, Discharge Summary,
  Clinical Summary, Consent and Authorization, Demographics, Immunization, Medication
  Record, Order and Requisition, Problem List, Forms and Questionnaires, Provider Letter,
  Patient Communication, Administrative, Others.
* **L2** under each L1 (Others last); **L3** only under the four Lab Report L2s (Lab
  Result, Radiology Report, Pathology Report, Diagnostic Study).
* Under **Progress Note** the L2 is the *content section* on the page (CC/HPI, Assessment
  and Plan, Vitals, Lab and Imaging Results, Medication List, ...), decided by section
  tagging, not by the model.
* **Specialty** is a separate axis (34 values incl. Others and Unknown). In the training
  labels Unknown means *not determined*, not "no specialty"; the pipeline never outputs
  it: a page with no specialty signal reads **General** (3.1).
* **Codeability**: codeable, non_codeable, discharge, needs_review.

Full lists with descriptions: appendix A. Renames from the 23 Oct version: appendix B.

**One taxonomy, everywhere [built].** The pipeline (`keyword-canon/page_taxonomy.json`),
the trainer (`training/bert-training/taxonomy.json`), the annotation tool and the
training copy (`training/taxonomy/taxonomy.json`) carry the same file. The v24 taxonomy
and the v1 ladder's page-type list are deleted; the keyword canon is converted once to
v29.2 paths (`page_canon.json`), keeping its lexicons. Nothing at run time translates
an old name.

### The page-essence rule (labels, and therefore Extracted)

A page is labelled by what the page itself holds, because the model sees one page at a
time:

* A page that is **wholly** lab results, imaging results, a medication list or an order
  list carries that class (Lab Report / Medication Record / Order and Requisition) even
  inside an office note. Headers, footers, a signature block, an allergy list or a short
  problem list do not change that.
* Only a **split page**, one that also carries note content (HPI, interval history, ROS,
  exam, assessment, plan), is Progress Note, with the L2 Lab and Imaging Results or
  Medication List.

Putting pages back into their documents is the Final step's job (section 3.2).

---

## 3. Page classification

### 3.1 Extracted: one page at a time [built]

```
 page text ──▶ page-axes model: p(L1), p(L2 | L1), p(L3 | L2) for every class
           └─▶ keyword canon: the canon terms on the page, each weighted per level
                    │
                    ▼
   at each level (L1; L2 under the winner; L3 under that):
      a strong canon term as the page's title? ──yes──▶ its class wins the level
      else  vote = (1 − k) × model + k × keywords; winner takes it if vote ≥ 0.2, else Others
                    │
                    ▼
   rules that look at more than words (lab guard, hospital stay, clinical summary)
                    │
                    ▼
   Extracted L1 / L2 / L3 + the vote's 5 best whole paths (every candidate kept)
```

**Both vote, at every level [built 2026-10-25].** The page-axes model (one DistilBERT
encoder, hierarchical heads: the L2 head masked to the L1's L2s, the L3 head to the L2's
L3s) gives a probability for every class at every level. The keyword canon gives each
class a score from the canon terms on the page. At L1, then L2 under the winning L1, then
L3 under the winning L2:

```
vote(class) = (1 − k) × p_model(class) + k × keywords(class) / Σ keywords
k           = share[level] × min(1, Σ keywords / full[level])
```

The winner takes the level when its vote is at least **0.2** (the minimum the user set
for the current model); otherwise the level is Others: L1 Others (`decided_by =
others`), "L1 / Others", or "L1 (L2) / Others". A page left Others keeps all its
candidate paths, and the Final step revisits it with its document's type (3.2).

* **Keyword weights: rare and unique score high, common score low.** Each canon term is
  a feature twice over: as the page's title (a title or strong term used as a heading in
  the first 12 lines) or anywhere else. Its weight for each class, at each level, is
  learned from the labelled training pages: log(P(class | term) / P(class)), smoothed,
  never below 0. A term only one kind of page carries weighs up to 6; a term every page
  carries weighs about 0. A term the pages rarely show takes its canon default instead:
  strong 4, title 2.5 (as a title; 1 elsewhere), body 1, ambiguous 0.3. Weights live in
  `keyword-canon/page_keyword_weights.json` (canon terms and numbers only, no page text);
  regenerate them after a canon change (`python -m stages.lib.page_classify.keyword_vote
  fit`).
* **Strong terms win outright.** The canon now has a fourth tier, `strong_terms`: rare
  words that name one page type (Superbill, Super Bill, Encounter Form; Medical Records
  Request, Request for Records, ...). Used as the page's title, a strong term pins its
  class at every level it names; two pins at one level cancel and the vote decides. This
  replaces the separate title rule table: words now live in one place, the canon.
* **Thin evidence cannot swing a page.** `k` grows with the keyword evidence: one
  ambiguous word moves the vote a little, several specific terms move it to the full
  share.
* **`share` and `full` are tuned on the validation pages** (evaluate, below): today
  share L1 0.2, L2 0.7, L3 0.0; full 6 at every level. On these pages the model is the
  better judge of L1 and L3; the keywords count most at L2.
* **Progress Note's L2 is the keywords' alone.** The model never learned it (its L2 head
  left the section L1 out), so the vote there is the keywords' (k = 1). This is what
  lets a Progress Note page that lists CT results read **Progress Note > Lab and Imaging
  Results**.
* **Nothing is ruled out.** `paths` keeps the vote's five best whole paths (vote(L1) ×
  vote(L2 | L1) × vote(L3 | L2), at most three under one L1). The Progress Note page that
  lists CT scans keeps both **Progress Note > Lab and Imaging Results** and **Lab Report >
  Radiology Report > CT Scan Report**; the Final step chooses with the document. The
  model's own best paths stay in `model_paths`.
* **Without a model** the keywords vote alone; **with no canon term on the page** the
  model votes alone.

**Rules on top [built].** After the vote, `page_arbitration.json` → `axes_rules` may
re-place the page; they are kept as rules because they need more than words:

* **clinical_summary**: a Summary of Care / CCD / C-CDA title or a CDA OID → Clinical
  Summary > Summary of Care (C-CDA); an After Visit Summary title → Clinical Summary >
  After-Visit Summary. A Discharge Summary page keeps its path.
* **hospital_stay**: a Progress Note page titled History and Physical or Consultation
  with hospital-stay evidence on it (admission date, room / bed, inpatient or
  observation status) → Discharge Summary > History and Physical / Consultation Note.
* **lab_guard**: below.

**Curated keywords per class (2026-10-25, user: "one by one, very relevant, standalone
words, L3 too").** `keyword-canon/class_keywords.json` gives every L1, L2 and L3 of the
taxonomy its own single words: **strong** words that on their own name the type
(Superbill, Echocardiogram, Urinalysis, Colonoscopy, HIPAA ...; used as the page's title
they pin the class) and **body** words the page typically carries (a consent: consent,
authorize, disclose, disclosure, release, revoke, voluntary, witness, guardian ...; a
chemistry report: sodium, potassium, creatinine, BUN ...). Generic document wording only:
no names, users, facilities, diseases or filler words ("any time", "please"); every L1
has at least five. `class_keywords.py` adds them to both copies of the canon (1,177
words) and the weights are refitted, so the labelled pages decide how much each word
counts. An earlier list mined automatically from the pages was dropped: frequency alone
let user names through, and pairs of adjacent field labels ("name phone") are not
keywords.

**The keyword canon was also updated (2026-10-25):** the strong tier; new entries Administrative >
Superbill and Provider Letter > Records Request (their titles moved from the rule
table); "request for records" removed from Consent and Authorization > Referral
Authorization (it named the wrong page type); and the note-section words (history of
present illness, HPI, review of systems, physical exam, assessment and plan, impression
and plan, interval history, subjective, objective, chief complaint, reason for visit)
added to the Progress Note entry, so a note page votes Progress Note against a report it
quotes.

**Guard: a lab report is never a Progress Note [built].** The client's main worry.
After the model, when it says Progress Note but the page has **no note-body section**
and **does have lab-result markers** (at least two, or a panel-title top line), the
Extracted path is Lab Report > Lab Result:

* note body (any, case-insensitive): chief complaint · history of present illness ·
  `\bhpi\b` · interval history · review of systems · `\bros\s*:` · physical exam ·
  assessment (and|&)? plan · assessment: · impression (and|&) plan · `\bplan\s*:` ·
  subjective: · objective:
* lab markers: specimen · accession · performing lab · result status · reference range ·
  ref range · collected · collection date · reported · ordering provider · final report ·
  flag · units · lab director · CLIA; or a top line that is a panel title (CBC, CMP, BMP,
  lipid panel, A1c, urinalysis, TSH, culture, surgical pathology) followed by
  final / result / panel / report.

**Specialty [built 2026-10-25].** Extracted, per page, in this order:

1. the model's, when it clears its gate: ≥ `specialty_min` (**0.50**), ≥
   `specialty_lead` (**0.10**) ahead of the second (both set by the user, 2026-10-25),
   trained on ≥ 10 pages, and not Unknown or Others;
2. the **specialty keywords** (`keyword-canon/specialty_canon.json`, a copy of
   appendix F): a letterhead term in the page's top 18% of lines scores 6, a body term
   (or a letterhead term lower down) 1, each once; the best needs ≥ 6 and a lead of 3
   (two close specialties are not a coin toss);
3. the specialty of the canon page type behind the decided path (Sleep Study Report →
   Sleep Study);
4. **General**. A page is never Unknown.

Final: **only the first page of the encounter (the continuity document) decides**, and
every page of it takes that specialty, so it never changes midway. A first page with
none gives General, whatever the later pages say.

**Codeability (Extracted).** From the canon entry behind the decided path; a `context`
rule leaves `needs_review`. This is the page's own view only: the codability the client
gets is the **Final** one (3.2, step 6), read from the Final page type.

**Progress Note L2.** Section tagging (`sections.json`, appendix G) names the content
sections on the page; the dominant one is the L2.

### 3.2 Final: in the document [built]

Run on continuity's documents, in this order:

1. **The document's L1.** The first page's Extracted L1; if the first page is Others,
   the weighted vote of the pages' L1s (weight = confidence) when one L1 holds the
   majority. **Except a short packet decided by its last page [built 2026-10-25, user]**
   (`page_arbitration.json` → `last_page_documents`): a document of at most **4 pages**
   whose **last page is Consent and Authorization** (notice or intake pages, then the
   signed consent) is consent on every page (`agreed_last_page`). Before that, such a
   document absorbs the short documents right before it when their pages are all
   consent, administrative, demographics or forms pages and the packet stays within
   4 pages (continuity may have split the notice from its signature page). This joining
   is for the page type only: DOS and encounter type keep continuity's documents.
2. **Every page under that L1.** A page whose Extracted L1 differs takes the best L2 the
   model gives **under the document's L1** (`by_L1`, already computed for every L1), or
   the canon's best match under it, else **"L1 / Others"**.
3. **Embedded families keep their own L2 inside a Progress Note.** A lab page or a
   medication list inside an office note becomes Progress Note > **Lab and Imaging
   Results** / **Medication List**. So a page Extracted as an independent *Lab Report >
   Pathology Report* reads, in Final, *Progress Note > Lab and Imaging Results* when its
   document is a Progress Note.
4. **The context rules** of the taxonomy (appendix C), each reading the document:
   * procedure_in_operative_document: a Procedure Note page bound into an operative
     document is Operative Note > Intra-operative Record;
   * admission_history_and_physical / inpatient_consultation: an H&P or consult is
     Discharge Summary > History and Physical / Consultation Note **only** in an
     inpatient or observation document; an office H&P stays Progress Note;
   * clinical_summary: CCD, Summary of Care, Transition of Care, After Visit Summary or a
     C-CDA header in the document title → Clinical Summary;
   * discharge_diagnosis_page, notice_inside_consent_packet, combined_endoscopy,
     telephone_encounter, dme_documents, interventional_radiology_procedure, superbill,
     records_request, scheduling_form: as written in appendix C.
5. **Others pages** never set a document's L1; they take the document's L1 with the
   best L2 under it (step 2), else "L1 / Others".

The placements inside a document are a table, `page_arbitration.json` → `final_placement`
(today: inside a Progress Note, Lab Report → Lab and Imaging Results, Medication Record →
Medication List, Problem List and Forms and Questionnaires to their Progress Note L2s;
inside an Operative Note, Procedure Note → Intra-operative Record). A family with no
entry there becomes "L1 / Others" **[decide: more placements]**.

**Rules on the page itself [built]** (`page_rules.py`, `page_arbitration.json` →
`axes_rules`), applied to the Extracted value: Clinical Summary by its title (CCD /
Summary of Care / Transition of Care / C-CDA → Summary of Care (C-CDA); After Visit
Summary → After-Visit Summary); Provider Letter > Records Request by title;
Administrative > Superbill by title only (invoices carry codes too); Discharge Summary >
History and Physical / Consultation Note for an H&P or consult page with hospital-stay
evidence on the page (admission date, room / bed, admitting physician, observation or
inpatient status).
6. **Specialty** from the document's first page (General when it has none).
7. **Codability from the Final page type [built 2026-10-25]**, never from the page's
   own (Extracted) score: the codeability tag of the Final path in the taxonomy
   (`page_taxonomy.json` → `codeability`, copied from the training taxonomy; appendix
   A lists every tag). The deepest level decides: Lab Report (Pathology Report) /
   Biopsy Report is Non-Codable (the L3's tag) although Pathology Report alone is
   Codable; an L1 alone reads its Others L2. A path tagged needs_review (Operative Note >
   Post-operative, Progress Note > Vitals, ...) takes the document's codability, the
   Final tag of its first page, when that is decided; otherwise it is Non-Codable and
   flagged for review. **No exceptions** (user, 2026-10-25): a lab page re-placed into a
   Progress Note is Progress Note > Lab and Imaging Results and Codable, as that type is
   tagged; a medication page placed there is Progress Note > Medication List,
   Non-Codable by its own tag. A page with no Final L1 (Others) keeps its own.

Extracted is never overwritten: a reviewer and the accuracy report see both.

---

## 4. Continuity: where one document ends and the next begins

### 4.1 How it decides [built]

Every **gap** between two consecutive pages gets a **break score**, a plain sum of
points: `+` toward a break, `−` toward joined; 1 weak, 2 moderate, 3 strong, 5 decisive.
Evidence comes from three places:

* **ending** — the earlier page alone: signed in its closing lines (+3), signed anywhere
  (+2), "page N of N" (+3), how full the page is (−2 … +2), "continued" (−3);
* **opening** — the later page alone: a title (+3), a visit heading such as Reason for
  Visit / Chief Complaint / HPI in the top band (+3, a break by itself unless printed
  page N of M runs on), where its text starts (±2);
* **pair** — the two together: different date of service (+5, decisive) or the same
  (−5), page numbers running on (−5, decisive), sections in order (−3), the same visit
  number (−3), the same header or footer line, wording across the gap, page type change
  (+3) or the same page type, a run of junk pages.

`score > 0` breaks, `< 0` joins, `0` joins and is flagged. A decisive (5) signal settles
the gap unless another 5 opposes it. Blank pages, single junk pages and duplicates are
transparent (the gap is scored across them). A Demographics page always breaks; a
non-junk Visual page always joins the page before. A **final pass** then splits a
document holding two label-read dates and merges neighbours across a weak break. Each
page is tagged New / Continue / Last / Standalone. Points: appendix J.

### 4.2 New signal: page type from the best paths, not the top 1 [to build]

Today the page-type signals compare the two pages' single best page type: +3 when they
differ, a join when they agree. An ambiguous page (Progress Note 0.45, Lab Report 0.40)
flips that +3 on a coin toss. The stored paths say how much the two pages *could* be the
same thing.

For each page, spread its stored path scores (the best 5) over L1s and normalise to sum 1:
`P(L1) = Σ score of the stored paths with that L1 / Σ all their scores`. The overlap of
two pages is

```
overlap = Σ over L1 of min(P_earlier(L1), P_later(L1))        0 = nothing shared, 1 = identical
```

| Condition | Points | Signal |
|---|---|---|
| overlap ≥ 0.60 | −2 | `page_type_overlap` (join) |
| overlap ≤ 0.10, and both pages' top path ≥ 0.60 | +2 | `page_type_disjoint` (break) |
| anything else | 0 | |
| the pair is an **embedded pair** (Progress Note ↔ Lab Report, Progress Note ↔ Medication Record, Progress Note ↔ Order and Requisition, Operative Note ↔ Procedure Note, Discharge Summary ↔ Lab Report) | 0 | never a break on its own: by page essence these pages sit inside notes |

These replace `page_type_change` (+3) and `same_page_type`. A page decided by the canon
or Others has no model paths: it uses its one path at score 1 (so it can join or break,
but only when the other page is confident). Weights are *assumed* until measured (4.5).

### 4.3 New signal: the encounter type changes [to build]

The page's **Extracted** encounter type (section 5.1) is read on both sides of the gap.
Not Available on either side says nothing.

| Earlier → later | Evidence on both pages | Points |
|---|---|---|
| Inpatient ↔ any Outpatient | stated / field / tier 1, or model ≥ 0.80 | **+3** |
| one Outpatient modality ↔ another (F2F ↔ Evisit ↔ Home Visit) | stated / field / tier 1 | **+2** |
| the same setting | stated / field on both | −1 |
| a weaker basis (inherited, prior, model < 0.80) | | 0 |

Signal `encounter_change`. It is not decisive: an inpatient stay can carry an office
follow-up letter, and a date or pagination still wins.

### 4.4 New signal: layout from the OCR boxes [to build]

Today continuity uses only each line's **vertical** position (header band = top 10%,
footer band = below 92%) and compares header / footer lines as **exact text**. Azure
(Final 2) gives every line's polygon and the page size; Docling (Final 1) gives word
polygons (grouped into lines in `continuity/layout.py`). Pages from one document come
from one template and one printer; a new document usually changes them.

**The layout fingerprint of a page** (from its lines' boxes, as fractions of the page):

| Feature | How |
|---|---|
| header zone | lines whose top is in the top 12%: text lower-cased, digits and dates masked (`#`), each with its horizontal anchor (left / centre / right third) |
| footer zone | lines whose bottom is below 90%, the same way |
| left margin | 10th percentile of the lines' left edges |
| right edge | 90th percentile of the lines' right edges |
| body line height | median line height outside the header and footer (a font-size proxy) |
| columns | 1 or 2: the lines' left edges clustered (a second cluster past 40% of the width holding ≥ 25% of the lines) |
| page size | width × height and orientation (Letter, Legal, A4, landscape) |

**The comparison:**

| Condition | Points | Signal |
|---|---|---|
| header zones share ≥ 50% of their (masked text, anchor) items, or footer zones do | −2 | `same_layout` (join) |
| both pages have a header zone, they share nothing, **and** at least one of: left margin differs by > 5% of the width, line height differs by > 25%, the column count differs | +2 | `layout_change` (break) |
| page size or orientation differs | +1 | `page_size_change` |

Not applied to handwritten, visual or form pages, or to a page with fewer than 8 lines:
their geometry is not a template. Across a transparent (blank / junk / duplicate) page
the comparison is with the page before it, as for every other signal. Masking digits
keeps "Page 2 of 5" / "Page 3 of 5" and date stamps from looking different. The existing
exact `same_header_line` / `same_footer_line` stay; `same_layout` is the fuzzy,
positional version.

### 4.5 Measuring the weights before trusting them [to build]

Every new weight starts *assumed*. Two labelled sources:

* the **170 labelled gaps** (explicit pagination on both pages) the current weights were
  fitted on (appendix J → measurement);
* the **training set's visits**: `encounter_tags.jsonl` groups 1,533 pages into 397
  visits over 100 charts. A gap inside a visit is a join, a gap between two visits of
  one chart is a break. The page images are in `Training/ocr-jpeg/`; their boxes come
  from Tesseract's word boxes (TSV) for the measurement.

For each new signal: how often it fires on joins and on breaks, and the balanced
accuracy of the whole score with and without it (GroupKFold by chart, as before). A
signal that fires on joins as often as on breaks gets 0 points.

### 4.6 What is stored

`page_continuity_results` and `staging/continuity.json`: per gap, the score, every
signal that fired with its points, the decision and its confidence; per page, the
document id and tag. The new signals appear there by name, so a wrong break can be
traced to the signal behind it.

---

## 5. Encounter type

Five values: **Inpatient**, **Outpatient (F2F)**, **Outpatient (Evisit)**, **Outpatient
(Home Visit)**, **Not Available**.

### 5.1 Extracted: the page's own vote [built 2026-10-25]

```
 page ──▶ family takes no encounter (a lab report, an order, a form)? ──yes──▶ Not Available
            │no
            ▼
          rules: tier 1 page type, tier 2 phrases, negatives ──found──▶ that setting (by tier1 / tier2)
            (a tier 2 F2F phrase gives way to the model at ≥ 0.95 for Evisit / Home Visit / Inpatient)
            │nothing
            ▼
          Evisit / Home Visit / Inpatient: the model gives it ≥ 0.50, or ≥ 0.30 with one of
          its keywords on the page? ──yes──▶ that setting (by model)
            │no (or F2F, or Not Available, or no model)
            ▼
          family default: Progress Note → F2F, Discharge Summary → Inpatient ──▶ (by family_default)
            │no family
            ▼
          no vote
```

* **The rules** (`encounter_classify.py`, appendix I): tier 1 page types that settle the
  setting (Discharge Summary → Inpatient, Telephone Encounter → Evisit, HouseCalls visit
  summary → Home Visit, ...), tier 2 / tier 3 phrases ("telehealth", "admitted", "home
  visit"), negatives that cancel a match. A page whose L1 family does not take an
  encounter votes Not Applicable; a page decided Others votes its own evidence.
* **The encounter model** (`models/encounter/`, trained by
  `training/bert-training-encounter`): one head over the five values (six once retrained
  on the page target: its "No evidence on page" never votes, 7.2). It is asked only
  for a page with **no tier 1 / tier 2 evidence** of its own, and its answer counts only
  for the classes and confidences in `encounter_canon.json` → `model_vote`:

  **The vote, with the retrained model (2026-10-25, user: keywords must not block).** For
  Evisit, Home Visit and Inpatient the model votes when it gives the setting **≥ 0.50**
  (its top answer or not), or **≥ 0.30 with one of the setting's keywords on the page**
  (`model_vote.confirm`): a keyword lowers the bar; it never blocks. F2F and Not Available
  stay with the rules; "No evidence on page" never votes.

  | Setting | Keywords that lower the bar to 0.30 (whole words) |
  |---|---|
  | Outpatient (Tele) / Evisit | video, camera, telephone, phone call, audio, virtual, remote, portal, MyChart, Zoom, Doxy, POS 02, POS 10, place of service 02 / 10, modifier 95, patient located, provider located, consent for telehealth, two-way, audiovisual |
  | Home | home, residence, homebound, caregiver, assisted living, apartment, POS 12, place of service 12, in the home, house call, family member present, home health, domiciliary, private residence |
  | Inpatient | admitted, admission, inpatient, hospital day, hospitalist, room, bed, observation, ICU, intensive care, nursing unit, ward, rounds, telemetry, length of stay, hospital course, discharge planning |

  **Phrases and keywords are different things.** The rules' *phrases* (tier 2:
  "telehealth visit", "home visit", "admitted", "office visit", "clinic note" ...) decide a
  page on their own, before the model is asked. The *keywords* above only lower the model's
  bar. **A very confident model beats a weak F2F phrase [built 2026-10-25, user]:** when
  the page's only evidence is a tier 2 F2F phrase ("office visit", "clinic note": template
  wording that also sits on telehealth and house-call pages) and the model says Evisit,
  Home Visit or Inpatient at **≥ 0.95**, the model's setting wins
  (`model_vote.override_f2f_phrase`). A phrase for any other setting stays ("inpatient"
  beats a model that says Evisit). On held-out pages this fires 4 times, right 4 times.

  Measured on the retrained model's held-out pages (validation + test, pages with a
  setting): Evisit 7 of 7 right at ≥ 0.30, 4 of them pages the rules miss (a keyword
  requirement had blocked 3 of those 4); Home Visit 2 of 2; Inpatient 28 of 37 right at
  ≥ 0.50 (21 right beyond the rules) and 32 of 43 with a keyword at ≥ 0.30. On the 22
  encounter test pages the rules with this vote get **19 of 21** (Inpatient 4/5, Not
  Available 4/4, Evisit 4/4, F2F 4/5, Home Visit 3/3), against 17 before the F2F-phrase
  override and 14 with the first model; the model alone gets 16. Still wrong: ENC_03
  (ambulatory surgery centre wording should give F2F; the Operative Note prior gives
  Inpatient) and ENC_09 (an ED visit ending in admission should be Inpatient).

  The samples are small; measure again on new charts before lowering a threshold. Each
  vote records `by: model` and the model's class and confidence in the encounter log
  (`staging/continuity.json`). Without the model folder (or torch) no page asks it and
  `/health` → `encounter_model.ready` is false.
* **What the training labels say about learnability**: 198 pages state the setting,
  94 carry a field, 236 are inferred from content (vitals + exam → F2F), 1,112 come from
  the page type, 1,086 are inherited from another page of the visit, 21 are the client
  default (never learned). Inherited labels train at half weight.

### 5.2 Final: the first page, else the encounter's vote [built; the client default to build]

The **encounter range** is the continuity document: from its New page through its
Continue pages to its Last page.

**The first page decides [built 2026-10-25, user]** when it has evidence of its own: a
tier 1 page type, a tier 2 phrase, or the encounter model's confident answer
(`decided_by = first_page`; conflict flagged when another page says otherwise). Only when
the first page cannot decide (it is a lab or other non-encounter page, or carries only
its family's default) do the pages vote -- **only the pages with evidence of their own**;
a family default (Progress Note → F2F, Discharge Summary → Inpatient) is the **fallback
when no page has any** (user, 2026-10-25). A lab or other non-encounter page reads
**Not Available**.

1. The first page still counts **twice**.
2. **Not Available wins only when every vote is Not Available**: one page voting a
   setting sets the Not Available votes aside.
3. Otherwise the weighted majority wins; a tie goes to the first page's vote.
   Confidence = the winner's share of the weight.
4. **The client default [to build]**: when the winner is an Outpatient setting but no
   page of the range has stated, field or content (exam / vitals) evidence of the
   modality, the encounter is **Outpatient (Evisit)**, `decided_by = client_default`.
   It is a rule after the vote, never something the model learns.
5. Defaults when no page votes: Progress Note → Outpatient (F2F), Discharge Summary →
   Inpatient, a lab report → Not Available.

Every page of the range carries the encounter's Final value; its Extracted value stays.

### 5.3 Encounter and continuity, both ways

* **Into continuity**: the Extracted encounter change between neighbours is a break
  signal (4.3).
* **Back from the encounter [to build]**: in the final pass, a document whose pages carry
  stated or field evidence of two different settings *and* two different label-read
  dates splits at the change, the same way two label dates split a document today.

### 5.4 How good is it today

The rules engine against the tagged training pages (section 8.1): 0.81 of pages right,
but **Evisit recall 0.07 and Home Visit 0.04**: the Progress Note default (F2F) wins
whenever the page does not say the modality in the engine's words. The **retrained
encounter model** (Colab, 2026-10-25; page target, No-evidence pages left out, Evisit /
Home Visit balanced with oversampling and 80 synthetic pages; best epoch 9) on its
held-out test pages: page accuracy **0.76**, macro F1 0.64; Evisit precision 1.00
(recall 3/5), Home Visit precision 1.00 (1/3), Inpatient precision 0.77 (recall 0.40),
F2F 0.47 / 0.57, Not Available recall 0.94; visits 0.71 (51 visits). The first model (visit
target): page accuracy 0.60. The vote in 5.1 uses it where the rules have no evidence.
The client default (5.2 step 4) is still to build.

---

## 6. What is stored, and where

| Where | Holds |
|---|---|
| `page_classification.axes` (Postgres JSONB) | Extracted L1 / L2 / L3 / specialty / codeability, each with confidence and `decided_by` (model / canon / others / guard), the **vote's best 5 paths** (`paths`, at most 3 per L1) and the model's own (`model_paths`), `rules` (a rule that fired), the canon's match, `by_L1` |
| `staging/page_classification.json` **[built]** | the same per page, beside the other staging files (Postgres stays the source the later stages read) |
| `staging/blank_junk.json`, `staging/extraction.json` | blank / junk / duplicate verdicts; key-value extraction, signatures |
| `staging/continuity.json`, `page_continuity_results` | per gap: score, signals, decision; per page: document, tag; Final page type, DOS, encounter |
| `encounter_type_results` | per page: Extracted setting, basis, evidence; per encounter: Final setting, confidence, decided_by. Each page's vote, including `by: model` with the model's class and confidence, is in the encounter log in `staging/continuity.json` |
| `page_sequencing_results`, `imaging/<chart>_sequencing.csv` | per page: suggested order (`seq`), method, confidence, review flag (section 9) |
| `imaging_final` | every field's Final value |

A `staging/page_classification.json` entry (made-up values; Progress Note has no model
L2, because section tagging decides it):

```json
{
  "page": "17.jpg",
  "extracted": {"L1": "Lab Report", "L2": "Pathology Report", "L3": "Biopsy Report",
                 "path": "Lab Report > Pathology Report > Biopsy Report",
                 "decided_by": "model", "confidence": 0.82,
                 "specialty": "General", "codeability": "non_codeable"},
  "model_paths": [
    {"rank": 1, "path": "Lab Report > Pathology Report > Biopsy Report", "score": 0.71,
     "l1_confidence": 0.86, "l2_confidence": 0.90, "l3_confidence": 0.92},
    {"rank": 2, "path": "Progress Note", "score": 0.09},
    {"rank": 3, "path": "Lab Report > Pathology Report > Cytology Report", "score": 0.05},
    {"rank": 4, "path": "Lab Report > Lab Result > Hematology Report", "score": 0.04},
    {"rank": 5, "path": "Procedure Note > Endoscopy (EGD)", "score": 0.02}
  ],
  "canon": {"page_type": "Pathology Report", "score": 0.66},
  "route": "model"
}
```

---

## 7. The models and their training

### 7.1 Page axes (`training/bert-training`)

* Input: `Training/processed/training_data.jsonl`, **2,785 pages after the second label
  pass of 2026-10-25** (364 pages' L1 / L2 / L3 changed against one set of definitions;
  11 junk pages removed). Specialty is now **page-level**: only what the page itself shows
  (786 pages changed; Unknown, 1,283 pages, means "no specialty on this page"). The 25
  synthetic pages (`--with-mock`) train, never score; the notebook adds them by default.
* **Four heads: L1, L2, L3, specialty.** Codeability is no longer learned (user,
  2026-10-25): the pipeline reads it from the taxonomy tag of the Final page type
  (3.2 step 7). A model trained before then still has the fifth head and still loads.
* Classes come from the taxonomy and the data, never from code: an L1 trains with ≥ 11
  pages, an L2 / L3 / specialty with ≥ 3; rarer ones fold (L1 → OTHER, L3 → Others) and
  are left to the keyword vote. With the new labels and the mock pages: 16 L1, 62 L2,
  38 L3, 29 specialties.
* **Small classes [built 2026-10-25]** (from the Claude Cowork pack's patch, ported):
  * the split spreads every label, not only L1 (`--split labels`): groups (charts) with
    the rarest labels are placed first, each on the side that most lacks its labels; a
    label in fewer than 3 groups stays in train; `split_report.csv` lists pages per side
    per class;
  * classes with fewer than 30 training pages are oversampled (repeat-factor sampling:
    a page repeats √(30 / pages of its rarest class) times, at most 4; today about 950
    training pages repeat, about 2,670 samples per epoch from 2,030 pages);
  * loss weights are the square root of inverse frequency over the pages as sampled.
* **20 epochs**; the best epoch by validation macro F1 of L1, L2 and L3 is kept.
* Reported: per-class recall (the notebook prints L1 recall of Administrative, Provider
  Letter, Problem List and Immunization), the **Lab Report ↔ Progress Note confusion in
  both directions**, full-path accuracy, hierarchical P/R, the routes, and
  `check_test_pages.py` on the pack's synthetic test pages (fails if a Lab Report test
  page comes back Progress Note).
* **Baseline to beat** (from the pack): TF-IDF + logistic regression, cross-validated by
  chart on the new labels, macro F1 0.65 for L1, 0.36 for L1 > L2, 0.36 for specialty.
  The pack's CPU check run of the patched trainer (DistilBERT, 256 tokens, 4 epochs):
  test macro F1 L1 0.56, L2 0.43, L3 0.56, specialty 0.30; Superbill F1 0.80.
* **Four encoders to compare [ready 2026-10-25]** (`page_axes.ipynb` → `RUNS`, same seed
  and split, mock pages on): `distilbert-base-uncased` (512 tokens, 15 epochs, lr 3e-5,
  batch 16), `roberta-base` (512, 12, 2e-5, 16), `emilyalsentzer/Bio_ClinicalBERT` (512,
  12, 2e-5, 16), `answerdotai/ModernBERT-base` (1,024, 12, 3e-5, 8; transformers ≥ 4.48).
  The last cell compares them on test macro F1 of L1, L2, L3 and specialty, small-class
  recall (fewer than 30 training pages), Lab Report read as Progress Note and the test
  pages. The pipeline's loaders take any of them: `AutoModel` / `AutoTokenizer` from the
  model folder, the hidden size from its config, `max_length` from `label_space.json`.
* Specialty training pages on the new split: 26 of 29 specialties have at least 10, so the
  pipeline's gate (`specialty_min_train_pages` 10) keeps them; Nutrition (3), Wound Care
  (5) and Nursing (7) are never asserted by the model and are left to the specialty
  keywords.

### 7.2 Encounter (`training/bert-training-encounter`)

* Input: `Training/encounter/encounter_tags.jsonl`, 2,785 pages.
* **Target [built 2026-10-25]:** `--target page` (default) learns `encounter_on_page`,
  the setting only when the page itself shows it, else **No evidence on page** (1,118
  pages). Trained as a class, that label won far too often (the user's first page-target
  run), so those pages are **left out of training and of the page scores** by default
  (`--no-evidence-weight 0`; 1 brings the class back). The model learns from the pages
  that show their setting and always names one; on a thin page its confidence is low, so
  the vote thresholds (5.1) keep it out, and the visit is applied to the whole document
  afterwards, as the pipeline does. `--target visit` learns `encounter_type`
  (the first run's target). Page-level counts: Not Available 1,132, F2F 360, Inpatient
  125, Evisit 33, Home Visit 17: the two small classes are very small.
* `train: false` rows (the client-default pages) are never learned; reported apart.
* Inherited labels at weight 0.5; macro-F1 selection.
* **Balancing Evisit and Home Visit [built 2026-10-25, user]:** classes with fewer than
  100 training pages are oversampled (repeat-factor sampling, at most 4 repeats): Evisit
  about ×2, Home Visit ×2.8, Inpatient ×1.3; the loss weights are softened to the square
  root of inverse frequency (`--oversample`, `--max-repeat`, `--class-weight`). Plus **80
  synthetic Evisit / Home Visit pages** (`make_synthetic_pages.py` → `synthetic_pages.jsonl`:
  invented practices, people and dates; the setting stated many ways -- video, telephone,
  portal, POS 02 / 10 / 12, modifier 95, residence, assisted living, homebound -- in
  different places; half with a neutral title so the title alone is not the cue), training
  side only: Evisit 24 → 64 and Home Visit 13 → 53 training pages. Scores stay real-page
  scores. Real pages remain the limit: more labelled charts with telehealth and home
  visits would help most.
* Split by chart; synthetic pages training only; **20 epochs**.
* Reported per class, page by page **and visit by visit** (the visit's truth is
  `encounter_type`; pages with no evidence abstain), by label basis, and on the pack's
  22 encounter test pages (`check_encounter_tests.py`; ENC_22, the client default, is
  reported, not scored).

### 7.3 Which OCR text the models see

Training text is Tesseract (the stage-2 preliminary OCR of a 2,200 px image). The
pipeline's best text is Azure → RapidOCR → Tesseract. Feeding the model text from
another engine than it was trained on costs accuracy; the choice is recorded in
section 8.

### 7.4 From Colab to the pipeline

1. Run `page_axes.ipynb` (and `encounter.ipynb`) on a GPU runtime: `--check`, the run,
   the metrics, the test pages.
2. Copy `encoder/`, `heads.pt`, `label_space.json` into
   `core-pipeline/models/page-family/page-axes/`, and the encounter run's `encoder/`,
   `head.pt`, `label_space.json` into `core-pipeline/models/encounter/`. The two
   `encoder/model.safetensors` files (265 MB each) are not in git: copy them onto every
   host. `/health` → `page_axes_model` and `encounter_model` say whether each loaded.
3. `page_arbitration.json` → `axes.taxonomy_version` is `v29.2`; a model trained on
   another taxonomy is refused at load and the canon decides (visible in `/health`).
4. Tune the gates on the run's validation predictions (`tune_thresholds.py`).

---

## 8. Measurements and decisions

### 8.1 Measured

**Encounter rules engine today** against `encounter_tags.jsonl` (pages of one visit as
one document; 2,729 learnable pages; page accuracy 0.813):

| Class | Pages | Recall | Precision |
|---|---|---|---|
| Outpatient (F2F) | 1,227 | 0.80 | 0.82 |
| Outpatient (Evisit) | 99 | **0.07** | 0.32 |
| Outpatient (Home Visit) | 51 | **0.04** | 0.67 |
| Inpatient | 234 | 0.62 | 0.56 |
| Not Available | 1,118 | 0.97 | 0.87 |

Rows are the truth, columns what the engine said:

| | F2F | Evisit | Home | Inpatient | NA |
|---|---|---|---|---|---|
| F2F | 985 | 0 | 0 | 101 | 141 |
| Evisit | 80 | 7 | 0 | 11 | 1 |
| Home | 46 | 0 | 2 | 1 | 2 |
| Inpatient | 69 | 0 | 0 | 144 | 21 |
| NA | 20 | 15 | 1 | 2 | 1,080 |

Visit level (382 visits): 0.71; recall F2F 0.79, Evisit 0.09, Home Visit 0.17,
Inpatient 0.69. The 21 client-default pages: 0 come out Evisit (17 F2F, 4 Not
Available). Each page voting alone: 0.75, so the encounter vote helps.

**Keyword canon after the conversion**: its path agrees with the majority label for 93
of the 106 canon page types that occur in the training data.

**Page-axes model** (Colab, 2026-10-25, best epoch 19 of 20; 458 held-out test pages):

| Head | Accuracy | Macro F1 |
|---|---|---|
| L1 | 0.64 | 0.44 |
| L2 (given the right L1) | 0.71 | 0.37 |
| L3 (given the right L2) | 0.57 | 0.26 |
| Specialty | 0.69 | 0.32 |
| Full path | 0.48 | hierarchical F1 0.55 |

Lab Report read as Progress Note: 4 of 94; Progress Note read as Lab Report: 15 of 183.
Path score as a gate (validation / test): at ≥ 0.50, 65% / 57% of pages qualify and
71% / 71% of those paths are fully right; below it, 28% / 16% (the path score gate was
replaced the same day by the vote, 3.1). On the twelve made-up check pages: 24 of 38 head checks
pass, no Lab Report page comes back as a Progress Note.

**The keyword vote** (2026-10-25; weights fitted on the 1,843 training pages, `share` /
`full` tuned on the 494 validation pages, reported on the 458 test pages; minimum 0.2;
L2 and L3 measured given the true parent, so every method is scored on the same pages):

| Test pages | Model alone | Keywords alone | The vote |
|---|---|---|---|
| L1 | 0.635 | 0.238 (61% of pages have no keyword evidence: Others) | 0.631 |
| L2, true L1 (not Progress Note) | 0.687 | 0.433 | 0.687 |
| L3, true L2 | 0.556 | 0.397 | 0.556 |
| Progress Note L2 | 0.339 (always "Others") | 0.246 | 0.246 |
| Lab Report read as Progress Note | 4 / 94 | 0 / 94 | 4 / 94 |

**With the curated class keywords** (same model answers; the relabelled pages of the second
label pass, so the model-alone numbers move a little too):

| | Validation: model alone | Validation: vote | Test: model alone | Test: vote |
|---|---|---|---|---|
| L1 | 0.725 | 0.727 | 0.638 | 0.633 |
| L2, true L1 | 0.745 | **0.778** | 0.707 | **0.729** |
| L3, true L2 | 0.522 | **0.598** | 0.456 | **0.574** |
| Progress Note L2 | 0.130 | **0.296** | 0.357 | 0.303 |
| Full path | 0.277 | **0.340** | 0.264 | 0.255 |

The words now earn their place below L1: L2 +2 to 3 points, L3 +8 to 12. L1 stays the
model's. The Progress Note L2 from keywords stays at the 0.2 minimum (user, 2026-10-25).

On validation the vote is slightly ahead (L1 0.733 vs 0.725, L2 0.761 vs 0.748). **On
average pages the canon's words add little to the model today:** the canon was written
as a list of names, and most of its terms are common across page types. Where it
matters is the rare page: strong terms (Superbill, records requests) now win against a
confident model, and page types the model never learned are named. The Progress Note L2
from keywords is unreliable: Lab and Imaging Results was found on 4 of 9 validation
pages and 0 of 3 test pages; at the 0.2 minimum it beats "always Others" on validation
(35 / 176 vs 31) and loses on test (45 / 183 vs 62). Too few pages to settle it.

**Correct answers the old path gate hid** (the pack's check: only 34 of 119 correct
test pages passed `path_min` 0.5 in its run). With the installed model's cached answers
on the relabelled pages, of the non-Progress-Note pages where the model's L1 and L2 are
right:

| | Model right | Old gate (path score ≥ 0.5) | The vote (≥ 0.2) |
|---|---|---|---|
| Test, all | 125 | 76 | **120** |
| Test, small classes (< 30 training pages) | 19 | 14 | **18** |
| Validation, all | 170 | 117 | **152** |
| Validation, small classes | 45 | 28 | **39** |

The path gate is gone (3.1); the vote keeps nearly every correct answer.

**Encounter model**: section 5.4; its vote thresholds: 5.1.

### 8.2 Waiting on the client [decide]

* **Five old page-type names with no exact home in v29.2.** The keyword list we
  converted from v24 had five page types the new taxonomy has no slot for. I placed
  them as below; confirm or move them:
  * Administration, Administrative Form, Policy → **Administrative > Others** (fine as
    is, unless you want a Policy page under Consent and Authorization > General Consent);
  * Pap and HPV Report, Pap smear report → now **Lab Report > Diagnostic Study**; a Pap
    smear is a cytology test, so **Lab Report > Pathology Report > Cytology Report** is
    the better home (recommended).
* A page only the Others step decided reads "Others / Others": acceptable?
* More Final placements inside a Progress Note (3.2).
* **Strong words.** A strong word, used as a page's title, decides the type on its own,
  even against a confident model. They are the `strong` lists in
  `keyword-canon/class_keywords.json`: please look down those lists and strike any word
  that can head a page of another type (a word like "assessment" must never be strong;
  "superbill" or "colonoscopy" can).
* Should `staging/page_classification.json` become what later stages read first?

* Payer record-retrieval fax covers and EHR import covers as junk by rule.
* Records Request defined by purpose (letter, fax or note).
* "Surgery center" / "ambulatory surgery" wording → Outpatient (F2F).
* Surgery scheduling forms as Administrative > Appointment Record.

### 8.3 Build order

1. **[built]** v29.2 names everywhere, the converted canon, the v1 ladder removed,
   the best 5 paths stored, the Lab Report guard, `staging/page_classification.json`. Until
   the v29.2 model is copied in, the canon decides every page (`/health` says so).
2. **[built 2026-10-25]** both models trained on Colab and copied in; L2 / L3 →
   Others, specialty keywords → General, the first page's specialty for the encounter,
   the encounter model as a vote (5.1).
3. **[built 2026-10-25]** the model and the keyword canon vote at every level (3.1):
   learned per-level term weights, strong terms, minimum 0.2, every candidate path kept;
   Final codability from the Final type (3.2 step 7).
4. **[built 2026-10-25]** the trainers for the relabelled data: page axes with four
   heads and the small-class split / oversampling / weights; encounter on the page-level
   target; specialty gate 0.5 / 0.1; codability from the Final type with no exception.
   **Next: retrain both on Colab** (`docs/NEXT_STEPS_TRAINING.md`).
5. **[to build]** encounter Extracted before continuity.
6. **[to build]** continuity signals 4.2, 4.3, 4.4; measured (4.5) before their weights
   are trusted.
7. **[to build]** the client default (5.2 step 4) and the encounter split (5.3).

---

## 9. Page sequencing [built]

Sequencing suggests the order a chart's pages should be read in. It never moves or
renames a file: **current sequence** is the scan order (`page_number`), **actual
sequence** is the suggested order (`page_sequencing_results.seq`). It runs after
encounter type and before `imaging_final` (stage `page_sequencing`; code in
`core-pipeline/stages/lib/sequencing/`, ported from advantmed-document-processing).

### 9.1 Two series: Main and J

| Page (blank / junk pass-2 verdict) | Sequence | Stored |
|---|---|---|
| Main (not blank, junk or duplicate) | **1, 2, 3 …**, ordered by the steps in 9.2 | `seq` = the number |
| Blank or junk | **J1, J2, J3 …** in scan order | `seq` NULL, `sequence_method` `blank_junk`; the CSV says `J1` |
| Duplicate (≥ 98%, "Duplicate (To Page N)") | **J series too** (2026-10-25, user): numbered with the blank and junk pages in scan order | as blank / junk |

Blank, junk and duplicate pages never enter the ordering, so they cannot push a Main
page out of place, and the Main series always runs 1 to the number of Main pages with
no gaps. The
ground truth (`page_ground_truth.page_sequence`, text) uses the same two series: "3" for
a Main page, "J2" for a blank / junk one. A client sheet that numbers every page
1, 2, 3 … is converted on display: pages whose ground truth is blank, junk or an invoice
become J1, J2 … and the Main pages are renumbered 1, 2, 3 …, each in stored order.

### 9.2 How Main pages are ordered

Text is the best OCR the page has (Final 2 → Final 1 → Tesseract; Tesseract never for a
handwritten or low-quality page). Each page gives the engine its first and last 5
lines (header / footer), first and last 15 lines (for continuation), word count and
the printed page number the key/value extraction found.

1. **Page markers.** Per page, one printed page number:
   * the key/value extraction's `printed_page_no` / `printed_page_total` first
     (`additional_page_details`; confidence 1.0 with a total, 0.85 without);
   * otherwise patterns in the footer, then the header, then the body ("Page 3 of 7",
     "Pg 3", "p. 3 of 7", "3 of 7", "3/7", "- 3 -", a footer line that is only a number).
     Patterns that say "page" score 1.0; bare-number ones 0.92 (0.85 with no total;
     0.78 for a lone footer number).
   * Markers are grouped into **families by their total** ("of 7"). The family on the
     most pages (then the most explicit, then the most distinct numbers) is the
     record's real pagination, trusted even when its total is larger than the upload (a
     partial upload of a bigger record). A page outside it keeps a lone marker only if
     the pattern says "page" and the total fits the upload (≤ 2 × pages, or pages + 20).
     Bare numbers on one page alone ("1/25", "2 of 3") are dates and doses, not
     pagination, and are dropped.
2. **Streams.** A chart can hold several printouts, each numbered from 1. Pages are
   grouped by marker total; a group of ≤ 2 pages next to a group 3 × its size is
   dropped as noise, and with only 1–2 marked pages in a chart of 5 or more, the chart
   is not split (one stream; the markers still order it, step 3). Inside a group, two pages claiming the same number are split
   into parallel streams (each page goes to the stream whose last page is nearest in
   scan order); a page with the same text as another claiming that number is a
   duplicate. Every stream after the first is flagged for review. Pages with no marker
   are orphans, ordered by step 4 (joined to the stream when there is only one).
3. **Order: scan order, except confident "page n of m" [built 2026-10-25, user].**
   Main pages keep their scan positions. Pages that confidently say **"page n of m"** -- a
   total in an explicit pattern ("Page 3 of 7", "Pg 3 of 7", "p. 3 of 7", "Page 3/7"), or
   read so by the key/value extraction -- and share the same m are one family and take
   the family's scan positions in page-number order. A bare "3 of 7", a lone footer
   number or a header group never moves a page.
   * A family may span continuity documents (encounters); when its numbers repeat (two
     printouts of the same length) it splits back by document.
   * A page moves at most **5 places**, unless every Main page of the documents it spans
     belongs to the family -- the whole document is numbered n of m -- and then the
     family is put in order however far, so a document scanned fully in reverse is
     reversed back.
   * A family stays in scan order, and the pages that would have moved are flagged
     (`sequence_method` `marker_held`, review), when two of its pages claim one number or
     a page would move too far. `explicit_marker` when the total equals the Main page
     count, otherwise `explicit_marker_reference` (confidence capped at 0.90).
4. **Pages with no marker.**
   * *Cross-encoder continuation* (off: `SEQUENCING_CROSS_ENCODER=false` and the ONNX
     model is not in `artifacts/`). When on, it scores "page B continues page A" from
     A's last 15 lines and B's first 15 lines, for pages within 12 positions of each
     other (at most 2,500 pairs), starts the chain at the page least likely to be a
     continuation (a "continued" heading counts against it), then always takes the best
     next page. Below 0.50 the page is flagged.
   * *Header groups* (on). Pages whose normalised header or footer words overlap by
     more than 70% (Jaccard) are one group: confidence 0.90 if a page in the group has
     a marker, else 0.75. They keep scan order.
   * *Scan order.* Anything left: `original_order`, confidence 0.30, flagged.
5. **No long moves.** Since the fences (2026-10-25), a fax cover on page 1 is no longer
   sent to the end and a near-blank Main page is no longer moved after the others: both
   keep their scan positions (a fax cover is usually junk already, J series). The stream
   split (step 2) still finds parallel printouts and flags them, but no longer moves a
   page further than the fences allow.
6. Main pages are renumbered 1 … n in the resulting order.

### 9.3 What is stored

| Field | `page_sequencing_results` | `imaging/<chart>_sequencing.csv` |
|---|---|---|
| scan order | `original_page_number` | `current_sequence` |
| suggested order | `seq` (Main only) | `actual_sequence` (`3`, `J2`, or empty) |
| how | `sequence_method`: `explicit_marker`, `explicit_marker_reference`, `marker_held`, `cross_encoder`, `header_group`, `original_order`, `blank_junk` | `sequence_method` |
| how sure | `confidence` | `confidence` |
| look at it | `review_flag` | `review_flag` (y / n), plus `stream_id` and `ocr_source` |

`imaging_final.seq` copies `seq`.

### 9.4 Accuracy: Main against Main only

The review UI's Sequencing tab compares the pipeline with the ground truth on pages
that are **Main on both sides**. The pipeline's Main pages are put in its order; a page
is a **break** when it is not the next page, in ground-truth order, after the page
before it (the first page: when it is not the ground truth's first). Accuracy is
1 − breaks / Main pages compared. Counting breaks rather than equal numbers means one
misplaced page costs one, not every page after it.

J pages are marked (✓ when the pipeline also has the page as blank / junk, ✗ when not)
but **not counted**: a page that is J on one side and Main on the other is a blank /
junk error, and it is scored by Blank / Junk accuracy, not here. A complete duplicate is a
J page like the others.

### 9.5 Open points [decide]

* Continuity runs before sequencing and reads scan order. A chart scanned out of order
  splits a document wherever the pages are out of place. Running sequencing first and
  letting continuity read the suggested order would fix that, at the cost of a wrong
  suggestion moving a break: measure on the ground-truth charts first.
* The cross-encoder is off and its model is not in the repository. Ship it, or remove it.
* Covers no longer move (the fences); confirm the client sheet keeps a fax cover where it was scanned.
* Moves from weaker evidence (a bare "n of m", header groups) stay off.

---

<!-- canon:start (generated by training/taxonomy/render_canon_doc.py; do not edit) -->

## Appendix: the canon, taxonomy v29.2

Rendered from the JSON files by `training/taxonomy/render_canon_doc.py`; run it after any canon change.

Contents: A. label space · B. renames · C. context and MECE rules · D. canon conversion tables · E. keyword canon · F. specialty · G. sections · H. encounter (training) · I. encounter (pipeline) · J. continuity

### A. The v29.2 label space

Version **v29.2**. Counts: L1 17, L2 102, L3 64, specialty 34, paths 167.

#### A.1 Rules

* **section_L1**: Progress Note
* **section_L1_note**: Under Progress Note, L2 is the content section on the page (axis B), decided by section tagging. It is confirmed in the tool and not trained.
* **fallback**: Others
* **specialty_unknown**: Unknown
* **specialty_other**: Others
* **specialty_note**: 'Unknown' means no signal on the page; 'Others' means a real service line the list does not carry yet, the signal to extend the lexicon.
* **codeability_values**: codeable, non_codeable, discharge, needs_review
* **codeability_default**: needs_review
* **note_v29_2**: Labels follow Training/processed/taxonomy_v29.json context_rules (page_essence etc.). Section L1 (Progress Note) L2 is owned by section tagging at inference, as before.

#### A.2 L1

| L1 | Description |
|---|---|
| Progress Note | A clinician's record of an encounter. L2 here is the CONTENT SECTION the page carries, not the kind of note, because a scanned page of a note is a page and what matters is what is on it. A note is a Progress Note whatever service produced it; the service line goes on the specialty axis. |
| Lab Report | The result of a test, study or examination performed on the patient. The distinction from Operative Note is that a result reports on looking, measuring or analysing, while an Operative Note records an intervention. |
| Operative Note | The record of an operation or an episode of intensive care: what was done in theatre, by whom, and under what anaesthesia. Repeated treatments and office procedures are Procedure Note instead. |
| Procedure Note | A treatment RECORD: a repeated therapeutic session or an administration of something to the patient. Neither an operation, which is Operative Note, nor an encounter narrative, which is Progress Note. Dialysis is the clearest case: a run is a procedure performed on a schedule and logged on a flow sheet, not a visit written up. |
| Discharge Summary | The closing documentation of an inpatient or observation stay, including its instructions, medications and transfers of care. The client treats this whole group as its own codeability bucket rather than as codeable or non-codeable. |
| Clinical Summary | A document that summarises care across a visit or many visits: CCD / Summary of Care / Transition of Care (C-CDA), After-Visit Summary, EHR patient-summary or chart-snapshot printouts. Decided by the document title. |
| Consent and Authorization |  |
| Demographics | Patient identity, contact and coverage information, carrying no clinical content. |
| Immunization | A record of vaccines administered or due. |
| Medication Record | A list or administration record of what the patient is taking or has been given, including blood products. |
| Order and Requisition | An instruction to perform something: a test, a treatment, a referral. The order is not the result, which is why these never sit under Lab Report/Result. |
| Problem List | A standing list of the patient's active or historical problems. Flat by design: the page type is the whole answer and no L2 adds anything. |
| Forms and Questionnaires | A structured form completed by or about the patient. Codeability depends on context: bound into a progress note it is part of that note, standing alone it is not. |
| Provider Letter | A letter between providers or to the practice about the patient: referral letters, consultation letters, records requests (defined by purpose: any letter, fax or EHR note requesting records). |
| Patient Communication | Correspondence between the practice and the patient or another provider, and material sent to the patient. |
| Administrative | Pages that exist for the running of the practice rather than the care of the patient: scheduling, billing, coding, records management. |
| Others | A page nothing else claims. This is a routing decision, not a classification: the page goes to an auditor rather than being forced into a bucket or dropped. |

#### A.3 L2 by L1

**Progress Note**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| CC/HPI | Codeable |  | The chief complaint and the history of present illness: why the patient came and the narrative of the problem. Usually the opening page of a note. |
| Visit Diagnosis | Codeable |  | The diagnosis or assessment arrived at for this encounter. Outranks every other section on a page, because a page that states the diagnosis is about the visit even if most of it is a data table. |
| Assessment and Plan | Codeable |  | The clinician's reasoning and what they intend to do next. Distinguished from Visit Diagnosis by being forward-looking rather than a statement of findings. |
| Summary | Codeable |  | A narrative recap of the encounter or stay, including hospital course where it appears inside a note. |
| Problem List | Non-Codeable |  | A problem list printed as a section inside a note, as opposed to a standalone problem list page, which is its own L1. |
| Medication List | Non-Codeable |  | Medications and allergies as a section inside a note. A standalone medication page is Medication Record instead. |
| Lab and Imaging Results | Codeable |  | A split page of a note that carries results AND note content (history, exam, plan). A page that is wholly results is Lab Report, not this (page-essence rule). |
| Procedure List | Codeable |  | A procedure described inside a note, typically done in the office. A formal operative report is Operative Note. |
| Vitals | Needs Review |  | A note page dominated by vital signs. |
| Physical Exam | Codeable |  | Examination findings, review of systems and vital signs. |
| Questionnaire | Needs Review |  | A form or questionnaire bound into a note, which makes it part of that note. The same form standing alone is Forms and Questionnaires. |
| Patient Instructions | Codeable |  | What the patient was told to do, and follow-up arrangements. |
| Signature | Codeable |  | A page carrying the attestation and little else, typically the closing page of a note. The same evidence is the End-of-document signal used to group pages into documents. |
| Others | Codeable |  | A progress note page whose section could not be determined. Most often a continuation page of body text with no header on it. |

**Lab Report**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Lab Result | Non-Codeable |  | Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Radiology Report | Non-Codeable |  | Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| Pathology Report | Codeable |  | Tissue and cell examination: biopsy, cytology, flow cytometry, molecular diagnostics. |
| Diagnostic Study | Non-Codeable |  | Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |

**Operative Note**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Surgery Report | Codeable |  | The operative report itself: the account of an operation, its findings and its closure. |
| Procedure List | Codeable |  | A procedure page belonging to an operation: the intraoperative record, phase record or OR summary. Distinguished from the Procedure Note L1 not by name, which is the same, but by the document the page is bound into. Standing alone it is a treatment record and belongs under Procedure Note. |
| Pre-operative | Codeable |  | Pre-operative. |
| Post-operative | Needs Review |  | Post-operative. |
| Anesthesia Record | Codeable |  | The anaesthesia record and post-anaesthesia care, including the transfer of care out of theatre. |
| Critical Care Record | Codeable |  | Intensive care documentation: ICU flow sheets and ventilator management records. |
| Triage Report | Codeable |  | The triage assessment taken at presentation, carrying acuity and arrival details. |
| Intra-operative Record | Needs Review |  | Intra-operative nursing / OR record: times, staff, positioning, counts. |
| Implant Record | Needs Review |  | Implant log or sticker page: devices, lot numbers. |
| Others | Codeable |  | An operative page that fits none of the above, such as an implant log. |

**Procedure Note**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Dialysis Record | Codeable |  | A dialysis run: treatment record, haemodialysis flow sheet or peritoneal dialysis record. Logged per session rather than written up per visit. |
| Infusion and Chemotherapy | Codeable |  | Administration of an infusion or chemotherapy, including the cycle and the agents given. |
| Radiation Therapy | Codeable |  | A radiation therapy treatment record: fields, fractions and dose delivered. |
| Wound Care | Codeable |  | Wound and burn treatment: measurements, debridement and dressing changes, recorded per session. |
| Therapy Session | Codeable |  | A therapy treatment session: physical, occupational, speech, respiratory, cardiac rehabilitation, chiropractic or acupuncture. The therapy DISCHARGE summary is Discharge Summary instead. |
| Elastography | Codeable |  | An elastography or FibroScan procedure measuring tissue stiffness. |
| Injection Visit | Codeable |  | A visit whose purpose is an injection, with the agent and site recorded. |
| Knee Procedure | Codeable |  | A procedure performed on the knee: injection, aspiration or arthrocentesis, written up on its own. The site is the L2 because it is what the code turns on. |
| Laser Procedure | Codeable |  | A laser treatment, including YAG and SLT. |
| Lumbar Procedure | Codeable |  | A procedure performed on the lumbar spine: injection, aspiration or arthrocentesis, written up on its own. The site is the L2 because it is what the code turns on. |
| Office Procedure | Codeable |  | A procedure performed in the office where no more specific type is identified, including gastroenterology and retinal office procedures. |
| Pathology Procedure | Codeable |  | A procedure carried out in the pathology laboratory, such as gross examination. |
| Endoscopy (EGD) | Codeable |  | Endoscopy procedure record (EGD) when kept as a procedure note rather than a report. |
| Cardiac Procedure | Codeable |  | Cardiac procedure record (cardioversion, cath-lab procedure log) that is not a study report. |
| Others | Codeable |  | A treatment record or procedure page with no specific procedure identified. A page headed only 'Procedure Note' with no site or modality lands here, because that is the label an unsure annotator reaches for. |

**Discharge Summary**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Depart/Discharge Note | Discharge |  | The discharge summary itself, in any of its names: depart note, depart summary, discharge note, discharge report, final case summary. |
| Discharge Instruction | Discharge |  | What the patient was told to do after discharge, including the Spanish-language versions of the same form. |
| Discharge Medication | Discharge |  | The medication list as it stands at discharge. |
| Transfer of Care | Discharge |  | Handover to another facility or service, including anaesthesia transfer of care. |
| Interim Summary | Discharge |  | A summary issued mid-stay rather than at discharge. |
| Short Stay Summary | Discharge |  | The discharge document for a short stay or observation episode. Its own header governs even where it embeds a history and physical. |
| Discharge Diagnosis | Discharge |  | Discharge Diagnosis. |
| History and Physical | Discharge |  | Admission H&P of an inpatient, observation or short stay. Office H&Ps are Progress Note. |
| Consultation Note | Discharge |  | Inpatient consult. Office consults are Progress Note. |
| Others | Discharge |  | A discharge-packet page that fits none of the above. |

**Clinical Summary**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Summary of Care (C-CDA) | Non-Codeable |  | CCD, Summary of Care, Transition of Care, C-CDA documents. |
| After-Visit Summary | Non-Codeable |  | After Visit Summary / Patient Clinical Summary given at the end of a visit. |
| Others | Non-Codeable |  | Others. |

**Consent and Authorization**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Consent for Treatment | Non-Codeable |  | Consent to treatment / conditions of treatment signed by the patient. |
| Surgical Consent | Needs Review |  | Surgical Consent. |
| Transfusion Consent | Needs Review |  | Transfusion Consent. |
| Immunization Consent | Needs Review |  | Immunization Consent. |
| HIPAA and Privacy | Non-Codeable |  | HIPAA and Privacy. |
| Release of Information | Needs Review |  | Release of Information. |
| Assignment of Benefits | Non-Codeable |  | The patient assigning insurance benefits to the provider. |
| General Consent | Non-Codeable |  | General Consent. |
| Prior Authorization | Needs Review |  | Prior Authorization. |
| Referral Authorization | Non-Codeable |  | Approval of a referral, with the visits authorised. |
| General Authorization | Non-Codeable |  | General Authorization. |
| Others | Non-Codeable |  | An administrative page that fits none of the above. |

**Demographics**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Patient Information | Codeable |  | The patient's identity, contact and coverage details. |
| Face Sheet | Codeable |  | The printed face sheet or registration page. Note that this overlaps Patient Demographics by design: both were asked for, and a face sheet is in practice the demographics page under another name. |
| Others | Non-Codeable |  | A demographic page that fits neither of the above. |

**Immunization**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Immunization Record | Non-Codeable |  | Vaccines given or due, in any format: immunisation list, vaccination administration record, flu shot record. |
| Others | Non-Codeable |  | An immunisation page that fits none of the above. |

**Medication Record**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Medication List | Non-Codeable |  | The current medication list as a standalone page. |
| Prescription Page | Non-Codeable |  | A prescription or Rx page. |
| Refill Record | Non-Codeable |  | A record of refills issued. |
| Blood Transfusion Record | Non-Codeable |  | Administration of blood or blood products, which is a medication administration rather than a procedure. |
| Others | Non-Codeable |  | A medication page that fits none of the above. |

**Order and Requisition**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Physician Order | Non-Codeable |  | Any order written by a clinician: medication, treatment, service request, post-operative orders. |
| Laboratory Requisition | Non-Codeable |  | A requisition or lab form requesting a test. |
| Referral Order | Needs Review |  | Referral Order. |
| Others | Non-Codeable |  | An order or request that fits neither of the above. |

**Problem List**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Problem List | Non-Codeable |  | A standing problem list page. |
| Diagnosis List/Index | Needs Review |  | A list or index of diagnoses, often coded, with no assessment. |
| Others | Non-Codeable |  | Others. |

**Forms and Questionnaires**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Assessment | Needs Review |  | A structured assessment or evaluation: functional status, ADL and IADL, nutrition, occupational health, fitness for duty, annual wellness. |
| Questionnaire | Needs Review |  | A questionnaire, checklist or screening instrument completed about the patient: screening tests, preventive checklists, health maintenance, Mini-Cog and clock drawing, growth charts. |
| Others | Needs Review |  | A form that is neither clearly a questionnaire nor clearly an assessment. |

**Provider Letter**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Referral Letter | Non-Codeable |  | Letter referring the patient to another provider. |
| Consultation Letter | Non-Codeable |  | Consultant letter back to the referring provider. |
| Records Request | Non-Codeable |  | Any letter, fax or EHR note whose purpose is requesting records. |
| Others | Non-Codeable |  | Others. |

**Patient Communication**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Patient Message | Non-Codeable |  | A message exchanged with the patient: telephone message, portal communication, web encounter. |
| Notice | Non-Codeable |  | A notice or notification sent to the patient, including discharge notices. |
| Patient Education Material | Non-Codeable |  | Educational material and patient instructions given out as their own document. |
| Telephone Encounter | Non-Codeable |  | Phone note, refill call, telephone message. |
| Others | Non-Codeable |  | A communication page that fits none of the above. |

**Administrative**

| L2 | Codeability | Specialty | Description |
|---|---|---|---|
| Appointment Record | Non-Codeable |  | Appointment notes and summaries: when the patient was or is due to be seen. |
| Billing & Invoices | Needs Review |  | Invoices, billing pages, superbills, charge sheets and estimates. Codeability here can depend on the ingest source rather than the page. |
| Patient Registration | Needs Review |  | Registration / intake demographics form for registration. |
| Superbill | Non-Codeable |  | Encounter form / superbill with ICD-10 and CPT codes. Invoices and statements stay Billing & Invoices. |
| Others | Non-Codeable |  | An administrative page that fits none of the above. |

#### A.4 L3 (Lab Report only)

**Lab Report > Lab Result**

| L3 | Specialty | Description |
|---|---|---|
| Allergy Testing Report |  | Allergy Testing Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Chemistry Report |  | Chemistry Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| FISH Report |  | FISH Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Genetic Test Report |  | Genetic Test Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Glucose Report |  | Glucose Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Hematology Report |  | Hematology Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Coagulation Report |  | INR and Prothrombin Time. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Laboratory Flow Sheet |  | Laboratory Flow Sheet. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Microbiology Report |  | Microbiology Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Urinalysis Report |  | Urinalysis Report. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |
| Others |  | A laboratory with no more specific type identified. Specimen-based testing: chemistry, haematology, microbiology, urinalysis, genetics, coagulation. |

**Lab Report > Radiology Report**

| L3 | Specialty | Description |
|---|---|---|
| Bone Mineral Density Report |  | Bone Mineral Density Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| Brain MRI Report |  | Brain MRI Report. |
| CT Angiography Report |  | CT Angiography Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| CT Scan Report |  | CT Scan Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| MR Angiography Report |  | MR Angiography Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| MRI Report |  | MRI Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| Mammogram Report |  | Mammogram Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| Nuclear Medicine Report |  | Nuclear Medicine Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| Ultrasound Report |  | Ultrasound Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| X-Ray Report |  | X-Ray Report. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |
| Others |  | A radiology with no more specific type identified. Imaging reports: plain film, CT, MRI, ultrasound, mammography, nuclear medicine, bone density. Angiography is held separately from plain CT and MR because it is a different protocol and a different code. |

**Lab Report > Pathology Report**

| L3 | Specialty | Description |
|---|---|---|
| Biopsy Report |  | Biopsy Report. Tissue and cell examination: biopsy, cytology, flow cytometry, molecular diagnostics. |
| Cytology Report |  | Cytology Report. Tissue and cell examination: biopsy, cytology, flow cytometry, molecular diagnostics. |
| Cytometry Report |  | Cytometry Report. Tissue and cell examination: biopsy, cytology, flow cytometry, molecular diagnostics. |
| Molecular Diagnostics Report |  | Molecular Diagnostics Report. Tissue and cell examination: biopsy, cytology, flow cytometry, molecular diagnostics. |
| Others |  | A pathology with no more specific type identified. Tissue and cell examination: biopsy, cytology, flow cytometry, molecular diagnostics. |

**Lab Report > Diagnostic Study**

| L3 | Specialty | Description |
|---|---|---|
| Ankle-Brachial Index Report |  | Ankle-Brachial Index Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Audiogram and Hearing Loss Report |  | Audiogram and Hearing Loss Report. |
| Behavioral Assessment |  | Behavioral Assessment. |
| Bronchoscopy Report |  | Bronchoscopy Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Cardiac Catheterization Report |  | Cardiac Catheterization Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Carotid Duplex Report |  | Carotid Duplex Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Colonoscopy Report |  | Colonoscopy Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Corneal Topography |  | Corneal Topography. |
| Cystoscopy Report |  | Cystoscopy Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Doppler Duplex Report |  | Doppler Duplex Report. |
| Echocardiogram Report |  | Echocardiogram Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Electrocardiogram (EKG/ECG) Report |  | Electrocardiogram. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Electroencephalogram Report |  | Electroencephalogram Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Electromyography and Nerve Conduction |  | Electromyography and Nerve Conduction. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Endoscopy Report |  | Endoscopy Report. |
| Esophageal Manometry Report |  | Esophageal Manometry Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Esophagogastroduodenoscopy (EGD) Report |  | Esophagogastroduodenoscopy Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Extremity Doppler Report |  | Extremity Doppler Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Holter Monitor Report |  | Holter Monitor Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Laryngoscopy Procedure Report |  | Laryngoscopy Procedure Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Stress Test |  | Stress Test Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Neurocognitive Screening Examination |  | Neurocognitive Screening Examination. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Optical Biometry (IOL Calculation) |  | Optical Biometry (IOL Calculation). |
| Optical Coherence Tomography Report |  | Optical Coherence Tomography Report. |
| Oximetry Report |  | Oximetry Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Pacemaker Device Report |  | Pacemaker Device Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Plethysmography Report |  | Plethysmography Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Pulmonary Function Test |  | Pulmonary Function Test. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Sigmoidoscopy Report |  | Sigmoidoscopy Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Sleep Study Report |  | Sleep Study Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Split Night Study Report |  | Split Night Study Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Sudoscan Report |  | Sudoscan Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Telemetry Report |  | Telemetry Report. |
| Titration Report |  | Titration Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Videonystagmography Report |  | Videonystagmography Report. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |
| Visual Field Report |  | Visual Field Report. |
| Others |  | A diagnostic study with no more specific type identified. Any other study that produces a report: cardiac, neurological, pulmonary, sleep, vascular, endoscopic and ophthalmic. Deliberately broad. It holds many service lines at once, which is why no single specialty is implied at this level and the specialty is read from the page instead. |

#### A.5 Specialty values

| Specialty | Description |
|---|---|
| Anesthesia | Anaesthesia and peri-anaesthetic care. Evidence: ASA class, induction, intubation, PACU. |
| Audiology | Hearing and balance, including ENT. Evidence: audiogram, tympanometry, speech discrimination. |
| Behavioral Health | Psychiatry, psychology and substance use. Evidence: PHQ-9, GAD-7, psychotherapy, mental status. |
| Cardiology | Heart and vascular. Evidence: echocardiogram, ejection fraction, pacemaker, telemetry. |
| Critical Care | ICU and intensive care. Evidence: ventilator settings, drips, hourly flow sheet. |
| Dermatology | Skin. Evidence: Mohs, skin biopsy, lesion, melanoma, cryotherapy. |
| Dialysis | Renal replacement therapy, separate from general nephrology. Evidence: hemodialysis, peritoneal dialysis, AV fistula. |
| Emergency | Emergency department, urgent care and prehospital. Evidence: triage, ESI level, ED course, disposition. |
| Gastroenterology | Digestive tract. Evidence: colonoscopy, EGD, sigmoidoscopy, polyp. |
| Home Health | Care delivered in the home. Evidence: OASIS, homebound, face-to-face encounter. |
| Infusion | Infusion therapy as a service line. Evidence: infusion centre, cycle, administration record. |
| Nephrology | Kidney disease outside dialysis. Evidence: creatinine clearance, ESRD, renal failure. |
| Neurology | Nervous system. Evidence: EEG, EMG, nerve conduction, seizure, neuropathy. |
| Nursing | Nursing documentation. Evidence: intake and output, shift assessment, repositioning. |
| Nutrition |  |
| Obstetrics and Gynecology | Pregnancy and women's health. Evidence: prenatal, gestational age, fetal heart, colposcopy. |
| Occupational Health | Employer-directed and workers' compensation care. Evidence: fitness for duty, work restriction, return to work. |
| Oncology | Cancer care. Evidence: chemotherapy, staging, metastatic, radiation therapy. |
| Ophthalmology | Eye care, optometry included. Evidence: OD/OS, IOP, visual acuity, fundus, slit lamp. |
| Orthopedics | Musculoskeletal and sports medicine. Evidence: arthroplasty, rotator cuff, fracture, range of motion. |
| Palliative Care | Palliative, hospice and pain management. Evidence: goals of care, comfort measures, pain score, MME. |
| Pathology | Tissue and cytology. Evidence: specimen, gross description, microscopic, immunohistochemistry. |
| Podiatry |  |
| Primary Care | Family medicine, internal medicine, general practice. Evidence: annual wellness, health maintenance. |
| Pulmonology | Lungs and respiratory. Evidence: spirometry, PFT, COPD, FEV1. |
| Radiology | Imaging as a service line. Evidence: imaging centre letterhead, contrast, tomography. |
| Rehabilitation | Physical, occupational and speech therapy, chiropractic. Evidence: gait training, therapeutic exercise, plan of care units. |
| Sleep Study | Sleep medicine. Evidence: polysomnography, apnea, CPAP, titration. |
| Surgery | General and surgical specialties. Evidence: estimated blood loss, specimen removed, wound closure. |
| Urology | Urinary tract and male reproductive. Evidence: cystoscopy, prostate, PSA, urodynamic. |
| Wound Care | Wound and burn management. Evidence: wound measurements, debridement, dressing change. |
| Pain Management | Pain management and interventional pain: injections, nerve blocks, pain clinic notes. |
| Others | A real service line this list does not yet carry. Annotator only; the detector never emits it, because it cannot recognise what it has no lexicon for. Seeing this value is the signal to add a specialty. |
| Unknown | No specialty signal on the page at all. This is a correct answer, not a miss: a specialty is detectable on 43% of pages alone and 33% more by inheriting from the document. |

### B. Renames from the 23 Oct v29 to v29.2

**L1**

| Old | v29.2 |
|---|---|
| Lab Report/Result | Lab Report |

**L2**

| Old | v29.2 |
|---|---|
| Lab Report/Result > Laboratory | Lab Report > Lab Result |
| Lab Report/Result > Radiology | Lab Report > Radiology Report |
| Lab Report/Result > Pathology | Lab Report > Pathology Report |
| Lab Report/Result > Diagnostic Study | Lab Report > Diagnostic Study |
| Progress Note > Procedure | Progress Note > Procedure List |
| Operative Note > Procedure | Operative Note > Procedure List |
| Demographics > Patient Demographics | Demographics > Patient Information |
| Administrative > Billing Statement | Administrative > Billing & Invoices |
| Demographics > Registration Form | Administrative > Patient Registration |
| Progress Note > Lab Results | Progress Note > Lab and Imaging Results |

**L3**

| Old | v29.2 |
|---|---|
| INR and Prothrombin Time | Coagulation Report |
| Stress Test Report | Stress Test |
| Esophagogastroduodenoscopy Report | Esophagogastroduodenoscopy (EGD) Report |
| Electrocardiogram | Electrocardiogram (EKG/ECG) Report |

**L3_extra**

| Old | v29.2 |
|---|---|
| Myocardial Perfusion Study | Stress Test |
| Pap and HPV Report | Lab Report > Diagnostic Study (no L3) or Pathology Report > Cytology Report |
| Colposcopy Examination | Lab Report > Diagnostic Study (no L3) |
| Fundus Photography Report | Lab Report > Diagnostic Study (no L3) |
| Lens Parameters | Lab Report > Diagnostic Study > Optical Biometry (IOL Calculation) |

**Split: a per-page decision, never automatic**

| Old | v29.2 |
|---|---|
| Progress Note > Imaging Results | Progress Note > Lab and Imaging Results (or Lab Report > Radiology Report when the page is wholly a report) |
| Progress Note > Lab Results | Progress Note > Lab and Imaging Results (or Lab Report > Lab Result when the page is wholly results) |
| Patient Communication > Provider Letter | Provider Letter > Referral Letter \| Consultation Letter \| Records Request \| Others (new L1) |

**New L1**: Clinical Summary, Provider Letter

**New L2**: Progress Note > Vitals, Operative Note > Intra-operative Record, Operative Note > Implant Record, Procedure Note > Endoscopy (EGD), Procedure Note > Cardiac Procedure, Discharge Summary > History and Physical, Discharge Summary > Consultation Note, Clinical Summary > Summary of Care (C-CDA), Clinical Summary > After-Visit Summary, Clinical Summary > Others, Consent and Authorization > Consent for Treatment, Problem List > Problem List, Problem List > Diagnosis List/Index, Problem List > Others, Provider Letter > Referral Letter, Provider Letter > Consultation Letter, Provider Letter > Records Request, Provider Letter > Others, Patient Communication > Telephone Encounter, Administrative > Superbill

### C. Context and MECE rules (v29.2 rules file)

Context rules run after continuity: they decide a page's class from the document it sits in or from what the page is wholly about. MECE rules decide between two classes that a name alone cannot separate.

> Rules that need the document the page sits in, so they run after the continuity step, not before it. A form or questionnaire bound into a progress note is part of that note; the same form standing alone as its own document is not.

#### C.absorb_into_progress_note

> A questionnaire page becomes L2 Questionnaire; a care plan page becomes L2 Plan of Care. Standing alone both keep their Misc path.

> Standing alone these keep their own L1: Forms and Questionnaires for a questionnaire or care plan.

> Medication List, Allergy List, Problem List page types are absorbed only when the page is split with note content; a page that is wholly a medication list is Medication Record (context_rules.page_essence).

* **when_document_L1**: Progress Note
* **page_types**: Questionnaire, Evaluation Form, Checklist, Preventive Health Checklist, Screening Test Record, Health Maintenance Report, Mini Mental Status Examination, Activities of Daily Living Flow Sheet, Staying Healthy Assessment, Functional Assessment, Nutrition Assessment, Assessment Note, Annual Health Assessment, Annual Wellness Visit, Plan of Care, Care Plan, Treatment Plan, Nursing Care Plan, Interdisciplinary Plan of Care, Medication List, Allergy List, Problem List, Clinical Diagnosis List
* **becomes**: L1: Progress Note; L2: Questionnaire; L3:
* **comment**: Standalone, these keep their Misc path (Screening and Checklist / Assessment and Evaluation).
* **becomes_by_page_type**: Plan of Care: Plan of Care; Care Plan: Plan of Care; Treatment Plan: Plan of Care; Nursing Care Plan: Plan of Care; Interdisciplinary Plan of Care: Plan of Care; Medication List: Medication List; Allergy List: Medication List; Problem List: Problem List; Clinical Diagnosis List: Problem List

#### C.procedure_in_operative_document

> A procedure page bound into an operative document belongs to that operation: the intraoperative record, the phase record, the OR summary. The same page standing on its own is a treatment record and belongs to the Procedure Note L1. Deciding this by NAME is impossible, because both are called a procedure note, so it is decided by the document the page sits in. Runs after the continuity step, like the other context rules.

> L2 renamed from the never-listed "Procedure" to Intra-operative Record (client approved 2026-10-24). The intraoperative nursing record (time-out, fire risk, equipment, staff and case times) lands here too.

* **when_document_L1**: Operative Note
* **page_types_from_L1**: Procedure Note
* **becomes**: L1: Operative Note; L2: Intra-operative Record; L3:
* **default_when_no_document**: Procedure Note

#### C.discharge_diagnosis_page

* **when_document_L1**: Discharge Summary
* **when_dominant_section**: visit_diagnosis
* **becomes**: L1: Discharge Summary; L2: Discharge Diagnosis; L3:
* **comment**: A page whose dominant header is a diagnosis list, sitting inside a discharge packet, is the discharge diagnosis page. The same page standing alone in an outpatient note is Progress Note > Visit Diagnosis. L1 is decided by the document family first, so this only fires inside a discharge document. It only overrides the Depart/Discharge Note catch-all: a Transfer of Care or Interim Summary page keeps its own L2 even when its dominant section is a diagnosis list, because that page's identity is the handover, not the diagnosis.
* **codeability**: discharge
* **only_when_L2_is**: Depart/Discharge Note

#### C.notice_inside_consent_packet

* **when_page_type_in**: General Notice, Discharge Notice, Notice, Notice of Privacy Practices, Privacy Notice, Patient Rights Notice, Policy Page, Office Policy, Financial Policy
* **becomes**: L1: Consent and Authorization; L2: General Consent; L3:
* **comment**: A notice about a policy is a consent wherever it sits, in a packet or on its own. Patient Communication > Notice keeps only notices that are not about a policy: a closure, a recall, a service change, a results-ready message.
* **applies_anywhere**: True
* **only_when_about**: privacy, health information practices, patient rights, patient responsibilities, office policy, financial policy, no show policy, cancellation policy, terms and conditions, assignment of benefits

#### C.admission_history_and_physical

* **when_page_type_in**: History and Physical, History & Physical, Admission H&P, Admission History and Physical
* **when_encounter**: hospital stay: inpatient, observation or short stay. Evidence on the page or in the chart: Adm Date / Date of admission, Admitting or Attending physician, Room/Bed, an observation or inpatient service, or a discharge document in the same chart.
* **becomes**: L1: Discharge Summary; L2: History and Physical; L3:
* **codeability**: discharge
* **pages_inside**: Every page of the admission H&P takes this path, including medication, lab or imaging pages bound inside it (same rule as lab/medication pages inside a progress note).
* **otherwise**: An office or clinic H&P (pre-operative clearance in a clinic, initial OB visit, new-patient H&P) stays Progress Note with its section-based L2. An "H&P reviewed, no changes" update before a procedure stays where it is (Operative Note > Pre-operative when it sits in the operative record).
* **comment**: Client direction 2026-10-24: the admission H&P belongs with the hospital-stay documents under Discharge Summary, next to Interim Summary, Transfer of Care and Short Stay Summary.

#### C.inpatient_consultation

* **when_page_type_in**: Consultation Note, Consultation Notes, Consult Note, Consultation Report
* **when_encounter**: hospital stay: inpatient, observation or short stay (same evidence as admission_history_and_physical)
* **becomes**: L1: Discharge Summary; L2: Consultation Note; L3:
* **codeability**: discharge
* **otherwise**: An outpatient consult visit stays Progress Note; a consult letter to the referring doctor is Provider Letter > Consultation Letter.
* **comment**: Client approved 2026-10-24: an inpatient consult belongs to the stay, like the admission H&P.

#### C.combined_endoscopy

* **rule**: A report of endoscopic findings is Lab Report > Diagnostic Study with the leaf of the procedure: Colonoscopy Report, Esophagogastroduodenoscopy (EGD) Report, Sigmoidoscopy Report, Endoscopy Report. A single report covering EGD and colonoscopy in one session is Colonoscopy Report. Procedure Note > Endoscopy (EGD) is kept for an office procedure note that documents doing an EGD without a findings report (consent, sedation, scope times only).
* **comment**: Client approved 2026-10-24.

#### C.telephone_encounter

* **when_page_type_in**: Telephone Encounter, Telephone Note, Phone Note, Postoperative Telephone Call, Post-Procedure Follow up Call, Nurse Triage Call
* **becomes**: L1: Patient Communication; L2: Telephone Encounter; L3:
* **otherwise**: A scheduled telephone or video VISIT with history, assessment and plan is a Progress Note (encounter type Outpatient (Evisit)), not a Telephone Encounter.
* **comment**: Client approved 2026-10-24.

#### C.clinical_summary

* **decided_by**: the TITLE of the document, never by a phrase inside a note
* **summary_of_care_titles**: Continuity of Care Document, Summary of Care, Transition of Care Document, Transition of Care Summary, Patient Summary of Care, Encounter Summary (C-CDA export with OID header such as 2.16.840.1.113883 / 2.840.114350)
* **after_visit_summary_titles**: After Visit Summary, Patient Clinical Summary, Visit Summary (patient copy)
* **becomes**: L1: Clinical Summary; L2: Summary of Care (C-CDA) \\| After-Visit Summary; L3:
* **pages_inside**: Every page of the summary takes this path, including the problem, medication, lab and note sections printed inside it (same parent-document rule as labs inside a progress note).
* **stays_progress_note**: An authored provider note that MENTIONS a summary ("After Visit Summary (Snapshot) printed", "Summary of care document sent to") is still a Progress Note. An Epic encounter print whose pages are the provider note stays Progress Note.
* **stays_discharge_summary**: A hospital discharge packet page titled Clinical Summary (Result Type Clinical Summary inside Discharge Documentation) stays Discharge Summary. Anesthesia Transfer of Care notes and true facility-to-facility transfer forms stay Discharge Summary > Transfer of Care.
* **codeability**: non_codeable: a summary export restates encounters; code from the authenticated source note
* **encounter_type**: no page-type prior: the setting comes from wording on the page (admit date, visit type), so an outpatient summary is no longer pushed to Inpatient
* **comment**: Client approved 2026-10-24 after Charts 8-11. Cross-validated by chart: Progress Note F1 0.817 -> 0.819 with the new class, so it does not take pages from Progress Note; the text model alone confuses Clinical Summary with Progress Note, so the title rule is what decides it.

#### C.specialty_from_page_type

* **rule**: When no letterhead names a specialty, these page types carry their own specialty (client, 2026-10-24).
* **Anesthesia**: Anesthesia Record, intra-operative anesthesia flowsheet (CRNA vitals, airway, lines and drains, reversal agents), anesthesia history, post-anesthesia care record
* **Critical Care**: Critical Care Record, ICU flow sheet, ventilator management record
* **Surgery**: Operative Note pages (surgery report, perioperative record, pre/post-operative) of a surgical operation, unless the letterhead or the operation names a more specific surgical specialty: Urology (ureteral stent, stone), Ophthalmology (cataract), Cardiology (TEE, cardioversion at a heart hospital), Pain Management (radiofrequency denervation), Gastroenterology (endoscopy at a GI centre)
* **Emergency**: ED Triage pages (Operative Note > Triage Report)
* **Home Health**: a self-assessment, health risk assessment or annual assessment done in the home (in-home visit, home safety assessment)

#### C.dme_documents

* **when**: durable medical equipment (DME/DMEPOS: braces, splints, walkers, wheelchairs, CPAP, oxygen, diabetic supplies) paperwork: DME orders or prescriptions, delivery tickets, proof-of-delivery or patient receipts, DMEPOS supplier notices
* **becomes**: L1: Order and Requisition; L2: Others; L3:
* **comment**: Client direction 2026-10-24: keep DME under Order and Requisition > Others (no separate L2).

#### C.interventional_radiology_procedure

* **when**: an interventional radiology (IR) procedure report: catheter, drain, stent, tube, port or line placement or exchange, image-guided biopsy or drainage
* **becomes**: L1: Procedure Note; L2: Others; L3:
* **otherwise**: A diagnostic imaging report with no procedure stays Lab Report > Radiology Report.
* **comment**: Client direction 2026-10-24 (Chart1 Pg14-16, IR nephroureteral catheter placement).

#### C.page_essence

* **rule**: Each page is labelled by what the page itself holds, because the model sees one page at a time. A page that is wholly lab results, imaging results, a medication list or an order list takes that standalone path (Lab Report, Medication Record, Order and Requisition) even when it sits inside a progress note, discharge packet or other document. Headers, footers, signature or routing blocks, an allergy list or a short problem list on the same page do not change this.
* **split_pages**: A page that also carries note content (HPI, interval history, ROS, exam, assessment, impression or plan) is a split page and keeps the parent document's path, with the L2 that fits the page (for example Progress Note > Lab and Imaging Results or Medication List).
* **grouping**: Grouping pages back into documents is done later, outside the page classifier.
* **comment**: Client direction 2026-10-24. Replaces the earlier parent-document rule for lab, medication and imaging pages.

#### C.superbill

* **when**: an encounter form or superbill: visit date, provider, diagnosis codes (ICD-10) and service codes (CPT/HCPCS), often with fee columns
* **becomes**: L1: Administrative; L2: Superbill; L3:
* **not**: an invoice, patient statement, cost estimate or payment receipt (Billing & Invoices)
* **comment**: Client direction 2026-10-24: Superbill is its own type; more examples to come.

#### C.records_request

* **when**: any page whose purpose is requesting medical records: a provider or payer request letter, a records-request fax, or an EHR addendum or note documenting a records request
* **becomes**: L1: Provider Letter; L2: Records Request; L3:
* **comment**: Client direction 2026-10-24: defined by purpose, not by form.

#### C.scheduling_form

* **when**: appointment lists, recall lists and surgery or procedure scheduling forms (surgeon, date, CPT/ICD for the booking, insurance pre-auth fields)
* **becomes**: L1: Administrative; L2: Appointment Record; L3:
* **comment**: Client direction 2026-10-24: a surgery scheduling form is Appointment Record, not a questionnaire.

#### MECE rules

> Three L1s can all receive something a human would call a form. The discriminator is what the form DOES, not that it is a form. Applied in this order, the three are mutually exclusive and between them exhaustive.

##### form_routing

test: grants a permission and carries a patient or guardian signature; goes_to: Consent and Authorization; examples: Surgical Consent Form, HIPAA Form, Release of Information Authorization, Assignment of Benefits, Patient Agreement Form, test: captures identity, contact or coverage, with no clinical content; goes_to: Administrative > Patient Registration; examples: Patient Registration, Registration Form, test: collects clinical information about the patient, scored or free text; goes_to: Forms and Questionnaires; examples: Health History Form, Questionnaire, Nutrition Assessment, Functional Status Report, Screening Test Record, test: a billing or scheduling page: a statement, invoice, superbill, appointment list or no-show letter; goes_to: Administrative; examples: Billing Statement, Invoice Page, Appointment Note, No Show Letter, test: a practice policy, rights or responsibilities page the patient signs or acknowledges; goes_to: Consent and Authorization > General Consent; examples: Financial Policy, Office Policy, Patient Rights and Responsibilities, Notice of Privacy Practices with signature line, test: an insurance or benefit verification page the practice or payer produced; goes_to: Consent and Authorization > General Authorization; examples: Authorization Verified, Benefit Verification, Eligibility Check, test: a chart artefact with no clinical or financial content: a document index, an import log, a fax banner page; goes_to: Others; examples: Document Index, Import Log, Chart Index

##### consent_vs_authorization

> Replaced 2026-10-23. The old test asked what the page permitted, which split badly: a HIPAA release permits a disclosure and is signed by the patient, so it answered both ways. The test is now who issues it.

* **test**: who signs or issues the page
* **consent**: the patient or their guardian signs it, including acknowledging a notice
* **authorization**: the hospital, provider or payer issues it
* **when_no_signature_block_is_legible**: fall back to the title: a page titled consent is a consent, a page titled authorization issued on practice or payer letterhead is an authorization, otherwise General Consent if addressed to the patient and Others if neither

##### hipaa_pages

> Both sides of a HIPAA page are patient-signed, so both are consents. The split is only how specific it is.

> Client direction 2026-10-24: a health information exchange (HIE) notice, HIE opt-out/opt-in form or HIE participation acknowledgement is HIPAA and Privacy, signed or not. It is about sharing health information, so it does not go to General Consent, General Authorization or Notice.

* **names_a_recipient**: Consent and Authorization > Release of Information
* **acknowledges_the_notice_only**: Consent and Authorization > HIPAA and Privacy
* **hie_notice**: Consent and Authorization > HIPAA and Privacy

##### context_resolved_pairs

> Three L2 labels exist under more than one L1. They are not duplicates: the PATH differs, and which one applies is decided by the document the page is bound into, not by the page. All three run after the continuity step. Listed here so the collision reads as a designed pair rather than an oversight, and so the annotation tool can show both options with the rule.

* **pairs**: label: Medication List; in_a_note: Progress Note > Medication List; standing_alone: Medication Record > Medication List; rule: a medication table printed inside a note is part of that note, label: Questionnaire; in_a_note: Progress Note > Questionnaire; standing_alone: Forms and Questionnaires > Questionnaire; rule: a form bound into a note is part of that note; this one also flips codeability, label: Procedure; in_a_note: Operative Note > Intra-operative Record; standing_alone: Procedure Note > (the specific procedure); rule: a procedure page in an operative document belongs to that operation

##### referral_pages

> Client direction 2026-10-24: a referral ORDER and a referral AUTHORIZATION are different documents from different issuers. The test is who issues the page.

* **referral_order**: path: Order and Requisition > Referral Order; issued_by: the PCP or another provider / practice; what_it_does: sends the patient to a specialist or service; signals: Referral Order, Referral Form, Request for Referral, Refer to / Referred To specialty, reason for referral, diagnosis and urgency, referring provider signature, a consult request written on a prescription pad
* **referral_authorization**: path: Consent and Authorization > Referral Authorization; issued_by: the insurance company, health plan, IPA or payer portal; what_it_does: approves, denies or pends a referral that a provider already ordered; signals: payer or plan letterhead / portal (UnitedHealthcare, Optum, Fidelis, Medicare Advantage), authorization or reference number, approved / denied / pended, visits requested vs authorized, validity dates, member ID, approval notification letter to the patient
* **prior_authorization**: path: Consent and Authorization > Prior Authorization; issued_by: the payer, or a pharmacy or imaging centre working a payer request; what_it_does: approval of a SERVICE, procedure, imaging study or medication rather than of a visit to another provider
* **referral_letter**: path: Provider Letter > Referral Letter; issued_by: a provider; what_it_does: a narrative letter to the specialist (Dear Dr ..., I would like to refer ...), not an order form
* **specialist_writes_back**: Provider Letter > Consultation Letter
* **both_on_one_page**: An EHR referral record that shows the order AND the payer decision (Authorized: 1, approved your referral) is Referral Authorization: the payer decision is what the page adds.
* **eligibility_check**: An eligibility or benefits check with no approval decision is Consent and Authorization > General Authorization (see form_routing).

##### notice_vs_consent

> Added 2026-10-23 at the client's instruction.

* **a notice about a policy, privacy, rights or responsibilities**: Consent and Authorization > General Consent
* **a notice about an event: closure, recall, results ready, service change**: Patient Communication > Notice
* **patient-facing teaching material about a condition or procedure**: Patient Communication > Patient Education Material
* **exception_hie**: An HIE notice is Consent and Authorization > HIPAA and Privacy (see hipaa_pages.hie_notice).

##### assessment_vs_questionnaire

* **rule**: Inside Forms and Questionnaires the L2 is decided by what the questions are about. Anything about a disease or clinical matter (symptoms, medical, surgical, family or ocular history, screening tools such as PHQ-2/PHQ-9/EPDS/MMSE/Mini-Cog/fall risk, health risk assessments, vaccine or contrast screening, pre-procedure clinical questions, growth charts, nutrition and functional status) is Assessment. Questions of a general kind with no clinical content (identity, insurance, employment, education, relationship, scheduling or practice workflow checklists) are Questionnaire.
* **mixed_page**: A page with both kinds goes to Assessment when any clinical question or score is on it.
* **comment**: Client direction 2026-10-24: "Anything about a disease or clinical - make it Assessment, any general question - Questionnaire."

#### consent_auth_side

> Who issues the page decides which word applies. The patient gives consent; the hospital, provider or payer gives authorization. This is a test about the signer, not about the word printed in the title: a page headed 'Authorization for Release of Information' is signed by the patient and is therefore a consent.

> Two L2s moved to the consent side under this rule despite carrying the word authorization in their usual titles. Release of Information is signed by the patient permitting a disclosure. Assignment of Benefits is signed by the patient assigning payment. Both are patient-given, so both are consents. Flagged because the naming reads the other way.

> A notice about a policy, privacy, patient rights or responsibilities is a consent wherever it sits, in a packet or on its own, and whether or not a signature line survived the scan (context_rules.notice_inside_consent_packet, applies_anywhere). Patient Communication > Notice keeps only event notices: a closure, a recall, a results-ready message, a service change.

* **rule**: patient_or_guardian_signs: consent; hospital_provider_or_payer_issues: authorization
* **consent**: Consent for Treatment, Surgical Consent, Transfusion Consent, Immunization Consent, HIPAA and Privacy, Release of Information, Assignment of Benefits, General Consent
* **authorization**: Prior Authorization, Referral Authorization, General Authorization
* **signature_line_evidence**: consent: patient signature, patient/guardian signature, signature of patient, patient or legal representative, guardian signature, signature of recipient, i acknowledge, i consent, i authorize the release, witness to patient signature; authorization: authorized representative of, provider signature, physician signature, utilization management, plan representative, payer signature, authorized by, approval number, reference number, valid from

#### generic_leaf_rule

> A page type whose name is just its own parent's name is the generic case, not a sibling of the specific ones. 'Endoscopy Report' next to Colonoscopy, Cystoscopy and Bronchoscopy, or 'Consent Form' next to Surgical and Transfusion Consent Form, is a label an annotator reaches for when unsure, which is what 'Others' is for. Structural, so it needs no list: if the label's meaningful words are a subset of the parent's, the leaf is blanked and the parent stands alone.

* **enabled**: True
* **stopwords**: report, record, note, form, page, result, study, list, summary, examination, exam, sheet, the, of, and
* **parent_also_covers**: Diagnostic Study: endoscopy, scope, imaging; Laboratory: laboratory, lab; Pathology: pathology; Radiology: radiology, imaging; Lab Result: laboratory, lab; Pathology Report: pathology; Radiology Report: radiology, imaging

#### lone_others_rule

> If 'Others' is the ONLY L2 under an L1 (or the only L3 under an L2) it carries no information: there is nothing for it to be other than. The level is dropped and the parent stands alone. Computed from the enumerated tree, so it stays correct as families are added.

* **enabled**: True

#### implied_specialty_rule

> An implied specialty may only sit on a fully specified path. A row with a blank L3, or any row whose L2 or L3 is Others, is a BUCKET: Diagnostic Study holds cardiology, sleep, GI and ophthalmology studies alike, and Others holds whatever did not fit, so inheriting one member's specialty and printing it against the whole bucket is wrong. Taxonomy table only. At inference a page resolving into a bucket still gets its specialty from the detector reading the page, which is the right source for it.

* **require_L3**: True
* **exclude_others**: True

#### l2_catch_all_rule

> Where an L2 has named L3 children, its catch-all child is 'Others', not a blank. A blank L3 next to named siblings reads as missing data; Others reads as a decision. Where an L2 has no L3 children at all the L3 stays blank, because there is nothing to be other than.

* **enabled**: True

#### no_L2_others

> L1s whose L2 set is closed: every page lands in one of the named buckets.

* **values**: Lab Report

#### flat_L1

> L1s with no L2 at all. None since Problem List gained one on 2026-10-23.

* **values**: 

#### L2_from_page_type

> L1s that were promoted out of Misc. Their subtree moved up a level, so the page type IS the L2 and L3 stays blank. Without this they would collapse to a single 'Others' L2.

* **values**: Patient Communication, Medication Record, Order and Requisition, Others, Problem List

#### collapse_L3

> L2 buckets that never expand to L3; L3 stays blank. Only the buckets with real page volume on the 624-page labelled set keep an L3 leaf, so this list is data, not logic in code.

* **Demographics**: Patient Information, Face Sheet, Others
* **Immunization**: Immunization Record, Others
* **Operative Note**: Surgery Report, Procedure List, Pre-operative, Post-operative, Anesthesia Record, Critical Care Record, Triage Report, Others
* **Administrative**: Appointment Record, Billing & Invoices, Patient Registration, Others
* **Forms and Questionnaires**: Assessment, Questionnaire, Others
* **Problem List**: Problem List, Diagnosis List/Index, Others
* **Medication Record**: Medication List, Prescription Page, Refill Record, Blood Transfusion Record, Others
* **Order and Requisition**: Physician Order, Laboratory Requisition, Referral Order, Others
* **Others**: Others
* **Discharge Summary**: Depart/Discharge Note, Discharge Instruction, Discharge Medication, Transfer of Care, Interim Summary, Short Stay Summary, Discharge Diagnosis, Others
* **Patient Communication**: Patient Message, Notice, Patient Education Material, Others
* **Procedure Note**: Dialysis Record, Infusion and Chemotherapy, Radiation Therapy, Wound Care, Therapy Session, Elastography, Injection Visit, Knee Procedure, Laser Procedure, Lumbar Procedure, Office Procedure, Pathology Procedure, Endoscopy (EGD), Others
* **Consent and Authorization**: Consent for Treatment, Surgical Consent, Transfusion Consent, Immunization Consent, HIPAA and Privacy, Release of Information, Assignment of Benefits, General Consent, Prior Authorization, Referral Authorization, General Authorization, Others
* **Provider Letter**: Referral Letter, Consultation Letter, Others

#### Other notes in the rules file

* **note**: Page type hierarchy, v10. Three principles settled here. (1) A diagnostic procedure that produces a report is a RESULT: colonoscopy, catheterisation, endoscopy and laryngoscopy sit under Lab Report > Diagnostic Study, not under Operative Note. Operative Note is for the operation itself. (2) A level that only names a service line is not a level: Cardiac Diagnostic and Neurodiagnostic are gone as L2s, folded into Diagnostic Study with the specialty carrying the service line. (3) No two L2 labels under one L1 may mean the same thing, so near-synonyms are merged rather than listed side by side. 'Others' sorts last at every level.
* **_comment_specialty**: Others = the page names a service line that is not one of the listed values. Unknown = no specialty signal on the page at all. They are different outcomes and the annotation tool must keep them apart: Others is a gap in this list, Unknown is a gap in the page.
* **_comment_page_artefact**: Fax covers, blank pages, index and separator pages are removed by the junk filter before classification, so they carry no page type here. The canon keeps its Page Artefact entries so the filter has something to match on. Also removed as junk (client direction 2026-10-24): insurance card scans, and image-only pages that carry no reading (X-ray or scan films, endoscopy photo pages, tracings with no measurements or interpretation). A report page that has findings text next to its images is kept. Also junk by rule (client direction 2026-10-24, after Charts 16-20): payer record-retrieval fax covers (e.g. Optum retrieval cover sheets) and EHR import cover, metadata and imported-items index pages. Blank, signature-only and footer-only report tails are junk.
* **_comment_procedure_note**: Procedure Note is a treatment RECORD: a repeated therapeutic session or an administration, which is neither an operation (Operative Note) nor an encounter narrative (Progress Note). Dialysis is the clearest case: a run is a procedure performed on a schedule, logged on a flow sheet, not a visit written up. Infusion, chemotherapy, radiation, wound care and the therapy disciplines behave the same way. The specialty still rides on the parallel axis, so Dialysis, Oncology, Wound Care and Rehabilitation are not lost by moving the document here.
* **_comment_procedure_L3**: Procedure Note has no L3: the specific procedure IS the L2. A knee injection and a lumbar epidural are different procedures and different codes, so they sit at the same level as a dialysis run rather than one tier below it.
* **_comment_procedure_scope**: Only procedures observed in the client's own page-type list or production volumes are carried. Site and modality variants that were not observed are deliberately absent: an empty class cannot be learned by the model and lengthens the annotator's dropdown for nothing. Add one when a page for it actually appears.
* **_comment_specialty_sweep**: A page type implies a specialty only when the study itself names a service line. A mammogram is breast imaging and belongs to oncology; a CT is a CT and could be ordered by anyone. The deliberately unassigned ones are listed below so the decision is visible rather than looking like an omission: on those pages the specialty comes from the letterhead via the detector, or from the document by inheritance, or it is Unknown.
* **_comment_podiatry**: Foot and ankle care. Named Podiatry rather than 'Foot and Ankle' because the letterhead evidence is the DPM credential and the word podiatry; 'foot and ankle' is carried as an alias. It overlaps Orthopedics by body site, so the rule is the provider, not the anatomy: a DPM or a podiatry practice is Podiatry, an orthopedic surgeon operating on a foot is Orthopedics. Rename to 'Foot and Ankle' in this list and in specialty.json if the client prefers that wording.
* **_comment_administrative_scope**: Administrative means money or scheduling, nothing else. Billing statements, invoices, superbills, statements of account, payment plans; appointment lists, no-show letters, scheduling notes. The old 'Administrative Record' catch-all was removed on 2026-10-23 because it had become a home for anything non-clinical: 15 of its 19 pages were financial-policy and patient-rights pages the patient signs, which are consents, and insurance verification pages, which are authorizations. A catch-all under a vague L1 attracts exactly the pages that belong somewhere specific.
* **_comment_problem_list**: Problem List took an L2 on 2026-10-23: a diagnosis list or index is a different page from a running problem list, and the client codes from them differently.
* **_comment_face_sheet**: A face sheet carries the encounter and the provider as well as the patient; a patient demographics page is the patient alone. That is the whole distinction.
* **_comment_provider_letter**: Split out of Patient Communication on 2026-10-23: a letter between clinicians is not communication with the patient. Referral and consultation letters sit inside it.
* **_comment_L3_2026_10_23**: Sleep Clinic Note folded into Sleep Study Report, and Gastroenterology Procedure Note into Endoscopy Report: a note is a Progress Note whatever service produced it, so neither belonged in a results tree. Laboratory Report and Laboratory Result deleted as leaves, since they only repeated their parent. Stress Test, Treadmill and Myocardial Perfusion are one study under three names.
* **_comment_ed_note**: ED Note is the page type; the L1 stays Progress Note and Emergency is the specialty, because a note is a Progress Note whatever service produced it.
* **_comment_2026_10_24**: Consistency fixes, no new design decisions: procedure_note_L2: removed duplicate EGD (merged into Endoscopy (EGD)) \| consent_auth_side._comment_notice: aligned with the 2026-10-23 rule (signature not required) \| collapse_L3[Operative Note]: ['Surgery Report', 'Procedure', 'Pre-operative', 'Post-operative', 'Anesthesia Record', 'Critical Care Record', 'Triage Report', 'Others'] -> ['Surgery Report', 'Procedure List', 'Pre-operative', 'Post-operative', 'Anesthesia Record', 'Critical Care Record', 'Triage Report', 'Others'] \| collapse_L3[Problem List]: [] -> ['Problem List', 'Diagnosis List/Index', 'Others'] \| collapse_L3[Order and Requisition]: ['Others'] -> ['Physician Order', 'Laboratory Requisition', 'Referral Order', 'Others'] \| collapse_L3[Discharge Summary]: ['Depart/Discharge Note', 'Discharge Instruction', 'Discharge Medication', 'Transfer of Care', 'Interim Summary', 'Short Stay Summary', 'Others'] -> ['Depart/Discharge Note', 'Discharge Instruction', 'Discharge Medication', 'Transfer of Care', 'Interim Summary', 'Short Stay Summary', 'Discharge Diagnosis', 'Others'] \| collapse_L3[Patient Communication]: ['Provider Letter', 'Patient Message', 'Notice', 'Patient Education Material', 'Others'] -> ['Patient Message', 'Notice', 'Patient Education Material', 'Others'] \| collapse_L3[Consent and Authorization]: ['Surgical Consent', 'Transfusion Consent', 'Immunization Consent', 'HIPAA and Privacy', 'Release of Information', 'Prior Authorization', 'Referral Authorization', 'Assignment of Benefits', 'Others'] -> ['Consent for Treatment', 'Surgical Consent', 'Transfusion Consent', 'Immunization Consent', 'HIPAA and Privacy', 'Release of Information', 'Assignment of Benefits', 'General Consent', 'Prior Authorization', 'Referral Authorization', 'General Authorization', 'Others'] \| collapse_L3[Provider Letter]: None -> ['Referral Letter', 'Consultation Letter', 'Others'] \| progress_note.from_sections[lab_results]: Lab Results -> Lab and Imaging Results \| progress_note.from_sections[imaging_results]: Imaging Results -> Lab and Imaging Results \| progress_note.from_sections[procedures]: Procedure -> Procedure List \| progress_note.from_sections[vitals]: Physical Exam -> Vitals \| L2_merge[Order and Requisition][Insurance Authorization Note]: __move:Authorization -> __move:Consent and Authorization \| L2_merge[Order and Requisition][Referral Authorization Form]: __move:Authorization -> __move:Consent and Authorization \| lab_report_L3: added explicit list of defined leaves (62 incl. 4 Others), taken from the canon, L3_additions and the L3 values the overrides already name; Cardiac Catheterization Report added; Pap and HPV Report merged into Cytology Report; undocumented merges recorded in L3_merge \| generic_leaf_rule.parent_also_covers: added current L2 names (Lab Result, Pathology Report, Radiology Report) \| family_map: Esophagogastroduodenoscopy Report and EGD now point at Endoscopy (EGD) \| page_type_overrides: 'Audiogram and Hearing Test' and 'Optical Coherence Tomography' renamed to the L3_additions names ('Audiogram and Hearing Loss Report', 'Optical Coherence Tomography Report'); the same study had two names
* **_comment_demographics_2026_10_24**: Client direction 2026-10-24: Demographics L2 is Patient Information, Face Sheet, Others. A page that carries only who the patient is (name, DOB, address, contact) is Patient Information. A Face Sheet carries that plus other blocks: insurance or subscriber, guarantor, emergency contact or next of kin, employer, attending physician or admission details. Two or more of those blocks make it a Face Sheet.
* **_comment_hp_2026_10_24**: Client direction 2026-10-24: Discharge Summary gains L2 History and Physical for the admission H&P of an inpatient, observation or short stay. See context_rules.admission_history_and_physical. Office H&Ps stay Progress Note.
* **_comment_taxonomy_2026_10_24b**: Client approved 2026-10-24 (after Charts 4-7): Operative Note > Intra-operative Record; Procedure Note > Cardiac Procedure; Discharge Summary > Consultation Note (inpatient); Provider Letter > Records Request; Patient Communication > Telephone Encounter; Lab Result leaf INR and Prothrombin Time renamed Coagulation Report; specialty Pain Management; context rules inpatient_consultation, combined_endoscopy, telephone_encounter.
* **_comment_taxonomy_2026_10_24c**: Client approved 2026-10-24 (after Charts 8-11): new L1 Clinical Summary (Summary of Care (C-CDA), After-Visit Summary, Others); Diagnostic Study leaves Optical Biometry (IOL Calculation) and Corneal Topography; Operative Note > Implant Record; Administrative L2 Billing Statement renamed Billing & Invoices (cost estimates, statements, invoices, superbills); Ophthalmology covers optometry (one combined specialty).
* **_comment_referral_2026_10_24**: Client direction 2026-10-24: Referral Order (provider/PCP issues) and Referral Authorization (insurance/payer issues) are kept apart by issuer. See mece_rules.referral_pages. 8 pages corrected.
* **_comment_specialty_2026_10_24**: Client direction 2026-10-24: Surgery, Critical Care and Anesthesia are set from the page type when no letterhead decides; self-assessments done in the home are Home Health. See context_rules.specialty_from_page_type.
* **_comment_forms_2026_10_24**: Client direction 2026-10-24: Forms and Questionnaires L2 split by content (mece_rules.assessment_vs_questionnaire). Growth Chart, Clock Drawing Test, MMSE, screening records and Health History Form now map to Assessment; a plain Checklist stays Questionnaire.
* **_comment_cleanup_2026_10_24k**: Client direction 2026-10-24: Myocardial Perfusion Stress Test combined into one leaf Stress Test (exercise, pharmacologic, nuclear or echo); insurance card scans and image-only pages are junk; DME paperwork goes to Order and Requisition > Others; IR procedure reports are Procedure Note > Others.
* **_comment_page_essence_2026_10_24**: Client direction 2026-10-24: page-essence rule (context_rules.page_essence) for lab, medication, imaging and order pages inside documents.
* **_comment_decisions_2026_10_24r**: Client decisions after Charts 16-20: (1) Superbill is its own L2 under Administrative (encounter form with ICD-10 and CPT codes); invoices, statements and estimates stay Billing & Invoices. (2) Payer retrieval fax covers and EHR import cover/metadata pages are junk by rule. (3) Provider Letter > Records Request is defined by purpose: any page whose purpose is requesting records (letter, fax or EHR note). (4) Ambulatory surgery centre wording sets the encounter to Outpatient (F2F) (encounter_canon.json). (5) Surgery scheduling forms are Administrative > Appointment Record.

### D. Canon page type → path (the conversion tables)

How a keyword-canon page type became its path in page_canon.json (`canon_convert.py`). `page_type_overrides` names one page type; `family_map` covers a whole family. An override wins over the family.

> Page types that move somewhere other than their family's default. Under v5 the long list of specialty-note overrides collapses: every one of them is Progress Note with a specialty, and the L2 comes from the page content, so only the specialty needs stating.

#### D.1 page_type_overrides

| Canon page type | L1 | L2 | L3 |
|---|---|---|---|
| Acupuncture Treatment Record | Procedure Note | Therapy Session |  |
| Admission Face Sheet | Demographics | Face Sheet |  |
| Admission Record | Progress Note | Summary |  |
| After Visit Summary | Clinical Summary | After-Visit Summary |  |
| Allergy Testing Report | Lab Report | Lab Result | Allergy Testing Report |
| Ambulatory Correspondence | Provider Letter | Others |  |
| Anesthesia Evaluation | Operative Note | Pre-operative |  |
| Anesthesia Record | Operative Note | Anesthesia Record |  |
| Anesthesia Transfer of Care Note | Operative Note | Anesthesia Record |  |
| Ankle-Brachial Index Report | Lab Report | Diagnostic Study | Ankle-Brachial Index Report |
| Antepartum Record | Progress Note |  |  |
| Assignment of Benefits | Consent and Authorization | Assignment of Benefits |  |
| Audiogram | Lab Report | Diagnostic Study | Audiogram and Hearing Loss Report |
| Audiology Report | Progress Note |  |  |
| Authorization Form | Consent and Authorization | General Authorization |  |
| Behavioral Assessment | Lab Report | Diagnostic Study | Behavioral Assessment |
| Blood Product Administration Record | Medication Record | Blood Transfusion Record |  |
| Blood Transfusion Record | Medication Record | Blood Transfusion Record |  |
| Bone Mineral Density Report | Lab Report | Radiology Report | Bone Mineral Density Report |
| Brain MRI Report | Lab Report | Radiology Report | Brain MRI Report |
| Bronchoscopy Report | Lab Report | Diagnostic Study | Bronchoscopy Report |
| Burn Treatment Record | Procedure Note | Wound Care |  |
| CT Angiography Report | Lab Report | Radiology Report | CT Angiography Report |
| Cardiac Catheterization Report | Lab Report | Diagnostic Study | Cardiac Catheterization Report |
| Cardiac Rehabilitation Note | Procedure Note | Therapy Session |  |
| Cardiology Note | Progress Note |  |  |
| Care Coordination Note | Progress Note | Others |  |
| Care Plan | Progress Note | Assessment and Plan |  |
| Carotid Duplex Report | Lab Report | Diagnostic Study | Carotid Duplex Report |
| Case Management Note | Progress Note | Others |  |
| Charge Sheet | Administrative | Superbill |  |
| Chemotherapy Administration Record | Procedure Note | Infusion and Chemotherapy |  |
| Chiropractic Treatment Note | Procedure Note | Therapy Session |  |
| Clinical Diagnosis List | Problem List | Diagnosis List/Index |  |
| Clock Drawing Test | Forms and Questionnaires | Assessment |  |
| Coding Record | Administrative | Others |  |
| Coding Summary | Administrative | Others |  |
| Colonoscopy Report | Lab Report | Diagnostic Study | Colonoscopy Report |
| Colposcopy Examination | Lab Report | Diagnostic Study |  |
| Consent Form | Consent and Authorization | General Consent |  |
| Consent for Treatment | Consent and Authorization | Consent for Treatment |  |
| Consent to Treat | Consent and Authorization | Consent for Treatment |  |
| Consulta | Progress Note |  |  |
| Consultation Letter | Provider Letter | Referral Letter |  |
| Consultation Note | Progress Note |  |  |
| Critical Care Note | Progress Note |  |  |
| Cystoscopy Report | Lab Report | Diagnostic Study | Cystoscopy Report |
| Dermatology Note | Progress Note |  |  |
| Device Specialist Note | Progress Note |  |  |
| Diagnosis Index | Problem List | Diagnosis List/Index |  |
| Diagnosis List | Problem List | Diagnosis List/Index |  |
| Dialysis Flow Sheet | Procedure Note | Dialysis Record |  |
| Dialysis Treatment Record | Procedure Note | Dialysis Record |  |
| Dietary Note | Progress Note |  |  |
| Dietitian Note | Progress Note |  |  |
| Doppler Duplex Report | Lab Report | Diagnostic Study | Doppler Duplex Report |
| ED Note | Progress Note |  |  |
| ED Provider Note | Progress Note |  |  |
| EGD Report | Lab Report | Diagnostic Study | Esophagogastroduodenoscopy (EGD) Report |
| Elastography Procedure Report | Procedure Note | Elastography |  |
| Emergency Department Note | Progress Note |  |  |
| Emergency Medical Services Report | Progress Note |  |  |
| Emergency Note | Progress Note |  |  |
| Endoscopy Report | Lab Report | Diagnostic Study | Endoscopy Report |
| Esophageal Manometry Report | Lab Report | Diagnostic Study | Esophageal Manometry Report |
| Esophagogastroduodenoscopy Report | Lab Report | Diagnostic Study | Esophagogastroduodenoscopy (EGD) Report |
| Extremity Doppler Report | Lab Report | Diagnostic Study | Extremity Doppler Report |
| Eye Examination Report | Progress Note |  |  |
| FISH Report | Lab Report | Lab Result | FISH Report |
| Face Sheet | Demographics | Face Sheet |  |
| Face to Face Evaluation Note | Progress Note |  |  |
| Fitness for Duty Evaluation | Forms and Questionnaires | Assessment |  |
| Functional Status Report | Forms and Questionnaires | Assessment |  |
| Fundus Photography Report | Lab Report | Diagnostic Study |  |
| Gastroenterology Procedure Note | Procedure Note | Office Procedure |  |
| Genetic Counseling Note | Progress Note |  |  |
| Genetic Test Report | Lab Report | Lab Result | Genetic Test Report |
| Growth Chart | Forms and Questionnaires | Assessment |  |
| HIPAA Form | Consent and Authorization | HIPAA and Privacy |  |
| Health History Form | Forms and Questionnaires | Assessment |  |
| Hearing Test Report | Lab Report | Diagnostic Study | Audiogram and Hearing Loss Report |
| Hemodialysis Flow Sheet | Procedure Note | Dialysis Record |  |
| Home Health Care Evaluation | Forms and Questionnaires | Assessment |  |
| Home Health Task List and Time Record | Progress Note | Others |  |
| Home Health Visit Note | Progress Note |  |  |
| Hospice Care Record | Progress Note |  |  |
| Housecalls Visit Summary | Progress Note |  |  |
| ICU Flow Sheet | Operative Note | Critical Care Record |  |
| Immunization Consent Form | Consent and Authorization | Immunization Consent |  |
| Immunization History | Immunization | Immunization Record |  |
| Immunization List | Immunization | Immunization Record |  |
| Immunization Record | Immunization | Immunization Record |  |
| Implant Record | Operative Note | Others |  |
| Infusion Record | Procedure Note | Infusion and Chemotherapy |  |
| Infusion Therapy Note | Procedure Note | Infusion and Chemotherapy |  |
| Injection Visit | Procedure Note | Injection Visit |  |
| Insurance Authorization Note | Consent and Authorization | Prior Authorization |  |
| Interdisciplinary Plan of Care | Progress Note | Assessment and Plan |  |
| Intraoperative Record | Operative Note | Procedure List |  |
| Intraoperative Report | Operative Note | Procedure List |  |
| Knee Procedure Note | Procedure Note | Knee Procedure |  |
| Laryngoscopy Procedure Report | Lab Report | Diagnostic Study | Laryngoscopy Procedure Report |
| Laser Procedure Report | Procedure Note | Laser Procedure |  |
| Laser Report | Procedure Note | Laser Procedure |  |
| Lens Parameters | Lab Report | Diagnostic Study |  |
| Lumbar Procedure Note | Procedure Note | Lumbar Procedure |  |
| MR Angiography Report | Lab Report | Radiology Report | MR Angiography Report |
| Mammogram Report | Lab Report | Radiology Report | Mammogram Report |
| Medical Nutrition Therapy Note | Progress Note |  |  |
| Medical Record Request | Others |  |  |
| Medicare Annual Wellness Visit | Forms and Questionnaires | Assessment |  |
| Medication Reconciliation | Medication Record | Medication List |  |
| Mohs Surgery Report | Operative Note | Surgery Report |  |
| Myocardial Perfusion Study | Lab Report | Radiology Report | Nuclear Medicine Report |
| Nephrology Note | Progress Note |  |  |
| Neurology Note | Progress Note |  |  |
| Nurses Notes | Progress Note |  |  |
| Nursing Assessment for Home Care | Forms and Questionnaires | Assessment |  |
| Nursing Care Plan | Progress Note | Assessment and Plan |  |
| Nursing Note | Progress Note |  |  |
| Nursing Visit Note | Progress Note | Others |  |
| Nutrition Assessment | Forms and Questionnaires | Assessment |  |
| Nutrition Note | Progress Note |  |  |
| OR Summary | Operative Note | Procedure List |  |
| Occupational Health Record | Forms and Questionnaires | Assessment |  |
| Occupational Therapy Daily Note | Procedure Note | Therapy Session |  |
| Occupational Therapy Discharge Summary | Discharge Summary | Depart/Discharge Note |  |
| Occupational Therapy Note | Procedure Note | Therapy Session |  |
| Office Procedure Report | Procedure Note | Office Procedure |  |
| Oncology Note | Progress Note |  |  |
| Operating Room Summary | Operative Note | Procedure List |  |
| Operative Note | Operative Note | Surgery Report |  |
| Operative Report | Operative Note | Surgery Report |  |
| Optical Coherence Tomography | Lab Report | Diagnostic Study | Optical Coherence Tomography Report |
| Oximetry Report | Lab Report | Diagnostic Study |  |
| Pain Clinic Note | Progress Note |  |  |
| Pain Management Consultation | Progress Note |  |  |
| Pain Management Note | Progress Note |  |  |
| Palliative Care Note | Progress Note |  |  |
| Pap and HPV Report | Lab Report | Diagnostic Study |  |
| Pathology Procedure Note | Procedure Note | Pathology Procedure |  |
| Patient Agreement Form | Consent and Authorization | General Consent |  |
| Patient Demographics | Demographics | Patient Information |  |
| Patient Registration | Administrative | Patient Registration |  |
| Perioperative Evaluation | Operative Note | Pre-operative |  |
| Peritoneal Dialysis Record | Procedure Note | Dialysis Record |  |
| Phase Record | Operative Note | Procedure List |  |
| Physical Therapy Discharge Summary | Discharge Summary | Depart/Discharge Note |  |
| Physical Therapy Note | Procedure Note | Therapy Session |  |
| Plan of Care | Progress Note | Assessment and Plan |  |
| Plethysmography Report | Lab Report | Diagnostic Study | Plethysmography Report |
| Policy Page | Consent and Authorization | General Consent |  |
| Post-Anesthesia Care Record | Operative Note | Anesthesia Record |  |
| Postoperative Follow-up Note | Operative Note | Post-operative |  |
| Postoperative Order | Operative Note | Post-operative |  |
| Postoperative Procedure Note | Operative Note | Post-operative |  |
| Postpartum Visit | Progress Note |  |  |
| Pre-Admission Testing Record | Operative Note | Pre-operative |  |
| Pre-Anesthesia Evaluation | Operative Note | Pre-operative |  |
| Prenatal Record | Progress Note |  |  |
| Preoperative Evaluation | Operative Note | Pre-operative |  |
| Preoperative Order | Operative Note | Pre-operative |  |
| Prior Authorization Request | Consent and Authorization | Prior Authorization |  |
| Procedure Note | Procedure Note | Others |  |
| Provider Letter | Provider Letter | Others |  |
| Psychiatric Evaluation | Progress Note |  |  |
| Pulmonary Function Test | Lab Report | Diagnostic Study | Pulmonary Function Test |
| Radiation Therapy Record | Procedure Note | Radiation Therapy |  |
| Records Request Letter | Others |  |  |
| Referral Authorization | Consent and Authorization | Referral Authorization |  |
| Referral Authorization Form | Consent and Authorization | Referral Authorization |  |
| Referral Letter | Provider Letter | Referral Letter |  |
| Registration Form | Administrative | Patient Registration |  |
| Registration Page | Administrative | Patient Registration |  |
| Rehabilitation Plan | Progress Note | Assessment and Plan |  |
| Rehabilitation Treatment Note | Procedure Note | Therapy Session |  |
| Release of Information Authorization | Consent and Authorization | Release of Information |  |
| Respiratory Therapy Note | Procedure Note | Therapy Session |  |
| Resumen de Alta | Discharge Summary | Depart/Discharge Note |  |
| Resumen de Instruccion | Discharge Summary | Discharge Instruction |  |
| Retinal Office Procedure Note | Procedure Note | Office Procedure |  |
| Short Stay Record | Discharge Summary | Short Stay Summary |  |
| Short Stay Summary | Discharge Summary | Short Stay Summary |  |
| Sigmoidoscopy Report | Lab Report | Diagnostic Study | Sigmoidoscopy Report |
| Signature Page | Progress Note | Signature |  |
| Skilled Nursing Visit Note | Progress Note |  |  |
| Sleep Clinic Note | Progress Note |  |  |
| Sleep Study Report | Lab Report | Diagnostic Study |  |
| Speech Therapy Note | Procedure Note | Therapy Session |  |
| Split Night Study Report | Lab Report | Diagnostic Study |  |
| Superbill | Administrative | Superbill |  |
| Surgery Report | Operative Note | Surgery Report |  |
| Surgical Consent Form | Consent and Authorization | Surgical Consent |  |
| Surgical Pathology Procedure | Operative Note | Surgery Report |  |
| Telemetry Record | Progress Note | Physical Exam |  |
| Telemetry Report | Lab Report | Diagnostic Study | Telemetry Report |
| Telemetry Strip | Lab Report | Diagnostic Study | Telemetry Report |
| Therapy Discharge Summary | Discharge Summary | Depart/Discharge Note |  |
| Therapy Report | Procedure Note | Therapy Session |  |
| Titration Report | Lab Report | Diagnostic Study |  |
| Transfusion Consent Form | Consent and Authorization | Transfusion Consent |  |
| Transfusion Flow Sheet | Medication Record | Blood Transfusion Record |  |
| Transfusion Record | Medication Record | Blood Transfusion Record |  |
| Transition of Care Summary | Clinical Summary | Summary of Care (C-CDA) |  |
| Travel Medicine Consultation | Progress Note |  |  |
| Treatment Consent | Consent and Authorization | Consent for Treatment |  |
| Treatment Plan | Progress Note | Assessment and Plan |  |
| Triage Assessment | Operative Note | Triage Report |  |
| Triage Call Note | Operative Note | Triage Report |  |
| Triage Note | Operative Note | Triage Report |  |
| Triage Report | Operative Note | Triage Report |  |
| Urology Note | Progress Note |  |  |
| Utilization Review Note | Consent and Authorization | Prior Authorization |  |
| Vaccination Administration Record | Immunization | Immunization Record |  |
| Vaccination Record | Immunization | Immunization Record |  |
| Ventilator Management Record | Operative Note | Critical Care Record |  |
| Visual Evaluation | Progress Note |  |  |
| Visual Field Interpretation | Lab Report | Diagnostic Study |  |
| Visual Field Report | Lab Report | Diagnostic Study | Visual Field Report |
| Vital Signs Record | Progress Note | Physical Exam |  |
| Web Encounter | Patient Communication | Patient Message |  |
| Work Injury Evaluation | Forms and Questionnaires | Assessment |  |
| Wound Care Note | Procedure Note | Wound Care |  |

#### D.2 family_map

> page_family -> L1 and, outside Progress Note, L2. Progress Note families carry no L2 here: their L2 comes from the section tagger. 'specialty' is set only where the family was really a service line, and the document then takes a generic path.

| Canon family | L1 | L2 | L3 |
|---|---|---|---|
| Admission Record | Progress Note |  |  |
| Ambulatory Correspondence | Provider Letter | Others |  |
| Anesthesia Record | Operative Note | Anesthesia Record |  |
| Appointment List | Administrative | Appointment Record |  |
| Appointment Note | Administrative | Appointment Record |  |
| Appointment Record | Administrative | Appointment Record |  |
| Assessment and Evaluation | Forms and Questionnaires | Assessment |  |
| Audiogram | Lab Report | Diagnostic Study | Audiogram and Hearing Loss Report |
| Authorization Form | Consent and Authorization |  |  |
| Behavioral Assessment | Lab Report | Diagnostic Study | Behavioral Assessment |
| Behavioral Health Note | Progress Note |  |  |
| Billing Statement | Administrative | Billing & Invoices |  |
| Cancellation Notice | Administrative | Appointment Record |  |
| Cardiac Diagnostic Test | Lab Report | Diagnostic Study |  |
| Care Coordination Note | Progress Note | Others |  |
| Case Management Note | Progress Note | Others |  |
| Clinical Diagnosis List | Problem List | Diagnosis List/Index |  |
| Coding Record | Administrative | Others |  |
| Coding Summary | Administrative | Others |  |
| Consent Form | Consent and Authorization |  |  |
| Consent for Treatment | Consent and Authorization | Consent for Treatment |  |
| Consent to Treat | Consent and Authorization | Consent for Treatment |  |
| Consultation Letter | Provider Letter | Consultation Letter |  |
| Critical Care Record | Progress Note |  |  |
| Diagnosis Index | Problem List | Diagnosis List/Index |  |
| Diagnosis List | Problem List | Diagnosis List/Index |  |
| Diagnostic Study | Lab Report | Diagnostic Study |  |
| Dialysis Record | Procedure Note | Dialysis Record |  |
| Discharge Diagnoses | Discharge Summary | Discharge Diagnosis |  |
| Discharge Diagnosis | Discharge Summary | Discharge Diagnosis |  |
| Discharge Diagnosis List | Discharge Summary | Discharge Diagnosis |  |
| Discharge Instructions | Discharge Summary | Discharge Instruction |  |
| Discharge Medication | Discharge Summary | Discharge Medication |  |
| Discharge Summary | Discharge Summary | Depart/Discharge Note |  |
| ED Note | Progress Note |  |  |
| EGD | Procedure Note | Endoscopy (EGD) |  |
| EGD Report | Lab Report | Diagnostic Study | Esophagogastroduodenoscopy (EGD) Report |
| Emergency Department Note | Progress Note |  |  |
| Endoscopy Report | Lab Report | Diagnostic Study |  |
| Esophagogastroduodenoscopy Report | Procedure Note | Endoscopy (EGD) |  |
| Explanation of Benefits | Administrative | Billing & Invoices |  |
| Eye Examination | Progress Note |  |  |
| Final Diagnoses | Discharge Summary | Discharge Diagnosis |  |
| Final Diagnosis | Discharge Summary | Discharge Diagnosis |  |
| Flow Sheet | Operative Note | Critical Care Record |  |
| Hearing Test Report | Lab Report | Diagnostic Study | Audiogram and Hearing Loss Report |
| Home Care Progress Note | Progress Note |  |  |
| Home Health Certification | Order and Requisition | Physician Order |  |
| Home Health Visit Note | Progress Note |  |  |
| Home Visit Note | Progress Note |  |  |
| Hospital Discharge Diagnosis | Discharge Summary | Discharge Diagnosis |  |
| Immunization Record | Immunization | Immunization Record |  |
| Implant Record | Operative Note | Others |  |
| Infusion Therapy Record | Procedure Note | Infusion and Chemotherapy |  |
| Interim Summary | Discharge Summary | Interim Summary |  |
| Invoice Page | Administrative | Billing & Invoices |  |
| Laboratory Report | Lab Report | Lab Result |  |
| Medical Record Request | Others |  |  |
| Medication Record | Medication Record |  |  |
| Neurodiagnostic Test | Lab Report | Diagnostic Study |  |
| No Show Letter | Administrative | Appointment Record |  |
| Nuclear Medicine Report | Lab Report | Radiology Report |  |
| Nursing Note | Progress Note |  |  |
| Obstetric and Gynecologic Record | Progress Note |  |  |
| Operative Report | Operative Note | Surgery Report |  |
| Optical Coherence Tomography | Lab Report | Diagnostic Study | Optical Coherence Tomography Report |
| Order and Requisition | Order and Requisition |  |  |
| Palliative Care Note | Progress Note |  |  |
| Pathology Report | Lab Report | Pathology Report |  |
| Patient Communication | Patient Communication |  |  |
| Patient Demographics | Demographics | Patient Information |  |
| Patient Registration | Administrative | Patient Registration |  |
| Payment Plan | Administrative | Billing & Invoices |  |
| Perioperative Evaluation | Operative Note | Pre-operative |  |
| Plan of Care | Progress Note |  |  |
| Plan of Treatment (Home Health) | Progress Note | Assessment and Plan |  |
| Prehospital Record | Progress Note |  |  |
| Principal Diagnosis | Discharge Summary | Discharge Diagnosis |  |
| Principal Discharge Diagnosis | Discharge Summary | Discharge Diagnosis |  |
| Problem List | Problem List |  |  |
| Procedure Note | Procedure Note |  |  |
| Progress Note | Progress Note |  |  |
| Provider Letter | Provider Letter | Others |  |
| Radiology Report | Lab Report | Radiology Report |  |
| Records Request Letter | Others |  |  |
| Referral Letter | Provider Letter | Referral Letter |  |
| Registration Form | Administrative | Patient Registration |  |
| Scheduling Note | Administrative | Appointment Record |  |
| Screening and Checklist | Forms and Questionnaires | Assessment |  |
| Sleep Study | Lab Report | Diagnostic Study |  |
| Specialty Note | Progress Note |  |  |
| Statement of Account | Administrative | Billing & Invoices |  |
| Superbill | Administrative | Superbill |  |
| Telemetry Report | Lab Report | Diagnostic Study | Telemetry Report |
| Telemetry Strip | Lab Report | Diagnostic Study | Telemetry Report |
| Telephone Encounter | Progress Note |  |  |
| Therapy Discharge Summary | Discharge Summary | Depart/Discharge Note |  |
| Therapy Note | Procedure Note | Therapy Session |  |
| Transfer of Care Summary | Discharge Summary | Transfer of Care |  |
| Treatment Consent | Consent and Authorization | Consent for Treatment |  |
| Treatment Consent Form | Consent and Authorization | Consent for Treatment |  |
| Unknown | Others |  |  |
| Utilization Review Note | Consent and Authorization | Prior Authorization |  |
| Visual Field Report | Lab Report | Diagnostic Study | Visual Field Report |
| Vitals and Telemetry | Progress Note |  |  |
| Wound Care Note | Procedure Note | Wound Care |  |

#### D.L2_merge

> Near-synonymous page types collapsed onto one L2 label, so no two L2s under an L1 can mean the same thing. An annotator who cannot tell two labels apart will use them interchangeably, and the model then learns a coin flip.

| Under | Old | Merged into |
|---|---|---|
| Order and Requisition | Order Note | Physician Order |
| Order and Requisition | Service Request | Physician Order |
| Order and Requisition | Physician Order | Physician Order |
| Order and Requisition | Laboratory Form | Laboratory Requisition |
| Order and Requisition | Laboratory Requisition | Laboratory Requisition |
| Order and Requisition | Insurance Authorization Note | __move:Consent and Authorization |
| Order and Requisition | Referral Authorization Form | __move:Consent and Authorization |
| Order and Requisition | Referral Order | Referral Order |
| Order and Requisition | Referral Form | Referral Order |
| Order and Requisition | Referral Request | Referral Order |
| Order and Requisition | Specialist Referral | Referral Order |
| Order and Requisition | Consultation Request | Referral Order |
| Order and Requisition | Imaging Order | Physician Order |
| Order and Requisition | Radiology Order | Physician Order |
| Order and Requisition | Medication Order | Physician Order |
| Order and Requisition | Nursing Order | Physician Order |
| Order and Requisition | Therapy Order | Physician Order |
| Problem List | Diagnosis List | Diagnosis List/Index |
| Problem List | Diagnosis Index | Diagnosis List/Index |
| Problem List | Clinical Diagnosis List | Diagnosis List/Index |
| Problem List | Problem List | Problem List |
| Forms and Questionnaires | Screening and Checklist | Assessment |
| Forms and Questionnaires | Checklist | Questionnaire |
| Forms and Questionnaires | Preventive Health Checklist | Assessment |
| Forms and Questionnaires | Screening Test Record | Assessment |
| Forms and Questionnaires | Health Maintenance Report | Assessment |
| Forms and Questionnaires | Clock Drawing Test | Assessment |
| Forms and Questionnaires | Growth Chart | Assessment |
| Forms and Questionnaires | Mini Mental Status Examination | Assessment |
| Forms and Questionnaires | Activities of Daily Living Flow Sheet | Assessment |
| Forms and Questionnaires | Staying Healthy Assessment | Assessment |
| Forms and Questionnaires | Assessment and Evaluation | Assessment |
| Forms and Questionnaires | Functional Status Report | Assessment |
| Forms and Questionnaires | Evaluation Form | Assessment |
| Forms and Questionnaires | Plan of Care | __move:Progress Note |
| Patient Communication | General Message | Patient Message |
| Patient Communication | Telephone Message | Patient Message |
| Patient Communication | Patient Portal Communication | Patient Message |
| Patient Communication | Ambulatory Correspondence | __move:Provider Letter |
| Patient Communication | Provider Letter | __move:Provider Letter |
| Patient Communication | General Notice | Notice |
| Patient Communication | Discharge Notice | Notice |
| Patient Communication | Patient Instruction | Patient Education Material |
| Patient Communication | Patient Education Material | Patient Education Material |
| Patient Communication | Web Encounter | Patient Message |
| Patient Communication | Notice of Privacy Practices | __move:Consent and Authorization |
| Patient Communication | Notice of Health Information Practices | __move:Consent and Authorization |
| Patient Communication | Privacy Notice | __move:Consent and Authorization |
| Patient Communication | Patient Rights Notice | __move:Consent and Authorization |
| Patient Communication | Patient Rights and Responsibilities | __move:Consent and Authorization |
| Patient Communication | Office Policy | __move:Consent and Authorization |
| Patient Communication | Financial Policy | __move:Consent and Authorization |
| Consent and Authorization | Surgical Consent Form | Surgical Consent |
| Consent and Authorization | Transfusion Consent Form | Transfusion Consent |
| Consent and Authorization | Immunization Consent Form | Immunization Consent |
| Consent and Authorization | HIPAA Form | HIPAA and Privacy |
| Consent and Authorization | Patient Agreement Form | General Consent |
| Consent and Authorization | Consent Form | General Consent |
| Consent and Authorization | Release of Information Authorization | Release of Information |
| Consent and Authorization | Prior Authorization Request | Prior Authorization |
| Consent and Authorization | Insurance Authorization Note | Prior Authorization |
| Consent and Authorization | Referral Authorization | Referral Authorization |
| Consent and Authorization | Referral Authorization Form | Referral Authorization |
| Consent and Authorization | Assignment of Benefits | Assignment of Benefits |
| Consent and Authorization | Authorization Form | General Authorization |
| Consent and Authorization | General Consent Form | General Consent |
| Consent and Authorization | Consent to Treat | Consent for Treatment |
| Consent and Authorization | Financial Consent | General Consent |
| Consent and Authorization | Treatment Authorization | General Authorization |
| Consent and Authorization | Service Authorization | General Authorization |
| Consent and Authorization | Notice of Privacy Practices | General Consent |
| Consent and Authorization | Notice of Health Information Practices | General Consent |
| Consent and Authorization | Privacy Notice | General Consent |
| Consent and Authorization | Patient Rights Notice | General Consent |
| Consent and Authorization | Patient Rights and Responsibilities | General Consent |
| Consent and Authorization | Office Policy | General Consent |
| Consent and Authorization | Financial Policy | General Consent |
| Consent and Authorization | Policy Page | General Consent |
| Consent and Authorization | Consent for Treatment | Consent for Treatment |
| Consent and Authorization | Treatment Consent Form | Consent for Treatment |
| Provider Letter | Provider Letter | Others |
| Provider Letter | Ambulatory Correspondence | Others |
| Provider Letter | Referral Letter | Referral Letter |
| Provider Letter | Consultation Letter | Consultation Letter |
| Provider Letter | Specialist Letter | Consultation Letter |
| Provider Letter | Consult Letter | Consultation Letter |
| Administrative | Patient Registration | Patient Registration |
| Administrative | Registration Form | Patient Registration |
| Procedure Note | EGD | Endoscopy (EGD) |
| Demographics | Patient Demographics | Patient Information |
| Demographics | Patient Information | Patient Information |
| Demographics | Demographics | Patient Information |
| Demographics | Face Sheet | Face Sheet |
| Demographics | Facesheet | Face Sheet |
| Demographics | Registration Face Sheet | Face Sheet |

#### D.L3_merge

> L3 labels under one L2 that a reader cannot tell apart. Each pair below is one study under two names, or a subset silently competing with its own parent. Keeping both guarantees the annotators split on it and the model learns noise.

| Under | Old | Merged into |
|---|---|---|
| Lab Report > Diagnostic Study | Sleep Clinic Note | Sleep Study Report |
| Lab Report > Diagnostic Study | Electrocardiogram | Electrocardiogram (EKG/ECG) Report |
| Lab Report > Diagnostic Study | Myocardial Perfusion Study | Stress Test |
| Lab Report > Diagnostic Study | Diagnostic Report | Others |
| Lab Report > Diagnostic Study | Gastroenterology Procedure Note | __move:Procedure Note |
| Lab Report > Diagnostic Study | Stress Test Report | Stress Test |
| Lab Report > Diagnostic Study | Treadmill Report | Stress Test |
| Lab Report > Diagnostic Study | Electromyography Report | Electromyography and Nerve Conduction |
| Lab Report > Diagnostic Study | Nerve Conduction Study | Electromyography and Nerve Conduction |
| Lab Report > Lab Result | Laboratory Report | __delete |
| Lab Report > Lab Result | Laboratory Result | __delete |
| Lab Report > Lab Result | INR Result | Coagulation Report |
| Lab Report > Lab Result | Prothrombin Time Result | Coagulation Report |
| Lab Report > Lab Result | Blood Glucose Result | Glucose Report |
| Lab Report > Radiology Report | Tomography Report | CT Scan Report |
| Lab Report > Radiology Report | Sonogram Report | Ultrasound Report |
| Lab Report > Radiology Report | Chest View | X-Ray Report |
| Lab Report > Radiology Report | SPECT Report | Nuclear Medicine Report |
| Lab Report > Radiology Report | Radiology Report | __delete |
| Lab Report > Pathology Report | Pap and HPV Report | Cytology Report |
| Lab Report > Pathology Report | Pathology Report | __delete |

### E. The keyword canon (page_canon.json)

Version 2026-10-25 (taxonomy v29.2). The keyword canon: page types with their lexicons (title / body / ambiguous terms), codeability and path. Paths are in the page taxonomy's names (page_taxonomy.json), put there by `python -m stages.lib.page_classify.canon_convert` (see paths_from). Lexicons are starting lists or the page type's own name: tune them on labelled pages.

Counts: page_types 295, families 85, from_labels 106, with_starting_lexicon 191, hand_placed 53, taxonomy_only 32, paths_reached 120.

**How to match**

1. Match case-insensitively on whole words or whole phrases, never on substrings of a longer word.
2. Longest match wins: when one term is contained in a longer term that also matched at the same place, count only the longer one.
3. title_terms are names a document prints as its title or main heading. They count more when found in the title zone (the first lines of the page).
4. A single-word title term counts as a title hit only when it is a heading on its own line, not when it appears inside a sentence.
5. body_terms are supporting words found in the body of that kind of page.
6. ambiguous_terms are short abbreviations, generic words, or terms shared by more than one page type. They add to a score only when another term of the same sub-type has already matched; they never decide a page alone.
7. Score each sub-type, take the best sub-type per page type as that page type's score, and report the top page type, its best sub-type, the score, the margin over the second page type, and whether a title term hit in the title zone.

**Weights**: title_term_in_title_zone: 3; title_term_elsewhere: 2; body_term: 1; ambiguous_term: 0.5. Title zone: the first 12 lines.

**Codeability rules**: page_type_tag: the page type's own tag; context: depends on the document the page is bound into (continuity); needs_review until then; source: depends on the ingest source, which is not on the page; needs_review until supplied

**Strong terms** are rare words only that page type carries: in the keyword vote (core-pipeline page_classify/keyword_vote.py) they score highest by default; every term's weight per level is then learned from the labelled pages (keyword-canon/page_keyword_weights.json).

| Page type | Family | Path | Codeability | Specialty | Strong terms | Title terms | Body terms | Ambiguous terms |
|---|---|---|---|---|---|---|---|---|
| Administration | Administration | Administrative > Others | non_codeable |  |  | Administration, fax cover sheet | cover page, blank, office use only, scanned by, page count |  |
| Appointments | Administration | Administrative > Appointment Record | non_codeable |  |  | Appointments, appointment summary | Appointment, appt, scheduled, rescheduled, cancelled, no show, check-in |  |
| Administrative | Administrative | Administrative | non_codeable |  |  |  | account, billing, insurance, appointment, registration, charges, payment |  |
| Superbill | Administrative | Administrative > Superbill | non_codeable |  | Superbill, Super Bill, Encounter Form |  | CPT, ICD-10, fee, charges, icd, modifier, units, encounter | procedure code, diagnosis code |
| Administrative Form | Administrative Record | Administrative > Others | non_codeable |  |  | Administrative Form |  |  |
| Policy Page | Administrative Record | Consent and Authorization > General Consent | non_codeable |  |  | Policy Page |  |  |
| Social Report | Administrative Record | Progress Note | non_codeable |  |  | Social Report |  |  |
| Allergies Report | Allergies Report | Lab Report > Lab Result > Allergy Testing Report | non_codeable |  | allergen, allergens | Allergies Report, allergy list | allergen, reaction, intolerance, ige, sensitization, class, kua, panel | Allergies, NKDA, severity |
| Annual Assessments | Annual Assessments | Forms and Questionnaires > Assessment | needs_review |  |  | Annual Assessments, Annual Health Assessment, comprehensive assessment, health risk assessment (HRA), in-home assessment | review of systems, chronic conditions reviewed |  |
| HouseCalls visit summary | Annual Assessments | Progress Note | codeable |  |  | HouseCalls visit summary, in-home assessment, health plan | HouseCalls, nurse practitioner, diagnoses confirmed |  |
| Medicare annual wellness visit | Annual Assessments | Forms and Questionnaires > Assessment | needs_review |  |  | Medicare annual wellness visit, Annual Wellness Visit, personalized prevention plan | Medicare Annual Wellness, G0438, G0439, Welcome to Medicare | AWV, IPPE |
| Appointment Note | Appointment Record | Administrative > Appointment Record | non_codeable |  |  | Appointment Note | appointment, appointments, scheduled, rescheduled, cancelled, noshow, arrived |  |
| Annual Health Assessment | Assessment and Evaluation | Forms and Questionnaires > Assessment | codeable |  |  | Annual Health Assessment |  |  |
| Functional Assessment | Assessment and Evaluation | Forms and Questionnaires > Assessment | codeable |  |  | Functional Assessment, activity evaluation, assessment form | functional status, ADLs, mobility, ambulation, fall risk, Barthel, Katz |  |
| Medicare Annual Wellness Visit | Assessment and Evaluation | Forms and Questionnaires > Assessment | codeable |  |  | Medicare Annual Wellness Visit |  |  |
| Nutrition Assessment | Assessment and Evaluation | Forms and Questionnaires > Assessment | codeable | Nutrition |  | Nutrition Assessment | assessment, functional, adl, iadl, independent, assistance, fall, risk |  |
| Staying Healthy Assessment | Assessment and Evaluation | Forms and Questionnaires > Assessment | needs_review |  |  | Staying Healthy Assessment, health education behavior | IHEBA, Medi-Cal, PCP signature | SHA |
| Referral Authorization | Authorization Form | Consent and Authorization > Referral Authorization | non_codeable |  |  | Referral Authorization |  |  |
| Behavioral Health Note | Behavioral Health Note | Progress Note | codeable | Behavioral Health |  | Behavioral Health Note |  |  |
| Initial Psychiatric Evaluation | Behavioral Health Note | Progress Note | codeable |  |  | Initial Psychiatric Evaluation |  |  |
| Psychiatric Evaluation | Behavioral Health Note | Progress Note | codeable | Behavioral Health |  | Psychiatric Evaluation, mental status exam | psychiatric, DSM-5, mood, affect, suicidal ideation, psychotherapy session | MSE |
| Billing / Invoice | Billing / Invoice | Administrative > Billing & Invoices | needs_review |  | invoice, statement | Billing / Invoice, Billing, Invoice, coding summary | Superbill, charge ticket, charges, balance due, claim, statement, estimate, invoice, balance, payment, due, adjustment, insurance, remittance | CPT, DRG |
| Invoice | Billing / Invoice | Administrative > Billing & Invoices | needs_review |  |  | Invoice, invoice number, records fee | amount due, copy fee, per page, remit to, tax ID |  |
| Invoice Page | Billing Statement | Administrative > Billing & Invoices | needs_review |  |  | Invoice Page |  |  |
| Cardiovascular Report | Cardiac Diagnostic Test | Lab Report > Diagnostic Study > Others | codeable |  |  | Cardiovascular Report |  |  |
| Echocardiogram Report | Cardiac Diagnostic Test | Lab Report > Diagnostic Study > Echocardiogram Report | codeable | Cardiology | echocardiogram, echocardiography | Echocardiogram Report | Echocardiogram, transthoracic, LV hypertrophy, valve regurgitation/stenosis, diastolic dysfunction, wall motion, ejection, fraction, ventricle, atrium, valve, lvef, transesophageal | TTE, LVEF, ejection fraction |
| Electrocardiogram | Cardiac Diagnostic Test | Lab Report > Diagnostic Study > Electrocardiogram (EKG/ECG) Report | codeable | Cardiology |  | Electrocardiogram |  |  |
| Extremity Doppler Report | Cardiac Diagnostic Test | Lab Report > Diagnostic Study > Extremity Doppler Report | codeable | Cardiology |  | Extremity Doppler Report |  |  |
| Pacemaker Device Report | Cardiac Diagnostic Test | Lab Report > Diagnostic Study > Pacemaker Device Report | codeable | Cardiology | pacemaker, interrogation | Pacemaker Device Report | pacemaker, interrogation, lead, leads, battery, pacing, sensing, impedance, device |  |
| Stress Test Report | Cardiac Diagnostic Test | Lab Report > Diagnostic Study > Stress Test | codeable | Cardiology |  | Stress Test Report |  |  |
| Treadmill Report | Cardiac Diagnostic Test | Lab Report > Diagnostic Study > Stress Test | codeable | Cardiology |  | Treadmill Report |  |  |
| CT Angiography | Cardiac Report | Lab Report > Radiology Report > CT Angiography Report | non_codeable | Cardiology | cta | CT Angiography | CT angiogram, coronary CTA, calcium score, cta, angiography, arterial, runoff, aorta, stenosis | CTA, stenosis, plaque, contrast, impression |
| Cardiac Report | Cardiac Report | Lab Report > Diagnostic Study > Others | non_codeable |  |  | Cardiac Report, heart report, diagnostic report | Cardiovascular, cardiology, cardiology consultation, event monitor | CAD, CHF, ejection fraction |
| Cardiac catheterization report | Cardiac Report | Lab Report > Diagnostic Study > Cardiac Catheterization Report | codeable | Cardiology |  | Cardiac catheterization report | Cardiac cath, left heart catheterization (LHC), coronary angiography, stent, LAD/RCA/LCx, stenosis %, LVEDP, cath lab | PCI, ejection fraction |
| EKG Report (12-lead / routine) | Cardiac Report | Lab Report > Diagnostic Study > Electrocardiogram (EKG/ECG) Report | codeable | Cardiology |  | EKG Report (12-lead / routine), EKG Report, Electrocardiogram Report | ECG 12 lead, EKG 12 lead, ECG routine, confirmed by, reviewed by | interpretation, impression |
| Stress Test | Cardiac Report | Lab Report > Diagnostic Study > Stress Test | codeable | Cardiology |  | Stress Test, exercise tolerance test (ETT), Bruce protocol | treadmill, METs, target heart rate, ST depression | ischemia |
| Treadmill report | Cardiac Report | Lab Report > Diagnostic Study > Stress Test | codeable | Cardiology |  | Treadmill report | Treadmill, Bruce, exercise time, METs, peak HR, ST changes |  |
| After-Visit Summary | Clinical Summary | Clinical Summary > After-Visit Summary | non_codeable |  |  |  | visit, today, instructions, appointments, upcoming, portal |  |
| Clinical Summary | Clinical Summary | Clinical Summary > Others | needs_review |  |  | Clinical Summary, Visit Summary, After Visit Summary, Patient Summary, chart summary, diagnosis list | Clinical Diagnoses | AVS, summary of care |
| Clinical Summary (class words) | Clinical Summary | Clinical Summary | non_codeable |  |  |  | summary, ccd, continuity, transition, care, document |  |
| Summary of Care (C-CDA) | Clinical Summary | Clinical Summary > Summary of Care (C-CDA) | non_codeable |  | ccd, c-cda |  | ccd, c-cda, transition, continuity, document, generated |  |
| Assignment of Benefits | Consent and Authorization | Consent and Authorization > Assignment of Benefits | non_codeable |  |  | Assignment of Benefits | assignment, assign, benefits, insurance, financial, responsibility, payment |  |
| Authorization Form | Consent and Authorization | Consent and Authorization > General Authorization | non_codeable |  |  | Authorization Form | authorize, authorization, permission, behalf |  |
| Consent Form | Consent and Authorization | Consent and Authorization > General Consent | non_codeable |  |  | Consent Form, informed consent | Consent, I hereby authorize, risks and benefits, release of information, patient signature | authorization, witness |
| Consent and Authorization | Consent and Authorization | Consent and Authorization | non_codeable |  | consent, authorization |  | consent, authorize, authorization, disclose, disclosure, release, revoke, voluntary, acknowledge, witness, guardian, representative, hereby |  |
| Consent for Treatment | Consent and Authorization | Consent and Authorization > Consent for Treatment | non_codeable |  |  |  | treatment, consent, conditions, voluntary, examination |  |
| HIPAA Form | Consent and Authorization | Consent and Authorization > HIPAA and Privacy | non_codeable |  | hipaa | HIPAA Form, authorization to release PHI, privacy policy | HIPAA, acknowledgment of receipt, privacy, practices, protected, phi, rights, notice, uses, disclosures | notice of privacy practices, signature |
| Immunization Consent Form | Consent and Authorization | Consent and Authorization > Immunization Consent | non_codeable |  |  | Immunization Consent Form | vaccine, vaccination, immunization, vis, consent |  |
| Patient Agreement Form | Consent and Authorization | Consent and Authorization > General Consent | non_codeable |  |  | Patient Agreement Form | consent, agree, acknowledge, policy, policies |  |
| Prior Authorization Request | Consent and Authorization | Consent and Authorization > Prior Authorization | non_codeable |  | preauthorization, precertification | Prior Authorization Request | prior, preauthorization, precertification, approved, denied, medical, necessity |  |
| Release of Information Authorization | Consent and Authorization | Consent and Authorization > Release of Information | non_codeable |  |  | Release of Information Authorization | release, disclose, recipient, purpose, expiration, revoke, redisclosure |  |
| Surgical Consent Form | Consent and Authorization | Consent and Authorization > Surgical Consent | non_codeable |  |  | Surgical Consent Form | surgical, operation, risks, benefits, alternatives, complications, anesthesia |  |
| Transfusion Consent Form | Consent and Authorization | Consent and Authorization > Transfusion Consent | non_codeable |  | transfusion | Transfusion Consent Form | transfusion, blood, products, refuse, risks |  |
| Demographics | Demographics | Demographics | non_codeable |  | demographics |  | address, phone, dob, sex, marital, race, ethnicity, language, emergency, contact, employer, guarantor |  |
| Carotid Duplex Report | Diagnostic Study | Lab Report > Diagnostic Study > Carotid Duplex Report | non_codeable | Cardiology |  | Carotid Duplex Report | carotid, ica, cca, velocity, velocities, plaque, duplex |  |
| Holter Monitor Report | Diagnostic Study | Lab Report > Diagnostic Study > Holter Monitor Report | non_codeable | Cardiology | holter | Holter Monitor Report | holter, beats, pvc, pac, pauses, hookup, diary |  |
| Plethysmography Report | Diagnostic Study | Lab Report > Diagnostic Study > Plethysmography Report | non_codeable | Cardiology | plethysmography | Plethysmography Report | plethysmography, volumes, tlc, rv, frc |  |
| Pulmonary Function Test | Diagnostic Study | Lab Report > Diagnostic Study > Pulmonary Function Test | non_codeable | Pulmonology | spirometry | Pulmonary Function Test | spirometry, fev1, fvc, dlco, bronchodilator, predicted |  |
| Dialysis Treatment Record | Dialysis Record | Procedure Note > Dialysis Record | codeable | Dialysis |  | Dialysis Treatment Record |  |  |
| Discharge Instructions | Discharge Instructions | Discharge Summary > Discharge Instruction | discharge |  |  | Discharge Instructions | home care, follow-up appointment, return to ED if, discharge medications, medication reconciliation, instructions, activity, diet, followup, return, symptoms, home |  |
| Consultation Note | Discharge Summary | Discharge Summary > Consultation Note | discharge |  |  |  | consultation, consulted, consult, requesting, inpatient, recommendations |  |
| Depart Note | Discharge Summary | Discharge Summary > Depart/Discharge Note | discharge |  |  | Depart Note, Depart Summary | Depart, departure, condition at departure, discharge, discharged, hospital, course, disposition, admission, condition | disposition |
| Discharge Medication | Discharge Summary | Discharge Summary > Discharge Medication | discharge |  |  | Discharge Medication | medications, discharge, continue, stop, new, changed, reconciliation |  |
| Discharge Summary | Discharge Summary | Discharge Summary > Depart/Discharge Note | discharge |  |  | Discharge Summary, Discharge Note | admission date, discharge date, hospital course, discharge diagnosis, condition at discharge, Transition of Care, Transfer of Care, Resumen de alta |  |
| Discharge Summary (class words) | Discharge Summary | Discharge Summary | discharge |  |  |  | admission, admitted, discharge, discharged, hospital, course, disposition, inpatient |  |
| Final Case Summary | Discharge Summary | Discharge Summary > Depart/Discharge Note | discharge |  |  | Final Case Summary | case closed, outcome | summary of care, final diagnosis |
| History and Physical | Discharge Summary | Discharge Summary > History and Physical | discharge |  |  |  | admission, admitted, admitting, history, physical, inpatient, observation |  |
| Principal Diagnosis | Discharge Summary | Discharge Summary > Discharge Diagnosis | discharge |  |  | Principal Diagnosis | secondary diagnoses, discharge, diagnosis, diagnoses, principal, secondary | primary diagnosis, final diagnosis |
| Short Stay Summary | Discharge Summary | Discharge Summary > Short Stay Summary | discharge |  |  | Short Stay Summary, short stay record | Short stay, observation, admit/discharge same day, brief hospital course, short, stay |  |
| ECG Graph | EKG / ECG Tracing | Lab Report > Diagnostic Study > Electrocardiogram (EKG/ECG) Report | codeable | Cardiology | electrocardiogram, ekg, ecg | ECG Graph, EKG graph | tracing, rhythm strip, 25 mm/s, 10 mm/mV, unconfirmed, machine read, ekg, ecg, rhythm, rate, pr, qrs, qt, qtc, axis, sinus |  |
| EKG / ECG Tracing | EKG / ECG Tracing | Lab Report > Diagnostic Study > Electrocardiogram (EKG/ECG) Report | codeable | Cardiology |  | EKG / ECG Tracing, ECG Tracing | tracing, rhythm strip, telemetry, rate | EKG, ECG, QRS, QTc |
| Holter monitor report | EKG / ECG Tracing | Lab Report > Diagnostic Study > Holter Monitor Report | non_codeable | Cardiology |  | Holter monitor report | Holter, 24-hour monitor, ambulatory ECG, PVCs, PACs, pauses, min/max heart rate, total beats |  |
| Audiology / hearing report | ENT Note | Progress Note | codeable |  |  | Audiology / hearing report, Audiology, hearing report | audiogram, hearing loss, sensorineural, dB HL, tympanometry, speech discrimination, hearing aid |  |
| ENT Note | ENT Note | Progress Note | codeable | Audiology |  | ENT Note | otolaryngology, ear, nose, throat, sinus, hearing | ENT |
| Laryngoscopy report | ENT Note | Lab Report > Diagnostic Study > Laryngoscopy Procedure Report | codeable | Audiology | laryngoscopy | Laryngoscopy report | Laryngoscopy, flexible fiberoptic, vocal cords, larynx, hoarseness, vocal, cords, glottis, epiglottis | ENT, dysphagia |
| Videonystagmography (VNG) | ENT Note | Progress Note | codeable |  |  | Videonystagmography (VNG), Videonystagmography | nystagmus, caloric, vestibular, vertigo, Dix-Hallpike | VNG, BPPV |
| Emergency Department Note | Emergency Department Note | Progress Note | codeable | Emergency |  | Emergency Department Note |  |  |
| EMS / ambulance report | Emergency Note | Progress Note | codeable | Emergency |  | EMS / ambulance report, ambulance report, run report, Patient Care Report | ambulance, paramedic, dispatch, scene, transport | EMS, PCR, EMT |
| Emergency Note | Emergency Note | Progress Note | codeable | Emergency |  | Emergency Note, ED Note, ER visit, ED provider note | Emergency Department, ED course, arrival time | MDM, disposition |
| Urgent care note | Emergency Note | Progress Note | codeable | Emergency |  | Urgent care note, acute visit | Urgent Care, walk-in, rapid strep/flu, discharged home | chief complaint |
| Colonoscopy Report | Endoscopy Report | Lab Report > Diagnostic Study > Colonoscopy Report | codeable | Gastroenterology |  | Colonoscopy Report |  |  |
| Endoscopy Report | Endoscopy Report | Lab Report > Diagnostic Study > Endoscopy Report | codeable | Gastroenterology | endoscopy | Endoscopy Report | endoscopy, endoscope, scope, mucosa, biopsies |  |
| Eye Examination Report | Eye Examination | Progress Note | codeable | Ophthalmology |  | Eye Examination Report |  |  |
| Visual Evaluation | Eye Examination | Progress Note | codeable | Ophthalmology |  | Visual Evaluation | vision, visual function | acuity |
| ICU Flow Sheet | Flow Sheet | Operative Note > Critical Care Record | codeable | Critical Care |  | ICU Flow Sheet | icu, ventilator, fio2, peep, drips, titrated, sedation, flowsheet |  |
| Forms | Forms | Forms and Questionnaires > Questionnaire | needs_review |  |  | Forms, intake form, patient history form, pre-op checklist | agreement, acknowledgment, pre-admission testing, health maintenance | Form, screening, signature |
| Mini-Mental State Exam | Forms | Forms and Questionnaires > Questionnaire | needs_review |  |  | Mini-Mental State Exam | Mini-Mental, Folstein, orientation, recall, clock drawing, 3-word recall | MMSE, score /30, Mini-Cog |
| Forms and Questionnaires | Forms and Questionnaires | Forms and Questionnaires | needs_review |  | questionnaire |  | questionnaire, screening, score, assessment, survey, responses |  |
| ADL/IADL flow sheet | Functional Assessment | Forms and Questionnaires > Assessment | needs_review |  |  | ADL/IADL flow sheet, flow sheet | bathing, toileting, feeding, transfers, independent/assist/dependent | ADL, IADL, dressing |
| Colonoscopy / sigmoidoscopy report | Gastroenterology / GI | Lab Report > Diagnostic Study > Colonoscopy Report | codeable | Gastroenterology | colonoscopy | Colonoscopy / sigmoidoscopy report, Colonoscopy, sigmoidoscopy report | flexible sigmoidoscopy, cecum reached, polyp, polypectomy, diverticulosis, bowel prep, withdrawal time, colonoscopy, cecum, colon, polyps, prep, withdrawal, terminal, ileum | biopsy |
| Endoscopy report | Gastroenterology / GI | Lab Report > Diagnostic Study > Others | non_codeable |  |  | Endoscopy report | Endoscopy, esophagogastroduodenoscopy, upper GI endoscopy, scope, biopsy taken, sedation, endoscopist | EGD, findings |
| Esophageal manometry | Gastroenterology / GI | Lab Report > Diagnostic Study > Esophageal Manometry Report | non_codeable | Gastroenterology | manometry | Esophageal manometry | high-resolution manometry, LES pressure, peristalsis, achalasia, motility, manometry, sphincter, swallows, pressure | IRP |
| Gastroenterology / GI | Gastroenterology / GI | Lab Report > Diagnostic Study > Others | non_codeable |  |  | Gastroenterology / GI, Gastroenterology, GI report | cirrhosis, H. pylori, hepatology | GI, GERD |
| Growth chart | Growth chart | Forms and Questionnaires > Questionnaire | needs_review |  |  | Growth chart | percentile, stature-for-age, length-for-age, weight-for-age, head circumference-for-age, BMI-for-age | CDC, WHO |
| ICU Note | ICU Note | Operative Note > Critical Care Record | codeable |  |  | ICU Note, ICU flow sheet | Critical care, critical care time, intubated, vasopressors, hourly vitals | ICU, I&O |
| Immunization | Immunization | Immunization | non_codeable |  | immunization, immunizations, vaccination |  | vaccine, vaccines, lot, manufacturer, administered, dose, booster, series |  |
| Immunization list | Immunizations | Immunization > Immunization Record | non_codeable |  |  | Immunization list | Immunization, vaccine, flu shot, influenza, Prevnar, Tdap, lot number, date given, site | VIS |
| Immunizations | Immunizations | Immunization > Immunization Record | non_codeable |  |  | Immunizations | vaccine, immunization, lot number, date given |  |
| Vaccine Record | Immunizations | Immunization > Immunization Record | non_codeable |  |  | Vaccine Record, Vaccine administration record | lot number, expiration, site, administered by, VIS date | VAR, manufacturer, route |
| Device Implant Log | Implant Record | Operative Note > Others | codeable |  |  | Device Implant Log |  |  |
| Device implant log | Implants | Operative Note > Others | codeable |  |  | Device implant log, Implant log | device, serial, lot, expiry, implanted by | UDI |
| Implants | Implants | Operative Note > Others | codeable |  |  | Implants | Implant, lot/serial number, model, prosthesis, pacemaker, joint | manufacturer, IOL |
| Pacemaker Report | Implants | Lab Report > Diagnostic Study > Pacemaker Device Report | codeable | Cardiology |  | Pacemaker Report | Pacemaker, device interrogation, device clinic, battery, lead impedance, % paced, remote monitoring, programmer | ICD |
| Radiation Therapy Record | Infusion Therapy Record | Procedure Note > Radiation Therapy | codeable | Oncology |  | Radiation Therapy Record | radiation, fraction, fractions, gy, cgy, dose, simulation, linac |  |
| Interim Summary | Interim Summary | Discharge Summary > Interim Summary | discharge |  |  | Interim Summary, Interim Note, interval summary | hospital course to date, interim, summary, hospital, course |  |
| Ankle-Brachial Index Report | Lab Report | Lab Report > Diagnostic Study > Ankle-Brachial Index Report | non_codeable | Cardiology | abi | Ankle-Brachial Index Report | abi, ankle, brachial, index, pressures, toe |  |
| Audiogram and Hearing Loss Report | Lab Report | Lab Report > Diagnostic Study > Audiogram and Hearing Loss Report | non_codeable |  | audiogram, audiology |  | audiogram, audiometry, tympanometry, thresholds, hz, db, speech |  |
| Behavioral Assessment | Lab Report | Lab Report > Diagnostic Study > Behavioral Assessment | non_codeable |  |  |  | behavioral, cognitive, testing, inventory, scores, psychological |  |
| Bronchoscopy Report | Lab Report | Lab Report > Diagnostic Study > Bronchoscopy Report | non_codeable | Pulmonology | bronchoscopy | Bronchoscopy Report | bronchoscopy, bronchoscope, bronchial, lavage, carina, airways |  |
| Chemistry Report | Lab Report | Lab Report > Lab Result > Chemistry Report | non_codeable |  | cmp, bmp | Chemistry Report | sodium, potassium, chloride, creatinine, bun, albumin, bilirubin, alt, ast, calcium, egfr, lipid, cholesterol, triglycerides |  |
| Corneal Topography | Lab Report | Lab Report > Diagnostic Study > Corneal Topography | non_codeable |  | topography |  | topography, corneal, keratometry, astigmatism, axial |  |
| Cystoscopy Report | Lab Report | Lab Report > Diagnostic Study > Cystoscopy Report | non_codeable | Urology | cystoscopy | Cystoscopy Report | cystoscopy, cystoscope, bladder, urethra, ureteral |  |
| Cytology Report | Lab Report | Lab Report > Pathology Report > Cytology Report | codeable | Pathology | cytology | Cytology Report | cytology, cytologic, pap, smear, cervical, adequacy, cells |  |
| Cytometry Report | Lab Report | Lab Report > Pathology Report > Cytometry Report | codeable | Pathology | cytometry | Cytometry Report | cytometry, flow, immunophenotyping, cd, markers, gating |  |
| Doppler Duplex Report | Lab Report | Lab Report > Diagnostic Study > Doppler Duplex Report | non_codeable |  |  |  | duplex, doppler, waveforms, venous, arterial, velocity |  |
| Esophagogastroduodenoscopy Report | Lab Report | Lab Report > Diagnostic Study > Esophagogastroduodenoscopy (EGD) Report | non_codeable | Gastroenterology | esophagogastroduodenoscopy, egd | Esophagogastroduodenoscopy Report | egd, esophagus, stomach, duodenum, antrum, gastric |  |
| INR and Prothrombin Time | Lab Report | Lab Report > Lab Result > Coagulation Report | non_codeable |  | coagulation, inr | INR and Prothrombin Time | inr, prothrombin, ptt, aptt, fibrinogen, warfarin, anticoagulation |  |
| Lab Report | Lab Report | Lab Report | needs_review |  |  |  | specimen, collected, received, reported, reference, range, flag, units, accession, laboratory, resulted, clia |  |
| Lab Report > Pathology Report | Lab Report | Lab Report > Pathology Report | codeable |  | pathology |  | specimen, gross, microscopic, diagnosis, pathologist, tissue, slides, block |  |
| Lab Report > Radiology Report | Lab Report | Lab Report > Radiology Report | non_codeable |  | radiology |  | impression, findings, technique, comparison, contrast, indication, radiologist, views, dictated |  |
| Lab Result | Lab Report | Lab Report > Lab Result | non_codeable |  |  |  | specimen, collected, reference, range, units, flag, accession, fasting, serum, plasma |  |
| Laboratory Flow Sheet | Lab Report | Lab Report > Lab Result > Laboratory Flow Sheet | non_codeable |  | flowsheet | Laboratory Flow Sheet | flowsheet, trend, trends, cumulative |  |
| MR Angiography Report | Lab Report | Lab Report > Radiology Report > MR Angiography Report | non_codeable | Neurology | mra | MR Angiography Report | mra, angiography, magnetic, resonance, vessels |  |
| Optical Coherence Tomography Report | Lab Report | Lab Report > Diagnostic Study > Optical Coherence Tomography Report | non_codeable |  | oct |  | oct, retinal, thickness, macula, rnfl, optic |  |
| Sigmoidoscopy Report | Lab Report | Lab Report > Diagnostic Study > Sigmoidoscopy Report | non_codeable | Gastroenterology | sigmoidoscopy | Sigmoidoscopy Report | sigmoidoscopy, sigmoid, rectum, flexible |  |
| Split Night Study Report | Lab Report | Lab Report > Diagnostic Study > Split Night Study Report | codeable | Sleep Study |  | Split Night Study Report | split, cpap, titration, diagnostic, ahi |  |
| Telemetry Report | Lab Report | Lab Report > Diagnostic Study > Telemetry Report | non_codeable |  | telemetry |  | telemetry, strips, rhythm, events, monitoring |  |
| Videonystagmography Report | Lab Report | Lab Report > Diagnostic Study > Videonystagmography Report | codeable | Neurology | videonystagmography, vng | Videonystagmography Report | vng, nystagmus, caloric, saccades, oculomotor, positional |  |
| Visual Field Interpretation | Lab Report | Lab Report > Diagnostic Study > Visual Field Report | codeable | Ophthalmology | perimetry | Visual Field Interpretation | perimetry, visual, field, humphrey, fixation, deviation |  |
| Allergy testing report | Laboratory Data | Lab Report > Lab Result > Allergy Testing Report | non_codeable |  |  | Allergy testing report | Allergy testing, skin prick, intradermal, wheal/flare, allergen panel, histamine control | IgE, RAST |
| Chemistry | Laboratory Data | Lab Report > Lab Result > Others | non_codeable |  |  | Chemistry | sodium, potassium, AST/ALT, reference range, H/L flags, collected, resulted | BMP, CMP, creatinine, BUN |
| Glucose report | Laboratory Data | Lab Report > Lab Result > Glucose Report | non_codeable |  |  | Glucose report, glucose log | Blood glucose, fingerstick, glucometer, Dexcom, Libre, time in range | FSBS, CGM |
| Hematology | Laboratory Data | Lab Report > Lab Result > Hematology Report | non_codeable |  | cbc, hematology | Hematology | hemoglobin, hematocrit, platelets, differential, reference range, wbc, rbc, mcv, mch, mchc, rdw, neutrophils, lymphocytes | CBC, WBC, MCV |
| Laboratory Data | Laboratory Data | Progress Note | non_codeable |  |  | Laboratory Data, lab results, lab flow sheet, lab requisition | reference range, abnormal flag, collected, resulted, prothrombin time | result, units, INR |
| Microbiology | Laboratory Data | Lab Report > Lab Result > Microbiology Report | non_codeable |  | microbiology, culture | Microbiology | culture, sensitivity, susceptibility, organism, gram stain, colony count, urine/blood/wound culture, organisms, growth, gram, colonies, isolated, mic |  |
| Urinalysis | Laboratory Data | Lab Report > Lab Result > Urinalysis Report | non_codeable |  | urinalysis | Urinalysis | urine dipstick, specific gravity, leukocyte esterase, nitrite, protein, urine, urinalysis, specific, gravity, ketones, leukocyte, casts, microscopic | UA, microscopic |
| Allergy List | Laboratory Report | Lab Report > Lab Result > Allergy Testing Report | non_codeable |  |  | Allergy List |  |  |
| Allergy Testing Report | Laboratory Report | Lab Report > Lab Result > Allergy Testing Report | non_codeable | Pulmonology |  | Allergy Testing Report |  |  |
| FISH Report | Laboratory Report | Lab Report > Lab Result > FISH Report | non_codeable | Pathology | fish | FISH Report | fluorescence in situ hybridization, probe, HER2, BCR-ABL, translocation, amplification, nuclei scored, hybridization, probes, nuclei, signals, fluorescence | FISH |
| Genetic Test Report | Laboratory Report | Lab Report > Lab Result > Genetic Test Report | non_codeable | Pathology | genetic, genomic | Genetic Test Report, Genetic test | variant, pathogenic, heterozygous, pharmacogenomic, Myriad, Invitae, variants, gene, genes, mutation, genotype, sequencing, carrier | mutation, BRCA |
| Glucose Report | Laboratory Report | Lab Report > Lab Result > Glucose Report | non_codeable |  | glucose | Glucose Report | glucose, a1c, hba1c, fasting, postprandial, tolerance |  |
| Hematology Report | Laboratory Report | Lab Report > Lab Result > Hematology Report | non_codeable |  |  | Hematology Report |  |  |
| Laboratory Result | Laboratory Report | Lab Report > Lab Result > Others | non_codeable |  |  | Laboratory Result |  |  |
| Microbiology Report | Laboratory Report | Lab Report > Lab Result > Microbiology Report | non_codeable |  |  | Microbiology Report |  |  |
| Urinalysis Report | Laboratory Report | Lab Report > Lab Result > Urinalysis Report | non_codeable |  |  | Urinalysis Report |  |  |
| Prescription / Rx page | Medication List | Medication Record > Medication List | non_codeable |  |  | Prescription / Rx page, Prescription, Rx page | sig, dispense, refills, DEA number, e-prescribed, pharmacy | Rx, DAW |
| Refill record | Medication List | Medication Record > Refill Record | non_codeable |  |  | Refill record | Refill, refill request, pharmacy, last filled, quantity, refills remaining, e-prescribe | approved/denied |
| Immunization List | Medication Record | Immunization > Immunization Record | non_codeable |  |  | Immunization List | vaccine, lot, manufacturer, administered, site, dose, series, due |  |
| Immunization Record | Medication Record | Immunization > Immunization Record | non_codeable |  |  | Immunization Record |  |  |
| Medication List | Medication Record | Medication Record > Medication List | non_codeable |  |  | Medication List | current medications, active meds, sig, start date, prescriber, medications, active, tablet, capsule, dose, frequency, taking | dose, frequency |
| Medication Reconciliation | Medication Record | Medication Record > Medication List | non_codeable |  |  | Medication Reconciliation |  |  |
| Medication Record | Medication Record | Medication Record | non_codeable |  |  |  | medications, medication, tablet, capsule, dose, sig, refills, pharmacy, prescribed |  |
| Prescription Page | Medication Record | Medication Record > Prescription Page | non_codeable |  | prescription | Prescription Page | prescription, rx, dispense, quantity, refills, substitution, dea, pharmacy |  |
| Refill Record | Medication Record | Medication Record > Refill Record | non_codeable |  |  | Refill Record | refill, refills, renewal, pharmacy, approved |  |
| Ambulatory Correspondence | Messages | Provider Letter > Others | non_codeable |  |  | Ambulatory Correspondence, AMB Correspondence, outside correspondence | correspondence, fax cover, received from, scanned document |  |
| Letter | Messages | Provider Letter > Others | non_codeable |  |  | Letter | Dear Dr., Thank you for referring, I had the pleasure of seeing, Re:, Sincerely, cc:, letterhead |  |
| Messages | Messages | Patient Communication > Patient Message | non_codeable |  |  | Messages, note to provider, staff message, web encounter | routed to, patient communication, memo, patient portal, message, portal, reply, replied, sent, inbox | Message |
| Notice | Messages | Patient Communication > Notice | non_codeable |  |  | Notice, advance beneficiary notice, notice of non-coverage, discharge notice, notification of discharge |  | notice of privacy practices, ABN |
| Notification | Messages | Patient Communication > Patient Message | non_codeable |  |  | Notification, result notification | alert, reminder, notified, auto-generated |  |
| Dialysis Note | Nephrology | Procedure Note > Dialysis Record | codeable |  | dialysis, hemodialysis | Dialysis Note, HD flow sheet | Dialysis, hemodialysis, peritoneal dialysis, dry weight, UF goal, Kt/V, AV fistula, dialysate, dialyzer, ultrafiltration, access, fistula, treatment | CAPD |
| Nephrology | Nephrology | Progress Note | codeable |  |  | Nephrology | CKD stage, proteinuria, dialysis access, fistula, renal | eGFR, creatinine, ESRD |
| Electromyography Report | Neurodiagnostic Test | Lab Report > Diagnostic Study > Electromyography and Nerve Conduction | codeable | Neurology |  | Electromyography Report |  |  |
| DPN results | Neurology | Lab Report > Diagnostic Study > Others | non_codeable | Neurology |  | DPN results | diabetic peripheral neuropathy, DPNCheck, sural, monofilament | DPN, severity |
| EEG / neurophysiology report | Neurology | Lab Report > Diagnostic Study > Electroencephalogram Report | codeable | Neurology | electroencephalogram, eeg | EEG / neurophysiology report, neurophysiology report | electroencephalogram, neurophysiology, epileptiform, spike and wave, background rhythm, seizure, photic stimulation, eeg, background, alpha, photic | EEG |
| EMG report | Neurology | Lab Report > Diagnostic Study > Electromyography and Nerve Conduction | non_codeable | Neurology | electromyography, emg | EMG report | electromyography, NCV / NCS, nerve conduction, sural nerve, amplitude, latency, radiculopathy, carpal tunnel, emg, ncs, conduction, motor, sensory, needle | EMG |
| Neurology | Neurology | Progress Note | codeable | Neurology |  | Neurology, neurological exam | cranial nerves, reflexes, seizure, stroke/CVA, neuropathy, Parkinson's | dementia, EMG |
| Sudoscan | Neurology | Lab Report > Diagnostic Study > Sudoscan Report | codeable | Neurology | sudoscan | Sudoscan | electrochemical skin conductance, sudomotor, hands/feet, neuropathy risk, sudoscan, esc, conductance | ESC |
| Myocardial perfusion imaging | Nuclear Medicine Report | Lab Report > Diagnostic Study > Stress Test | codeable | Cardiology |  | Myocardial perfusion imaging | Myocardial perfusion, nuclear stress, Lexiscan, sestamibi, Cardiolite, reversible defect, SPECT, gated EF, stress, treadmill, bruce, mets, exercise, protocol, regadenoson, perfusion | MPI, ischemia |
| Nuclear Medicine Report | Nuclear Medicine Report | Lab Report > Radiology Report > Nuclear Medicine Report | codeable | Radiology | scintigraphy | Nuclear Medicine Report | Nuclear medicine, radiotracer, Tc-99m, uptake, bone scan, thyroid scan, nuclear, technetium, scintigraphy, spect, pet | HIDA, PET |
| SPECT report | Nuclear Medicine Report | Lab Report > Radiology Report > Nuclear Medicine Report | codeable | Radiology |  | SPECT report | SPECT, perfusion, gated, tracer, defect | ischemia, infarct |
| Nurses Notes | Nursing Note | Progress Note | codeable | Nursing |  | Nurses Notes |  |  |
| Nursing Note | Nursing Note | Progress Note | non_codeable | Nursing |  | Nursing Note, Nurse's note, nursing chart, home health visit, skilled nursing visit | vital signs, flowsheet | RN, LPN |
| Antepartum Record | Obstetric and Gynecologic Record | Progress Note | codeable | Obstetrics and Gynecology |  | Antepartum Record |  |  |
| Pap and HPV Report | Obstetric and Gynecologic Record | Lab Report > Diagnostic Study | codeable | Obstetrics and Gynecology |  | Pap and HPV Report |  |  |
| Prenatal Record | Obstetric and Gynecologic Record | Progress Note | codeable | Obstetrics and Gynecology |  | Prenatal Record |  |  |
| Antepartum record | Obstetrics & Women's Health | Progress Note | codeable |  |  | Antepartum record, antepartum visit, NST (non-stress test) | gestational age, fetal heart tones, prenatal labs reviewed, high-risk pregnancy | EDD |
| Colposcopy exam | Obstetrics & Women's Health | Lab Report > Diagnostic Study | codeable | Obstetrics and Gynecology |  | Colposcopy exam | Colposcopy, cervix, acetowhite, transformation zone, cervical biopsy, abnormal Pap, study, interpretation, interpreting, tracing, recording, monitoring, performed, technician | ECC, CIN |
| Obstetrics & Women's Health | Obstetrics & Women's Health | Progress Note | codeable |  |  | Obstetrics & Women's Health | pregnancy, gravida / para, cervix | OB, GYN, EDD, LMP |
| Pap smear report | Obstetrics & Women's Health | Lab Report > Diagnostic Study | codeable | Obstetrics and Gynecology |  | Pap smear report | Pap smear, cervical cytology, ThinPrep, ASC-US, high-risk HPV, co-testing | NILM, LSIL, HSIL, HPV |
| Postpartum visit / exam | Obstetrics & Women's Health | Progress Note | codeable | Obstetrics and Gynecology |  | Postpartum visit / exam, Postpartum visit, PP exam | 6-week check, delivery date, lochia, breastfeeding/lactation, incision/perineum, contraception counseling, s/p NSVD or C-section | EPDS |
| Prenatal record / ACOG form | Obstetrics & Women's Health | Progress Note | codeable |  |  | Prenatal record / ACOG form, Prenatal record, ACOG form, ACOG Antepartum Record, Prenatal Flow Sheet, OB history | EDD/EDC, gravida/para (G_P_), gestational age, weeks gestation, fundal height, FHR/FHT, fetal movement | LMP |
| Occupational & Physical Exams | Occupational & Physical Exams | Progress Note | codeable | Occupational Health |  | Occupational & Physical Exams, physical examination form | cleared, restrictions, work status, fitness for duty, school physical, travel clinic | employer |
| Occupational health record | Occupational & Physical Exams | Progress Note | codeable | Occupational Health |  | Occupational health record | Occupational health, employee health, work status, restrictions, DOT physical | employer |
| Sports physical exam | Occupational & Physical Exams | Progress Note | codeable | Primary Care |  | Sports physical exam, musculoskeletal exam | Sports physical, pre-participation, cleared for sports | PPE |
| Work injury evaluation | Occupational & Physical Exams | Progress Note | codeable | Occupational Health |  | Work injury evaluation | Work injury, workers' comp, date of injury (DOI), mechanism, work restrictions | MMI |
| Blood transfusion record | Oncology | Medication Record > Blood Transfusion Record | codeable |  |  | Blood transfusion record | Transfusion, unit number, blood type, crossmatch, start/stop time, transfusion reaction, two-nurse check, unit, units, blood, products, reaction | PRBC |
| Chemotherapy administration record | Oncology | Procedure Note > Infusion and Chemotherapy | codeable | Oncology | infusion, chemotherapy | Chemotherapy administration record | Chemotherapy, cycle/day (C1D1), regimen, infusion start/stop, premedication, port accessed, infusion, cycle, rate, iv, administered | dose |
| Infusion therapy note | Oncology | Procedure Note > Infusion and Chemotherapy | codeable | Infusion |  | Infusion therapy note | Infusion, Remicade, iron infusion, start/stop time, PICC/port | IV, IVIG, tolerated well |
| Oncology | Oncology | Progress Note | codeable |  |  | Oncology | hematology/oncology, malignant neoplasm, staging/TNM, metastatic, chemotherapy cycle, tumor markers | carcinoma, ECOG |
| Radiation therapy record | Oncology | Procedure Note > Radiation Therapy | codeable |  |  | Radiation therapy record | Radiation therapy, fractions, Gy/cGy, treatment site, radiation oncologist, simulation | XRT |
| Anesthesia Record | Operative Note | Operative Note > Anesthesia Record | codeable |  | anesthesia |  | anesthesia, induction, airway, intubation, sedation, asa, anesthesiologist, crna, emergence |  |
| Implant Record | Operative Note | Operative Note > Implant Record | needs_review |  | implant, implants |  | implant, lot, serial, manufacturer, catalog, sticker, device |  |
| Intra-operative Record | Operative Note | Operative Note > Intra-operative Record | needs_review |  | intraoperative |  | intraoperative, counts, sponge, needle, circulator, scrub, positioning, tourniquet, cautery |  |
| Operative Note | Operative Note | Operative Note | codeable |  | operative |  | preoperative, postoperative, surgeon, anesthesia, incision, procedure, estimated, specimens, complications, closure |  |
| Post-operative | Operative Note | Operative Note > Post-operative | needs_review |  | postoperative |  | postoperative, pacu, recovery, aldrete, discharge, dressing |  |
| Procedure | Operative Note | Operative Note > Procedure List | codeable |  |  | Procedure |  |  |
| Anaesthesia Report | Operative Report | Operative Note > Surgery Report | codeable |  |  | Anaesthesia Report, Anesthesia Record, pre-anesthesia evaluation | post-anesthesia, ASA class, Mallampati, airway, induction, intubation, anesthesiologist, Aldrete, surgeon, assistant, incision, closure, prepped, draped, sutures, estimated, specimens, findings | CRNA, PACU |
| Operation Note | Operative Report | Operative Note > Surgery Report | codeable |  |  | Operation Note, Operative Note, Op Note, brief op note | complications | procedure, findings, disposition |
| Operation Summary | Operative Report | Operative Note > Surgery Report | codeable | Surgery |  | Operation Summary, OR summary, case summary | in / out times, surgeon, wound class |  |
| Operative Report | Operative Report | Operative Note > Surgery Report | codeable |  |  | Operative Report, intraoperative record | preoperative diagnosis, postoperative diagnosis, procedure performed, surgeon, specimens | EBL, findings |
| Phase Record | Operative Report | Operative Note > Procedure List | codeable | Anesthesia |  | Phase Record, recovery record | Phase I, Phase II, Aldrete score, discharge criteria met, procedures, cpt, phase, case, scheduled | PACU |
| Post Operative Followup Note | Operative Report | Progress Note | codeable | Surgery |  | Post Operative Followup Note, postoperative visit | Post-op, s/p, incision healing, sutures removed, global period | POD |
| Pre Operative Evaluation | Operative Report | Progress Note | codeable |  |  | Pre Operative Evaluation, Preoperative evaluation | pre-op clearance, surgical risk, cleared for surgery, planned procedure | RCRI |
| Triage Report | Operative Report | Operative Note > Triage Report | codeable | Emergency |  | Triage Report |  |  |
| Fundus photography | Ophthalmology Note | Lab Report > Diagnostic Study | codeable | Ophthalmology |  | Fundus photography | Fundus photo, retinal imaging, optic disc, macula, retinopathy, drusen | interpretation |
| Laser procedure report | Ophthalmology Note | Procedure Note > Laser Procedure | codeable | Ophthalmology |  | Laser procedure report | Laser, YAG capsulotomy, photocoagulation, laser settings, spots, power, yag, slt, capsulotomy, trabeculoplasty, shots | SLT |
| Lens Parameters | Ophthalmology Note | Lab Report > Diagnostic Study > Optical Biometry (IOL Calculation) | codeable | Ophthalmology | biometry | Lens Parameters | Lens, sphere, cylinder, axis, add, base curve, diameter, IOL power, biometry, iol, axial, length, keratometry, power |  |
| Ophthalmology Note | Ophthalmology Note | Progress Note | codeable |  |  | Ophthalmology Note, ocular history | Ophthalmology, anterior segment, posterior segment, macula, glaucoma, cataract, visual field | IOP, OCT |
| Optometry note | Ophthalmology Note | Progress Note | codeable | Ophthalmology |  | Optometry note, dilated exam | Optometry, O.D. (Doctor of Optometry), visual field, Humphrey, retinopathy, background/proliferative, refraction, spectacle Rx |  |
| Retinal / Eye Report | Ophthalmology Note | Progress Note | codeable |  |  | Retinal / Eye Report, Retinal, Eye Report, dilated fundus exam | visual acuity, retina, intravitreal injection, Eylea, Avastin, laser photocoagulation | OD, OS, OU, IOP |
| Laboratory Requisition | Order and Requisition | Order and Requisition > Laboratory Requisition | non_codeable |  |  | Laboratory Requisition | requisition, tests, collection, fasting, draw, specimen, bill |  |
| Order and Requisition | Order and Requisition | Order and Requisition | non_codeable |  | requisition |  | order, orders, ordered, requisition, priority, stat, routine, indication |  |
| Physician Order | Order and Requisition | Order and Requisition > Physician Order | non_codeable |  |  | Physician Order |  |  |
| Referral Authorization Form | Order and Requisition | Consent and Authorization > Referral Authorization | non_codeable |  |  | Referral Authorization Form |  |  |
| Referral Order | Order and Requisition | Order and Requisition > Referral Order | needs_review |  |  |  | referral, referred, referring, specialist, reason, priority |  |
| Medication Orders | Orders | Order and Requisition > Physician Order | non_codeable |  |  | Medication Orders, Medication Order, med order | start / stop date, pharmacy verify, orders, order, ordered, verbal, telephone, stat, continue, discontinue | dose, route, frequency |
| Orders | Orders | Order and Requisition > Physician Order | non_codeable |  |  | Orders, order date, order status, standing order, physician order, verbal order, order note | ordered by | Order |
| Post Operative Orders | Orders | Order and Requisition > Physician Order | non_codeable |  |  | Post Operative Orders, Post-operative orders, post-op orders, PACU orders | advance diet, activity, DVT prophylaxis |  |
| Pre Operative Orders | Orders | Forms and Questionnaires > Others | needs_review |  |  | Pre Operative Orders, Pre-operative orders, pre-op orders | NPO after midnight, pre-op antibiotics, type and screen |  |
| Protocols | Orders | Order and Requisition > Others | non_codeable |  |  | Protocols, per protocol, standing protocol | Protocol, algorithm, sliding scale, guideline, pathway |  |
| Nutrition assessment | Other Evaluation | Progress Note | codeable |  |  | Nutrition assessment, diet order | dietitian, weight loss, malnutrition, albumin, tube feeding, caloric needs | RD, BMI |
| Other Evaluation | Other Evaluation | Forms and Questionnaires > Assessment | needs_review |  |  | Other Evaluation, initial evaluation, inpatient evaluation | re-evaluation, evaluated by | Evaluation, findings, goals |
| Index Page | Page Artefact | Others | non_codeable |  |  | Index Page, encounter index, encounter list, document list | Index, procedure index, table of contents, page range |  |
| Signature Page | Page Artefact | Progress Note | non_codeable |  |  | Signature Page |  |  |
| Pain & Palliative Note | Pain & Palliative Note | Progress Note | codeable | Palliative Care |  | Pain & Palliative Note | pain management, chronic pain, epidural steroid injection, nerve block, palliative care, goals of care, code status, hospice, comfort care | pain scale |
| Biopsy Report | Pathology Report | Lab Report > Pathology Report > Biopsy Report | codeable | Pathology | biopsy | Biopsy Report | histology, benign/malignant, margins, biopsy, gross, microscopic, tissue, fragments, stain | Biopsy, specimen, carcinoma, final diagnosis |
| Cytology / molecular report | Pathology Report | Lab Report > Pathology Report > Molecular Diagnostics Report | codeable | Pathology | molecular | Cytology / molecular report, Cytology, molecular report | cytopathology, adequacy, Bethesda, molecular diagnostics, molecular, pcr, amplification, assay, detected, mutation | FNA, PCR, mutation, NGS, EGFR, KRAS |
| Mohs surgery report | Pathology Report | Lab Report > Pathology Report > Others | non_codeable | Dermatology |  | Mohs surgery report | Mohs, stages, margins clear, basal cell, squamous cell, defect size, repair |  |
| Pathology Report | Pathology Report | Lab Report > Pathology Report > Others | codeable | Pathology |  | Pathology Report | Pathology, surgical pathology, gross description, margins, pathologist, flow cytometry | specimen, microscopic, final diagnosis |
| Tissue Report | Pathology Report | Lab Report > Pathology Report > Others | non_codeable |  |  | Tissue Report, Tissue examination | gross, pathologist | microscopic, specimen, diagnosis |
| General Notice | Patient Communication | Patient Communication > Notice | non_codeable |  |  | General Notice | notice, notification, notified, informed |  |
| Patient Communication | Patient Communication | Patient Communication | non_codeable |  |  |  | message, messages, call, called, phone, portal, voicemail, callback |  |
| Patient Communication > Telephone Encounter | Patient Communication | Patient Communication > Telephone Encounter | non_codeable |  |  |  | telephone, phone, called, caller, spoke, voicemail, callback, advised |  |
| Patient Education Material | Patient Communication | Patient Communication > Patient Education Material | non_codeable |  |  | Patient Education Material |  |  |
| Telephone Message | Patient Communication | Patient Communication > Patient Message | non_codeable |  |  | Telephone Message |  |  |
| Face Sheet | Patient Demographics | Demographics > Face Sheet | codeable |  |  | Face Sheet | facesheet, admit date, admitting diagnosis, room / bed, face, sheet, admission, account, guarantor, subscriber, insurance, admitting | attending |
| Patient Demographics | Patient Demographics | Demographics > Patient Information | codeable |  |  | Patient Demographics, admission record | Demographics, insurance, guarantor, emergency contact, address, application, phone, dob, emergency, contact, language, preferred, pharmacy | DOB, MRN |
| Patient Information | Patient Demographics | Demographics > Patient Information | codeable |  |  | Patient Information | patient details, contact information, preferred language, marital status | employer |
| Patient Registration | Patient Demographics | Administrative > Patient Registration | codeable |  | registration | Patient Registration, registration form, new patient registration | Registration, registered on, responsible party, insurance card, guarantor, subscriber, employer, emergency, insurance |  |
| Patient Education | Patient Education | Patient Communication > Patient Education Material | non_codeable |  |  | Patient Education, patient instructions | what is, when to call your doctor, follow up with, take as directed, education, learn, information, tips, handout |  |
| Perioperative Evaluation | Perioperative Evaluation | Operative Note > Pre-operative | codeable |  | preoperative | Perioperative Evaluation | preoperative, npo, checklist, clearance, consent, verified |  |
| Plan of Care | Plan of Care | Progress Note | codeable |  |  | Plan of Care | CMS-485, frequency/duration, certification period | POC, goals, interventions |
| Policy | Policy | Administrative > Others | non_codeable |  |  | Policy, office policy, office policies, financial policy, payment policy, no-show policy, cancellation policy, appointment policy, policy pages | patient rights and responsibilities |  |
| Clinical Diagnosis List | Problem List | Problem List > Diagnosis List/Index | non_codeable |  |  | Clinical Diagnosis List | diagnoses, icd, codes, index |  |
| Problem List | Problem List | Problem List > Problem List | non_codeable |  |  | Problem List | active problems, resolved problems, onset date, noted date, ICD-10, problems, active, resolved, onset, noted |  |
| Standalone Patient Summary | Problem List | Problem List | needs_review |  |  | Standalone Patient Summary | problems, problem, active, resolved, onset, icd |  |
| Cardiac Catheterization Report | Procedure Note | Lab Report > Diagnostic Study > Cardiac Catheterization Report | codeable | Cardiology | catheterization | Cardiac Catheterization Report | catheterization, coronary, angiography, ventriculography, lad, lcx, rca, stent, fluoroscopy |  |
| Cardiac Procedure | Procedure Note | Procedure Note > Cardiac Procedure | codeable |  | cardioversion |  | cardioversion, joules, cath, lab, sheath, fluoroscopy |  |
| Elastography | Procedure Note | Procedure Note > Elastography | codeable |  | elastography, fibroscan | Elastography | elastography, fibroscan, stiffness, kpa, cap |  |
| Endoscopy (EGD) | Procedure Note | Procedure Note > Endoscopy (EGD) | codeable |  |  |  | egd, endoscopy, scope, sedation, esophagus |  |
| Injection Visit | Procedure Note | Procedure Note > Injection Visit | codeable |  |  | Injection Visit | injection, injected, site, lot, administered, intramuscular, subcutaneous |  |
| Knee Procedure | Procedure Note | Procedure Note > Knee Procedure | codeable |  |  | Knee Procedure | knee, aspiration, arthrocentesis, intraarticular, injection |  |
| Lumbar Procedure | Procedure Note | Procedure Note > Lumbar Procedure | codeable |  |  | Lumbar Procedure | lumbar, epidural, facet, fluoroscopy, transforaminal, needle |  |
| Office Procedure | Procedure Note | Procedure Note > Office Procedure | codeable |  |  | Office Procedure | procedure, office, anesthetic, lidocaine, sterile, tolerated |  |
| Pathology Procedure | Procedure Note | Procedure Note > Pathology Procedure | codeable |  |  | Pathology Procedure | gross, grossing, cassettes, sections, fixed |  |
| Procedure Note | Procedure Note | Procedure Note > Others | codeable |  |  | Procedure Note | indication, time-out, local anesthesia, complications: none | technique, tolerated well |
| Procedure Note (class words) | Procedure Note | Procedure Note | codeable |  |  |  | procedure, performed, tolerated, session, treatment, administered |  |
| Admission Record | Progress Note | Progress Note | needs_review |  |  | Admission Record | hpi, subjective, objective, assessment, plan, ros, exam, complaint, presents, interval, followup, counseled |  |
| Assessment and Plan | Progress Note | Progress Note > Assessment and Plan | codeable |  |  |  | assessment, plan, continue, recommend, recommended, followup, monitor, discussed, counseled |  |
| CC/HPI | Progress Note | Progress Note > CC/HPI | codeable |  |  |  | complaint, hpi, presents, onset, duration, illness, reports, denies, history |  |
| F2F Evaluation Note | Progress Note | Progress Note | codeable |  |  | F2F Evaluation Note, F2F encounter | Face-to-face, home health certification, clinical findings support | DME |
| Lab and Imaging Results | Progress Note | Progress Note > Lab and Imaging Results | codeable |  |  |  | results, reviewed, labs, imaging, findings, impression, values |  |
| Office Visit | Progress Note | Progress Note | codeable |  |  | Office Visit, return visit, initial visit | f/u | OV, follow-up, RTC, new patient |
| Office Visit Note | Progress Note | Progress Note | codeable |  |  | Office Visit Note | history of present illness, HPI, review of systems, ROS, physical exam, physical examination, assessment and plan, assessment & plan, impression and plan, interval history, subjective, objective, chief complaint, reason for visit |  |
| Patient Instructions | Progress Note | Progress Note > Patient Instructions | codeable |  |  |  | instructions, return, followup, call, precautions, education, advised |  |
| Physical Exam | Progress Note | Progress Note > Physical Exam | codeable |  |  |  | exam, examination, general, heent, neck, lungs, heart, abdomen, extremities, neurological, skin, ros |  |
| Physician Note | Progress Note | Progress Note | codeable |  |  | Physician Note | electronically signed by | MD, DO, attending, assessment, plan |
| Procedure List | Progress Note | Progress Note > Procedure List | codeable |  |  |  | procedure, performed, tolerated, indication, anesthetic, consent |  |
| Progress Note | Progress Note | Progress Note | codeable |  |  | Progress Note, History & Physical | Subjective, Objective, Consultation, reason for consult, established patient | SOAP, Assessment, Plan, Chief Complaint, HPI, H&P, encounter, follow-up, interval history |
| Progress Note > Medication List | Progress Note | Progress Note > Medication List | non_codeable |  |  |  | medications, allergies, tablet, capsule, dose, sig, refills, reconciliation |  |
| Progress Note > Problem List | Progress Note | Progress Note > Problem List | non_codeable |  |  |  | problems, problem, resolved, onset, chronicity |  |
| Progress Note > Questionnaire | Progress Note | Progress Note > Questionnaire | needs_review |  |  |  | questionnaire, screening, score, phq, gad, audit, responses |  |
| Progress Note > Summary | Progress Note | Progress Note > Summary | codeable |  |  |  | summary, course, hospitalization, recap, overview |  |
| Progress Note > Visit Diagnosis | Progress Note | Progress Note > Visit Diagnosis | codeable |  |  |  | diagnosis, diagnoses, icd, encounter, primary, secondary, assessed |  |
| Signature | Progress Note | Progress Note > Signature | codeable |  |  |  | electronically, signed, attestation, attest, cosigned, authenticated, addendum |  |
| Signature page | Progress Note | Progress Note | codeable |  |  | Signature page | signed by, electronically signed, date/time signed | Signature, witness |
| Social History | Progress Note | Progress Note | codeable |  |  | Social History | social work, psychosocial, living situation, tobacco, alcohol, caregiver, support system |  |
| Summary | Progress Note | Progress Note | codeable |  |  | Summary, encounter summary, summary of visit |  | summary of care |
| Visit Diagnosis | Progress Note | Progress Note | codeable |  |  | Visit Diagnosis, Visit Diagnoses, encounter diagnoses, diagnoses this visit, diagnosis associated with visit | ICD-10 | primary diagnosis |
| Vitals | Progress Note | Progress Note > Vitals | needs_review |  |  |  | vitals, bp, pulse, temperature, respiration, weight, height, bmi, spo2 |  |
| Consultation Letter | Provider Letter | Provider Letter > Consultation Letter | non_codeable |  |  |  | consultation, consulted, recommendations, thank, opinion |  |
| Provider Letter | Provider Letter | Provider Letter > Others | codeable |  |  | Provider Letter |  |  |
| Provider Letter (class words) | Provider Letter | Provider Letter | non_codeable |  |  |  | dear, sincerely, regards, letter, colleague, thank |  |
| Records Request | Provider Letter | Provider Letter > Records Request | non_codeable |  | Medical Records Request, Medical Record Request, Request for Medical Records, Request for Records, Records Request, Record Request, Request for Copies of Medical Records |  | please send, copies of records, date range of records, records, request, copies, send, subpoena, attention | request, records |
| Referral Letter | Provider Letter | Provider Letter > Referral Letter | non_codeable |  |  |  | referring, referral, evaluation, kindly, thank |  |
| Behavioral health note | Psychiatric Evaluation | Progress Note | codeable |  |  | Behavioral health note, client note | Behavioral health, counselor, presenting problem, GAD-7, mood | PHQ-9 |
| Group Therapy Note | Psychiatric Evaluation | Progress Note | codeable |  |  | Group Therapy Note | Group therapy, group session, 90853, participation, topic, facilitator |  |
| Initial psychiatric evaluation | Psychiatric Evaluation | Progress Note | codeable |  |  | Initial psychiatric evaluation, psychiatric history | psychiatric intake, diagnostic impression, DSM-5 | new patient |
| Neurocognitive screening exam | Psychiatric Evaluation | Lab Report > Diagnostic Study > Neurocognitive Screening Examination | codeable | Neurology |  | Neurocognitive screening exam | Neurocognitive, cognitive screening, MoCA, SLUMS, memory, cognitive, mmse, recall, orientation, clock | Mini-Cog, dementia, score /30 |
| Psychiatric Note | Psychiatric Evaluation | Progress Note | codeable |  |  | Psychiatric Note, Psychiatric progress note | psychiatry follow-up, medication management, mental status | interval history |
| Substance use treatment record | Psychiatric Evaluation | Progress Note | codeable | Behavioral Health |  | Substance use treatment record | Substance use disorder, alcohol dependence, opioid use disorder, Suboxone, methadone, detox, relapse | SUD, MAT |
| Bone Density (DEXA) Report | Radiology Report | Lab Report > Radiology Report > Bone Mineral Density Report | non_codeable |  | dexa, densitometry | Bone Density (DEXA) Report, Bone Density Report | bone mineral density, T-score, Z-score, osteopenia, osteoporosis, femoral neck, lumbar spine, dexa, densitometry, tscore, zscore, bmd, femoral, lumbar, frax | DEXA, DXA, BMD, FRAX |
| Brain MRI Report | Radiology Report | Lab Report > Radiology Report > Brain MRI Report | non_codeable | Neurology |  | Brain MRI Report | brain, flair, diffusion, ventricles, intracranial, parenchyma |  |
| CT Scan Report | Radiology Report | Lab Report > Radiology Report > CT Scan Report | non_codeable |  |  | CT Scan Report | CAT scan, computed tomography, axial images, Hounsfield, ct, computed, tomography, axial, reformats, slices | CT, contrast, impression |
| MR brain report | Radiology Report | Lab Report > Radiology Report > MRI Report | non_codeable |  | mri | MR brain report | MRI brain, MR head, with/without contrast, white matter, FLAIR, mri, magnetic, resonance, weighted, gadolinium, sagittal, coronal | infarct, impression |
| MRI Report | Radiology Report | Lab Report > Radiology Report > MRI Report | non_codeable |  |  | MRI Report | magnetic resonance, T1/T2, gadolinium, radiologist | MRI, impression |
| Radiology Report | Radiology Report | Lab Report > Radiology Report > Mammogram Report | non_codeable |  | mammogram, mammography | Radiology Report, imaging report | Radiology, exam, comparison, radiologist, mammogram, BI-RADS, mammography, birads, breast, tomosynthesis, density, screening | technique, findings, impression |
| Sonogram Report | Radiology Report | Lab Report > Radiology Report > Ultrasound Report | non_codeable |  | ultrasound, sonogram | Sonogram Report | ultrasound, sonogram, sonographic, echogenic, doppler, transducer |  |
| Sonogram report | Radiology Report | Lab Report > Radiology Report > Ultrasound Report | non_codeable |  |  | Sonogram report | Sonogram, sonography, ultrasound, echogenic, transducer, sonographer | impression |
| Tomography | Radiology Report | Lab Report > Radiology Report > CT Scan Report | non_codeable |  |  | Tomography | computed tomography, slices, axial | CT, contrast, impression |
| Tomography Report | Radiology Report | Lab Report > Radiology Report > CT Scan Report | non_codeable |  |  | Tomography Report |  |  |
| Ultrasound Report | Radiology Report | Lab Report > Radiology Report > Ultrasound Report | non_codeable |  |  | Ultrasound Report | Ultrasound, sonographic, echogenicity, abdomen/pelvis/thyroid/renal | US, Doppler, impression |
| X Ray Report | Radiology Report | Lab Report > Radiology Report > X-Ray Report | non_codeable |  | radiograph, x-ray | X Ray Report | X-ray, radiograph, chest X-ray, views, AP / lateral, PA and lateral, fracture, infiltrate, radiographs, pa, lateral, film | CXR |
| X-Ray Report | Radiology Report | Lab Report > Radiology Report > X-Ray Report | non_codeable |  |  | X-Ray Report |  |  |
| Insurance Authorization Form | Referral & Authorization | Consent and Authorization > Referral Authorization | non_codeable |  |  | Insurance Authorization Form, Prior authorization | auth number, payer, insurance verification, units approved, effective dates, referral, authorized, visits, approved, valid, units | approved/denied |
| Referral & Authorization | Referral & Authorization | Consent and Authorization > Referral Authorization | non_codeable |  |  | Referral & Authorization | auth number, approved, valid from / to | referral, authorization, request |
| Referral Request | Referral & Authorization | Consent and Authorization > Referral Authorization | non_codeable |  |  | Referral Request, referral order, reason for referral | refer to, referred to, priority, referring provider | Referral |
| Referral authorization form | Referral & Authorization | Consent and Authorization > Referral Authorization | non_codeable |  |  | Referral authorization form, authorization number, diagnosis for referral | referred to, referring provider, number of visits approved, valid from/to | Referral |
| Oximetry report | Respiratory | Lab Report > Diagnostic Study > Oximetry Report | codeable | Sleep Study |  | Oximetry report | Oximetry, pulse ox, SpO2, overnight oximetry, desaturation, room air, O2 saturation, 88% |  |
| Pulmonary function test / spirometry | Respiratory | Lab Report > Diagnostic Study > Pulmonary Function Test | non_codeable |  |  | Pulmonary function test / spirometry, Pulmonary function test, spirometry | FEV1, FEV1/FVC, % predicted, bronchodilator response, obstructive/restrictive | PFT, FVC, DLCO |
| Respiratory | Respiratory | Progress Note | codeable | Pulmonology |  | Respiratory | Respiratory therapy, nebulizer, albuterol, oxygen L/min, BiPAP/CPAP, breath sounds | RT, COPD |
| Ventilator management record | Respiratory | Operative Note > Critical Care Record | codeable | Critical Care |  | Ventilator management record | Ventilator, vent settings, FiO2, tidal volume, AC/SIMV, weaning, extubation, tracheostomy | PEEP |
| Checklist | Screening and Checklist | Forms and Questionnaires > Questionnaire | needs_review |  |  | Checklist, check list, preventive health checklist | yes / no, initials, questionnaire, survey, screening, phq, gad, score, responses | completed |
| Growth Chart | Screening and Checklist | Forms and Questionnaires > Assessment | non_codeable |  |  | Growth Chart |  |  |
| Questionnaire | Screening and Checklist | Forms and Questionnaires > Questionnaire | needs_review |  |  | Questionnaire, intake form, health history form | patient-completed, please circle, yes/no | PHQ-9 |
| CPAP / BiPAP titration report | Sleep Study | Lab Report > Diagnostic Study > Titration Report | codeable | Sleep Study |  | CPAP / BiPAP titration report, BiPAP titration report | Titration, BiPAP, optimal pressure, cm H2O, residual AHI, mask, cpap, pressure, leak | CPAP |
| Oximetry Report | Sleep Study | Lab Report > Diagnostic Study > Oximetry Report | codeable | Sleep Study | oximetry | Oximetry Report | oximetry, saturation, desaturation, spo2, overnight, nadir |  |
| Polysomnography | Sleep Study | Lab Report > Diagnostic Study > Sleep Study Report | codeable | Sleep Study | polysomnography, polysomnogram | Polysomnography, sleep study | obstructive sleep apnea, sleep efficiency, desaturation nadir, polysomnography, ahi, apnea, hypopnea, arousal, rem, sleep | PSG, AHI, RDI, REM |
| Sleep Study | Sleep Study | Lab Report > Diagnostic Study > Sleep Study Report | codeable | Sleep Study |  | Sleep Study | sleep clinic, sleep medicine, apnea, CPAP compliance, Epworth, sleep efficiency | AHI |
| Sleep Study Report | Sleep Study | Lab Report > Diagnostic Study > Sleep Study Report | codeable | Sleep Study |  | Sleep Study Report |  |  |
| Split-night sleep study | Sleep Study | Lab Report > Diagnostic Study > Sleep Study Report | codeable | Sleep Study |  | Split-night sleep study | Split night, diagnostic portion, titration portion, CPAP pressure | AHI |
| Audiology Report | Specialty Note | Progress Note | codeable | Audiology |  | Audiology Report |  |  |
| Nephrology Note | Specialty Note | Progress Note | codeable | Nephrology |  | Nephrology Note |  |  |
| Oncology Note | Specialty Note | Progress Note | codeable | Oncology |  | Oncology Note |  |  |
| Phone Note | Telephone Encounter | Progress Note | non_codeable |  |  | Phone Note, telephone note | spoke with patient, pt called, advised, call documented |  |
| Telephone Encounter | Telephone Encounter | Progress Note | codeable |  |  | Telephone Encounter, phone visit, telephone message, message taken by | call back number, triage call, nurse triage |  |
| Acupuncture treatment record | Therapy | Procedure Note > Therapy Session | codeable |  |  | Acupuncture treatment record | Acupuncture, L.Ac, needles, points, meridian, therapy, therapist, session, goals, exercises, strengthening, gait, rehabilitation | session |
| Chiropractic treatment note | Therapy | Procedure Note > Therapy Session | codeable | Orthopedics |  | Chiropractic treatment note | Chiropractic, D.C., subluxation, spinal manipulation, adjustment, cervical/lumbar |  |
| Occupational Therapy | Therapy | Procedure Note > Therapy Session | codeable | Rehabilitation |  | Occupational Therapy, OT daily note | OTR/L, ADL training, fine motor, splint | OT, upper extremity |
| Physical Therapy | Therapy | Procedure Note > Therapy Session | codeable |  |  | Physical Therapy | strength 4/5, gait training, therapeutic exercise | PT, DPT, ROM, plan of care, goals |
| Rehabilitation Plan | Therapy | Progress Note | codeable | Rehabilitation |  | Rehabilitation Plan | short-term/long-term, duration | goals, frequency, interventions |
| Speech therapy note | Therapy | Procedure Note > Therapy Session | codeable | Rehabilitation |  | Speech therapy note, swallow evaluation | Speech therapy, aphasia, cognitive-linguistic, dysarthria | SLP, dysphagia |
| Therapy | Therapy | Procedure Note > Therapy Session | codeable | Rehabilitation |  | Therapy, daily treatment note | therapist, treatment rendered, minutes, progress toward goals, cardiac rehab | units |
| Physical Therapy Note | Therapy Note | Procedure Note > Therapy Session | codeable | Rehabilitation |  | Physical Therapy Note |  |  |
| Transfer Summary | Transfer of Care Summary | Discharge Summary > Transfer of Care | discharge |  |  | Transfer Summary | transferred to, reason for transfer, accepting physician, hospital course, condition at transfer, transfer, transferred, handoff, handover, receiving, accepting |  |
| ED Triage | Triage Note | Operative Note > Triage Report | non_codeable |  | triage | ED Triage | emergency triage, ESI level, arrival mode, triage time, triage nurse, triage, acuity, esi, arrival, chief, mode | acuity |
| Triage Note | Triage Note | Operative Note > Triage Report | non_codeable | Emergency |  | Triage Note | Triage, ESI level, arrival mode | acuity, chief complaint, vitals, pain scale |
| Carotid duplex | Vascular Studies | Lab Report > Diagnostic Study > Carotid Duplex Report | non_codeable |  |  | Carotid duplex | carotid Doppler, % stenosis, peak systolic velocity | ICA, CCA, plaque |
| Doppler-duplex | Vascular Studies | Lab Report > Diagnostic Study > Extremity Doppler Report | codeable | Cardiology |  | Doppler-duplex | duplex, velocity, waveform, flow, color Doppler, spectral, vascular ultrasound, extremity, venous, femoral, popliteal, doppler, compressibility | Doppler |
| Lower / upper extremity Doppler-duplex | Vascular Studies | Lab Report > Diagnostic Study > Extremity Doppler Report | codeable | Cardiology |  | Lower / upper extremity Doppler-duplex, upper extremity Doppler-duplex | Lower extremity, venous duplex, arterial Doppler, waveform, reflux, vascular lab | upper extremity, DVT, PAD, stenosis |
| Plethysmography report | Vascular Studies | Lab Report > Diagnostic Study > Plethysmography Report | non_codeable |  |  | Plethysmography report | Plethysmography, pulse volume recording, segmental pressures, waveforms, body box, lung volumes | PVR |
| Vascular Studies | Vascular Studies | Lab Report > Diagnostic Study > Others | non_codeable | Cardiology |  | Vascular Studies | vascular lab, arterial, venous, waveform, velocity, ankle-brachial index | stenosis, ABI |
| Burn treatment record | Wound & Infection Care | Procedure Note > Wound Care | codeable | Wound Care |  | Burn treatment record | Burn, % TBSA, degree/thickness, Silvadene, grafting, debridement, dressing change, wound, dressing, granulation, drainage, measurements, length, width, depth |  |
| Infection control note | Wound & Infection Care | Procedure Note > Wound Care | codeable |  |  | Infection control note | Infection control, isolation, C. diff, contact precautions, culture positive, antibiotic | MRSA |
| Wound & Infection Care | Wound & Infection Care | Procedure Note > Wound Care | codeable | Wound Care |  | Wound & Infection Care | wound, ulcer, infection, isolation | dressing |
| Wound care note | Wound & Infection Care | Procedure Note > Wound Care | codeable |  |  | Wound care note | Wound care, pressure ulcer stage, diabetic foot ulcer, measurements (L x W x D), debridement, drainage, granulation | dressing |
| Infection Control Note | Wound Care Note | Procedure Note > Wound Care | codeable |  |  | Infection Control Note |  |  |
| Wound Care Note | Wound Care Note | Procedure Note > Wound Care | codeable | Wound Care |  | Wound Care Note |  |  |

### F. Specialty lexicon (specialty.json)

The specialty stream. Parallel to page type, never nested inside it: a page is Progress Note / Office Visit Note AND Ophthalmology, not Progress Note > Ophthalmology. Optional by design. Measured on 623 real pages, a specialty is detectable on 43% and two or more compete on 12%, so 'Unspecified' is a normal answer rather than a failure. Strongest evidence is the practice name in the letterhead, which is why header hits are weighted above body hits.

**Matching**: header_band_fraction: 0.18; header_weight: 6.0; body_weight: 1.0; min_score: 6.0; margin_to_decide: 3.0; comment_margin: If the top two specialties are within this, the page is Ambiguous rather than a coin flip. Resolve it at document level instead.; inherit_across_document: True; comment_inherit: A specialty found on any page of a document applies to every page of it. A lab table on page 4 of an ophthalmology note is still ophthalmology.

| Specialty | Header / letterhead terms | Body terms | Note |
|---|---|---|---|
| Ophthalmology | ophthalmology, optometry, eye institute, eye center, eye associates, eye care, retina, vision center, optometrist, visionworks, eye specialists, eye care center | intraocular, fundus, visual field, slit lamp, visual acuity, tonometry, dilated retinal, cataract, glaucoma, macular, refraction, iol calculation, intraocular lens, corneal topography | Ophthalmology and optometry are one combined specialty (client, 2026-10-24). |
| Cardiology | cardiology, cardiovascular, heart institute, heart center, cardiac center | echocardiogram, electrocardiogram, pacemaker, ejection fraction, stress test, telemetry, atrial fibrillation, coronary |  |
| Oncology | oncology, cancer center, cancer institute, hematology oncology, infusion center | chemotherapy, tumor board, staging, metastatic, radiation therapy, cycle 1, carcinoma |  |
| Urology | urology, urologic, urological associates, urological | cystoscopy, prostate, bladder instillation, bcg, psa, urodynamic, nephrolithiasis, renal stone |  |
| Orthopedics | orthopaedic, orthopedic, bone and joint, sports medicine, spine center | arthroplasty, rotator cuff, fracture, acl, meniscus, range of motion, joint injection |  |
| Neurology | neurology, neurological, neuroscience | electroencephalogram, electromyography, nerve conduction, seizure, neuropathy, migraine, parkinson |  |
| Nephrology | nephrology, renal associates, kidney center, dialysis center | hemodialysis, peritoneal dialysis, esrd, creatinine clearance, av fistula, renal failure |  |
| Obstetrics and Gynecology | obstetric, gynecolog, ob/gyn, obgyn, maternal fetal, women's health | prenatal, antepartum, postpartum, gestational age, fetal heart, colposcopy, pap smear, trimester |  |
| Gastroenterology | gastroenterology, digestive health, gi associates | colonoscopy, endoscopy, egd, sigmoidoscopy, polyp, esophageal, hepatic |  |
| Pulmonology | pulmonology, pulmonary, respiratory, sleep center, sleep lab | spirometry, pulmonary function, copd, sleep study, apnea, cpap, fev1 |  |
| Dermatology | dermatology, skin center, skin institute | mohs, skin biopsy, lesion, melanoma, basal cell, cryotherapy |  |
| Behavioral Health | psychiatry, psychiatric, behavioral health, mental health, psychology | psychotherapy, depression screening, phq-9, gad-7, axis i, suicidal ideation |  |
| Radiology | radiology, imaging center, imaging services, diagnostic imaging | impression:, findings:, contrast, tomography, mammography |  |
| Pathology | pathology, laboratory medicine, cytopathology | specimen, gross description, microscopic, immunohistochem, histolog |  |
| Rehabilitation | physical therapy, occupational therapy, speech therapy, rehabilitation, rehab services | range of motion, gait training, therapeutic exercise, functional mobility, plan of care units |  |
| Audiology | audiology, hearing center, ear nose and throat, otolaryngology | audiogram, tympanometry, hearing aid, speech discrimination |  |
| Primary Care | family medicine, family practice, internal medicine, primary care, general practice, family health | annual wellness, health maintenance, preventive care |  |
| Emergency | emergency department, emergency room, urgent care, emergency services | triage, chief complaint:, ed course, disposition: |  |
| Podiatry | podiatry, podiatric, foot and ankle, foot & ankle, foot clinic, d.p.m, dpm, foot care center, ankle institute | plantar fasciitis, hallux valgus, onychomycosis, bunionectomy, diabetic foot exam, nail debridement, hammertoe, metatarsal, charcot foot, ankle brachial index |  |
| Home Health | home care, home health, home health agency, home health care, homecare services, in-home care, nursing visit note, skilled nursing visit, visiting nurse, in-home visit, in home visit, home visit summary | activities of daily living in the home, attended by, can patient direct a home care worker, caregiver, dates of service, home safety, homebound reason, identify all individuals living in the home, light housekeeping, lives alone, meal preparation, needs assistance for all activities, nursing assessment for home care, supervisory only, task list and time record, taxing effort to leave home, total time, type of visit: skilled, unable to leave home, in-home assessment, in home assessment |  |
| Pain Management | pain management, pain & spine, pain and spine, interventional pain, spine and pain, pain clinic, pain center, pain centre, pain institute, pain specialists | medial branch block, radiofrequency ablation, epidural steroid injection, facet injection, pain contract, controlled substance agreement, trigger point injection, pmp reviewed |  |
| Anesthesia | anesthesia, anesthesiology, anesthesia record, anesthesia associates, crna | airway, laryngoscopy grade, mallampati, asa class, sugammadex, propofol, induction, extubated, monitored anesthesia care |  |
| Critical Care | critical care, intensive care unit, icu, micu, sicu, ccu | ventilator, fio2, peep, vasopressor, central line, arterial line, sedation vacation |  |
| Surgery | general surgery, surgical associates, surgical specialists, department of surgery, surgical group | operative report, preoperative diagnosis, postoperative diagnosis, estimated blood loss, incision, specimens removed, wound closure | Not "surgery center" or "surgical center": an ambulatory surgery centre hosts eye, GI and pain procedures, so its name does not decide the specialty. |

**Page types that imply a specialty**

| Page type | Specialty |
|---|---|
| Cardiology Note | page_family: Progress Note; page_type: Progress Note; specialty: Cardiology |
| Dermatology Note | page_family: Progress Note; page_type: Progress Note; specialty: Dermatology |
| Nephrology Note | page_family: Progress Note; page_type: Progress Note; specialty: Nephrology |
| Neurology Note | page_family: Progress Note; page_type: Progress Note; specialty: Neurology |
| Oncology Note | page_family: Progress Note; page_type: Progress Note; specialty: Oncology |
| Urology Note | page_family: Progress Note; page_type: Progress Note; specialty: Urology |
| Audiology Report | page_family: Progress Note; page_type: Progress Note; specialty: Audiology |
| Genetic Counseling Note | page_family: Progress Note; page_type: Consultation Note; specialty: Primary Care |
| Travel Medicine Consultation | page_family: Progress Note; page_type: Consultation Note; specialty: Primary Care |
| Ophthalmology Report | page_family: Eye Examination; page_type: Eye Examination Report; specialty: Ophthalmology |
| Optometry Note | page_family: Eye Examination; page_type: Eye Examination Report; specialty: Ophthalmology |
| Physical Therapy Note | page_family: Therapy Note; page_type: Therapy Note; specialty: Rehabilitation |
| Occupational Therapy Note | page_family: Therapy Note; page_type: Therapy Note; specialty: Rehabilitation |
| Occupational Therapy Daily Note | page_family: Therapy Note; page_type: Therapy Note; specialty: Rehabilitation |
| Speech Therapy Note | page_family: Therapy Note; page_type: Therapy Note; specialty: Rehabilitation |
| Respiratory Therapy Note | page_family: Therapy Note; page_type: Therapy Note; specialty: Pulmonology |
| Chiropractic Treatment Note | page_family: Therapy Note; page_type: Therapy Note; specialty: Rehabilitation |
| Acupuncture Treatment Record | page_family: Therapy Note; page_type: Therapy Note; specialty: Rehabilitation |
| Cardiac Rehabilitation Note | page_family: Therapy Note; page_type: Therapy Note; specialty: Cardiology |
| Physical Therapy Discharge Summary | page_family: Therapy Discharge Summary; page_type: Therapy Discharge Summary; specialty: Rehabilitation |
| Occupational Therapy Discharge Summary | page_family: Therapy Discharge Summary; page_type: Therapy Discharge Summary; specialty: Rehabilitation |
| Behavioral Health Discharge Summary | page_family: Discharge Summary; page_type: Discharge Summary; specialty: Behavioral Health |

### G. Section lexicon (sections.json) — Progress Note L2

Page content sections for the subcategory axis. A page can carry several, so this is multi-label. Each section lists the header phrases that announce it. 'rollup' phrases are sub-parts that imply the parent section (exam regions imply Physical Exam, analytes imply Lab Results) and are deliberately weaker evidence than a real header. 'priority' breaks ties when one dominant section must be named: lower wins, and it is ordered by what a coder cares about, so a page carrying both a diagnosis and a lab table is reported as Visit Diagnosis.

**Matching**: header_line_only: True; header_band: top_fraction: 0.15; multiplier: 2.0; header_weight: 6.0; rollup_weight: 1.0; min_score_to_tag: 4.0; administrative_is_furniture: True

| Section | Definition |
|---|---|
| visit_diagnosis | display: Visit Diagnosis; priority: 10; codeable_signal: True; headers: assessment, assessment and plan, assessment plan, impression, diagnosis, diagnoses, visit diagnosis, visit diagnoses, principal diagnosis, encounter diagnosis, active problems, problem list, clinical diagnosis, imp plan, impression plan, discharge diagnosis, discharge diagnoses, final diagnosis, final diagnoses, principal discharge diagnosis, diagnoses at discharge, diagnosis at discharge; rollup: |
| plan | display: Plan; priority: 15; codeable_signal: True; headers: plan, treatment plan, plan of care, recommendations, disposition, orders placed; rollup: |
| history_of_present_illness | display: History of Present Illness; priority: 20; codeable_signal: True; headers: hpi, history of present illness, interval history, interval hx, subjective, narrative; rollup: |
| chief_complaint | display: Chief Complaint; priority: 25; codeable_signal: False; headers: chief complaint, chief complaints, reason for visit, presenting complaint, reason for encounter; rollup: |
| physical_exam | display: Physical Exam; priority: 30; codeable_signal: True; headers: physical exam, physical examination, exam, objective, examination; rollup: general, constitutional, heent, neck, chest, lungs, respiratory, cardiovascular, heart, abdomen, extremities, skin, neurological, neurologic, psychiatric, musculoskeletal, lymphatic |
| review_of_systems | display: Review of Systems; priority: 35; codeable_signal: False; headers: ros, review of systems, systems review; rollup: |
| vitals | display: Vitals; priority: 40; codeable_signal: False; headers: vitals, vital signs, vs; rollup: blood pressure, pulse, temperature, respirations, spo, oxygen saturation, height, weight, bmi |
| lab_results | display: Lab Results; priority: 45; codeable_signal: False; headers: labs, lab results, laboratory, laboratory results, lab data, results, diagnostic results; rollup: cbc, bmp, cmp, a1c, hemoglobin, glucose, protein, bilirubin, ketones, urinalysis, creatinine, tsh, cholesterol, specific gravity |
| imaging_results | display: Imaging Results; priority: 46; codeable_signal: False; headers: imaging, imaging results, radiology, radiology results; rollup: x ray, ct scan, mri, ultrasound, echocardiogram |
| procedures | display: Procedures; priority: 50; codeable_signal: True; headers: procedures, procedure, procedure performed, procedures performed, in office procedures; rollup: |
| medications | display: Medications; priority: 55; codeable_signal: False; headers: medications, current medications, medication list, current outpatient medications, prescriptions, meds; rollup: |
| allergies | display: Allergies; priority: 56; codeable_signal: False; headers: allergies, drug allergies, allergy list, nkda; rollup: |
| immunizations | display: Immunizations; priority: 57; codeable_signal: False; headers: immunizations, immunization history, vaccines, vaccination history; rollup: |
| past_medical_history | display: Past Medical History; priority: 60; codeable_signal: True; headers: past medical history, pmh, medical history, past surgical history, psh, surgical history, treatment history, case history; rollup: |
| social_history | display: Social History; priority: 65; codeable_signal: False; headers: social history, sochx, smoking status, tobacco use, alcohol use, substance use; rollup: |
| family_history | display: Family History; priority: 66; codeable_signal: False; headers: family history, famhx, family hx; rollup: mother, father, grandmother, grandfather, sibling |
| summary | display: Summary; priority: 70; codeable_signal: False; headers: summary, clinical summary, visit summary, encounter summary, patient summary, after visit summary, comments; rollup: |
| patient_instructions | display: Patient Instructions; priority: 75; codeable_signal: False; headers: instructions, patient instructions, patient education, education, discharge instructions, home care instructions; rollup: |
| follow_up | display: Follow Up; priority: 80; codeable_signal: False; headers: follow up, followup, next appointment, return visit, return to clinic, disposition and follow up; rollup: |
| administrative | display: Administrative; priority: 95; codeable_signal: False; furniture: True; headers: mrn, dob, date of birth, encounter id, encounter info, note date, provider, care team, billing summary, appointment type, patient mrn, account, guarantor, location, printed, phone, signed, electronically signed; rollup: |

### H. Encounter lexicon used to label the training data (Training/processed/encounter_canon.json)

Encounter type vocabulary. Four real settings plus Not Available. Scored per page; the answer belongs to the DOCUMENT, so page scores are pooled over the document before a label is given. 'strong' phrases are worth 3, 'weak' 1, and a strong phrase in the top header band is worth 5. Negatives cancel their own class only.

**Classes**: Inpatient, Outpatient (F2F), Outpatient (Evisit), Outpatient (Home Visit), Not Available

**Default when outpatient but the modality is unclear**: Outpatient (Evisit) — Client instruction: an outpatient encounter with no modality statement defaults to Evisit. Applied as an explicit rule after scoring, never learned, so it stays auditable.

**Matching**: header_band_fraction: 0.18; strong_weight: 3.0; strong_header_weight: 5.0; weak_weight: 1.0; min_score: 3.0; margin_to_decide: 2.0

| Class | Strong terms | Weak terms | Other |
|---|---|---|---|
| Inpatient | date of admission, admitted on, admission date, discharge summary, hospital course, discharge diagnosis, inpatient, post-op day, transferred to the floor, admitted to the hospital, room number, attending of record, daily progress note, icu, intensive care unit, observation status, length of stay, bed assignment | ward, rounds, nursing shift, telemetry, nurse station, admitting physician, step down | negative: discharged from clinic, office visit |
| Outpatient (F2F) | office visit, seen in the office, clinic visit, presented to the clinic, in-person visit, face to face visit, patient presents to the office, examined in clinic, vitals obtained in office, ambulatory visit, outpatient clinic, walk-in visit, surgery center, surgical center, surgery centre, surgical centre, ambulatory surgery, ambulatory surgical, outpatient surgery | exam room, waiting room, front desk, check-in, height and weight measured, blood pressure taken | negative: telehealth, video visit, telephone visit, home visit |
| Outpatient (Evisit) | telehealth, telemedicine, video visit, virtual visit, telephone visit, audio-only visit, audio visit, e-visit, evisit, econsult, e-consult, consented to telehealth, patient was not physically present, visit conducted by video, visit conducted by telephone, remote evaluation, portal encounter, web encounter, originating site, distant site, modifier 95, pos 02, pos 10 | called the patient, connected via, verbal consent obtained, patient at home during the call | negative: seen in the office, home visit, in-person visit |
| Outpatient (Home Visit) | domiciliary visit, home based primary care, home health aide, home health visit, home visit, homebound, homebound reason, house call, in-home assessment, nursing visit note, patient seen at home, plan of treatment for home health, pos 12, seen at the patient's residence, skilled nursing visit in the home, taxing effort to leave home, unable to leave home, visit in the home | attended by, caregiver present at home, change linens, home environment, home safety, light housekeeping, lives alone, assessed at home, meal preparation, residence, task list and time record | negative: telehealth, video visit, office visit |
| Not Available | specimen collected, specimen received, collection date, ordering provider, ordering physician, requisition, accession number, performing laboratory, reference range, this report is not a visit note, final report, signed by pathologist, radiologist signature, technologist, fasting status | lab director, clia, result status, specimen type, report date | negative: chief complaint, history of present illness, assessment and plan |

**Document-type priors** (applied before the lexicon)

> Page type is stronger evidence than any phrase for some documents. Applied before the lexicon; the lexicon then only has to settle the outpatient modality.

> An Operative Note from an ambulatory surgery centre is outpatient, and a Patient Communication page can be the only record of an evisit. Both are overridden by a strong lexicon hit, which is why the prior is a starting point and not a decision.

| L1 | Prior |
|---|---|
| Discharge Summary | Inpatient |
| Operative Note | Inpatient |
| Lab Report | Not Available |
| Order and Requisition | Not Available |
| Consent and Authorization | Not Available |
| Immunization | Not Available |
| Demographics | Not Available |
| Patient Communication | Not Available |

**Patient-type field**

* **label_pattern**: (?i)\b(patient\s*type(?:\s*/\s*class)?\|pt\.?\s*type\|patient\s*class\|encounter\s*type\|admit\s*type)\b
* **value_window_chars**: 160
* **values**: outpatient: Outpatient (F2F); ambulatory: Outpatient (F2F); same day: Outpatient (F2F); inpatient: Inpatient; observation: Inpatient
* **page_rule**: A page that carries the field takes its value when the page was suggested a different setting (Inpatient or an Outpatient class). Not Available pages (labs, consents) keep Not Available.
* **chart_rule**: When a Demographics page (face sheet, registration) in a chart carries the field and no page in that chart carries a different value, the chart suggestion is that value, and every page in the chart suggested Inpatient flips to Outpatient (F2F) when the field says outpatient or ambulatory.
* **comment**: Client approved 2026-10-24. An explicit patient-type field outranks phrases such as "admission date", which hospital headers print on outpatient procedure records too (Chart 15: outpatient colonoscopy, 20 pages had been suggested Inpatient).

* **_comment_weak_terms_removed**: Removed on first run because they fire on unrelated text: 'floor' matched 'pelvic floor laxity', 'unit' and 'units' match every lab result line, and a bare 'phone' appears in every patient banner. A weak term that fires everywhere is worse than no term, because it shifts every page by the same amount and only adds noise to the margin.
* **_comment_word_boundaries**: All matching is whole-phrase with word boundaries. On the first run substring matching put 150 outpatient pages into Inpatient, because 'icu' is inside 'difficulty' and 'diverticulosis'.
* **_comment_2026_10_24**: Prior key 'Lab Report/Result' renamed to 'Lab Report' to match taxonomy_v29 L1. With the old key the prior never fired on current data, and about 290 lab pages fell to 'no evidence'.
* **_comment_asc_2026_10_24**: Client direction 2026-10-24: ambulatory surgery centre wording is a strong Outpatient (F2F) hit, so it overrides the Operative Note prior (Inpatient).

### I. The pipeline's encounter canon (core-pipeline keyword-canon/encounter_canon.json)

Encounter setting per visit. Evidence is ranked, not added up: the highest non-empty tier decides alone. tier1 = the page's own matched page type that exists in one setting only, keyed on page_taxonomy.json sub-type names, else page type names. tier2 = text that names the setting. tier3 = hints that also occur in other settings; they only break a tie inside the deciding tier. context phrases are logged and never score. A negative removes the tier 2/3 finding it names (cancels) or every tier 2/3 finding of a setting (cancels_setting). A phrase is recorded once per visit however often it appears. Lower priority wins a tie. Reloads on change. 2026-10-20 (user): follow-up / return / established / new-patient / wellness visits are tier 2 Outpatient (F2F); more tele, inpatient and home phrases.

**Settings**

| Key | Stored as |
|---|---|
| home | label: Home; priority: 10 |
| outpatient_tele | label: Outpatient (Tele); priority: 20 |
| outpatient_f2f | label: Outpatient (F2F); priority: 30 |
| inpatient | label: Inpatient; priority: 40 |

**Confidence by tier**: tier1: 0.95; tier1_conflict: 0.7; tier2: 0.8; tier2_conflict: 0.6

**Default**: setting: outpatient_f2f; confidence: 0.3; note: 2026-10-20 (user): a visit with no setting evidence, or no usable date, is Outpatient (F2F) at this low confidence; decided_by 'default', and reason keeps why (no_setting_evidence / no_date / default_date). Empty setting = leave it unresolved.

**Applies to**: families: Progress Note, Discharge Summary; not_applicable: setting: not_applicable; label: Not Available; confidence: 1.0; note: 2026-10-23 (user): a page whose L1 family is not listed votes Not Applicable (lab, orders, forms...); listed families vote their evidence or their vote.family_defaults. Empty families = every page votes its evidence. 2026-10-25 (user): shown as Not Available.

**Tier 1: page types that decide the setting**

| Page type | Setting |
|---|---|
| Discharge Summary | inpatient |
| ICU Note | inpatient |
| Ventilator management record | inpatient |
| Operative Report | inpatient |
| Emergency Note | inpatient |
| Urgent care note | outpatient_f2f |
| HouseCalls visit summary | home |
| Telephone Encounter | outpatient_tele |
| Office Visit | outpatient_f2f |
| Operative Note | inpatient |
| Critical Care Record | inpatient |
| Surgery Report | inpatient |

**Tier2: phrases**

| Setting | Phrases |
|---|---|
| outpatient_tele | telehealth, tele-health, telemedicine, tele-medicine, tele visit, televisit, virtual visit, video visit, video encounter, telephone visit, phone visit, remote visit, audio only visit, audiovisual visit, telehealth visit, telemedicine visit, telephonic visit, telephone encounter, via telephone, via video, audio-video visit, audio and video, virtual care, e-visit, evisit |
| inpatient | hospital course, admission date, date of admission, admitted on, inpatient, in-patient, discharge summary, discharge note, discharge report, operative note, operative notes, operative report, op note, emergency department note, emergency note, ed note, er note, admission note, admit note, inpatient progress note, hospitalist progress note, date of discharge, discharge date, length of stay, observation status, icu progress note |
| home | skilled nurse visit, skilled nursing visit, home visit, home health visit, housecalls visit, house call visit, in-home visit, in home visit, in-home assessment, home based primary care, home-based primary care |
| outpatient_f2f | office visit, office visits, office visit note, office visit report, clinic visit, clinic note, face to face, face-to-face, f2f, place of service, appointment facility, reason for appointment, follow up visit, follow-up visit, followup visit, follow up appointment, follow-up appointment, follow up office visit, return visit, return office visit, established patient visit, new patient visit, established patient, new patient, annual wellness visit, wellness visit, annual physical, preventive visit, well child visit, routine visit, routine follow up, routine follow-up, in-person visit, in person visit, seen in clinic, seen in the office, seen in office, outpatient visit, outpatient clinic, clinic follow up, office follow up, pcp visit, primary care visit, urgent care visit, walk-in visit |

**Tier3: phrases**

| Setting | Phrases |
|---|---|
| outpatient_f2f | chief complaint, history of present illness, hpi:, subjective:, objective:, assessment and plan, assessment & plan, a/p:, examination:, soap note, soap notes, s.o.a.p, subjective objective assessment plan, cheddar, follow up, follow-up, reason for visit, reason for consult, outpatient, details:, drugs:, 6 month follow |
| inpatient | history and physical, history & physical, h&p, h and p, admission h&p, hospitalization, consultation, consult note, consultation note |

**Context phrases**: diagnostic imaging, radiology report, imaging report, x-ray report, ct scan report, mri report, ultrasound report

**Negatives** (phrases that cancel a match): phrase: follow up with your primary care; cancels: follow up, phrase: outpatient follow up; cancels: follow up, phrase: follow up in the office; cancels: follow up, phrase: discharged home; cancels_setting: home, phrase: discharge to home; cancels_setting: home, phrase: discharged to home; cancels_setting: home, phrase: scheduled for admission; cancels: admission date, date of admission, admitted on, phrase: pending admission; cancels: admission date, date of admission, admitted on, phrase: if admitted; cancels: admission date, date of admission, admitted on, phrase: telephone message; cancels_setting: outpatient_tele, phrase: phone message; cancels_setting: outpatient_tele, phrase: left a message; cancels_setting: outpatient_tele

**Vote**: note: 2026-10-25 (user): the encounter's FIRST PAGE decides when it has evidence of its own (tier 1 page type, tier 2 phrase, or the encounter model: decided_by first_page). Only when it cannot (Not Applicable, its family default, or nothing) do the pages vote -- only those with evidence of their own; the family defaults (Progress Note -> F2F) only when no page has any: the first page counts first_page_weight times; Not Applicable wins only when every vote is Not Applicable; otherwise the weighted majority of the settings wins (a tie goes to the first page's vote), confidence = the winner's share of the weight.; first_page_weight: 2; family_defaults: Progress Note: outpatient_f2f; Discharge Summary: inpatient

### J. The pipeline's continuity canon (keyword-canon/continuity_canon.json)

* **version**: 4

* **note**: Document splitting (docs/APPROACH.md §4, from Document_Splitting.md, 2026-10-20). Every gap between two consecutive pages gets a break score: a plain sum of points (1 weak, 2 moderate, 3 strong, 5 decisive), positive = break, negative = joined. Break After of a page and Break Before of the next are the same number. score > 0 breaks, < 0 joins, 0 joins and is flagged. A 5-point signal settles the gap unless another 5 opposes it (then a different date wins, medium confidence, flagged). Blank pages, single junk pages and duplicates are transparent; a run of two or more junk pages is its own segment. A final pass then merges documents across weak breaks and splits a document holding two confidently read dates. Reloads on change.

#### pagination

* **comment**: Printed 'Page N of M', read from the header and footer bands (or the key/value page number). Never splits a document: an undated page after 'page N of N' (the last page) starts a new one, and an undated page whose number follows the previous page's (N -> N+1, same M) is a confirmed continue page. Page 1 also confirms a document's first page. max_total is page_arbitration.json's max_document_pages.
* **patterns**: page\s*[:#]?\s*(\d{1,3})\s*(?:of\\|/)\s*(\d{1,3}), \bpg\s*[:#]?\s*(\d{1,3})\s*(?:of\\|/)\s*(\d{1,3})
* **bare_pattern**: (?<![\w/.])(\d{1,3})\s+of\s+(\d{1,3})(?![\w/.])
* **max_total**: 20

#### bands

* **comment**: Where the header and footer are. With line positions: a line whose top is above head_top is header, one whose bottom is below foot_bottom is footer (fractions of page height). Without: the first head_lines and last foot_lines lines of text, and a quarter of the lines each way for pagination. A line starting below foot_bottom is left out of where the page's text ends.
* **head_top**: 0.1
* **foot_bottom**: 0.92
* **head_lines**: 4
* **foot_lines**: 3
* **text_band_fraction**: 0.25

#### points

* **comment**: The points of every signal (docs/APPROACH.md §4). + pushes toward break, - toward joined. 'side' is where the evidence is read: ending (the earlier page alone), opening (the later page alone), pair (both). 'status': measured (the difference figure from the 170 labelled gaps) or assumed.
* **different_dos**: side: pair; points: 5; decisive: True; status: assumed; note: Points by how the date was found (dos_trust): label / admit-discharge 5 (decisive), edge keyword 3, keyless 2; the lower of the two dates' trust counts.
* **same_dos**: side: pair; points: -5; decisive: True; status: assumed; note: Against the open document's date, by the same trust tiers.
* **pagination_follows**: side: pair; points: -5; decisive: True; status: measured (defines the ground truth)
* **signed_in_tail**: side: ending; points: 3; status: measured, difference 21
* **signed_anywhere**: side: ending; points: 2; status: measured, difference 26
* **last_numbered_page**: side: ending; points: 3; status: assumed
* **has_title**: side: opening; points: 3; status: assumed: check first (section headings mistaken for titles)
* **visit_heading**: side: opening; points: 3; status: 2026-10-23 (user): a visit-opening heading (visit_headings) in the page's top band opens a new Progress Note: +3 toward a break, and with visit_headings.decisive a break whatever the score, unless the printed page N of M runs on.
* **page_type_change**: side: pair; points: 3; scaled: True; status: assumed
* **junk_run**: side: pair; points: 3; points_three_or_more: 5; scaled: True; status: assumed; note: At each end of a run of two or more junk pages, times the junk confidence; three or more is decisive when the scaled value is at least decisive_at.
* **says_continued**: side: ending; points: -3; status: assumed
* **sections_forward**: side: pair; points: -3; scaled: True; status: measured, difference 27, no false positives
* **same_visit_number**: side: pair; points: -3; status: measured, difference 8, no false positives
* **page_fill**: side: ending; points: 2; scaled: True; status: measured on the join side, difference 34; note: The curve in page_fill: -2 for a full page up to +2 in the ending band, 0 for a nearly empty page.
* **text_start**: side: opening; points: 2; scaled: True; status: unmeasured; note: The curve in text_start: -1 straight on .. +2 with air above. Recorded only until measured (apply false).
* **same_header_line**: side: pair; points: -2; status: measured, difference 31 (raw lines; normalised should beat it)
* **wording_char**: side: pair; points: -2; scaled: True; status: measured, difference 27 (furniture removed)
* **same_junk_kind**: side: pair; points: -2; status: assumed
* **blank_between**: side: pair; points: 1; status: deliberately weak
* **same_footer_line**: side: pair; points: -1; status: measured, difference 8
* **wording_word**: side: pair; points: -1; scaled: True; status: measured, difference 28, not independent of wording_char
* **cut_mid_sentence**: side: ending; points: -1; status: rejected standalone; a tie-breaker only when wording_char fires
* **starts_mid_thought**: side: opening; points: -1; status: rejected standalone; a tie-breaker only when wording_char fires
* **same_page_type**: side: pair; points: -1; status: deliberately weak; the full type path

#### dos_trust

* **comment**: How the DOS stage's date was found, from the extraction staging (staging/extraction.json dos rows, their tier): points for different_dos / same_dos. A date the staging does not explain counts as default.
* **service**: 5
* **admit**: 5
* **discharge**: 5
* **collected**: 3
* **received**: 3
* **weak**: 3
* **printed**: 3
* **keyless**: 2
* **default**: 3

#### junk_run

* **decisive_at**: 4.5

#### page_fill

* **comment**: Where the earlier page's body text ends (the bottom of its last line starting above the footer band), as a fraction of the page height, -> a value times page_fill.points: >= full_at -1 (full page), taper_at..full_at -1..0, band_high..taper_at 0..+1, band_low..band_high +1 (the ending band), empty_at..band_low +1..0, below empty_at 0 (too little to judge). Without line positions: -1 when the page has >= text_proxy_lines lines or >= text_proxy_chars characters, else nothing; the gap is flagged when that decided it.
* **full_at**: 0.92
* **taper_at**: 0.8
* **band_high**: 0.65
* **band_low**: 0.5
* **empty_at**: 0.35
* **text_proxy_lines**: 28
* **text_proxy_chars**: 1800

#### text_start

* **comment**: How far below the header furniture the later page's first body line starts, as a fraction of the page height -> a value times text_start.points: <= straight_at -0.5, straight_at..near_at -0.5..0, near_at..air_at 0..+1, >= air_at +1. apply false: computed and stored, not added to the score.
* **straight_at**: 0.03
* **near_at**: 0.12
* **air_at**: 0.25
* **apply**: False

#### semantic

* **comment**: Furniture (lines of >= furniture_min_chars, normalised, on both pages) is removed first; then the earlier page's last two body lines by position, extended back to tail_nth_word words, against the later page's first two, forward. Character 3-5 grams and word 1-2 grams, TF-IDF fitted on every block of the chart; normalised by norm_divisor and clipped to 1.
* **furniture_min_chars**: 12
* **tail_nth_word**: 20
* **char**: analyzer: char_wb; ngram: 3, 5; min_df: 2; norm_divisor: 0.25
* **word**: analyzer: word; ngram: 1, 2; min_df: 2; norm_divisor: 0.15

#### section_order

* **comment**: A clinical note's conventional section order; a page whose sections move forward from the previous page's continues it. Each step lists the section_header_canon.json names that count as it.
* **steps**: chief_complaint, Chief Complaint, CC, Reason for Visit, Reason for Consult, Reason for Admission, history_of_present_illness, History of Present Illness, HPI, review_of_systems, Review of Systems, ROS, past_medical_history, Past Medical History, PMH, Past Surgical History, PSH, social_history, Social History, family_history, Family History, medications, Medications, Current Medications, Medication List, allergies, Allergies, Allergy List, vitals, Vital Signs, Vitals, physical_exam, Physical Examination, Physical Exam, lab_results, Lab Results, Laboratory, imaging_results, Imaging, Radiology, problem_list, Problem List, Active Problems, visit_diagnosis, Assessment, Diagnosis, Diagnoses, Impression, Clinical Diagnoses, Principal Diagnosis, plan, Plan, Assessment and Plan, A/P, Plan of Care, Treatment Plan, patient_instructions, Discharge Instructions, After Visit Summary, follow_up, Follow Up
* **opens_note**: chief_complaint, history_of_present_illness

#### visit_headings

* **comment**: 2026-10-23 (user): headings that open a visit. A page with one at the start of a line in its top band (top_fraction of the page height; the first 8 lines without positions), or as a section heading there, scores visit_heading toward a break. With decisive true (2026-10-23, user) such a page always starts a new document, unless the printed page N of M runs on from the page before.
* **top_fraction**: 0.33
* **terms**: reason for visit, reason for appointment, visit information, visit info, chief complaint, cc, hpi, history of present illness
* **decisive**: True

#### demographics_break

* **comment**: 2026-10-23 (user): a Demographics page never shares a document with another kind of page (a Progress Note, say). Every gap between a page of these types and a page of others is a break, whatever the score, and the final pass does not merge it back; consecutive Demographics pages stay together. Standing alone, the document takes default_final_dos (2222-02-02). Names are page names (page taxonomy L1 / L2 / L3 or canon page types) matched without case against every name of the page.
* **page_types**: Demographics, Patient Demographics, Patient Information, Patient Registration

#### default_final_dos

* **comment**: A stand-alone document of these pages (every page carrying a name listed here) is not a visit: its Final DOS is DOS_DEFAULT_DATE (dos_canon.json, 2222-02-02) on every page, whatever dates its pages carry. One of these pages inside a visit's document (a progress note with a problem-list page) takes the visit's DOS. Each page's Extracted DOS is untouched. Names are canon page types (page_canon.json) or L1 / L2 / L3 of page_taxonomy.json — matched without case against every name of the page (axes.names_of).
* **page_types**: Patient Demographics, Patient Information, Patient Registration, Growth chart, Problem List, Immunizations, Immunization list, Demographics, Immunization, Immunization Record

#### decision

* **comment**: score > 0 break, < 0 joined, 0 joined and flagged. Confidence level from \|score\|: >= high_at high, >= medium_at medium, else low. P(break) = 1 / (1 + exp(-(score - b) / a)), confidence = \|2P - 1\|; x section_disagreement_factor when the three sub-scores point opposite ways. a and b are to be fitted on the labelled gaps; until then a = 2, b = 0.
* **high_at**: 5
* **medium_at**: 2
* **a**: 2.0
* **b**: 0.0
* **section_disagreement_factor**: 0.6
* **review_below**: 0.25

#### conflict_rules

* **comment**: Pattern A (ending >= at, pair <= -at: a finished document followed by the same visit -> break) and pattern B (opening >= at, pair <= -at: a section heading taken for a title -> join). Either drops the confidence one level and flags the gap. apply false: the patterns are recorded, not allowed to change an answer, until counted on a run (docs §7.6).
* **at**: 4
* **apply**: False

#### final_pass

* **comment**: After every document exists. A merge undoes only a weak break (confidence low or score <= weak_score), never a decisive one; up to max_passes passes. One split rule: a document holding two or more different dates read from a label (trust 5) splits at each date change. Last, progress_note_runs_on: a document opening on a progress_note_families page (L1) absorbs the documents after it until a new date read from a label (trust 5), a signature on its last page, a decisive break or a junk group.
* **weak_score**: 2
* **max_passes**: 3
* **rules**: same_date: True; orphan_page: True; same_type_untitled: True; continued_across: True; junk_rejoin: True; split_two_dates: True; progress_note_runs_on: True
* **progress_note_families**: Progress Note

#### review_triggers

* **comment**: Flag a gap or page for review: low confidence, two decisive signals disagreeing, a section disagreement (conflict pattern), a gap the text proxy for page fill decided, and every page of a document longer than long_run_without_pagination with no printed page number.
* **long_run_without_pagination**: 25

#### measurement

* **comment**: Provenance for the weights (docs/APPROACH.md §4.5). Measured with standalone reimplementations of the extractors, not the pipeline's own outputs: re-measure against real DOS, signature flags, line polygons and page-type confidences before trusting the exact weights. The ranking of the top signals should survive; the weights will move.
* **n_pairs**: 985
* **n_labelled**: 170
* **n_continue**: 129
* **n_new**: 41
* **n_charts**: 15
* **ground_truth**: explicit pagination on both pages
* **cv_balanced_accuracy**: 0.79
* **cv_std**: 0.186
* **cv_split**: GroupKFold by chart_id
* **fitted**: 2026-10-18
* **known_bias**: documents printing pagination are longer and more template-driven; furniture signals may be overstated and prose signals understated

#### visual_join

* **comment**: 2026-10-23 (user): a Visual page (ocr_quality_results.document_type 'visual' with fewer than blank_junk visual.max_words words) that is not junk always joins the page before it, whatever the score.
* **enabled**: True

<!-- canon:end -->
