-- Database changes of 2026-11-07 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-11-07.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-11-07.sql        (Windows PowerShell)

-- 1. chart_list.tags (2026-11-07): free-text tags on a chart, a JSON array of strings, set
--    with utilities/tag_chart.py. review-ui's History and Accuracy filter on them.
ALTER TABLE chart_list ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;
