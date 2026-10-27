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

-- 3. dos_extraction_results.extraction_method allows 'kv' (the DOS stage writes it). Our
--    databases had this from temp_changes_to_db.sql; v1.sql lacked it until 2026-10-27, so a
--    database built from v1.sql refused the client copy's DOS rows.
ALTER TABLE dos_extraction_results
    DROP CONSTRAINT IF EXISTS dos_extraction_results_extraction_method_check;
ALTER TABLE dos_extraction_results
    ADD CONSTRAINT dos_extraction_results_extraction_method_check
    CHECK (extraction_method IS NULL OR extraction_method IN ('rules','llm','rules+llm','kv'));

-- 4. member_extraction_results.other_names (2026-10-27): the people a page names who are not
--    the manifest member, for review-ui's member rejection table. Until this runs the member
--    stage writes the rest of the row and logs that the column is missing.
ALTER TABLE member_extraction_results
    ADD COLUMN IF NOT EXISTS other_names JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 5. chart_ground_truth (2026-10-28): the reviewer's Accept / Reject for the member, from
--    review-ui's Rejection Rules tab. review-ui also creates it on its first save.
CREATE TABLE IF NOT EXISTS chart_ground_truth (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    member_decision         VARCHAR(10) CHECK (member_decision IS NULL OR
                                               member_decision IN ('accept','reject')),
    member_decision_reason  TEXT,
    user_id                 BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_chart_ground_truth_chart_name UNIQUE (chart_name)
);
DROP TRIGGER IF EXISTS trg_chart_ground_truth_updated_at ON chart_ground_truth;
CREATE TRIGGER trg_chart_ground_truth_updated_at
    BEFORE UPDATE ON chart_ground_truth
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
