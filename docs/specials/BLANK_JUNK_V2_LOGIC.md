# Blank / Junk Logic v2

Nov 28, 2026 · @Algodel

## At a glance

**Pass 1 decides only sure-shot cases and never gets undone; pass 2 sends every other page down one of three routes (Visual, Short, General), each running the same four-tier ladder, then post-processing handles invoices, continuations, duplicates and confidence.** The checks mostly stay; they are regrouped so that a rule's power (does it beat clinical evidence or not) is set by its tier, not by where it sits in a 45-step chain.

(Diagram in the doc: pass 1 sure-shot rules lock their pages (B1–B5, J1, J2 cover mark anywhere on the page, D1); the rest go through pass-2 entry checks, one route chosen by layout (Visual, Short, General), the same four-tier ladder, then post-processing P1 Invoicing, P2 Continuations, P3 Duplicates, P4 LLM check, P5 Confidence and review.)

The dashed line: a locked pass-1 verdict goes straight to the final verdict. Today's IDs map to the new ones in "The three routes".

## Review against the code (2026-11-28)

**Checked against `core-pipeline/modules/blank_junk/` at commit 1017c28; every item below is folded into this design.**

**Already in the code (no build needed)**

| v2 item | Today |
| --- | --- |
| B4 edge lines in pass 1 and on the final OCR | R1d runs in both passes (pass 2: final text of 1–4 lines) |
| `middle_band_ink` in the profile; `short` = no text and no ink in 25–75% | A page with a header and a footer has its 25–75% band measured for ink |
| Same OCR engine for the page and its neighbours | The Short route compares on one engine: Docling, then Doc Intel, then Tesseract |
| One debug record per page | `staging/blank_junk.json`: every check `no` / `fired` / `not reached`, short-page checks, model, duplicate, final |
| Signature Log keeps the page and sets Page Type | R3: Main, Page Type Signature Page / Signature Log |
| Visual pages are never short | `is_short` already excludes visual pages |

**Rules added today, now in this design**

- **Cards (V-A1, G-A4, G-W1):** with strong evidence (licence, 3+ insurance groups, card-back lines, Medicare or other ID, barcode), letter / statement words no longer block a card; form words still do.
- **S-A3:** a provider or date in the shared header / footer does not keep the page; its own electronic signature does.
- **P3 Duplicates:** every page is compared on its Docling text, never across OCR engines.
- **S-K6 "runs on from the previous page" (new keep, shadow first):** a short page whose opening continues the previous page's closing paragraph stays Main. It reads the text-continuity measure that `continuity.json` now stores for every pair of pages, junked short pages included; profile field `prev_text_continuity`.

**Settled**

