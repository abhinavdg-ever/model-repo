# DOS extraction

One row per page, in the DB and in `imaging/<chart>_dos.csv`. The logic —
candidates, features, score, chart resolution — is described in
[`docs/LOGIC.md` § Date of service](../../../../docs/LOGIC.md#6-date-of-service).

Weights, label phrases and the three settings (`DOS_MAX_AGE_YEARS`,
`DOS_MIN_SCORE`, `DOS_DEFAULT_DATE`) live in
`../keyword-canon/dos_canon.json` and reload on change.

## CSV columns

```csv
chart_name,page_name,page_number,dos_from,dos_to,dos_from_iso,dos_to_iso,doc_dos_from,doc_dos_to,doc_dos_from_iso,doc_dos_to_iso,match_type,keyword,confidence,extraction_method
```

`confidence` is the chosen candidate's score (the inherited encounter's on a
page with no date of its own; 0 on the default).

## Debugging a pick

`DOS_DEBUG=true` writes `<chart>/debug/<chart>_dos_candidates.csv`: every
candidate with its feature vector, score and `chosen`.
