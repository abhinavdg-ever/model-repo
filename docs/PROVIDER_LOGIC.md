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

## 1. Summary

1. Three extractors read the page's OCR words: **provider name** (from a key such as
   "Provider:", "Attending", "Ordering Physician"), **e-signature** (from "Electronically
   signed by", "Signed by" …) and **credentials** (MD, DO, NP … after a name).
2. A trained model scores each candidate; the rules decide what is acceptable.
3. Per page, **one provider** is chosen — the best-scoring signer or provider name —
   and **signature present** is set when a signature was found.
4. That one row per page is the stage's answer: `<chart>_provider_signature.csv`, then
   `imaging_final`.

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

## 5. One row per page — `signature_fields`

| Column | Value |
|---|---|
| `provider_name` | among the signature names and the kept provider names, the one with the **highest score**; a tie goes to the earliest, so a signer beats a provider-name hit of equal score. Credentials kept in capitals |
| `signature_present` | **y** when an accepted signature has a name, a date or a model hit; **n** otherwise (provider-name hits alone never make it **y**) |
| `signature_key`, `sentence`, `region`, `ner_text`, `source` | from the first signature |
| `signature_date` | the signature's date, as ISO |
| `confidence` | the signature's score |

Written to `imaging/<chart>_provider_signature.csv`. The `imaging_final` stage reads
the same rows into `imaging_final.provider_name` and `signature_present` — the CSV and
the table hold the same values.

---

## 6. What uses it

| Where | Uses | Effect |
|---|---|---|
| **Blank / junk R3** (pass 2) | `signature_present = y`, or a signature phrase in the text | **Main** — except when DistilBERT and the keywords both say junk (JJ): the vote decides ([`BLANK_JUNK_LOGIC.md`](BLANK_JUNK_LOGIC.md)) |
| **Continuity** check 6 | `signature_present = y`, or a signature block in the text | a signed page ends its document; an undated next page starts a new one ([`CONTINUITY_LOGIC.md`](CONTINUITY_LOGIC.md)) |
| **imaging_final** | `provider_name`, `signature_present` | the Final provider (each page's own; a duplicate takes its original's) |
| **review-ui page view** | Production: `imaging_final`. Local: `provider_signature.csv` | Provider Name, Provider Credentials (split from the name), Provider Signature Yes / No |
| **Accuracy — Provider Credentials** | the credentials split from `imaging_final.provider_name` | every page: any one credential shared = Match; a ground truth of NA / blank / None is Not Found — none extracted = Match, some = No Match |

---

## 7. Known gaps

- **Confidence and signature date are CSV-only.** `imaging_final` has no columns for
  them, so Production Mode shows NA as the provider confidence.
- **No keyless provider.** A name without a key ("— John Smith, MD" alone at the
  bottom) is found only through the signature extractor or not at all.
- **Keys that often fail** (from `utilities/harvest_keys.py --field provider` on the
  local charts): Author found 17× / 0 values, Provider 22 of 29 failed, Performed by
  7 / 7 failed; labels with no key yet: "Entered By", "Filed by", "Authorized by",
  "Adm. By", "Patient Care Team".
