# DOS extraction (imaging pipeline)

One **CSV row per page**.

## Scan window

- Scans the **full page** OCR (DOS may appear in the middle, not only top/bottom).
- Still uses visit / Admit / Discharge keywords + date patterns.
- Admit + Discharge → `dos_from` / `dos_to`; single date → from == to.
- **Multiple dates in the same year** → listed comma-separated on that page.

## Confidence

| Case | Confidence |
|------|------------|
| Found in **first or last ~60 words** | **95%** (`0.95`) |
| Found only in middle (full-page, not in edges) | **70%** (`0.70`) |
| Multiple same-year dates on the page | **60%** (`0.60`) |
| Hardcoded default **`02-02-2022`** (preamble / non-encounter) | **80%** (`0.80`) |

## Page vs document DOS

| Fields | When filled |
|--------|-------------|
| `dos_from` / `dos_to` | Only if DOS extracted **on that page**; else blank |
| `doc_dos_from` / `doc_dos_to` | Document-level: carry forward last encounter; before first DOS or immunization-like pages → `02-02-2022` @ 80% |

## CSV columns

```csv
chart_name,page_name,page_number,dos_from,dos_to,dos_from_iso,dos_to_iso,doc_dos_from,doc_dos_to,doc_dos_from_iso,doc_dos_to_iso,confidence
```

The Imaging UI maps `confidence` → `dosConfidence` (Page Details + Doc Summary “With Confidence”).

## Run

```bash
cd 02-imaging-pipeline/dos-extraction
python extract_dos.py
python extract_dos.py --llm
```
