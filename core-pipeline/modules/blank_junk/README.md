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
| R4 signature | 2 | an Electronic Signature (kv_extract) → Main, unless p(junk) > 0.95 |
| R6 short page | 2 | content in the top 15%, nothing below → Junk / Others |
| R5 DistilBERT | 2 | p(junk) ≥ 0.85 → Junk; 0.50–0.85 → Junk for review; else Main |

After the ladder (`post_process`): a signature or provider details keep a junked page Main
(not a card). Duplicates are found in pass 2 (`stage._classify`, `classify.word_overlap`).
Continuity later junks a short page whose words are all on a nearby page
(`continuity/short_pages.py`).

Settings: `../../resources/canon/blank_junk_context_canon.json`.

| File | Role |
|---|---|
| `stage.py` | the two passes, duplicates, the CSV and staging JSON |
| `method.py` | the rule ladder and `post_process` |
| `card_rules.py` | ID / insurance card detection |
| `classify.py` | codes, labels, duplicate helpers (word overlap, window, fingerprint) |
| `model_bridge.py`, `model/src/` | DistilBERT's probabilities |
