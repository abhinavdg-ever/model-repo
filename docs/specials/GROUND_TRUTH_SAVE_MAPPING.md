# Ground truth: what a review-ui save writes

Date: 2026-11-14. From `review-ui/frontend/src/ImagingPanel.tsx` (form → request:
`groundTruthBody`, `blankJunkStored`, `visibilityStored`, `presenceStored`) and
`review-ui/backend/app/services/ground_truth.py` (request → database: `upsert_page_ground_truth`,
`clean_label`, `yes_no_label`, `spell_codeable`).

## Every Page Details save

| Table | What happens |
|---|---|
| `page_ground_truth` | One row per (`chart_name`, `page_number`), upserted. The columns below are set; `source_page_id` = the page file name; `user_id` and `updated_at` stamped. `page_sequence`, `deleted_level`, `source_path` are left as they were |
| `page_ground_truth_entry` | One new row: `entry_mode = 'manual_entry'`, `labels` = JSON of all 25 columns as saved, `user_id` |

Every field: blank / `NA` / `N/A` / `Not Available` → **NULL**; `Skipped` → **`Skipped`**, except: Member Name / DOB / ID have no Skipped choice and store blank / NA / NULL as **`Not Found`**; the Provider rows have no Skipped choice (Signature blank → `No`; Credentials blank → `Not Found`; Name blank → NULL, shown as Not Found, free text and not matched) (2026-11-14).

## Page Details → `page_ground_truth`

