# Provider name, credentials and signature — logic as implemented

How the pipeline finds a page's provider, their credentials and whether the page is
signed, where each value is stored, and what uses it.

| | |
|---|---|
| Extractors | `core-pipeline/stages/lib/extraction/provider_name/`, `provider_credentials/`, `electronic_signature/` |
| Page summary | `extraction/results.py` (`signature_fields`) |
| Stage | `kv_extract` (seq 56), key/value extraction — before blank/junk pass 2 |
| Keys | `provider_name/keys.json` (46), `electronic_signature/keys.json` (30), `provider_name/roles.json`, `provider_name/suffixes.txt` (227 credentials) |
| Staging | `<chart>/staging/extraction.json` — every candidate, per field |
| Outputs | DB `provider_signature_results` + `imaging/<chart>_provider_signature.csv` (kv_extract); DB `imaging_final` + `imaging/<chart>_final.csv` (imaging_final) — §2 |
| review-ui | Production: the database (`imaging_final`); Local: the CSVs only — §3 |
| Database changes | [`schema/changes_2026-10-18.sql`](../schema/changes_2026-10-18.sql) |

---

## How to read this

| Order | Section |
|---|---|
| 1 | **The rule** — how a page's signature, provider and credentials are decided |
| 2 | **What it writes** — the database tables and CSVs |
| 3 | **How review-ui is powered** — which store each screen reads |
| 4 | Other pipeline stages that use it |
| 5 | Known gaps |
| Appendix | How the candidate rows the rule picks from are made (the extractors), and the credential lists |

Order of work: the **extractors** (Appendix) read the page and write candidate rows to
`staging/extraction.json` → **the rule** (§1) picks from them → the result is **written**
(§2) → **review-ui** shows it (§3).

---

## 1. The rule (2026-10-18, provider_version 2)

A row **counts** when it is `accepted = true` **or** its `score` is above **0.7** — for
the electronic-signature, provider-name and credentials rows alike.

1. **Candidate signers** = the page's counted `electronic_signature` rows, in order: the
   **selected** one first (the highest-scoring when several are selected, else the
   highest-scoring), then the others by score.
2. Each candidate's name and credentials are found by the steps below.
3. **Provider Signature = Yes** only when at least one signer is found (a name or
   credentials). **No** when there is no counted signature — even if the page has
   provider credentials or a provider name — and when a signature yields neither a name
   nor credentials anywhere.
4. **No counted signature ⇒ Signature No, but the page's provider is still named**
   (`source = page_provider`, `signers` empty): the best counted `provider_credentials`
   row that has a `provider_name` (name = its `provider_name`, credentials = its
   `value`); else the best counted `provider_name` row (its `value`, credentials split
   off). Confidence = that row's score × 0.75. Nothing counted ⇒ no name, no credentials.
5. review-ui lists several signers numbered, **one per line** (1. … / 2. …).

For each counted signature:

| Step | Name | Credentials |
|---|---|---|
| 1. The signature row's fields | its `provider_name`, credentials stripped | its `designation`; else the credentials written in `provider_name` |
| 2. Still missing — the same row | its `ner_text` | searched in its `sentence` with the credentials canon (`provider_credentials_canon.json`) |
| 3a. **Several counted signatures** | stop here: a signer still missing its name **or** its credentials is removed | |
| 3b. **One counted signature** | first a counted `provider_credentials` row that names its provider: name = its `provider_name`, credentials = its `value` (the credential, as `designation` is on a signature) unless the signature already gave some; else the page's counted `provider_name` rows (the best-scoring), credentials stripped | the page's counted `provider_credentials` rows' `value` (the same `provider_name` first, else the first) |

A `provider_credentials` row: `{"key": "Credential", "value": "FNP", "provider_name": "Meghan Newman", "score": 0.9, "accepted": true}` — `value` is the credential, `provider_name` the person.

The same person signing twice is listed once.

**Confidence** — the signature's `score`:

| Case | Confidence |
|---|---|
| One signature, name and credentials from its own row (steps 1–2) | its score |
| One signature, name or credentials from outside it (step 3b) | score × **0.75** |
| Several signatures | each signer: its score × **0.9**; the page: **their average** |

