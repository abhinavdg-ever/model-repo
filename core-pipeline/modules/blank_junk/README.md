# Blank / junk / duplicate

The stage (`stage.py`, pass 1 at seq 30 and pass 2 at seq 60) decides each page with
`method.decide`, one rule ladder (first rule that fires decides; nothing runs after it), then
the short page and duplicates in `stage._classify`.

**The full logic, for people: [`docs/specials/BLANK_JUNK_LOGIC.md`](../../../docs/specials/BLANK_JUNK_LOGIC.md).**

| Rule (reason starts) | Pass | Verdict |
|---|---|---|
| R1, R2, R1E | 1, 2 | blank / placeholder → Blank; cover mark or "End of Report" in < 10 words → Junk / Cover Page; < 15 characters → Junk / Others |
| R3V, HW | 2 | visual < 20 words (not a card), > 30% handwritten → Main |
| R4 | 2 | Electronic Signature, none on the previous / next non-blank page, raw p(junk) < 0.95 → Main |
| LAB, CLINICAL_CONTENT | 2 | lab result words; 2+ ICD-10 codes or Social History → Main |
| R3 | 2 | card → Junk / Cards |
| FAX, INDEX | 2 | fax header unsigned → Junk / Letter/Fax; contents page → Junk / Others |
| `short_page:` | 2 | content in the top 20%: signature carried on → Main; < 5 words not on the previous / next page → Junk / Others |
| R5, INVOICE | 2 | the classifier; an invoice is Main, a terms page (T&C, Terms and Conditions, Payment Terms) Junk / Others |

Duplicates: `dup_rules.py` (2026-11-11, docs/specials/BLANK_JUNK_LOGIC.md): Duplicate of N, Copy of N, or nothing.
Continuity's unjunk inside a Progress Note is off (`continuity/stage.UNJUNK_IN_NOTES`).

Settings: `../../resources/canon/blank_junk_context_canon.json`.

| File | Role |
|---|---|
| `stage.py` | the two passes, duplicates, the CSV and staging JSON |
| `method.py` | the rule ladder |
| `card_rules.py` | ID / insurance card detection |
| `classify.py` | codes, labels, word helpers |
| `dup_rules.py` | the duplicate rules (body text, hard blocks, exact, runs, conflicts, page images) |
| `model_bridge.py`, `model/src/` | DistilBERT's probabilities |