| Metric (UI row) | UI value | Table | Column | Stored value |
|---|---|---|---|---|
| Member Name | Yes | page_ground_truth | `member_name` | `Yes` |
| Member Name | No | page_ground_truth | `member_name` | `No` |
| Member Name | Not Found | page_ground_truth | `member_name` | `Not Found` |
| Member Name | (no Skipped choice since 2026-11-14; an old Skipped) | page_ground_truth | `member_name` | `Not Found` |
| Member Name | (blank) | page_ground_truth | `member_name` | `Not Found` (since 2026-11-14) |
| Member DOB | Yes / No / Not Found / (blank) | page_ground_truth | `member_dob` | `Yes` / `No` / `Not Found` / `Not Found` |
| Member ID | Yes / No / Not Found / (blank) | page_ground_truth | `member_id` | `Yes` / `No` / `Not Found` / `Not Found` |
| Type (Printed/HW) | Printed / Handwritten / Form / Visual / Blank | page_ground_truth | `printed_or_handwritten` | as chosen |
| Handwritten % | Negligible (~0%) / Low (<10%) / Medium (~25%) / High (50% or higher) | page_ground_truth | `handwritten_pct` | the label text |
| Visibility | Visible | page_ground_truth | `is_visible` | `Yes` |
| Visibility | Not visible | page_ground_truth | `is_visible` | `No` |
| Visibility | (blank) | page_ground_truth | `is_visible` | NULL |
| Quality | High / Medium / Low | page_ground_truth | `quality` | `High` / `Medium` / `Low` |
| Text Orientation | 0° / 90° / 180° / 270° (or any angle) | page_ground_truth | `rotation` | the number without °: `0` / `90` / `180` / `270` |
| Tilt (Text) | free text | page_ground_truth | `tilt` | as typed |
| Mirrored (Text) | Yes / No | page_ground_truth | `mirrored` | `Yes` / `No` |
| Encounter Type | Outpatient (F2F) / Outpatient (Tele) / Inpatient / Home | page_ground_truth | `encounter_label` | as chosen |
| Encounter Type | Not Available | page_ground_truth | `encounter_label` | NULL |
| Is Blank or Junk? | Yes (Blank) | page_ground_truth | `blank_page` | `Yes` |
| | | page_ground_truth | `junk_page` | `No` |
| | | page_ground_truth | `is_invoice` | `No` |
| Is Blank or Junk? | Yes (Junk) | page_ground_truth | `blank_page` | `No` |
| | | page_ground_truth | `junk_page` | `Yes` |
| | | page_ground_truth | `is_invoice` | `No` |
| Is Blank or Junk? | No | page_ground_truth | `blank_page` / `junk_page` / `is_invoice` | `No` / `No` / `No` |
| Is Blank or Junk? | (blank) | page_ground_truth | `blank_page` / `junk_page` / `is_invoice` | NULL / NULL / NULL |
| Is Duplicate | Yes | page_ground_truth | `is_duplicate` | `Yes` (the pipeline's "Yes (to page N)" is saved as `Yes`) |
| Is Duplicate | Partial | page_ground_truth | `is_duplicate` | `Partial` |
| Is Duplicate | No | page_ground_truth | `is_duplicate` | `No` |
| Page Type | an L1 / client label / new type | page_ground_truth | **`encounter_type`** | the page type (**the Accuracy View reads this column**) |
| Page Type | (same save) | page_ground_truth | `page_type` | the client sheet's own value if the page has one (e.g. Accept / Unaccept), else the same page type |
| Page Subtype | an L2 / new subtype | page_ground_truth | `page_subtype` | as chosen |
| Specialty | a specialty / new one | page_ground_truth | `provider_specialty` | as chosen |
| Is Codable Or Non-Codable | Codable | page_ground_truth | `codeable` | `Codable` |
| Is Codable Or Non-Codable | Non-Codable | page_ground_truth | `codeable` | `Non-Codable` (any spelling: Codeable, Non Codeable … is stored this way) |
| DOS From | a date | page_ground_truth | `dos_from` | `YYYY-MM-DD` |
| DOS To | a date | page_ground_truth | `dos_to` | `YYYY-MM-DD` |
| Provider Name | free text (no Skipped choice; blank = Not Found) | page_ground_truth | `rendering_provider` | as typed; several signers' `1. Dr A` / `2. Dr B` → `Dr A, Dr B`; blank / Skipped → NULL (shows Not Found) |
| Provider Credentials | one or more (MD, DO, NP …) (no Skipped choice) | page_ground_truth | `provider_credentials` | as typed, comma separated; `1. MD` / `2. NP` → `MD, NP`; blank / NA / Skipped → `Not Found` (2026-11-14) |
| Provider Signature | Yes | page_ground_truth | `provider_signature` | `Yes` |
| Provider Signature | No | page_ground_truth | `provider_signature` | `No` |
| Provider Signature | (no Skipped choice since 2026-11-14; an old Skipped) | page_ground_truth | `provider_signature` | `No` |
| Provider Signature | (blank) | page_ground_truth | `provider_signature` | `No` (no signature found is No) |

Notes:

- **Not Found** is a real answer only for Member Name / DOB / ID (stored as the text `Not Found`). On every other field it means empty and is stored as NULL.
- **Is Invoice** has no row of its own: a **Page Type of Invoice sets it to `Yes`** (2026-11-14); otherwise Blank / Junk sets it (`No` whenever Blank / Junk is answered). It can also come from a spreadsheet load or `utilities/gt_replace_value.py`.
- **No Skipped answer** on any row (2026-11-14): Skipped is saved as an empty cell (Member: Not Found; Signature: No).

## Other tabs (chart level)

| Tab | Table | Column(s) | Stored value |
|---|---|---|---|
| Sequencing | `page_ground_truth` (+ a `page_ground_truth_entry` manual_entry) | `page_sequence` | `1`, `2`, `3` … for main pages; `J1`, `J2` … for blank / junk pages |
| Encounter | `encounter_review` | `seq`, `start_page`, `end_page`, `page_type`, `encounter_type`, `dos_from`, `dos_to`, `user_id` | one row per encounter; a save replaces the chart's rows |
| Rejection Rules | `chart_ground_truth` | `member_decision`, `member_decision_reason`, `user_id` | `accept` / `reject` and the reason |
| Mark Reviewed | `chart_review` (+ `chart_review_entry` log: `action`, `fields`) | `user_id`, `created_at` | who marked the chart and when |

All are keyed by **chart_name**, so they survive deleting a chart's pipeline results.

## On Cosmos (`DB_BACKEND=cosmos`)

| Postgres | Cosmos `ground_truth` container (partition `/chart_name`) |
|---|---|
| `page_ground_truth` row | document `page-<page_number>`: the same column names and values |
| `page_ground_truth_entry` rows | `entries[]` inside that document |
| `chart_review` / `chart_review_entry` / `chart_ground_truth` / `encounter_review` | document `chart`: `chart_review`, `review_entries`, `chart_ground_truth`, `encounter_review` |
