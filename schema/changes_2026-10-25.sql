-- Database changes of 2026-10-25 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Additive only: nothing is
-- dropped or rewritten. Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-10-25.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-10-25.sql        (Windows PowerShell)

-- 1. imaging_final.confidences / review_fields (2026-10-25): every Final value's
--    confidence on one scale (>= 0.80 good, 0.50-0.80 passable, < 0.50 review;
--    core-pipeline/stages/lib/confidence.py) and the names of the values below the review
--    line. Until this runs, imaging_final is written without them (the stage logs it).
ALTER TABLE imaging_final ADD COLUMN IF NOT EXISTS confidences JSONB;
ALTER TABLE imaging_final ADD COLUMN IF NOT EXISTS review_fields JSONB;
