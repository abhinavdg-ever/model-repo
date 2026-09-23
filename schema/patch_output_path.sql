-- =====================================================================
-- Patch: bring an EXISTING V1 database up to current V1
-- =====================================================================
-- First-time setup? Do NOT use this file. Apply the full schema instead:
--
--     psql "$DATABASE_URL" -f schema/v1.sql        # required (all tables)
--     psql "$DATABASE_URL" -f schema/v2.sql        # optional (proposals only)
--
-- Existing database that already applied an older v1.sql (and maybe an older
-- v2.sql that defined encounter/sequencing as proposals)? Apply this patch:
--
--     psql "$DATABASE_URL" -f schema/patch_output_path.sql
--
-- Idempotent — safe to re-run.
--
-- WHAT THIS ADDS / UPGRADES
-- ---------------------------------------------------------------------
--   chart_list.output_path              (if missing)
--   chart_list.status 'rejected'→'completed'
--   page_list.use_corrected             (if missing)
--   page_list.image_path                (if missing; backfill pages/<name>)
--   pipeline_stage rows:
--       section_headers seq 55  phase-1   (OCR JSON only; no new table)
--       page_subtype    seq 85  phase-1   (codeable CSV; page_classification)
--       encounter_type  seq 90  phase-1
--       page_sequencing seq 95  phase-1
--   TABLES (were proposals in older v2.sql — now required V1):
--       page_classification       (codeable; blank/junk → non_codeable)
--       encounter_type_results
--       page_sequencing_results
--   page_stage_status pending rows for those three stages on existing pages
--
-- NOT created here (already in older v1.sql — apply full v1.sql on empty DBs):
--   chart_list, page_list, page_stage_status, manifest_member_list,
--   ocr_results, ocr_quality_results, blank_junk_classification,
--   member_*, dos_extraction_results, pipeline_jobs, …
-- NOT created here (still V2 proposals — only if you apply v2.sql):
--   chunk_results, rejection_results, users, …
-- =====================================================================

SET client_min_messages TO WARNING;
SET search_path TO public;

-- ---------------------------------------------------------------------
-- chart_list.output_path + legacy status cleanup
-- ---------------------------------------------------------------------

ALTER TABLE chart_list
    ADD COLUMN IF NOT EXISTS output_path TEXT;

UPDATE chart_list
   SET status = 'completed'
 WHERE status = 'rejected';

COMMENT ON COLUMN chart_list.output_path IS
    'Blob/local write destination for this chart, e.g. Processed/Run1/Batch1/<chart_name>';

-- ---------------------------------------------------------------------
-- page_list: which workspace image stages should read
-- ---------------------------------------------------------------------

ALTER TABLE page_list
    ADD COLUMN IF NOT EXISTS use_corrected BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE page_list
    ADD COLUMN IF NOT EXISTS image_path TEXT;

UPDATE page_list
   SET image_path = 'pages/' || page_name
 WHERE image_path IS NULL
   AND page_name IS NOT NULL
   AND page_name <> '';

COMMENT ON COLUMN page_list.use_corrected IS
    'True when stages should read corrected-pages/ instead of pages/';
COMMENT ON COLUMN page_list.image_path IS
    'Chart-relative image path: pages/<page_name> or corrected-pages/<file>';

-- ---------------------------------------------------------------------
-- pipeline_stage — promote / register the three new phase-1 stages
-- ---------------------------------------------------------------------
-- Older v2.sql may have registered encounter_type / page_sequencing /
-- page_subtype as is_phase1=FALSE at different seq values. Upsert fixes that.

INSERT INTO pipeline_stage (stage_name, pass_no, seq, label, is_phase1) VALUES
    ('section_headers',  1, 55, 'Section Header Match',          TRUE),
    ('page_subtype',     1, 85, 'Codeable / Non-Codeable (TF)',  TRUE),
    ('encounter_type',   1, 90, 'Encounter Type (TF)',           TRUE),
    ('page_sequencing',  1, 95, 'Page Sequencing',               TRUE)
ON CONFLICT (stage_name, pass_no) DO UPDATE SET
    seq       = EXCLUDED.seq,
    label     = EXCLUDED.label,
    is_phase1 = EXCLUDED.is_phase1,
    updated_at = now();