Each signer carries its own (`signers[].confidence`); the page's is `confidence` in
`provider_signature_results` and the CSV, and `provider_confidence` in `imaging_final`.

Examples:

| Signature rows | Signers |
|---|---|
| "WILLIAM J WALL III, MD", designation MD | 1. WILLIAM J WALL III · MD (step 1) |
| `provider_name` empty, `ner_text` "WILLIAM J WALL III", sentence "…SIGNED BY: WILLIAM J WALL III, MD on…" | 1. WILLIAM J WALL III · MD (step 2) |
| "Meghan Newman", no designation, sentence "… Meghan Newman , DNP on 05/19/2025 …" | 1. Meghan Newman · DNP (step 2) |
| "WILLIAM J WALL III, MD" (selected) + "Jane Roe", no credentials anywhere in its row | 1. WILLIAM J WALL III · MD — Jane Roe removed (step 3a) |
| one signature, key "Signed", no name; a credentials row "FNP" naming "Meghan Newman" (score 0.9) | 1. Meghan Newman · FNP (step 3b) |
| one signature, key "Signed", no name; no credentials row with a name; page `provider_name` "Daniel J Hamilton, MD" (accepted) | 1. Daniel J Hamilton · MD (step 3b) |
| a signature scored 0.75, not accepted | counts (> 0.7) |
| one signature, nothing found in it or on the page | **Signature No**; no name, no credentials |
| no electronic signature; credentials row "MD" naming "Weidong Huang" (0.9); provider_name "Finkelstein Tom I, MD" (0.77) | **Signature No**; Weidong Huang · MD (the credentials row first), confidence 0.675 |
| no electronic signature; only provider_name "Finkelstein Tom I, MD" (0.77, accepted) | **Signature No**; Finkelstein Tom I · MD, confidence 0.58 |
| no electronic signature, no counted provider or credentials row | **Signature No**; no name, no credentials |

A credential is a token in `provider_name/suffixes.txt` or `provider_credentials_canon.json`,
printed in capitals (or its usual case: PhD, PharmD); combined ones count ("APRN.CNP",
"MD/PhD"); "M.D." keeps its dots. A mis-cased one ("Rd", "Md") is dropped from the name.

The rows it picks from (the electronic-signature, provider-name and credentials
candidates) are made by the extractors: see the Appendix.

Code: `signature_fields`, `split_name_credentials`, `credentials_in_sentence` in
`extraction/results.py` (`COUNTS_ABOVE` = 0.7, `OUTSIDE_FACTOR`, `SEVERAL_FACTOR`); `page_signature` in `extraction/stage.py`.

---

## 2. What it writes

```
staging/extraction.json            (the extractors' candidates — read ONLY by kv_extract)
        │  kv_extract (seq 56): the rule in §1, one row per page
        ├──► DB  provider_signature_results          (one row per page)
        └──► CSV imaging/<chart>_provider_signature.csv   (same values)
                 │
                 │  imaging_final (seq 100): copies the provider into the Final row
                 ├──► DB  imaging_final              (one row per page, every Final field)
                 └──► CSV imaging/<chart>_final.csv  (same values)
```

Nothing after `kv_extract` reads `extraction.json`.

### 2.1 `provider_signature_results` (DB) and `<chart>_provider_signature.csv`

Written by `kv_extract`, one row per page, the same values in both.

| Column | Value | In DB | In CSV |
|---|---|---|---|
| `chart_id`, `page_id` | the page | yes | — |
| `chart_name`, `page_name`, `page_number` | the page | — | yes |
| `signature_present` | at least one signer found from a counted electronic signature (§1) | `true` / `false` | `y` / `n` |
| `provider_name` | the first signer's name, **without** credentials (= `signers[0].name`; kept for v1 rows and single-name readers until the switch to v2 only) | yes | yes |
| `provider_credentials` | the first signer's credentials | yes | yes |
| `signers` | every signer, in order: `[{"name", "credentials", "signature_date", "confidence"}]` | JSONB | JSON text |
| `provider_version` | **2** (this rule) | yes | yes |
| `confidence` | the page's confidence (§1) | yes | yes |
| `signature_date` | the first signer's date (ISO) | yes | yes |
| `signature_key`, `sentence`, `region`, `scale`, `ner_text`, `source` | the first (selected) signature's | yes | yes |

