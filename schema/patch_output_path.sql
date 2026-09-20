-- =====================================================================
-- Patch: chart_list.output_path + stop writing status=rejected
-- =====================================================================
-- Apply on an existing V1 database (recreate from v1.sql is also fine):
--
--     psql "$DATABASE_URL" -f schema/patch_output_path.sql
--
-- New column: output_path — Processed/Run1/Batch1/<chart> derived from
-- Raw_Input/Run1/Batch1/DEID_*/ when the chart is ingested.
-- Also remaps any legacy chart_list.status = 'rejected' → 'completed'
-- (accept/reject stays on member_verification_summary).
-- =====================================================================

SET client_min_messages TO WARNING;

ALTER TABLE chart_list
    ADD COLUMN IF NOT EXISTS output_path TEXT;

UPDATE chart_list
   SET status = 'completed'
 WHERE status = 'rejected';

COMMENT ON COLUMN chart_list.output_path IS
    'Blob/local write destination for this chart, e.g. Processed/Run1/Batch1/<chart_name>';
