-- Database changes of 2026-10-22 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Additive only: nothing is
-- dropped or rewritten. Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-10-22.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-10-22.sql        (Windows PowerShell)

-- 1. blank_junk_classification.duplicate_confidence (2026-10-22): each page's highest
--    duplicate similarity, whatever its verdict, for review-ui's Duplicate confidence.
--    The blank_junk stage also adds the column on its first write. Same definition as
--    schema/v1.sql.
ALTER TABLE blank_junk_classification ADD COLUMN IF NOT EXISTS duplicate_confidence NUMERIC(5,4);

-- The final-verdict view carries it, appended so the view can be replaced in place.
-- Until this runs, review-ui reads no score and shows the earlier confidence.
CREATE OR REPLACE VIEW v_page_blank_junk_final AS
SELECT chart_id, page_id, blank_junk_flag, junk_subtype, duplicate_of_page_id,
       ocr_source, pass_no, confidence, reason, updated_at,
       duplicate_confidence
FROM blank_junk_classification
WHERE is_final;