### 2.2 `imaging_final` (DB) and `<chart>_final.csv`

Written by `imaging_final` from `provider_signature_results` (never the staging), next to
every other Final field of the page.

| Column | From `provider_signature_results` |
|---|---|
| `provider_name` | `provider_name` |
| `provider_credentials` | `provider_credentials` |
| `signers` | `signers` |
| `provider_version` | `provider_version` |
| `provider_confidence` | `confidence` |
| `signature_present` | `signature_present` |

A duplicate page takes these from the page it duplicates.

### 2.3 Old (v1) and new (v2) rows — which columns are filled

✅ = filled · ❌ = NULL

**`provider_signature_results`** — every row is v2: nothing wrote this table before
2026-10-18, so there are no v1 rows in it. A chart gets rows when `kv_extract` re-runs.

| Column | v2, signature **Yes** | v2, signature **No** |
|---|---|---|
| `chart_id`, `page_id` | ✅ | ✅ |
| `signature_present` | ✅ `true` | ✅ `false` |
| `provider_name` | ✅ the first signer's bare name (NULL only if no name was found anywhere) | ✅ the page's provider (§1 rule 4), else NULL |
| `provider_credentials` | ✅ the first signer's credentials (NULL if none were found) | ✅ the page's provider's, else NULL |
| `signers` | ✅ `[{"name", "credentials", "signature_date", "confidence"}, …]` | ✅ `[]` |
| `provider_version` | ✅ `2` | ✅ `2` |
| `confidence` | ✅ (§1) | ✅ the page provider row's score × 0.75, else NULL |
| `signature_date` | ✅ the first signer's (NULL if none printed) | ❌ |
| `signature_key`, `region`, `scale`, `sentence`, `ner_text`, `source` | ✅ the first (selected) signature's | ❌ |
| `created_at`, `updated_at` | ✅ | ✅ |

**`imaging_final`** — the provider columns (every other column is the same in v1 and v2):

| Column | **v1** row (written before 2026-10-18) | **v2**, signature Yes | **v2**, signature No |
|---|---|---|---|
| `signature_present` | ✅ | ✅ `true` | ✅ `false` |
| `provider_name` | ✅ the name **with** its credentials ("Ramsey Luce, R.N."); could be filled with **no** signature (the old rule named a provider anyway) | ✅ bare name | ✅ the page's provider, else NULL |
| `provider_credentials` | ❌ (new column) | ✅ | ✅ the page's provider's, else NULL |
| `signers` | ❌ | ✅ the list | ❌ (an empty list is stored as NULL here) |
| `provider_version` | ❌ — review-ui reads NULL as **1** | ✅ `2` | ✅ `2` |
| `provider_confidence` | ❌ | ✅ | ✅ when the page's provider is named, else NULL |

**The CSVs** — `<chart>_provider_signature.csv` has the same columns and values as
`provider_signature_results` (`signature_present` as `y` / `n`, `signers` as JSON text,
`[]` with no signature); a CSV from before 2026-10-18 has no `provider_credentials`,
`signers` or `provider_version` columns and is read as v1. `<chart>_final.csv` mirrors
`imaging_final`.

**In short:** v1 = old `imaging_final` rows with only `provider_name` (credentials inside)
and `signature_present`; v2 = everything after a re-run, all provider columns filled when
the page is signed; with no signature `signature_present = false`, `provider_version = 2`,
an empty `signers`, and the page's provider in `provider_name` / `provider_credentials` /
confidence when a counted provider or credentials row names one (§1 rule 4).

No column is removed. A chart becomes v2 when `kv_extract` and `imaging_final` re-run.
The writers write only the columns a table has, so a database without the new columns
keeps working.

**Database changes:** [`schema/changes_2026-10-18.sql`](../schema/changes_2026-10-18.sql) —
additive, safe to re-run (`psql "$DATABASE_URL" -f schema/changes_2026-10-18.sql`). A new
database gets the same from `schema/v1.sql`.

