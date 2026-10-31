-- Database changes of 2026-10-31 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-10-31.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-10-31.sql        (Windows PowerShell)

-- 1. blank_junk_classification.junk_subtype gains 'Cards' (2026-10-31): an ID or
--    insurance card found by the blank / junk rule R3C. The CHECK is replaced with the
--    same list plus Cards; no row changes.
ALTER TABLE blank_junk_classification
    DROP CONSTRAINT IF EXISTS blank_junk_classification_junk_subtype_check;
ALTER TABLE blank_junk_classification
    ADD CONSTRAINT blank_junk_classification_junk_subtype_check
    CHECK (junk_subtype IS NULL OR junk_subtype IN (
        'Invoice','Cover Page','Record Request/Transmittal',
        'Instructions','Letter/Fax','Others','Cards'
    ));
