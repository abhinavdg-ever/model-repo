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
| Outputs | `imaging/<chart>_provider_signature.csv`; `imaging_final.provider_name`, `signature_present` |

---

## 1. Summary — the rule (2026-10-18, provider_version 2)

1. **Provider Signature = Yes** when the page has an `electronic_signature` row with
   **`accepted = true`**; otherwise **No**.
2. **No** ⇒ no provider name, no credentials.
3. **Yes** ⇒ **every accepted signature is a signer**, listed in order: the **selected**
   one first (the highest-scoring when several are selected, else the highest-scoring
   accepted one), then the others by score. A signer with no name is left out; the same
   person twice is one signer. No roles: review-ui numbers them 1. 2. 3.

Each signer's name and credentials, first found wins:

| Step | Name | Credentials | For |
|---|---|---|---|
| 1. The signature row's fields | its `provider_name`, credentials stripped | its `designation`; else the credentials that were in the name | every signer |
| 2. The signature row's `sentence` | the words right after the key, up to " on ", " at ", a date or a time (two words at least) | the credentials among them | every signer |
| 3. The page's accepted `provider_credentials` | — | the one for the same provider | every signer |
| 3b. | — | else the first accepted | the first signer only |
| 4. The page's `provider_name` rows | the best-scoring (selected first), credentials stripped | its credentials, if still none | the first signer only |

Examples:

| Signature rows | Signers |
|---|---|
| "WILLIAM J WALL III, MD", designation MD (selected) | 1. WILLIAM J WALL III · MD |
| "Meghan Newman", no designation, sentence "Electronically signed by Meghan Newman , DNP on 05/19/2025 …" | 1. Meghan Newman · DNP (step 2) |
| "Jane Roe, DNP" (accepted, score 0.95) + "WILLIAM J WALL III, MD" (selected, 0.90) | 1. WILLIAM J WALL III · MD  2. Jane Roe · DNP |
| key "Signed", sentence "Electronically Signed", no name; page provider "Daniel J Hamilton, MD" | 1. Daniel J Hamilton · MD (step 4) |
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

Two places, used differently:

| | How | Used by |
|---|---|---|
| **On the chosen provider** | the credential tokens at the end of the provider name (`signature_people` splits "Ramsey Luce, R.N." into "Ramsey Luce" + "R.N.") | **review-ui** (page view, accuracy, Excel compare) |
| `provider_credentials` field (staging only) | every credential in `provider_credentials_canon.json` (38: MD, DO, DMD, DDS, DPM, OD, DC, PhD, PsyD, PharmD, DNP, NP, PA, PA-C, RN, LPN, APRN, FNP, CRNA, CNP …; not LVN or MA) after a name on the same line; MD / PA / MA / DC dropped when followed by a ZIP code (a state, not a credential); the provider = up to 4 words before it, back to a colon | the extraction pane only |

## 5. Where it is stored — one row per page

`kv_extract` turns the staging (`staging/extraction.json`) into these, and **everything
downstream reads them, never the staging**:

| Store | Read by |
|---|---|
| **`provider_signature_results`** (DB, v1.sql) | `imaging_final` |
| `imaging/<chart>_provider_signature.csv` (same values) | blank/junk R3 and continuity (`signature_present = y`); review-ui Local Mode |
| `imaging_final` (DB) / `<chart>_final.csv` — copied by `imaging_final` | review-ui Production Mode, accuracy, Excel Compare |

| Column | Value |
|---|---|
| `signature_present` | an accepted signature (`y` / `n` in the CSV) |
| `provider_name` | the first signer's name, **without** credentials |
| `provider_credentials` | the first signer's credentials |
| `signers` | every signer: `[{"name", "credentials", "signature_date"}]` (JSON) |
| `provider_version` | **2** — this rule |
| `signature_key`, `sentence`, `region`, `scale`, `ner_text`, `source`, `signature_date`, `confidence` | the first signature's |

### Versions — old and new rows side by side

| `provider_version` | Written by | review-ui reads |
|---|---|---|
| **2** | this rule | `signers` (numbered 1. 2. 3.), else `provider_name` + `provider_credentials`; credentials for accuracy = all signers' |
| **1** or none | before 2026-10-18 | `provider_name` alone, credentials split off it (`provider_credentials` ignored) |

No column is removed; a chart switches to v2 when `kv_extract` and `imaging_final` re-run.
The writers write only the columns a table has, so a database without the new columns
(`schema/temp_changes_to_db.sql` not applied) keeps working.

---

## 6. What uses it

| Where | Uses | Effect |
|---|---|---|
| **Blank / junk R3** (pass 2) | `signature_present = y`, or a signature phrase in the text | **Main** — except when DistilBERT and the keywords both say junk (JJ): the vote decides ([`BLANK_JUNK_LOGIC.md`](BLANK_JUNK_LOGIC.md)) |
| **Continuity** check 6 | `signature_present = y`, or a signature block in the text | a signed page ends its document; an undated next page starts a new one ([`CONTINUITY_LOGIC.md`](CONTINUITY_LOGIC.md)) |
| **imaging_final** | `provider_signature_results` | the Final provider, credentials, signers, version (each page's own; a duplicate takes its original's) |
| **review-ui page view** | Production: `imaging_final`. Local: `provider_signature.csv` | Provider Name and Provider Credentials — numbered 1. 2. 3. with several signers (by `provider_version`, §5) — and Provider Signature Yes / No |
| **Accuracy — Provider Credentials** | every signer's credentials from `imaging_final` (v2), else split from `provider_name` (v1) | every page: any one credential shared = Match; a ground truth of NA / blank / None is Not Found — none extracted = Match, some = No Match |

---

## 7. Known gaps

- **Confidence is not in `imaging_final`.** It is in `provider_signature_results` and
  the CSV; Production Mode shows NA as the provider confidence.
- **No keyless provider.** A name without a key ("— John Smith, MD" alone at the
  bottom) is found only through the signature extractor or not at all.
- **Keys that often fail** (from `utilities/harvest_keys.py --field provider` on the
  local charts): Author found 17× / 0 values, Provider 22 of 29 failed, Performed by
  7 / 7 failed; labels with no key yet: "Entered By", "Filed by", "Authorized by",
  "Adm. By", "Patient Care Team".
