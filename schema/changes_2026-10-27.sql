-- Database changes of 2026-10-27 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Additive only: nothing is
-- dropped or rewritten. Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-10-27.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-10-27.sql        (Windows PowerShell)

-- 1. page_ground_truth.encounter_label (2026-10-27): the page's encounter type
--    (Outpatient (F2F), Inpatient, ...) entered in Page Details. encounter_type keeps the
--    client sheet's Encounter Type column, which holds the page type. review-ui also adds
--    this column itself on its first ground-truth save.
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS encounter_label TEXT;

-- 2. The other Page Details cells now saved (2026-10-27). In v1.sql already; added here
--    for a database made before them.
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS tilt TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS quality TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS mirrored TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS printed_or_handwritten TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS handwritten_pct TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS is_duplicate TEXT;
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS is_visible TEXT;
