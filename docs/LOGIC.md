# Logic — how each decision is made, and what it writes

The reasoning inside each stage, and the exact rows it produces.
For the order things run in, see [FLOW.md](FLOW.md).

Every algorithm here is ported from the reference implementations under
the V1 prototypes. Where the port differs from them, it says so and why.

---

## Contents

1. [Preliminary OCR](#1-preliminary-ocr)
2. [Rotation and handwriting](#2-rotation-and-handwriting)
3. [Blank / junk / duplicate](#3-blank--junk--duplicate)
4. [Final OCR](#4-final-ocr)
5. [Member verification](#5-member-verification) ← the accept/reject decision
6. [Date of service](#6-date-of-service)
7. [Page type](#7-page-type)
   - [Document continuity](#7b-document-continuity) ← documents; Final page type
8. [Encounter type](#8-encounter-type)
9. [Chart status](#9-chart-status)

---

## 1. Preliminary OCR

**Source:** V1 `ts_ocr.py` · **Stage:** `stages/lib/ocr/stage_prelim.py`

> Reads `corrected-pages/<n>.jpg` when stage 1 wrote one, else `pages/<n>.jpg`.
> A 270°-rotated page OCRs at ~0.01 text similarity to the same page upright;
> corrected it is ~1.0. See [SCALING.md](SCALING.md) for worker pool shape.

Tesseract over every page. Deliberately cheap and deliberately first: its only
job is to give the blank/junk classifier something to read, so pages can be
ruled out before the expensive OCR runs.

```mermaid
flowchart LR
  IMG["pages/N.jpg"] --> T["pytesseract.image_to_string"]
  T --> DB[("ocr_results<br/>ocr_type='tesseract'")]
  T --> TXT[/"ocr/&lt;chart&gt;_prelim.txt"/]
```

**Writes**

| Target | Columns |
|---|---|
| `ocr_results` | `page_id`, `ocr_type='tesseract'`, `raw_text`, `char_count`, `text_sha256` |
| `page_stage_status` | `stage_name='ocr_prelim'`, `pass_no=1`, `status` |
| disk | `ocr/<chart>_prelim.txt`, blocks delimited `===== <page_name> =====` |

`char_count` and `text_sha256` exist so later stages can test for "is there any
text" and "has this changed" without pulling the `TEXT` column.

The combined `.txt` is rebuilt from the database on every run, so a resumed run
still produces a file covering all pages — not just the ones it touched.

---

## 2. Rotation and handwriting

**Source:** `stages/lib/image_preprocess/rotation.py`, `hw_printed.py` · **Stage:** `stages/lib/image_preprocess/stage.py`

Two independent measurements per page.

```mermaid
flowchart TD
  IMG["pages/N.jpg"] --> R["OSD turn, then tilt on that page"]
  IMG --> H["classify_image_type()<br/>models/hw/*.pth or .pkl"]
  R --> RES["orientation · mirrored · tilt"]
  H --> HRES["printed | handwritten + confidence"]
  RES --> Q[("ocr_quality_results")]
  HRES --> Q
  HRES -.->|"drives skip rules"| BJ["blank/junk pass 1"]
  HRES -.-> F["final OCR eligibility"]
```

The handwriting label is the single most consequential output of this stage: it
decides whether a page is judged in blank/junk pass 1 or held back to pass 2.

Both the classifier and the detector are built **once per process**. The v6
implementation rebuilt them inside the per-page function, unpickling the model
for every page in the chart.

`orientation_angle` (database, `rotation_deg` / `orientation` in the CSV,
review-ui) is the **text's orientation as scanned**: how far the text sits
rotated counter-clockwise from upright, which is also the clockwise turn
applied to straighten it. Text running bottom-to-top is stored as 90 and
turned 90° clockwise. This matches how client ground truth records rotation.

Coarse orientation starts from Tesseract OSD. No correction — turn, mirror
or tilt — is kept unless Tesseract reads the page at least as well with it as
without it. A read is scored in English dictionary words, and it *reads* when
it has at least 8 tokens of three or more letters and 40% of them are English.
(The junk module's gibberish check is not used: upside-down and mirrored reads
keep their vowels, and on four real charts it passed every one of them.)

Reads use Tesseract `--psm 4` on a copy of the page shrunk to 1600 px. The
default layout analysis turns top-to-bottom text back on its own, so a page
turned 90° read as well as the upright page and the two could not be told
apart. Under PSM 4 only the upright page reads.

1. **Turn.** OSD's turn stands when the page reads well at it (20+ English
   words) — one read, the common case. Otherwise all four quarter-turns are
   read and the one with the most English words is kept, if it reads. So a
   turn that comes out gibberish goes back to 0° when the page as it arrived
   reads better, and a page that is gibberish at 0° is turned when a
   quarter-turn reads. Ties keep OSD's turn, then 0°.
2. **Mirror**, decided with the turn. Only when no unflipped turn reads well
   are the four flipped pages read too; a flip is kept only when it reads more
   English than every unflipped turn. This also catches a page that is upside
   down *and* mirrored (turn 180° + flip).
3. Nothing reads at all — handwriting, a poor scan: OSD's turn stands,
   unmirrored. A page tagged `blank` is not read.
4. **Tilt** is measured last, on the page as it will be saved — turned and,
   if mirrored, flipped — because a flip reverses the direction of a lean.
   A tilt that would be applied is undone (`tilt_angle` 0) when the
   straightened page reads more than 10% fewer English words than the
   unstraightened one; a word or two either way is Tesseract noise.

`method` is `readability` when reading changed OSD's turn or added a flip.
The geometric detector's own coarse guess is not used.

Measured on 106 text pages from four charts, each turned 90/180/270,
mirrored, and flipped vertically: every case recovered, and no upright page
was moved.

Tilt is applied only up to `MAX_TILT_TO_APPLY` degrees either way (default
5). A larger reading is still stored in `tilt_angle` but the page is not
straightened by it, and on its own it does not produce a corrected copy.

**Writes**

| Target | Columns |
|---|---|
| `ocr_quality_results` | `printed_or_handwritten`, `hw_method`, `hw_confidence`, `document_type`, `handwritten_probability`, `is_visible`, `handwritten_area_pct`, `review_required`, `orientation_angle`, `tilt_angle`, `mirrored`, `rotation_applied`, plus the placeholder `quality_tag` / `quality_score` |
| disk | `imaging/<chart>_rotation.csv`, `imaging/<chart>_hw_printed.csv` |

Both CSVs are rebuilt from the database, so a resumed run cannot leave a
half-written file.

Failure is non-fatal by design: an unreadable image falls back to
`printed / 0.5 / "fallback"` rather than stopping the chart. The fallback is
recorded in `hw_method` so it is distinguishable from a real prediction.

### `quality_tag` / `quality_score` are measured

Stage 2 runs `quality_analyzer` (engineering submetrics, 0–10). The stored
`quality_score` is in [0, 1] (`score / 10`). Tags:

| `quality_score` | `quality_tag` |
|---|---|
| ≥ 0.70 | `high` |
| ≥ 0.40 | `medium` |
| else | `low` |

**Post-process (teammate `image_preprocessing` rule):** if the page is
`handwritten` and the tag would be `high`, store **`medium`** instead. The
numeric `quality_score` is unchanged — only the discrete label moves. That
keeps Final2's `high_quality_printed` skip honest (handwritten pages never
qualified anyway) while the UI does not show a handwritten page as “high”.

Submetrics and warnings live in `quality_detail` JSONB. Handwriting is separate
(`printed_or_handwritten` / `hw_method` / `hw_confidence`) — ConvNeXt when the
`.pth` is present, otherwise the RF pickle. In v7 `quality_tag` wrongly held the
classifier method; that value now lives in `hw_method`.

**Page-tag model.** A checkpoint saved with `task: "page_tags"` under the same
file name is loaded instead of the two-class model (`hw_method =
convnext_page_tags`). It gives a page type (Printed, Handwritten, Form, Visual,
Blank, Uncertain), visibility (Visible / Not visible) and the share of the page
that is handwriting. A type under the checkpoint's minimum confidence (0.50 if
absent) becomes Uncertain, except Blank. The ink upgrade does not run.
`printed_or_handwritten` keeps its four values:

| Page type | `printed_or_handwritten` |
|---|---|
| Printed, Visual | `printed` |
| Handwritten | `handwritten` |
| Form | `mixed` if ≥ 5% handwriting, else `printed` |
| Blank, Uncertain | `uncertain` |

They are stored in `ocr_quality_results.document_type`,
`handwritten_probability`, `is_visible` and `handwritten_area_pct`, and in the
hw CSV under the same names. `review_required` (quality CSV too) is true when
quality is `low` (not on a blank page), the type is uncertain, or the page is
not visible. With the two-class model the type is printed / handwritten /
uncertain / blank, and `is_visible` and `handwritten_area_pct` are NULL.

**Every page decision reads `document_type`** (`db.page_type()`; a row written
before the column existed falls back to `printed_or_handwritten`, `mixed` read
as form). Printed and visual pages are printed text: they run blank/junk
pass 1, and at `quality_tag=high` skip Final OCR 2. Handwritten, form, blank
and uncertain pages skip pass 1 and are judged on final OCR. Only a
handwritten page has High quality capped to Medium, is carried through
downstream stages before its pass-2 verdict, and keeps OSD's orientation
(form does too). `printed_or_handwritten` is still written but nothing decides
on it.

review-ui shows Type (Printed/HW), Handwritten % (area), Visibility, Quality,
Orientation Angle (Page), Tilt (Text) and Mirrored (Text) on the page details
and the summary table. P(handwritten) and `review_required` are in the
downloads only.

---

## 3. Blank / junk / duplicate

**Source:** `advantmed-imaging-ui/02-imaging-pipeline/junk-classification/` → `stages/lib/blank_junk/` · **Stage:** `stages/lib/blank_junk/stage.py`

### Why two passes

Tesseract reads handwriting badly, and low-quality scans are unreliable on
prelim text. So handwritten / form / blank / uncertain types **and** `quality_tag=low`
pages skip pass 1 and are judged in pass 2 on final OCR (final2 if present,
else final1).

```mermaid
flowchart TD
  subgraph P1["Pass 1 — prelim (Tesseract) text"]
    A{"HW / form / blank / uncertain<br/>or quality=low?"} -- yes --> SKIP["skipped"]
    A -- no --> CLS1["method.decide() — pass 1"]
  end

  CLS1 --> OUT1{"verdict"}
  OUT1 -- "blank/junk/duplicate" --> DROP["No final OCR.<br/>Page is finished."]
  OUT1 -- "main" --> F["final OCR 1 (+ 2 unless high+printed)"]
  SKIP --> F

  subgraph P2["Pass 2 — final2 else final1"]
    F --> CLS2["method.decide() — pass 2"]
    CLS2 --> OUT2["verdict"]
  end

  OUT2 --> FINAL["mark_blank_junk_final()<br/>highest pass per page wins"]
  DROP --> FINAL
  FINAL --> V[("v_page_blank_junk_final<br/>exactly one row per page")]
```

### The decision (`blank_junk/method.py`)

Full method, with reasoning and worked examples: [`BLANK_JUNK_PROPOSAL.md`](BLANK_JUNK_PROPOSAL.md).
Words, weights, thresholds and geometry: `keyword-canon/blank_junk_context_canon.json`
(reloads on change).

**Keyword canon.** Three contexts — clinical, junk (one group per subtype),
demographic — plus a neutral list that never scores (fax, phone, insurance…).
Group score = weight × **unique** hits, capped per group; context score = sum of
its groups. C = clinical ≥ 4, C⁺ ≥ 8, J = junk ≥ 4, J⁺ ≥ 7, D = demographic ≥ 3.
In pass 2 a junk word in the header (top 12%) or footer (bottom 8%) counts only
when the body has a junk word too.

**Rules, first that fires decides:**

| # | Rule | Verdict | Pass |
|---|---|---|---|
| R1 | No ink (HW stage `blank_page` / `faint_marks_only`); or, not Visual, 0 characters or the word "blank" in < 30 characters | `blank` | 1, 2 |
| R1 | Not Visual, < 15 characters otherwise | junk / Cover Page when a Cover Page term (`cover_page`, `cover_stamps`) is there, else junk / Others | 1, 2 |
| R2 | "Intentionally left blank" on ≤ 25 words | junk / Others | 1, 2 |
| R3 | Provider Signature = Yes (KV extraction), or a canon signature phrase — unless DistilBERT and the keywords both say JUNK (JJ): then the vote (R9) decides, `votes.r3_skipped` | Main | 2 |
| R4 | DistilBERT p(junk) > 0.85 — **not** when C or D (then the vote decides) | junk / DistilBERT's subtype | 2 |
| R5 | Content ends in the top 15% of the page (running header / footer lines ignored) and no ink from 17% to 94% of the height (ink = at least `body_ink_max_fraction` of that band; `votes.short_page` records `ink`, `ink_fraction`, `ink_limit`) — whatever the page says | junk / Others | 2 |
| R6 | record_request + roi_vendors ≥ 7 and not C⁺ | junk / Record Request | 1, 2 |
| R7 | C⁺ | Main | 1, 2 |
| R8 | `document_type = 'visual'` → Main, unless invoice / retrieval wording ≥ 7 | Main or junk | 1 (pass 2 skips these pages) |
| R9 | The vote, below | | 1, 2 |

**The vote (R9).** Two voters: DistilBERT (raw 8-label softmax) and the keyword
canon each cast JUNK / KEEP / ABSTAIN, strong or weak. Ballots are written
DistilBERT first (`JA` = DistilBERT junk, keywords abstain).

In pass 2 the keywords read the final OCR, plus the **junk** terms only the
Tesseract text had (a vendor name in a logo or banner the final OCR dropped, such
as "Optum"), on printed, non-low-quality pages. Those are listed in
`votes.kw.tesseract_only`. Clinical and demographic scores read the final OCR
alone.


| Ballots | Verdict |
|---|---|
| J J | Junk, whatever the strength (pass 1: only when both are strong) |
| J A | Junk (DistilBERT alone) |
| A J | Junk when the keyword junk score ≥ 7 (`voters.keyword.junk_alone_min`), else Main |
| J K, K J | Main |
| K K, K A, A K, A A | Main |

Then the vetoes turn a Junk into Main: clinical (C), demographic (D without
retrieval / invoice wording), handwritten area > 30%. Pass 1 only takes J J with
both strong; everything else stays Main and is judged again in pass 2 on final
OCR. A model that is not loaded abstains (`/health` → `engine: keywords`).
TF-IDF is no longer a voter.

Subtype: the canon's best junk group, else DistilBERT's label, else Others.

**Confidence** is the evidence for the verdict, not a fixed number. Signals:
DistilBERT's probability for the verdict (when it scored the page, ≥ 8 words),
the keywords (junk score / 10, or clinical-or-demographic / 10; when they point
the other way, 1 − that), and for R5–R8 the rule's own value (R5 0.95, R6 0.97,
R7 0.95, R8 0.90). Signals that agree combine as 1 − ∏(1 − p); a disagreement
is averaged; no signal is 0.50. R1–R3 are observations and stay fixed (1.00 /
0.99 / 0.95). A contested call — a veto, a split vote — therefore comes out low
on its own; filter on low confidence to review. The signals are staged as
`confidence_from`. Patient-education
handouts score as clinical (`patient_education` canon group), so DistilBERT
cannot force them to junk. Every ballot and the deciding rule are staged in
`<chart>/staging/blank_junk.json` (beside `extraction.json`), not the database.

`junk_subtype` is constrained by a `CHECK` to the six labels the review UI
renders. Anything else maps to `Others` rather than becoming an unrenderable
string. The previous post-model rules (`postprocess.py`: repeated fax / record
request, vendor ×3, two-heading keep, 20-word short page, Visual-neighbour keep)
no longer run; the ladder above replaces them.

### Duplicate detection

Duplicates are checked **after** the model's verdict, and only between pages the
model kept as Main — a blank or junk page is never compared. A Main page is a
duplicate when its normalized OCR text is **≥ 98% similar**
(`difflib.SequenceMatcher`, run only when the two lengths are within 10% of each
other) to a Main neighbor **within ±5 pages** in chart order, or when its text is
**wholly contained** in that neighbor's.

| Rule | Behaviour |
|---|---|
| Window | Compare only pages 5 before / 5 after (page order); SequenceMatcher only when the lengths are within 10% |
| Threshold | Similarity **≥ 0.98** on whitespace-stripped lowercase text (UI: 100% → Yes, [98%, 100%) → May Be; May Be display confidence = `1 + (sim − 1) × 10`, e.g. 98%→80% / 99%→90%) |
| Containment | The shorter page's normalized text appears whole inside the longer one → duplicate, confidence 1.0 |
| Who wins | Higher normalized character count stays **main**; on a tie, the **earlier** page |
| Excluded | Blank / junk pages and texts shorter than 300 normalized characters (~50 words) |
| Cross-pass | Prior-pass `main` pages are neighbors in pass 2 (so HW can match a printed original) |

`duplicate_of_page_id` records the kept original. A demoted prior-main page gets
an updated row on the current pass.

### One final verdict

Each pass writes its own row (`UNIQUE (page_id, pass_no)`). Then
`mark_blank_junk_final()` stamps the highest pass per page, and a partial unique
index (`WHERE is_final`) makes "two finals for one page" unrepresentable.
Downstream stages read `v_page_blank_junk_final` and never re-derive precedence.

**Writes**

| Target | Columns |
|---|---|
| `blank_junk_classification` | `pass_no`, `blank_junk_flag`, `junk_subtype`, `duplicate_of_page_id`, `ocr_source`, `confidence`, `reason`, `is_final` |
| disk | `imaging/<chart>_junk.csv` — **fully rewritten from the database** after each pass |
| disk | `staging/blank_junk.json` — every ballot and deciding rule per page and pass |

> v6 truncated the CSV in pass 1 and appended in pass 2, so re-running pass 2
> alone duplicated every row. The full rewrite makes the file converge.

---

## 4. Final OCR

**Stages:** `stages/lib/ocr/stage_final1.py` (Docling layout + RapidOCR; falls back to RapidOCR-onnx only on a page timeout or a crash), `stages/lib/ocr/stage_final2.py` (Azure Document Intelligence `prebuilt-read`)

Both run on pages not ruled out by pass 1, **plus every handwritten /
low-quality page**. Azure final2 additionally **skips high-quality printed**
pages (`skip_reason=high_quality_printed`) — final1 is enough for them.

```mermaid
flowchart LR
  E{"eligible?"} -->|"HW / low quality"| YES["run"]
  E -->|"pass 1 said main"| YES
  E -->|"pass 1 said blank/junk/dup"| NO["skipped<br/>reason: blank_junk_pass1"]
  YES --> F1[("ocr_results<br/>'docling'")]
  YES --> HQ{"high + printed?"}
  HQ -->|no| F2[("ocr_results<br/>'azuredocintel'")]
  HQ -->|yes| SKIP2["skipped Azure"]
```

`ocr_type='docling'` is the slot the review UI labels "Final (OSS)". When Docling
and the RapidOCR `.pth` models under `RAPID_MODELS_DIR` are ready, final1 uses
Docling's layout / TableFormer / reading-order pipeline (V1 `os_ocr.py`).
Otherwise it falls back to **RapidOCR-onnx only** (no Tesseract — prelim already
did that). Either way the on-disk artifact is `ocr/<chart>_final1.json`. See
[`docs/API.md` § Downloading models](API.md#2-downloading-models).

**final1 and final2 store JSON**, not bare text: `{pageNumber, fileName, content, …}`.
final1 also carries `markdown` and (when Docling ran) the full `document`
layout dump. Every consumer unwraps `content` — the review UI's Postgres and
Local adapters do it too.

Both engines/clients are built once per process. Stage 5 is the one billed per
page, which is why `pages_needing_stage` exists: a resumed run sends only the
pages that never completed.

When Azure DI is not configured the stage logs a warning and produces no text.
Member/DOS then use final1 (and prelim only for printed non-low-quality pages).

---

## 5. Member verification

**Source:** V1 `Member_Verification/` (~2,800 lines) → `core-pipeline/stages/lib/member/`
**Stage:** `stages/lib/member/stage.py`

This stage produces the accept/reject decision. It is a faithful port — function
names, call order and thresholds match the reference so a run is diffable
against it page for page.

### The central idea: expected-member driven

The pipeline does **not** extract a name from the page and then compare it. It
takes the manifest's first / middle / last / DOB / MemberID and **searches the
page for those specific values**. That is why `manifest_member_list` stores name
parts separately — `classify_two_word_name` matches first and last
independently, and a single joined string cannot drive it.

### Per-page algorithm

```mermaid
flowchart TD
  START["Page text + expected member"] --> NAME

  subgraph NAME["Name"]
    N1["find_two_word_name / find_three_word_name<br/>over the whole page"]
    N1 --> N2{"found?"}
    N2 -- yes --> NR["source = rule based"]
    N2 -- no --> N3["NER: build the sentence around each<br/>patient-name key, read it"]
    N3 --> NN["source = ner"]
  end

  NAME --> DOB
  subgraph DOB["DOB"]
    D1["extract_dob — the three parts<br/>adjacent, any supported order"]
    D1 --> D2{"found?"} -- no --> D3["NER on date-of-birth key sentences"]
  end

  DOB --> MID
  subgraph MID["MemberID"]
    M1["extract_member_id — exact value,<br/>not a substring of a longer token"]
    M1 --> M2{"found?"} -- no --> M3["NER on member-id key sentences"]
  end

  MID --> VER["verify_page()"]
  VER --> COMB{"combine_evidences"}
  COMB --> PS["page_status"]
```

Rules run first over the whole page; **NER is reached only for a field the rules
missed**. A page the rules already matched costs no model time.

### The verification rule (`rules/base_rules.py`)

```
name not matched                        → Reject
name matched, initial only              → Accept iff DOB **and** MemberID found
name matched, both parts full           → Accept iff DOB **or** MemberID found
```

A name on its own is never enough. An initial-only match ("J Anderson") needs
both corroborators, because a single initial plus a surname is weak evidence.

| Name evidence | DOB | MemberID | Verdict |
|---|---|---|---|
| `Justin Anderson` | ✓ | — | **Accept** |
| `Justin Anderson` | — | ✓ | **Accept** |
| `Justin Anderson` | — | — | Reject |
| `J Anderson` | ✓ | ✓ | **Accept** |
| `J Anderson` | ✓ | — | Reject |
| `Marcus Anderson` | ✓ | ✓ | Reject — a *different* member |

That last row is the one that matters: a matching surname next to a different
given name is classified `ONE_FULL_WRONG`, not "partial match".

### The three page buckets

```mermaid
flowchart TD
  V{"verify_page()"} -- Accept --> VER["**Verified**"]
  V -- Reject --> W{"wrong_member_on_page():<br/>did NER read names off this page,<br/>and does none of them match?"}
  W -- yes --> WM["**Wrong_Member**"]
  W -- "no names read" --> NV["**Not_Verified**"]
  W -- "a name matched" --> NV

  style WM fill:#fde8e8,stroke:#c74a4a
```

A page with nothing detected is **not** evidence of another member — it is
merely unverified. Only a page that positively names someone else counts.

### The document decision (`rules/what_if_rules.py`)

```
reject_threshold(total_pages) = max(1, min(5, ceil(total_pages × 0.10)))
document = Reject  if  wrong_member_pages ≥ reject_threshold
           Accept  otherwise
```

| Chart size | Rejects at |
|---|---|
| 3 pages | 1 wrong page |
| 12 pages | 2 |
| 40 pages | 4 |
| 60+ pages | 5 (the cap) |

The threshold is computed on the chart's **whole** page count, not on the subset
that reached this stage — blank/junk pages are excluded from checking but still
count toward the document's size. Pinned by
`test_threshold_uses_the_whole_document_not_the_subset`.

### ⚠️ The NER layer and what turning it off costs

`wrong_member_on_page()` decides from the names the NER layer read out of the
page's patient-name sentences. **With `MEMBER_NER_ENABLED=false`:**

- no page can ever be classified `wrong_member`;
- therefore `wrong_member_pages` is always 0;
- therefore **no document can ever be Rejected**;
- and a field the rules missed is never recovered, so recall is lower.

This is not a silent degradation. Every row carries `ner_enabled`, and the
summary's `decision_reason` is suffixed `|ner_disabled`. `GET /health` reports
it too.

#### Turning it on

The NER **code** is fully ported — `model.py`, `catalog.py`, `keys.py`,
`name.py`, `dob.py`, `member_id.py` and the model downloader. Two things are not
vendored, because neither belongs in a git repository:

1. **The runtime** — `gliner`, `torch`, `transformers` (~2.5 GB installed), in
   `requirements-models.txt`.
2. **The checkpoints** — ~2 GB of weights.

```bash
# 1. Runtime
pip install -r requirements-models.txt

# 2. Checkpoints (into MEMBER_NER_MODELS_PATH)
python -m stages.lib.member.extractors.ner_based.model_downloader
python -m stages.lib.member.extractors.ner_based.model_downloader --check

# 3. Enable
export MEMBER_NER_ENABLED=true
curl localhost:8001/health | jq .member_ner     # expect ready: true
```

The downloader verifies each checkpoint twice: every file present and non-empty,
then an actual load plus one prediction — a snapshot can complete with all files
in place and still not load, which otherwise only shows up later as a run that
detects nothing.

`GET /health` and the member stage's first log line report which of the three
preconditions is unmet, because they need different fixes:

| `reason` | Fix |
|---|---|
| `gliner not installed (…)` | `pip install -r requirements-models.txt` |
| `checkpoints missing: …` | run the downloader |
| `MEMBER_NER_ENABLED=false` | set the flag |

With the layer on, the reference's fail-loud contract is unchanged: a model that
will not load raises rather than quietly returning no hits.

### Writes

| Target | Columns |
|---|---|
| `member_extraction_results` (one per page) | `extracted_name/dob/member_id`, `detection_source_name/dob/member_id` (`rule_based`\|`ner`\|`''`), `ner_key_source_*` (which key sentence), `page_status` (`verified`\|`wrong_member`\|`not_verified`), `page_verified`, `confidence`, `provided_*`, `matched_member_list_id` |
| `member_verification_summary` (one per chart) | `final_status`, `document_decision` (`accept`\|`reject`), `name_mode`, `pages_checked`, `pages_matched`, `wrong_member_pages`, `reject_threshold`, `decision_reason` |
| disk | `_member_extraction.csv`, `_member_verification.csv`, `_member_v1_compare.csv` |

`_member_v1_compare.csv` is written in the **reference's own column order**
(`RecordId, Total_Page_Count, Page_No, Detection_Source_Name, …`) so a pipeline
run can be diffed directly against a V1 run.

`final_status` triages for the reviewer; `document_decision` is the business
outcome:

| Condition | `final_status` | `document_decision` |
|---|---|---|
| wrong-member pages ≥ threshold | `failed` | `reject` |
| ≥ 1 page verified | `verified` | `accept` |
| every page blank/junk/duplicate | `skipped` | *(null)* — chart still `completed` |
| no manifest row for the record | `needs_review` | *(null)* |
| manifest has no usable name | `needs_review` | `accept` |
| pages checked, none verified | `needs_review` | `accept` |

`confidence` is derived, not a model score: 0.5 for a verified page plus ~1/6
per field found, rules weighted above NER. The reference carried no numeric
confidence — it reported detection source per field, which is what the UI shows.

---

## 6. Date of service

**Engine:** `stages/lib/dos/dos_logic.py` · **Stage:** `stages/lib/dos/stage.py` ·
**Profile:** `keyword-canon/dos_canon.json` (labels, settings — reloads on change) ·
**Full detail:** [`DOS_LOGIC.md`](DOS_LOGIC.md)

Per page, two states:

| # | State | Confidence | `match_type` |
|---|---|---|---|
| 1 | **Key/value.** Among the dates the extractor accepted, by the tier it gave them, the first group in the page kind's order (`kv_priority`; kind = the classification L1): **discharge** (Discharge Summary) = Admit + Discharge pair → service → weak → dt/dd → header/footer; **lab** (Lab Report/Result) = collected → received → Report Date → service → weak → dt/dd → header/footer → Admit + Discharge pair; **other** = service → weak → dt/dd → header/footer → Admit + Discharge pair. Within a group the highest score. Only weak keys have a floor: score > `weak_min_score` (0.3). Print-key dates never. A range keeps its end | the date's score (model, else rules) | `kv` / `admit_discharge_pair` |
| 2 | **Edge keyword**, only when 1 has no date. A DOS label starting within the first or last `edge_words` (20) words of the page, with a date within `label_reach_chars` (40) after it and only filler words (`label_gap_words`: at, on, of, from, is, time, date, dt) between. An encounter label wins; otherwise Admission Date is **From** and Discharge Date is **To**; a discharge date alone is both. A written range after an encounter label keeps both ends. A date right after `D:`, `T:`, `DD:`, `DT:`, `TD:`, `TT:`, Dictated or Transcribed is never taken. Read on the final OCR text; on a page with no Final2 text, then on the Tesseract text (not for handwritten / low-quality pages) | 0.50 | `edge_keyword` / `edge_keyword_tesseract` / `admit_discharge_pair` |
| — | Nothing: the page has no DOS | 0 | `no_date_found` |

**Discharge pages** (L1 Discharge Summary) take the Admit date as From and the
Discharge date (on or after it) as To before anything else; one alone is both.
Key/value dates first, an edge label fills a missing side. `source` `admit_discharge`.

**Dictation dates.** On an "other" page with no key/value or edge date: the dictated date (`D:`, `DD:`, Dictated), else the transcribed one, at 0.30 (`dictation`).

**Print dates are never a DOS.** A Printed / Print Date label keeps its date
from other keys but the pair is rejected.

There is no LLM pass and no other fallback.

**Spaced dates.** A date OCR'd with spaced slashes (`05 / 20 / 2025`, `05/`
`20/` `2025`) is merged into one word, `05/20/2025`, when the page's words are
read (`merge_spaced_dates`), so every extractor and the model take it as a
plain date. A date with spaces only (`06 12 24`) is never read.

**Extracted vs Final.** This stage writes each page's own date (Extracted).
The Final DOS is the document's first page's, on every page of it, set by
`imaging_final` from the continuity documents (§7b). The `doc_dos_*` columns
are the page's own date, or `DOS_DEFAULT_DATE` (2222-02-02) when it has none.

`DOS_DEBUG=true` (env) writes every DOS label found at a page's edges, its
date, which edge and whether it was chosen to
`<chart>/debug/<chart>_dos_candidates.csv`.

**Writes**

| Target | Columns |
|---|---|
| `dos_extraction_results` | `date_of_service_from/to` (the page's own), `*_doclevel` (own, else the default), `dates`, `confidence`, `extraction_method` (`kv` or `rules`) |
| disk | `imaging/<chart>_dos.csv`; its `source` column names the state that gave the date (`kv`, `edge`, `edge_tesseract`, `admit_discharge`, `none`) |

---

## 7. Page type

**Full design:** [PAGE_CLASSIFICATION.md](PAGE_CLASSIFICATION.md).

Per page (`page_subtype` stage, the **Extracted** answer): BERT
(`models/page-family/`) predicts a model type with a confidence; the keyword
model (`keyword-canon/page_keyword_canon.json`) predicts a sub-type with a
score, margin and title hit; the ladder in `page_arbitration.json` decides
the page type (agreement → bert_high → keyword_only_class → keyword_title →
bert_medium → keyword_body → bert_low), then the sub-type, then codability
from `page_taxonomy.json`. No model installed → keywords only, visible in
`/health`. Text: Final2 → Final1 → Tesseract.

Across pages (`imaging_final`, the **Final** answer): every page of a
continuity document takes the **first page's page type**; the sub-type can
differ (a page whose own type differs keeps it as the sub-type: Progress Note /
Laboratory Data). Only when the first page is Unknown does a confidence-weighted
vote decide. Inside a Progress Note, Laboratory Data and Medication List stay
Non-Codable. A page on a weak link (an undated continue page) is flagged.

**Writes**

| Target | Columns |
|---|---|
| `page_classification` | `page_type`, `page_subtype`, `model_type`, `classification_category`, `decided_by`, `needs_review`, `bert_model_type`, `bert_confidence`, `keyword_page_subtype`, `keyword_score`, `keyword_margin`, `keyword_title_hit`; `axes` (JSONB, the v24 page axes behind `page_type` / `page_subtype`) |
| disk | `imaging/<chart>_codeable.csv` |

---

## 7b. Document continuity

**Runs:** after page type and DOS (`continuity`, seq 87), on every page that
is not blank, junk or duplicate. **Decides:** which document each page belongs
to. Settings live in `keyword-canon/continuity_canon.json` and reload on change.

| Page | Result | `decided_by` |
|---|---|---|
| First page of the chart, or after a blank/junk page | new document | `first_page` / `blank_junk` |
| Has a DOS, the same From and To as the open document's | continue (always, even after a signature) | `same_dos` |
| Has a DOS, a different one | new document | `new_dos` |
| No DOS, previous page signed | new document | `signature` |
| No DOS, printed page number follows the previous page's (N → N+1 of the same M) | continue, confirmed — overrides the checks below | `pagination` |
| No DOS, the previous page's printed number says it was the last ("page N of N") | new document | `page_number_end` |
| No DOS, looks like a first page: the keyword model's title for a page type other than the previous page's | new document | `first_page_marker` |
| No DOS, page type changes and the classifier is sure: the chance it is still the previous page's type (its confidence when the types agree, one minus it when they differ) is at most `document_end.max_same_type_probability` (10%) and its confidence in the new type is above `min_new_type_confidence` (70%) | new document | `page_type` |
| No DOS, the previous page's text stops above `document_end.page_end_max` (60%) of the page height (footer band excluded; needs Final2/Final1 line positions) | new document | `page_end` |
| No DOS, none of the above | continue | `continue_page` |

A duplicate page in between does not break a document. Printed `Page N of M`
is recorded (`page_no` / `page_total`) and confirms a document's first page;
on an undated page, consecutive numbers confirm a continue page and "page N of
N" ends the document (above). Page numbers never split a document.

**Final values** (the `imaging_final` stage): Final page type = the document's
first page's (sub-type can differ, §7). Final DOS and Final encounter type =
the **first page's**, on every page; a stand-alone document whose every page is
a `default_final_dos` type (demographics, registration, growth chart, problem
list, immunizations) takes 2222-02-02. A document's DOS in review-ui's Doc Summary is its first
page's.

**Writes**

| Target | Columns |
|---|---|
| `page_continuity_results` | `document_seq`, `seq` (page within the document), `position`, `relation`, `decided_by`, `confidence_level`, `link_strength` (strong only when every link from the start is a shared DOS), `start_confirmed`, `evidence`; `score` and `review_required` are no longer used, `in_progress_note` is always false |
| `imaging_final` (the `imaging_final` stage) | one row per page: the Final value of every reviewer field; page type agreed across the document (`page_type_source`), DOS and encounter type the first page's (`dos_source = document`); a duplicate page copies the page it duplicates (`duplicate`, `duplicate_of_page_id`); `specialty` is the page's own page-axes specialty, `Unknown` when it has none (no ground truth). See [CONTINUITY.md](CONTINUITY.md) |
| disk | `imaging/<chart>_continuity.csv`: the same per page, plus the printed `page_no` / `page_total`, the `section_headers`, the page's own `page_type`, `dos_from`, `dos_to`, and `layout_source` (final2 / final1 / text) |

---

## 8. Encounter type

**Engine:** `stages/lib/encounter/encounter_classify.py` · **Stage:**
`stages/lib/encounter/stage.py` · **Catalog:** `keyword-canon/encounter_canon.json`

One answer per visit — Outpatient (F2F), Outpatient (Tele), Inpatient or Home —
stamped on every page of the visit. **Evidence is ranked, not added up:** the
most authoritative evidence a visit has decides alone, so repeated "follow up"
can never outweigh one discharge summary.

### Visits

A visit is a run of **consecutive** pages sharing a date: the page's
document-level DOS, else its page-level DOS. The DOS default
(`DOS_DEFAULT_DATE`) is the absence of a date. A page without a usable date is
a visit of its own and stays unresolved (`no_date` / `default_date`). The same
date forty pages later is a second visit.

### Evidence

Each finding is recorded **once per visit**, however often its words appear.

| Tier | What | Decides? |
|---|---|---|
| 1 | A page type that exists in one setting only (`tier1_page_types`, keyed on the page type id). The page's own page type | Yes, alone |
| 2 | Text naming the setting: "telehealth", "hospital course", "home health visit", "place of service" | Only when tier 1 is empty |
| 3 | Hints found in several settings: "chief complaint", "follow up", "consultation", "h&p" | Never — breaks a tie inside the deciding tier |
| context | "radiology report", "mri report" … | Logged only |

Phrases match on word boundaries; a phrase ending in punctuation (`a/p:`,
`hpi:`) has no trailing boundary. **Negatives** ("discharged home",
"telephone message", "follow up with your primary care") remove the tier 2/3
finding they name, or every tier 2/3 finding of a setting. They never touch
tier 1.

### Decision

| Situation | Answer | `confidence` |
|---|---|---|
| Tier 1, one setting | that setting | 0.95 |
| Tier 1, two settings | more tier 3 hints, then setting priority (Home 10, Tele 20, F2F 30, Inpatient 40); `conflict=y` | 0.70 |
| Tier 1 empty, tier 2 one setting | that setting | 0.80 |
| Tier 2, two settings | as above; `conflict=y` | 0.60 |
| Nothing in tiers 1–2 | empty, `reason=no_setting_evidence` | 0 |

An unresolved visit never inherits from a neighbouring page. The buckets live
in the catalog; calibrate them against a labelled sample.

The **loader refuses** a catalog where a setting is unknown, a tier 1 id is not
a page type id, a phrase sits in two tiers or under two settings, a context
phrase also scores, or a negative cancels a phrase that does not exist.

### Writes

| Target | Columns |
|---|---|
| `encounter_type_results` | resolved pages only: `encounter_type`, `confidence`, `matched_keyword`. Rows for pages that are now unresolved are deleted |
| disk | `imaging/<chart>_encounter.csv`, every page: `encounter_type`, `encounter_label`, `confidence`, `matched_keyword` (page type for tier 1, phrase for tier 2), `continue_applied` (the page alone would answer differently or not at all), `decided_by` (`tier1`/`tier2`/`unresolved`), `reason`, `conflict` |

`ENCOUNTER_DEBUG=true` writes one evidence record per visit to
`<chart>/debug/<chart>_encounter_evidence.csv`: every finding with its tier,
source and page, cancelled findings, negatives that fired, context, the
deciding tier, the winner and any contender.

---

## 9. Chart status

**Module:** `core-pipeline/db/chart_status.py`

```
current_stage = earliest stage, in pipeline_stage.seq order,
                where not every page is completed|skipped
```

| Condition (checked in order) | `status` |
|---|---|
| No pages yet | `received` / `downloading` |
| A page `failed` in a stage that is not otherwise complete | `failed` |
| Some stage incomplete | `processing` (+ `current_stage`, `current_pass`) |
| All done, member `final_status='needs_review'` | `needs_review` |
| All done (including member `document_decision='reject'`) | `completed` |

Accept/reject is recorded on `member_verification_summary` only — `chart_list.status`
is never set to `rejected`.

A page that `failed` in an otherwise-finished stage does **not** fail the chart —
every page reached a terminal state, so the stage is done and the failure stays
recorded on the page.

`skipped` counts as done, so a chart of entirely blank pages still reaches
`completed`.

### Why v7 split the column

v6 packed the stage name into `chart_list.status`, so:

- every new stage needed a `CHECK`-constraint migration, and
- **two passes of one stage were unrepresentable** — `blank_junk` appeared once
  in the ordering, before `ocr_final1`, so a chart sitting in pass 2 reported
  blank/junk already finished.

v8 uses lifecycle `status` + `current_stage` + `current_pass`, with order in the
`pipeline_stage` table.

---

## Where the logic lives

| Concern | File |
|---|---|
| Stage order, skip rules | `core-pipeline/orchestrator/runner.py`, `stages/_support.py` |
| Blank/junk detectors | `core-pipeline/stages/lib/blank_junk/` |
| Member rules + NER | `core-pipeline/stages/lib/member/` |
| Member driver (ported `run.py`) | `core-pipeline/stages/lib/member/engine.py` |
| DOS regex + LLM | `core-pipeline/stages/lib/dos/dos_logic.py` |
| Document continuity, Final page type | `core-pipeline/stages/lib/continuity/`, `keyword-canon/continuity_canon.json` |
| Status derivation | `core-pipeline/db/chart_status.py` |
| Persistence | `core-pipeline/db/__init__.py` |

Behaviour above is pinned by `tests/` (253 tests). A failure there means the port
has drifted from the reference — the fix is to restore it, not to update the
expectation.
