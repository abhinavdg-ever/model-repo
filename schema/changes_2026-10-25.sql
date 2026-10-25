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

-- 2. Production Mode reads every output from Postgres (2026-10-25); before this, three
--    values came only from the chart workspace's staging files.
--    a. additional_page_details.extraction_fields: every field the extraction staged for
--       the page (the Extraction tab's member, DOS, provider, signature rows).
ALTER TABLE additional_page_details
    ADD COLUMN IF NOT EXISTS extraction_fields JSONB NOT NULL DEFAULT '{}'::jsonb;

--    b. page_continuity_results.label: the continuity label as shown ('Continue? (Doc 3)').
ALTER TABLE page_continuity_results ADD COLUMN IF NOT EXISTS label VARCHAR(60);

--    c. encounter_type_results: the encounter each page belongs to and how its setting was
--       decided; every page of an encounter carries the same values, and review-ui's Doc
--       Summary groups the pages by encounter_seq.
ALTER TABLE encounter_type_results
    ADD COLUMN IF NOT EXISTS encounter_seq INT,
    ADD COLUMN IF NOT EXISTS decided_by VARCHAR(40),
    ADD COLUMN IF NOT EXISTS reason TEXT,
    ADD COLUMN IF NOT EXISTS conflict BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS contenders JSONB NOT NULL DEFAULT '[]'::jsonb;

--    d. encounter_results, a separate per-encounter table this file created for a few
--       hours on 2026-10-25, merged into encounter_type_results above (user). Nothing
--       read it yet; dropped where an earlier copy of this file created it.
DROP TABLE IF EXISTS encounter_results;
