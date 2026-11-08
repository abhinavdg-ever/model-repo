# Blank / junk / duplicate

The stage (`stage.py`, pass 1 at seq 30 and pass 2 at seq 60) decides each page with
`method.decide`, a rule ladder, then DistilBERT:

| Rule | Pass | Verdict |
|---|---|---|
| R1 blank / tiny | 1, 2 | no ink or no text → Blank; under 15 characters → Junk / Cover Page or Others |
| R2 placeholder | 1, 2 | "intentionally left blank" on ≤ 25 words → Blank |
| R3 card | 1, 2 | a strong ID / insurance card (`card_rules.detect`) on a printed or visual page → Junk / Cards |
| R3V visual | 1, 2 | a visual page under 20 words that is not a card → Main |
| pass 1 | 1 | DistilBERT p(keep) > 0.95 → Main, final; p(junk) > 0.95 → Junk, final; else Main for now |
| R4 signature | 2 | an Electronic Signature (kv_extract), none on the previous / next non-blank page, raw p(junk) < 0.95 → Main |
| FAX | 2 | Fax in the section headers and no signature → Junk / Letter/Fax |
| INDEX | 2 | "Index" / "Contents" as a title and 3+ lines with page numbers → Junk / Others (2026-11-08) |
| short page | 2 | content in the top 20% of the page: see below |
| R5 classifier | 2 | the service's JUNK (raw p(keep) < 0.99) → Junk / its subtype; else Main |

The short page (2026-11-08, `stage._classify`; reason `short_page:<rule>`), on pages that got
past the earlier rules; "before" / "after" = the previous / next non-blank page; first that applies:

1. The page before ends in its Electronic Signature (kv_extract: footer band, or its signature
   sentence in the last paragraph) and this page has the provider's name / credentials or a
   date → Main.
2. "End of Report" on it → Junk / Cover Page.
3. Fewer than 5 distinct words not on the page before or after → Junk / Others (unless they are
   only "Diagnosis" / "Visit Diagnosis").

Full logic in one place: `docs/specials/BLANK_JUNK_LOGIC.md`.

After the ladder (`post_process`): lab results, ICD-10 codes / Social History keep a junked page
Main; an invoice is Main only with an amount and a date. Duplicates are found in pass 2
(`stage._classify`, `classify.word_overlap`).

Settings: `../../resources/canon/blank_junk_context_canon.json`.

| File | Role |
|---|---|
| `stage.py` | the two passes, duplicates, the CSV and staging JSON |
| `method.py` | the rule ladder and `post_process` |
| `card_rules.py` | ID / insurance card detection |
| `classify.py` | codes, labels, duplicate helpers (word overlap, window, fingerprint) |
| `model_bridge.py`, `model/src/` | DistilBERT's probabilities |
