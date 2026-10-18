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
| Outputs | DB `provider_signature_results` + `imaging/<chart>_provider_signature.csv` (kv_extract); DB `imaging_final` + `imaging/<chart>_final.csv` (imaging_final) — §5 |
| review-ui | Production: `imaging_final`; Local: `imaging_final` and the CSV, or the CSV alone — §6 |
| Database changes | [`schema/changes_2026-10-18.sql`](../schema/changes_2026-10-18.sql) |

---

## 1. Summary — the rule (2026-10-18, provider_version 2)

1. **Provider Signature = Yes** when the page has an `electronic_signature` row with
   **`accepted = true`**; otherwise **No**.
2. **No** ⇒ no provider name, no credentials.
3. **Yes** ⇒ every accepted signature is a candidate signer, in order: the **selected**
   one first (the highest-scoring when several are selected, else the highest-scoring
   accepted one), then the others by score. No roles: review-ui numbers them 1. 2. 3.

For each accepted signature:

| Step | Name | Credentials |
|---|---|---|
| 1. The signature row's fields | its `provider_name`, credentials stripped | its `designation`; else the credentials written in `provider_name` |
| 2. Still missing — the same row | its `ner_text` | searched in its `sentence` with the credentials canon (`provider_credentials_canon.json`) |
| 3a. **Several accepted signatures** | stop here: a signer still missing its name **or** its credentials is removed | |
| 3b. **One accepted signature** | the page's `provider_name` rows with `accepted = true` (the best-scoring), credentials stripped | the page's `provider_credentials` rows with `accepted = true` (the same provider's first, else the first) |

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
| one signature, key "Signed", no name; page `provider_name` "Daniel J Hamilton, MD" (accepted) | 1. Daniel J Hamilton · MD (step 3b) |
| none accepted | Signature No; no name, no credentials |

A credential is a token in `provider_name/suffixes.txt` or `provider_credentials_canon.json`,
printed in capitals (or its usual case: PhD, PharmD); combined ones count ("APRN.CNP",
"MD/PhD"); "M.D." keeps its dots. A mis-cased one ("Rd", "Md") is dropped from the name.

Code: `signature_fields`, `split_name_credentials`, `name_from_sentence` in
`extraction/results.py`; `page_signature` in `extraction/stage.py`.

---

## 2. Provider name

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

## 3. Electronic signature

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

## 4. Credentials

| Where credentials come from | How | Used for |
|---|---|---|
| A signature's `designation` | as the extractor read it ("MD", "APRN.CNP") | step 1 (§1) |
| A signature's `provider_name` | the credential tokens in it ("WILLIAM J WALL III, MD" → MD) | step 1, when `designation` is empty |
| A signature's `sentence` | every credential of `provider_credentials_canon.json` in it, in order | step 2 |
| The page's `provider_credentials` rows (`accepted = true`) | the credentials extractor: a credential after a name on the same line; MD / PA / MA / DC dropped when followed by a ZIP code (a state); the provider = up to 4 words before it | step 3b, one signature only |

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

---

## 5. What it writes

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

### 5.1 `provider_signature_results` (DB) and `<chart>_provider_signature.csv`

Written by `kv_extract`, one row per page, the same values in both.

| Column | Value | In DB | In CSV |
|---|---|---|---|
| `chart_id`, `page_id` | the page | yes | — |
| `chart_name`, `page_name`, `page_number` | the page | — | yes |
| `signature_present` | an accepted electronic signature | `true` / `false` | `y` / `n` |
| `provider_name` | the first signer's name, **without** credentials | yes | yes |
| `provider_credentials` | the first signer's credentials | yes | yes |
| `signers` | every signer, in order: `[{"name", "credentials", "signature_date", "confidence"}]` | JSONB | JSON text |
| `provider_version` | **2** (this rule) | yes | yes |
| `confidence` | the page's confidence (§1) | yes | yes |
| `signature_date` | the first signer's date (ISO) | yes | yes |
| `signature_key`, `sentence`, `region`, `scale`, `ner_text`, `source` | the first (selected) signature's | yes | yes |

### 5.2 `imaging_final` (DB) and `<chart>_final.csv`

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

### 5.3 Old and new rows side by side

| `provider_version` | Rows written | `provider_name` holds | `provider_credentials`, `signers` |
|---|---|---|---|
| **2** | from 2026-10-18 | the bare name | filled |
| **1** or NULL | before | the name **with** its credentials ("Ramsey Luce, R.N.") | empty (or no such column) |

No column is removed. A chart becomes v2 when `kv_extract` and `imaging_final` re-run.
The writers write only the columns a table has, so a database without the new columns
keeps working.

**Database changes:** [`schema/changes_2026-10-18.sql`](../schema/changes_2026-10-18.sql) —
additive, safe to re-run (`psql "$DATABASE_URL" -f schema/changes_2026-10-18.sql`). A new
database gets the same from `schema/v1.sql`.

---

## 6. How review-ui is powered

### 6.1 Where each mode reads from

| Mode (`DATA_MODE` in `review-ui/.env`) | Provider fields come from |
|---|---|
| **Production** | **`imaging_final`** (DB) only |
| **Local, with a database** | `imaging_final` (DB), then `<chart>_provider_signature.csv` from the workspace on top |
| **Local, no database** | `<chart>_provider_signature.csv` only |

The accuracy report always reads the database view `v_accuracy_page` (which takes
`imaging_final`'s provider columns). Excel Compare reads `imaging_final` when a database
is set.

### 6.2 How a row is read — by `provider_version`

`signer_fields` in `review-ui/backend/app/services/imaging_overlays.py`:

| `provider_version` | Provider Name | Provider Credentials |
|---|---|---|
| **2**, `signers` has entries | every signer's name, in order | every signer's credentials, in the same order |
| **2**, no `signers` | `provider_name` | `provider_credentials` |
| **1** / NULL | `provider_name` with the credentials split off | the credentials split off `provider_name` (`provider_credentials` is not read) |

The name is always shown without credentials.

### 6.3 What the page view shows (Page Details → Provider Extraction)

| Row | Extracted / Final | Confidence |
|---|---|---|
| **Provider Name** | one signer: the name. Several: `1. WILLIAM J WALL III   2. Jane Roe` | the page's provider confidence (`provider_confidence` / CSV `confidence`) |
| **Provider Credentials** | one signer: `MD`. Several: `1. MD   2. DNP` | the same |
| **Provider Signature** | `Yes` / `No` from `signature_present` | the same |

Ground truth for these rows is the page's `provider_credentials` / `provider_signature`
labels (`page_ground_truth`).

### 6.4 Accuracy and Excel Compare

| | Uses |
|---|---|
| **Accuracy — Provider Credentials** | the credentials of **every** signer (v2) joined "MD \| DNP", else those split off `provider_name` (v1). Every page: any one credential shared with the ground truth = Match; a ground truth of NA / blank / None is Not Found — none extracted = Match, some = No Match |
| **Excel Compare** | the same provider name and credentials, from `imaging_final` |

---

## 7. What else in the pipeline uses it

| Where | Reads | Effect |
|---|---|---|
| **Blank / junk R3** (pass 2) | `<chart>_provider_signature.csv` `signature_present = y`, or a signature phrase in the text | **Main** — except when DistilBERT and the keywords both say junk (JJ): the vote decides ([`BLANK_JUNK_LOGIC.md`](BLANK_JUNK_LOGIC.md)) |
| **Continuity** check 6 | the same CSV `signature_present = y`, or a signature block in the text | a signed page ends its document; an undated next page starts a new one ([`CONTINUITY_LOGIC.md`](CONTINUITY_LOGIC.md)) |

---

## 8. Known gaps

- **No keyless provider.** A name without a key ("— John Smith, MD" alone at the
  bottom) is found only through the signature extractor or not at all.
- **Keys that often fail** (from `utilities/harvest_keys.py --field provider` on the
  local charts): Author found 17× / 0 values, Provider 22 of 29 failed, Performed by
  7 / 7 failed; labels with no key yet: "Entered By", "Filed by", "Authorized by",
  "Adm. By", "Patient Care Team".