-- ---------------------------------------------------------------------
-- page_classification  (codeable / non_codeable / discharge_summary)
-- Main pages from TF; blank/junk/duplicate stamped non_codeable with
-- junk_subtype (or Blank / Duplicate) as page_subtype.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS page_classification (
    id                        BIGSERIAL PRIMARY KEY,
    chart_id                  BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                   BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    page_subtype              VARCHAR(200),
    classification_category   VARCHAR(20) NOT NULL,
    duplicate_flag            BOOLEAN NOT NULL DEFAULT FALSE,
    confidence                NUMERIC(5,4),
    confidence_level          VARCHAR(10),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE page_classification
    ALTER COLUMN page_subtype TYPE VARCHAR(200);

DO $$
BEGIN
    ALTER TABLE page_classification
        DROP CONSTRAINT IF EXISTS page_classification_classification_category_check;
    ALTER TABLE page_classification
        ADD CONSTRAINT page_classification_classification_category_check
        CHECK (classification_category IN (
            'codeable','non_codeable','discharge_summary'
        ));
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'page_classification'::regclass
           AND contype = 'u'
           AND conname = 'page_classification_page_id_key'
    ) THEN
        DELETE FROM page_classification a
         USING page_classification b
         WHERE a.page_id = b.page_id AND a.id < b.id;
        ALTER TABLE page_classification
            ADD CONSTRAINT page_classification_page_id_key UNIQUE (page_id);
    END IF;
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_page_classification_chart_id
    ON page_classification(chart_id);
CREATE INDEX IF NOT EXISTS idx_page_classification_page_id
    ON page_classification(page_id);

DROP TRIGGER IF EXISTS trg_page_classification_updated_at ON page_classification;
CREATE TRIGGER trg_page_classification_updated_at
    BEFORE UPDATE ON page_classification
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- encounter_type_results  (moved from v2 → v1)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS encounter_type_results (
    id                BIGSERIAL PRIMARY KEY,
    chart_id          BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id           BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    encounter_type    VARCHAR(30) NOT NULL,
    confidence        NUMERIC(5,4),
    matched_keyword   VARCHAR(200),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Upgrade a thin older-v2 copy (nullable page_id, no matched_keyword, no UNIQUE).
ALTER TABLE encounter_type_results
    ADD COLUMN IF NOT EXISTS matched_keyword VARCHAR(200);

DO $$
BEGIN
    -- Drop loose/missing CHECK, install the four allowed tags.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'encounter_type_results'::regclass
           AND contype = 'c'
           AND conname = 'encounter_type_results_encounter_type_check'
    ) THEN
        ALTER TABLE encounter_type_results
            DROP CONSTRAINT encounter_type_results_encounter_type_check;
    END IF;
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;

ALTER TABLE encounter_type_results
    DROP CONSTRAINT IF EXISTS encounter_type_results_encounter_type_check;

ALTER TABLE encounter_type_results
    ADD CONSTRAINT encounter_type_results_encounter_type_check
    CHECK (encounter_type IN (
        'outpatient_f2f', 'outpatient_tele', 'inpatient', 'home'
    ));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'encounter_type_results'::regclass
           AND contype = 'u'
           AND conname = 'encounter_type_results_page_id_key'
    ) THEN
        -- Deduplicate before UNIQUE (keep newest row per page_id).
        DELETE FROM encounter_type_results a
         USING encounter_type_results b
         WHERE a.page_id IS NOT NULL
           AND a.page_id = b.page_id
           AND a.id < b.id;
        ALTER TABLE encounter_type_results
            ADD CONSTRAINT encounter_type_results_page_id_key UNIQUE (page_id);
    END IF;
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_encounter_type_results_chart_id
    ON encounter_type_results(chart_id);
CREATE INDEX IF NOT EXISTS idx_encounter_type_results_page_id
    ON encounter_type_results(page_id);

DROP TRIGGER IF EXISTS trg_encounter_type_results_updated_at ON encounter_type_results;
CREATE TRIGGER trg_encounter_type_results_updated_at
    BEFORE UPDATE ON encounter_type_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- page_sequencing_results  (moved from v2 → v1; gained method + review_flag)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS page_sequencing_results (
    id                     BIGSERIAL PRIMARY KEY,
    chart_id               BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    original_page_number   INT,
    seq                    INT,
    confidence             NUMERIC(5,4),
    sequence_method        VARCHAR(64),
    review_flag            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);

ALTER TABLE page_sequencing_results
    ADD COLUMN IF NOT EXISTS sequence_method VARCHAR(64);
ALTER TABLE page_sequencing_results
    ADD COLUMN IF NOT EXISTS review_flag BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_page_sequencing_results_chart_id
    ON page_sequencing_results(chart_id);
CREATE INDEX IF NOT EXISTS idx_page_sequencing_results_page_id
    ON page_sequencing_results(page_id);

DROP TRIGGER IF EXISTS trg_page_sequencing_results_updated_at ON page_sequencing_results;
CREATE TRIGGER trg_page_sequencing_results_updated_at
    BEFORE UPDATE ON page_sequencing_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- Seed page_stage_status so existing charts can run the new stages
-- ---------------------------------------------------------------------

INSERT INTO page_stage_status (chart_id, page_id, stage_name, pass_no, status)
SELECT p.chart_id, p.id, s.stage_name, s.pass_no, 'pending'
  FROM page_list p
  CROSS JOIN (
      VALUES
          ('section_headers', 1::smallint),
          ('page_subtype',    1::smallint),
          ('encounter_type',  1::smallint),
          ('page_sequencing', 1::smallint)
  ) AS s(stage_name, pass_no)
ON CONFLICT (page_id, stage_name, pass_no) DO NOTHING;
