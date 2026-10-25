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

--    c. encounter_results: the encounter grouping (Doc Summary), one row per encounter.
CREATE TABLE IF NOT EXISTS encounter_results (
    id                BIGSERIAL PRIMARY KEY,
    chart_id          BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    seq               INT NOT NULL,
    document_seq      INT,
    page_family       VARCHAR(100),
    pages             JSONB NOT NULL DEFAULT '[]'::jsonb,
    dos_from          VARCHAR(20),
    dos_to            VARCHAR(20),
    encounter_type    VARCHAR(30),
    encounter_label   VARCHAR(60),
    confidence        NUMERIC(5,4),
    decided_by        VARCHAR(40),
    reason            TEXT,
    matched_keyword   VARCHAR(200),
    conflict          BOOLEAN NOT NULL DEFAULT FALSE,
    contenders        JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chart_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_encounter_results_chart_id ON encounter_results(chart_id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_encounter_results_updated_at') THEN
        CREATE TRIGGER trg_encounter_results_updated_at
            BEFORE UPDATE ON encounter_results
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;