| Question | In this design | Code change |
| --- | --- | --- |
| R4c, model p(blank) over 0.98 | Kept, as G-A6 (tier 1, no veto); p(blank) is still never added to p(junk) | None |
| J1 End of Report word limit | Under **25** words | Today under 10, shared with the cover mark: split the two limits |
| J2 cover mark | Pass 1 only, mark anywhere on the page, under 10 words | Today both passes: drop the pass-2 run |
| R2c tiny page | Moves to the Short route as S-W3; verdict unchanged | Only the pass changes |
| Debug file | Today's one file, extended (see "Making it scalable") | Extend, do not split yet |
| B5 placeholder guard | 25 words or fewer (replaces today's 30-character guard for the word "blank") | Guard changes |
| Where this design lives | `docs/specials/BLANK_JUNK_V2_LOGIC.md` in the repo | — |

## What changes from today

**The checks mostly stay; what changes is the order they run in and how they are grouped.** Today about 45 checks run in one long first-fire-wins chain, and the model veto, cover-page and blank rules are patched in at different places. v2 measures each page once, keeps pass 1 to sure-shot cases only, and runs the same four-step decision ladder in every pass-2 route.

| Topic | Today | v2 | Why |
| --- | --- | --- | --- |
| **Pass 1 scope** | 11 rules (R1a–R1f, R2a–R2c), all final | **8 sure-shot rules**: no ink, stage-1 blank with no text, no text, edge lines only, placeholder, End of Report alone, cover mark anywhere on the page, exact duplicate within 3 pages. R2c (tiny page) moves to pass 2 | A pass-1 verdict is never undone, so it must be measured or near-certain. A 14-character page needs the final OCR |
| **Pass-1 regret** | No second look | Never undone, but a pass-1 page whose Final OCR 1 text shows clinical evidence is **flagged for review** | Keeps the never-undo contract without silently losing a page |
| **Page measurements** | Computed inside each check, sometimes twice | **One page profile** per page per pass: text, ink, layout, signature, provider, dates, clinical evidence, neighbour overlap, model probabilities | Each check reads facts instead of re-deriving them, and the debug file shows the facts once |
| **Order inside pass 2** | Common checks, then one route, then Others; hard-junk rules sit before or after the keeps one by one | Route first, then **one ladder in every route**: (1) strong junk, (2) keep evidence, (3) weak junk, (4) model | Whether a rule can beat clinical evidence becomes a property of the rule (its tier), not of its position in a long list |
| **Route order** | Short, then Visual, then General | **Visual, then Short, then General**, chosen from the layout alone | A visual page is never short, so checking it first makes the choice simple and clear |
| **Model veto** | O3 for some rules, a separate 99% rule for cards, none for others | **One veto:** any pattern junk (tiers 1 and 3) becomes Main when the model's p(keep) is 0.99 or more; measured blanks are never vetoed | One rule, one setting |
| **Model "BLANK"** | p(blank) > 98% gives Junk / Others (R4c); a BLANK flag gives Junk / Others | p(blank) is **not** added to p(junk): it is recorded, and decides only through its own rule G-A6 (today's R4c: over 0.98 on a page with text is Junk / Others, no veto). On a page with text, a BLANK top label falls back to p(keep) vs p(junk) and goes to review; **Blank comes only from measurement** | Blank keeps one meaning: nothing on the page. Counting p(blank) as junk turned too many real pages into junk |
| **Cover page over 20 words** | Turned into Junk / Others afterwards | The Cover Page detectors require 20 words or fewer; a longer retrieval cover is Junk / Others from the start | No after-the-fact patching |
| **Signature Log** | R3, a common check that runs before the routes | A keep in tier 2 of the General route, which also sets Page Type Signature Page | It is keep evidence |
| **Invoices** | Inside "Others", only when the model says invoice | **A post-processing step** run on every pass-2 page that ended Main or model-Junk: invoice detector, terms page, facility page, multi-page invoices | Invoice pages are found by evidence, not only when the model guesses them |
| **Settings** | Code constants and canon keys mixed; several canon keys unused | Every threshold and word list in canon, in one detector registry; unused keys removed | New junk types and threshold changes become canon edits |

## The page profile

**Every pass builds one profile per page before any rule runs; rules only read it.** The profile is written whole to the staging file, so the debug view shows the facts and then the decisions.

| Group | Fields | Source |
| --- | --- | --- |
| Page | `page_type` (printed, handwritten, mixed, form, visual, uncertain), `quality`, `routing_row`, `handwritten_pct`, `visual` (typed visual **and** under 20 words) | Stage 1, OCR routing |
| Text | `engine` used, `chars`, `words`, `distinct_words`, `lines`, `text_area_pct`, `content_ends_pct`, `first_line`, `last_line`, `headings` (stage 6) | Pass 1: Tesseract. Pass 2: Azure, else Docling |
| Ink and layout | `no_ink`, `stage1_blank`, `edge_lines` {top, bottom}, `body_ink_pct`, `middle_band_ink`, `short` (no text and no ink in the 25–75% band) | Page image, line boxes |
| Signing | `signature` (kv\_extract), `signature_repeated` (same spot on a neighbour), `provider` (kv\_extract or name + credential), `credential` | kv\_extract, credentials canon |
| Dates | `has_date`, `date_in_running_line` | Text |
| Clinical evidence | `pn_sections` (strong / weak), `icd_codes`, `doses`, `med_words`, `lab_groups`, `measurements`, `social_history`, `signature_log_header` | Text, Progress Note headers canon |
| Junk patterns | Every detector's result: `hit`, `tier`, `margin`, `evidence` ("The three routes") | Detector registry |
| Neighbours | `prev_page_id`, `next_page_id`, `same_engine`, `own_lines`, `own_words`, `shared_line_share`, `same_order_share`, `prev_pn_header_in_bottom_third`, `prev_signature_tail`, prev\_text\_continuity | Previous / next page with text |
| Model (pass 2) | `p_keep`, `p_junk` (JUNK\_\* labels **only**), `p_blank` (recorded; decides only through G-A6, today's R4c), `label`, `subtype`, `service_flag`, `service_reason`, `model_version` | DistilBERT b\_jnk v3 |

**The model runs on every pass-2 page, once, before the ladder.** Rules can then use its probabilities (the veto, the keep tiers) without a second call, and the debug file always shows what the model thought, even when a rule decided.

## Pass 1: sure-shot cases only

**Pass 1 decides only what can be measured or is near-certain, and its verdicts are final.** Everything else is Main for now and goes to pass 2. A rule earns a place in pass 1 only if it is at least 99.5% right on reviewed pages; one that drops below that moves to pass 2 by a canon switch (`pass: 2`).

**Pages judged:** printed pages with quality not Low (routing rows 1–4), on Tesseract text. B1 and B2 also run on any page stage 1 typed Blank or found inkless.

| ID | Rule | Fires when | Verdict | Confidence | Was |
| --- | --- | --- | --- | --- | --- |
| **B1** | No ink | Stage 1 found no ink (`blank_page` / `faint_marks_only`) | Blank | 1.0 | R1a |
| **B2** | Stage-1 blank, no text | Stage 1 typed it Blank and the OCR has under 15 characters | Blank | 1.0 | R1b |
| **B3** | No text | 0 characters of text, page not visual | Blank | 1.0 | R1c |
| **B4** | Edge lines only | At most 1 line in the header band and 1 in the footer band, no body ink between, at most 4 lines in all, no credential | Blank | 1.0 | R1d |
| **B5** | Placeholder | A placeholder phrase ("intentionally left blank", "this page left blank", "blank page", "page not used", or the word "blank" alone on a line) on a page of 25 words or fewer | Blank | 0.97 | R1e, R1f |
| **J1** | End of Report alone | "End of Report" / "End of the Report" on a page of under 25 words | Junk / Cover Page | 0.97 | R2b |
| **J2** | Cover mark | accept / accepted / unaccept / unaccepted / cover as a whole word anywhere on the page (top, middle or bottom) on a page of under 10 words | Junk / Cover Page | 0.97 | R2a |
| **D1** | Exact duplicate nearby | The same body text as a page 1–3 places earlier, both pages 25+ words | Duplicate | 1.0 | pass-1 duplicates |
| **P0** | Nothing fired | — | Main for now (pass 2 decides) | 0.40 | R0 |

**Guards on B3, B5, J1 and J2** (B1, B2, B4 measure ink, so they need none): the page is not visual, its handwritten share is under 5%, and it has no provider, credential or electronic signature.

**Moved to pass 2:** R2c (under 15 characters). It reads a few characters of Tesseract text, which is where Tesseract is least reliable; on the final OCR it becomes a detector in the Short route. **Stays in pass 1 only:** the cover mark (R2a, now J2), at any position on the page; pass 2 has no cover-mark rule.

### The never-undo contract

- A pass-1 Blank, Junk or Duplicate is **locked**: pass 2 skips the page (`skipped: settled in pass 1`), Azure OCR is not paid for it, and later stages leave it out. Only a reviewer can change it.
- **Regret flag:** Final OCR 1 still runs on every page. When a locked page's Final OCR 1 text has 25+ words and any clinical evidence (a Progress Note header, an ICD-10 code, 2+ doses, a lab result group or an electronic signature), the page gets `review: pass1_regret`. The verdict does not change; the flag goes to the review queue and to the pass-1 precision report.
- Each pass-1 rule's precision on reviewed pages is reported weekly in `metrics`, so a rule that starts making mistakes is seen before it hurts.

## Pass 2: entry and route

**Pass 2 judges every page pass 1 did not lock, on the final OCR, in three steps: entry checks, one route, then post-processing.**

**Pages judged:** every page that pass 1 left Main (P0) or did not judge (handwritten, mixed, printed Low, form, visual, uncertain). A page with no final OCR text yet is skipped, unless stage 1 typed it Blank.

### Entry checks (same functions as pass 1)

1. **B1–B4 on the final OCR** (B4 when the final text has 1–4 lines). A page that comes back blank after the final OCR is Blank.
2. **B5 and J1** on pages pass 1 did not judge, with the same guards. J2 (cover mark) is pass 1 only and is not repeated here.

An entry verdict is final for pass 2: no route runs and the model cannot veto it.

### Route choice: from the layout alone, first match wins

| Order | Route | The page is | Why first |
| --- | --- | --- | --- |
| 1 | **Visual** | Typed visual **and** under 20 words | A picture page has too few words for text rules; cards and scanner artefacts are what matter |
| 2 | **Short** | Text only at the top and / or bottom (no text and no ink in the 25–75% band), handwritten share 30% or less | Its words are mostly the neighbours' header and footer, so it is judged against its neighbours |
| 3 | **General** | Everything else | A full page: patterns, clinical evidence and the model |

Today's exception "a page with a Financials heading is not short" goes: Financials is a tier-1 detector in both the Short and the General route, so it fires either way.

## The decision ladder

**Every route runs the same four tiers, in order; the first tier that has a hit decides.** Inside a tier, the detectors run in the priority set in the registry, and the first hit wins. A route only chooses which detectors and keeps sit in each tier ("The three routes").

| Tier | What it holds | Beats clinical evidence? | Result | Model veto (p(keep) ≥ 0.99) |
| --- | --- | --- | --- | --- |
| **1. Strong junk** | Patterns so specific that clinical words on the page do not save it: contents page, fax cover sheet, retrieval cover, strong card, Financials, document cover sheet, page made only of its neighbours' lines | Yes | Junk / the detector's subtype | Yes, except in the Short route |
| **2. Keep** | Clinical evidence: handwriting over 30%, the page's own electronic signature, lab results, a medication list, ICD-10 codes, Progress Note sections, a signature log; on short pages also carry-overs from the previous page | — | Main (Signature Log also sets Page Type) | — |
| **3. Weak junk** | Patterns that lose to clinical evidence: other cards, fax header unsigned, fax routing, few words of its own, same words as a neighbour | No (tier 2 already kept the page) | Junk / the detector's subtype | Yes, except in the Short route |
| **4. Model** | Nothing above fired | — | The model's flag: KEEP gives Main; JUNK gives Junk / subtype (Invoice goes to post-processing); a BLANK top label is set aside and p(keep) vs p(junk) decides, with review; no model gives Main, review | — |

**Rules that apply to every tier**

- **One veto.** A tier-1 or tier-3 junk becomes Main when p(keep) ≥ 0.99 (canon `veto.keep_at`), marked `vetoed_by_model` and sent to review. Short pages have no veto, because the model reads too few words there. Entry verdicts (measured blanks) are never vetoed.
- **Blank comes only from measurement** (entry checks, Short route S-A1). The model's BLANK probability is recorded but never added to p(junk); it decides only through G-A6 (R4c). When BLANK is the model's top label on a page with text, tier 4 sets it aside, compares p(keep) with p(junk), and sets `review_required` (`model_blank_on_text`).
- **The model protects from its own mistakes:** a JUNK flag from the service's "admin rule" on a page with lab-result or clinical-list evidence is KEEP (today's wrapper overrule), and a "protocol keep" with raw p(junk) ≥ 0.95 and p(keep) < 0.99 is Junk (today's protocol-keep overrule).
- **Review band:** a tier-4 verdict whose model probability is under 0.70, or any vetoed verdict, sets `review_required`.
- **Every detector writes the same record**: `hit`, `tier`, `margin` (how far past its threshold), `evidence` (the words or measures that fired it). Confidence is computed from these ("P5 Confidence and review").

## The three routes

**Each route is the ladder filled with its own detectors.** IDs are new and grouped by tier (A = strong junk, K = keep, W = weak junk, M = model); the last column gives today's ID, so nothing is lost.

### Visual route (typed visual, under 20 words)

| ID | Tier | Rule | Fires when | Verdict | Was |
| --- | --- | --- | --- | --- | --- |
| V-A1 | 1 | Card | `card_rules.detect` finds a card; on a visual page a licence or state ID counts on its own words; letter / statement words do not block a strong card | Junk / Cards | V1 |
| V-A2 | 1 | Scanner artefact (new, off until checked) | Under 4 words and the image is near-uniform: dark ink over 85% of the page, or ink only in a band along one edge | Junk / Others | — |
| V-K1 | 2 | Picture page | Anything else: ECG strip, photo, wound picture, diagram | Main | V2 |

The model is not asked on visual pages under 20 words: it has too little text to read.

### Short route (text only at the top and / or bottom)

First the page's **own lines** are found: lines not shared with the previous or next page (same OCR engine for all three, placeholder values such as "None recorded" count as no content), as today.

| ID | Tier | Rule | Fires when | Verdict | Was |
| --- | --- | --- | --- | --- | --- |
| S-A1 | 1 | Header / footer only, 1–2 lines | Every line in the header or footer band, at most 2 at each edge, each a running marker or on a neighbour; unsigned | **Blank** | S4 |
| S-A2 | 1 | Running header / footer only | Every line a running line, with a page number or running label; unsigned | Junk / Others | S5 |
| S-A3 | 1 | Every line on a neighbour | All lines are the previous or next page's; its own non-repeated electronic signature keeps it; a provider or date in the shared header / footer does not | Junk / Others | S6 |
| S-A4 | 1 | Document cover sheet | Own lines are only label / value fields, 3+ fields, one naming the document, no value over 6 words | Junk / Cover Page | S1 |
| S-A5 | 1 | End of Report | "End of Report" is all of its own lines | Junk / Cover Page | S2 |
| S-A6 | 1 | Next-page pointer | Its only own content points to another page | Junk / Others | S3 |
| S-A8 | 1 | Financials heading | A line or heading that is only "Financial(s)" or "Financial Summary" | Junk / Others | R4a |
| S-K1 | 2 | Its own signature or provider | Electronic signature not repeated on a neighbour, or a provider not printed in the same spot on a neighbour | Main | S11 |
| S-K2 | 2 | Measurements or doses | Own lines hold a medication list, a lab result group, an ICD-10 code, or 2+ measurements | Main | S12 |
| S-K3 | 2 | Carries the previous section | The previous page has a Progress Note header in its bottom third, and this page adds 2+ words | Main | S7 |
| S-K4 | 2 | Carries a signature | The previous page ends with a signature line, this page's own lines carry a date and 2+ added words | Main | S8 |
| S-K5 | 2 | Note header plus words | A Progress Note header on its own lines plus 3+ other words | Main | S9 |
| S-K6 | 2 | Runs on from the previous page (new, shadow first) | The page's opening continues the previous page's closing paragraph: `prev_text_continuity` shows high character / word similarity and a mid-sentence join (the previous page ends mid-sentence, this one starts mid-thought). Thresholds in canon, set from the shadow run | Main | new |
| S-W1 | 3 | Fax routing | A fax number and a To / Attn / From line, no note content | Junk / Letter/Fax | S10 |
| S-W2 | 3 | Same words as a neighbour | 98%+ of its words, in order, are on the previous or next page | Junk / Others | S13 |
| S-W3 | 3 | Tiny page | Under 15 characters, not visual, no provider or date | Junk / Others | R2c (moved from pass 1) |
| S-W4 | 3 | Nothing of its own | 0–1 own words, or only section headings, Progress Note header words or title-like lines | Junk / Others | S14 (junk half) |
| S-M1 | 4 | Model | 2+ own words (2–4 words included: they no longer default to Main) | The model's flag | S14 (keep half), S15, O1 |

Every verdict on a short page is capped at 0.90 confidence.

### General route (every other page)

| ID | Tier | Rule | Fires when | Verdict | Was |
| --- | --- | --- | --- | --- | --- |
| G-A1 | 1 | Contents page | A contents title in the top third, 3+ entry lines, entries 40%+ of the lines after it | Junk / Others | R4d |
| G-A2 | 1 | Fax cover sheet | "Fax" on the page and 2+ routing boxes, or a fax title with From and To labels | Junk / Letter/Fax | R4e |
| G-A3 | 1 | Retrieval cover sheet | "Retrieved by" on the page, and every other line is a label : value field (Patient Name:, DOB:, Member ID:, Measure:, Dates of Service: …) with no free text; 60 words or fewer | Junk / Cover Page (20 words or fewer), else Junk / Others | R4b |
| G-A4 | 1 | Strong card | A card with strong evidence (licence, 3+ insurance groups, card-back lines, Medicare or other ID, barcode), or any card on a page typed visual. Letter / statement words do not block a strong card; form words still do | Junk / Cards | G1 |
| G-A5 | 1 | Financials heading | As S-A8 | Junk / Others | R4a |
| G-A6 | 1 | Model sure it is blank | p(blank) over 0.98 on a page with text; no veto | Junk / Others | R4c (kept) |
| G-K1 | 2 | Signature log | "Signature Log(s)" in a heading or the first 8 lines, handwriting 20%+ | Main, Page Type Signature Page | R3 |
| G-K2 | 2 | Handwriting | Handwritten share over 30%, page not typed visual | Main | G2 |
| G-K3 | 2 | Its own signature | Electronic signature not repeated on a neighbour, p(junk) under 0.95 | Main | G3 |
| G-K4 | 2 | Lab result | One term from each lab group | Main | G4 |
| G-K5 | 2 | Medication list | 3+ different doses, or a medication word and 2 doses | Main | G5 |
| G-K6 | 2 | Clinical content | 2+ ICD-10 codes, 2+ strong Progress Note sections, or a Social History header | Main | G6 |
| G-W1 | 3 | Other card | Any other card on a printed or uncertain page | Junk / Cards | G7 |
| G-W2 | 3 | Fax header, unsigned | A section header with a fax term, no keeping signature, not between two Progress Note pages | Junk / Letter/Fax | G8 |
| G-M1 | 4 | Model | Nothing above fired | The model's flag | O1 |

**Kept from today:** R4c, as G-A6 (decided 2026-11-28: it should not be undone, so it has no veto). It is the only place p(blank) decides; p(blank) is never added to p(junk).

## Post-processing

**After every page has a route verdict, five steps run over the whole chart, in this order.** Locked pass-1 pages and entry verdicts are not touched, except by the duplicate step's labels and the review flags.

### P1 Invoicing

**Invoices are found by evidence on the page, not only when the model guesses "invoice".** P1 runs on every pass-2 page whose verdict came from tier 4 (the model), or that is Main with no tier-2 clinical keep. A page kept for clinical evidence (lab, medication list, ICD codes, note sections) is never turned into an invoice: a superbill or a charge ticket is left to page classification.

**Invoice evidence** (words in canon `invoicing`):

- **Title:** Invoice, Statement, Statement of Account, Itemized Statement, Bill.
- **Wording lines:** Bill To, Remit To, Invoice Number / Date, Due Date, Amount Due, Balance Due, Subtotal, Total, Tax ID, Copy Fee, Per Page, Records Fee.
- **Dollar table:** a dollar amount in a table row or cell (Docling text first).
- **Terms wording:** terms and conditions, payment terms, terms of payment / sale / service / use.
- **Facility words:** hospital, clinic, medical center, health center, surgery center.

| ID | Fires when | Result | Confidence | Was |
| --- | --- | --- | --- | --- |
| I1 | A dollar table **and** a title or a wording line (or the model said invoice) | Main, Page Type **Invoice / Invoice Page**, Non-Codable | evidence formula | O2 step 1 |
| I2 | Terms wording before any invoice wording, no dollar table | Junk / Others (terms page) | 0.30 if the model said invoice, else evidence | O2 step 2 |
| I3 | A title and 2+ wording lines with dollar amounts, no table | Main, Page Type Invoice | evidence formula | new |
| I4 | The model said invoice, a near-miss card (stopped only by its own guards), no note content | Junk / Cards | 0.30 | O1 near-miss card |
| I5 | The model said invoice, no invoice evidence, a facility word | Main, not an invoice; page classification decides; review | 0.30 | O2 step 3 |
| I6 | The model said invoice, nothing above | Main, Page Type Invoice; review | 0.30 | O2 step 4 |
| I7 | The page right after an I1 / I3 / I7 page has a dollar table or wording line, and shares its running header | Main, Page Type Invoice (invoice continued) | evidence formula | new |
| — | No invoice evidence and the model did not say invoice | Unchanged | — | — |

The staging file's summary counts invoice pages, terms pages and continued invoices per chart.

### P2 Continuations

- **Contents page continued:** the page right after a G-A1 page is Junk / Others when it has 2+ entry lines and at most 120 distinct words (today's R4d continuation).
- Invoice continuation is I7 above.

### P3 Duplicates

Unchanged from today (`dup_rules.py`): Main pages only, body text on one OCR engine (Docling, which reads every page), each page against its 3 most similar earlier pages; the hard blocks; exact at any distance, only exact within 3 pages; Rule A and Rule B for runs; conflicts to review; labels "Duplicate of N" and "Copy of N".

### P4 LLM second opinion (optional, off by default)

Today's O4: a short page with 2+ words whose previous page is a full Progress Note page, when its verdict came from S-K1–S-K5, S-W2–S-W4 or S-M1. It may answer Main or Junk / Others, capped at 0.90; a failed call keeps the rules' verdict.

### P5 Confidence and review

| Verdict from | Confidence |
| --- | --- |
| B1–B4, D1 (measured) | 1.0 |
| B5, J1, J2 | 0.97 |
| A tier-1, tier-2 or tier-3 detector | `0.5 + 0.45 × margin × support`, combined in log-odds with the model's probability for that verdict (today's `evidence.py`) |
| Tier 4, the model | The model's probability for its flag |
| No model | 0.40 |
| Any short page | at most 0.90 |
| Anything | never below 0.20 |

`review_required` is set by: a model veto, a tier-4 probability under 0.70, `pass1_regret`, a duplicate conflict, I5 or I6, a short-page model verdict, and a model BLANK top label on a page with text. The reason list says which.

## Making it scalable

**A new junk type, a moved threshold or a rule switched off becomes a canon edit; code only grows when a new kind of measurement is needed.**

### One detector registry in canon

`resources/canon/blank_junk_rules.json` replaces the scattered constants and the unused keys of `blank_junk_context_canon.json`. Each rule names its pass, routes, tier, priority, verdict and settings:

```json
{
  "id": "G-A2", "name": "fax cover sheet", "enabled": true,
  "pass": 2, "routes": ["general"], "tier": 1, "priority": 20,
  "verdict": {"flag": "junk", "subtype": "Letter/Fax"},
  "kind": "any_of",
  "params": {
    "requires_any": ["fax", "facsimile"],
    "options": [
      {"kind": "distinct_lines", "terms": ["urgent", "for review", "please comment", "please reply", "reply asap", "please recycle", "as requested"], "min": 2},
      {"kind": "labels", "title": ["fax", "facsimile"], "all_of": [["from", "sender"], ["to", "attn", "attention", "recipient"]]}
    ]
  },
  "veto": true,
  "was": "R4e"
}
```

**Generic detector kinds**, so most rules need no new code: `phrase_on_short_page` (B5, J1, J2, S-A5), `title_and_entries` (G-A1), `label_value_fields` (S-A4, G-A3), `distinct_lines` and `labels` (G-A2), `group_count` (cards, lab groups), `regex_count` (ICD codes, doses, measurements), `own_lines` (S-A1–S-A3, S-W2, S-W4), `measure` (B1–B4, handwriting, ink). A detector kind not on the list is new code plus a test.

### One debug record per page, in the staging files

Today's one staging file, `staging/blank_junk.json` (both passes, the same JSON as the Cosmos staging document), is extended to hold per page: the **profile**, the **route**, every rule of the route as `no` / `hit` with tier, margin and evidence, the **model** probabilities, the **veto**, the **post-processing** steps that touched it, the **duplicate** result and the **final** verdict, confidence and review reasons. It splits into `03_blank_junk_p1.json` and `08_blank_junk_p2.json` only when the staging redesign lands; the per-page record does not change.

### Measured per rule

- Per rule and week, in `metrics`: hits, and precision against reviewed pages (Blank / Junk / Main ground truth).
- Pass-1 rules must stay at 99.5% or better; the report lists every miss and every `pass1_regret`.
- A new or changed rule ships `enabled: "shadow"` first: it is evaluated and logged, but does not decide, until its precision on reviewed pages is known.

### Tests

- One synthetic page per rule (no real text) that fires it, and one near-miss that must not.
- Ladder tests: a tier-1 hit beats a keep; a tier-3 hit loses to a keep; the veto applies to tiers 1 and 3 but not to entry verdicts or short pages.
- A regression set of reviewed charts: the v2 verdict is compared with today's on every page, and every change is listed with its rule IDs before the switch.

## What to give Claude Code

**Give it three things:** this design as a file (`docs/specials/BLANK_JUNK_V2_LOGIC.md`), today's logic (`docs/specials/BLANK_JUNK_LOGIC.md`, which your Word file was written from), and the prompt below. Claude Code cannot open this page, so it reads the file.

**Build order** (one branch per step; today's behaviour stays the default until step 7):

1. **Page profile.** `modules/blank_junk/profile.py` builds the profile ("The page profile") once per page per pass; today's checks read it instead of measuring again. Verdicts must not change.
2. **Registry and detector kinds.** `resources/canon/blank_junk_rules.json` with every rule (new ID, `was`, tier, routes, params), and `modules/blank_junk/detectors.py` with the generic kinds. Run in **compatibility order** (today's first-fire order) and prove identical verdicts on the regression charts.
3. **The ladder.** `modules/blank_junk/ladder.py`: route choice (Visual, Short, General) and the four tiers, with the one veto. Behind `BLANK_JUNK_LOGIC = v1 | v2 | shadow`; in `shadow`, v1 decides and every page where v2 differs is listed with both rule IDs.
4. **Pass 1 trimmed.** B1–B5, J1, J2, D1 only; R2c moves to the Short route; the lock and the `pass1_regret` flag.
5. **Post-processing.** P1 Invoicing (I1–I7), P2 continuations, P3 duplicates (unchanged), P4 LLM (unchanged, off), P5 confidence and review.
6. **Debug record and metrics.** Extend today's `staging/blank_junk.json` with the profile, the route, the tier of each rule and the post-processing steps; per-rule hits and precision in `metrics`.
7. **Switch.** After review of the shadow list, `BLANK_JUNK_LOGIC=v2` becomes the default; `docs/specials/BLANK_JUNK_LOGIC.md` is rewritten from the code.

**Done when**

- Step 2 gives the same verdict as today on every page of the regression charts.
- Every pass-1 rule is at least 99.5% right on reviewed pages; every `pass1_regret` page is listed.
- In shadow, every v1 vs v2 difference is listed (page ids, both rule IDs, ground truth where there is one); v2 is not worse on Blank / Junk / Main accuracy against ground truth, and Junk precision does not drop.
- Invoice pages found by I1 / I3 / I7 that today's logic missed are listed for review.
- Each rule has a firing and a near-miss test page; the ladder tests pass.
- No page text in the staging files or logs.

**Prompt for Claude Code**

```markdown
Read docs/specials/BLANK_JUNK_V2_LOGIC.md (the new design)
and docs/specials/BLANK_JUNK_LOGIC.md (what the code does today), then
core-pipeline/modules/blank_junk/ (method.py, stage.py, card_rules.py, dup_rules.py, evidence.py, model_bridge.py)
and resources/canon/blank_junk_context_canon.json.

Implement the design's "Build order", one branch per step. Before each step, show me what exists and your
plan, and wait for my OK. Keep today's behaviour as the default (BLANK_JUNK_LOGIC=v1) until I approve the
shadow list in step 3.

Rules: pass-1 verdicts are locked (never undone by pass 2); every threshold and word list lives in
resources/canon/blank_junk_rules.json; every rule keeps a `was` link to today's ID; no page text in logs or
staging; one test page per rule plus a near-miss; python -m pytest tests/ -q passes after every step.

After each step, report: verdicts changed (count and page ids, by rule), per-rule hit counts on the
regression charts, and which "Done when" checks pass.
```