---

## 3. How review-ui is powered

### 3.1 Where each mode reads from

| Mode (`DATA_MODE` in `review-ui/.env`) | Provider fields come from |
|---|---|
| **Production** | **`imaging_final`** (DB) only |
| **Local** | `<chart>_provider_signature.csv` in the workspace **only** — never the database, with or without one set |

In Local Mode every page result (provider, DOS, page type, …) comes from the chart's CSVs;
only reviewer input — ground truth, the member manifest, Run / Batch — is read from the
database when one is set. The accuracy report always reads the database view `v_accuracy_page` (which takes
`imaging_final`'s provider columns). Excel Compare reads `imaging_final` when a database
is set.

### 3.2 How a row is read — by `provider_version`

`signer_fields` in `review-ui/backend/app/services/imaging_overlays.py`:

| `provider_version` | Provider Name | Provider Credentials |
|---|---|---|
| **2**, `signers` has entries | every signer's name, in order | every signer's credentials, in the same order |
| **2**, no `signers` | `provider_name` | `provider_credentials` |
| **1** / NULL | `provider_name` with the credentials split off | the credentials split off `provider_name` (`provider_credentials` is not read) |

The name is always shown without credentials, and the credentials without dots
(`undotted`): `M.D.` → `MD`, `Ph.D.` → `PhD`, `F.N.P.-B.C.` → `FNP-BC`; a dot between two
longer credentials separates them (`APRN.CNP` → `APRN CNP`). The CSV and the database keep
the credentials as read; the accuracy match ignores dots either way.

### 3.3 What the page view shows (Page Details → Provider Extraction)

| Row | Extracted / Final | Confidence |
|---|---|---|
| **Provider Name** | one signer: the name. Several: one per line — `1. WILLIAM J WALL III` / `2. Jane Roe` | the page's provider confidence (`provider_confidence` / CSV `confidence`) |
| **Provider Credentials** | one signer: `MD`. Several: one per line — `1. MD` / `2. DNP` | the same |
| **Provider Signature** | `Yes` / `No` from `signature_present` | the same |

Ground truth for these rows is the page's `provider_credentials` / `provider_signature`
labels (`page_ground_truth`).

### 3.4 Accuracy and Excel Compare

| | Uses |
|---|---|
| **Accuracy — Provider Signature** | Every labelled page, Match / No Match: the ground truth's Provider Signature (Yes / No; credentials in the label mean Yes; NA / blank is No) against `signature_present`. Under **Signature = Yes** (cases = pages the ground truth or the pipeline calls signed): Precision, Recall and **Cred. Match** (in place of F1) — Recall on credentials: of the pages the ground truth gives credentials for, the share where the credentials of **every** signer (v2) joined "MD \| DNP", else those split off `provider_name` (v1), share any one credential with the ground truth's |
| **Excel Compare** | the same provider name and credentials, from `imaging_final` |

---

## 4. What else in the pipeline uses it

| Where | Reads | Effect |
|---|---|---|
| **Blank / junk R3** (pass 2) | `<chart>_provider_signature.csv` `signature_present = y`, or a signature phrase in the text | **Main** — except when DistilBERT and the keywords both say junk (JJ): the vote decides ([`BLANK_JUNK_LOGIC.md`](BLANK_JUNK_LOGIC.md)) |
| **Continuity** check 6 | the same CSV `signature_present = y`, or a signature block in the text | a signed page ends its document; an undated next page starts a new one ([`CONTINUITY_LOGIC.md`](CONTINUITY_LOGIC.md)) |

---

## 5. Known gaps

- **No keyless provider.** A name without a key ("— John Smith, MD" alone at the
  bottom) is found only through the signature extractor or not at all.
- **Keys that often fail** (from `utilities/harvest_keys.py --field provider` on the
  local charts): Author found 17× / 0 values, Provider 22 of 29 failed, Performed by
  7 / 7 failed; labels with no key yet: "Entered By", "Filed by", "Authorized by",
  "Adm. By", "Patient Care Team".

---

# Appendix — how the candidate rows are made

The extractors run inside `kv_extract` on the page's OCR words and write their
candidates to `staging/extraction.json`, each marked `accepted` (valid) and `selected`
(the extractor's best). §1 picks from these rows.

## A. Provider name extractor

**Keys** (`provider_name/keys.json`, OCR near-misses allowed): Provider, PCP, Author,
Attending (Physician / Provider), Ordering Physician / Provider, Performed by,
Interpreting Physician, Co-signer, Nurse Practitioner, Optometrist, Bill Under,
Progress Notes by, … Grouped into roles (`roles.json`): Physician, Nurse Practitioner,
CRNA, Pathologist, Anesthesiologist, Psychologist, Optometrist, Surgeon, Resident.

**The value must be a person's name**, with optional credential / designation tokens
around it. Anything else (a department, a phone number, "N/A") is rejected.

| Profile | Where the name is looked for |
|---|---|
| standard | a window near the key; a credential preferred, optional when the name model is sure |
| role / designation | anywhere; an ID in the full-width ±5% band, or the best person name scoring ≥ 0.70 |
| Bill Under | header / footer only |
| "Progress Note" (no "by") | only with a credential suffix |
| key after the value ("Allison Moosally, MD (Primary Provider)", "… as PCP") | the same-line words before the key |

**Kept for the page** (`credentialed_providers`): a name that starts with **Dr.** or
carries a **credential**, or sits next to a **Provider** label. A shorter copy of a
longer name is dropped. A page can keep more than one (provider and cosigner).

No keyless provider names: without a key there is no provider name.

## B. Electronic signature extractor

**Keys** (`electronic_signature/keys.json`): Electronically signed by, Signed
electronically by, Digitally signed by, Electronically approved by, Cosigned by,
Signed by, Signer, Verified by, Provider Signature, Signature, Sign Information, …

From the key's box (words in raw OCR order):

| Where the name is | Accepted when |
|---|---|
| right after the key | always (credential optional) |
| right before the key, nothing usable after | strong keys ("… by", "Electronically / Digitally …"): always; weak keys: needs a credential |
| elsewhere in the box | needs a credential |

- **Signer** = as printed, from the first name word to the last credential
  ("Agati, Alicia, APRN.CNP").
- **Designation** = the credential on its own ("APRN.CNP").
- **Signature date** = the first date after the name, else the date nearest the key;
  blank if none.

## C. Credentials — where they come from, and the lists

| Where credentials come from | How | Used for |
|---|---|---|
| A signature's `designation` | as the extractor read it ("MD", "APRN.CNP") | §1 step 1 |
| A signature's `provider_name` | the credential tokens in it ("WILLIAM J WALL III, MD" → MD) | §1 step 1, when `designation` is empty |
| A signature's `sentence` | every credential of `provider_credentials_canon.json` in it, in order | §1 step 2 |
| The page's `provider_credentials` rows (`accepted = true`) | the credentials extractor: a credential after a name on the same line; MD / PA / MA / DC dropped when followed by a ZIP code (a state); the provider = up to 4 words before it | §1 step 3b, one signature only |

**The lists:**

- `keyword-canon/provider_credentials_canon.json` — **38**, the ones searched for: MD, DO,
  MBBS, DM, DMD, DDS, DPM, OD, DC, PhD, PsyD, PharmD, DNP, NP, APRN, ARNP, CRNP, CNP, FNP,
  FNP-C, FNP-BC, AGACNP, PMHNP, PMHNP-BC, CNS, CNM, CRNA, PA, PA-C, RN, LPN, LCSW, LPC,
  LMFT, PT, DPT, OT, SLP. Not LVN or MA.
- `extraction/provider_name/suffixes.txt` — **227**, used to strip credentials off a name.
- `keyword-canon/provider_credentials_reference.json` — the client's **codable (66)** and
  **non-codable (143)** credentials with descriptions. Not read by the pipeline yet.

A token is a credential when it is in a list and printed in capitals (or its usual case:
PhD, PharmD): "Rd" (a road) and "Md" (a state) are not. Combined credentials count
("APRN.CNP", "MD/PhD"); "M.D." keeps its dots.
