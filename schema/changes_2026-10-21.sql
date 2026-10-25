-- Database changes of 2026-10-21 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Additive only: nothing is
-- dropped or rewritten. Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-10-21.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-10-21.sql        (Windows PowerShell)
--
-- Run the earlier change file first:
--   schema/changes_2026-10-19.sql (sections for 2026-10-18 to 2026-10-20)

-- 1. encounter_review (2026-10-21): a reviewer's encounters (page ranges with page
--    type, encounter type, DOS) saved on review-ui's Encounter tab. Not ground truth.
--    review-ui also creates it on first use. Same definition as schema/v1.sql.
CREATE TABLE IF NOT EXISTS encounter_review (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    seq                     INT NOT NULL,
    start_page              INT NOT NULL CHECK (start_page >= 1),
    end_page                INT NOT NULL,
    page_type               TEXT,
    encounter_type          TEXT,
    dos_from                TEXT,
    dos_to                  TEXT,
    user_id                 BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_encounter_review_chart_name_seq UNIQUE (chart_name, seq),
    CHECK (end_page >= start_page)
);
CREATE INDEX IF NOT EXISTS idx_encounter_review_chart_name ON encounter_review(chart_name);

DROP TRIGGER IF EXISTS trg_encounter_review_updated_at ON encounter_review;
CREATE TRIGGER trg_encounter_review_updated_at
    BEFORE UPDATE ON encounter_review
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 2. manifest_member_list.project_scope (2026-10-21): the client's "Project Scope" column
--    of the metadata sheet, review-ui's Scope filter. core-pipeline also adds it on the
--    first manifest load. Same definition as schema/v1.sql.
ALTER TABLE manifest_member_list ADD COLUMN IF NOT EXISTS project_scope VARCHAR(255);

-- 3. additional_page_details.heading_candidates (2026-10-21): every heading row the
--    key/value stage's detector found, accepted or not, with the page size. review-ui's
--    Production Mode shows the headings and the lighter candidates from it. core-pipeline
--    also adds it on its first write. Same definition as schema/v1.sql.
ALTER TABLE additional_page_details
    ADD COLUMN IF NOT EXISTS heading_candidates JSONB NOT NULL DEFAULT '{}'::jsonb;
