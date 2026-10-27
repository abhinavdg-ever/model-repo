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
