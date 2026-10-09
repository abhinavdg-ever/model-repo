-- One-time changes for a database that already has schema/v1.sql applied.
-- Covers 8–9 Oct 2026. Do not re-apply schema/v1.sql, and do not run
-- schema/v2.sql (that file also creates tables nothing writes yet).
-- Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/temp_changes_to_db.sql

-- Morning: ground-truth Member ID, the key/value stage, DOS method 'kv'.
ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS member_id TEXT;

INSERT INTO pipeline_stage (stage_name, pass_no, seq, label, is_phase1)
VALUES ('kv_extract', 1, 56, 'Key/Value Extraction', TRUE)
ON CONFLICT (stage_name, pass_no) DO NOTHING;

ALTER TABLE dos_extraction_results
    DROP CONSTRAINT IF EXISTS dos_extraction_results_extraction_method_check;
ALTER TABLE dos_extraction_results
    ADD CONSTRAINT dos_extraction_results_extraction_method_check
    CHECK (extraction_method IS NULL OR extraction_method IN
           ('rules','llm','rules+llm','kv'));

-- Afternoon: page-tag columns. Stage 1 fails without these.
ALTER TABLE ocr_quality_results
  ADD COLUMN IF NOT EXISTS document_type VARCHAR(20) CHECK (document_type IS NULL OR
      document_type IN ('printed','handwritten','form','visual','blank','uncertain')),
  ADD COLUMN IF NOT EXISTS handwritten_probability NUMERIC(5,4),
  ADD COLUMN IF NOT EXISTS is_visible BOOLEAN,
  ADD COLUMN IF NOT EXISTS handwritten_area_pct NUMERIC(5,2) CHECK (handwritten_area_pct IS NULL
      OR handwritten_area_pct BETWEEN 0 AND 100),
  ADD COLUMN IF NOT EXISTS review_required BOOLEAN;

-- Provider signature, in the shape the extractor writes
-- (key, region, scale, sentence, NER text, provider name, date, confidence, source).
-- A database that applied an older v2.sql already has this table with
-- bounding_box and a nullable page_id. The new columns are added beside those.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'provider_signature_results'
    ) THEN
        CREATE TABLE provider_signature_results (
            id                  BIGSERIAL PRIMARY KEY,
            chart_id            BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
            page_id             BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
            signature_present   BOOLEAN NOT NULL DEFAULT FALSE,
            signature_key       VARCHAR(200),
            region              VARCHAR(50),
            scale               VARCHAR(50),
            sentence            TEXT,
            ner_text            TEXT,
            provider_name       TEXT,
            signature_date      DATE,
            confidence          NUMERIC(5,4),
            source              VARCHAR(20),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (page_id)
        );
        CREATE INDEX idx_provider_signature_results_chart_id
            ON provider_signature_results(chart_id);
        CREATE INDEX idx_provider_signature_results_page_id
            ON provider_signature_results(page_id);
        CREATE TRIGGER trg_provider_signature_results_updated_at
            BEFORE UPDATE ON provider_signature_results
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    ELSE
        ALTER TABLE provider_signature_results
            ADD COLUMN IF NOT EXISTS signature_key VARCHAR(200),
            ADD COLUMN IF NOT EXISTS region VARCHAR(50),
            ADD COLUMN IF NOT EXISTS scale VARCHAR(50),
            ADD COLUMN IF NOT EXISTS sentence TEXT,
            ADD COLUMN IF NOT EXISTS ner_text TEXT,
            ADD COLUMN IF NOT EXISTS provider_name TEXT,
            ADD COLUMN IF NOT EXISTS source VARCHAR(20);
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'idx_provider_signature_results_page_id'
        ) THEN
            CREATE INDEX idx_provider_signature_results_page_id
                ON provider_signature_results(page_id);
        END IF;
    END IF;
END $$;

-- Printed page number plus the page's section headers. New table.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'additional_page_details'
    ) THEN
        CREATE TABLE additional_page_details (
            id                      BIGSERIAL PRIMARY KEY,
            chart_id                BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
            page_id                 BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
            page_number_key         VARCHAR(200),
            page_number_region      VARCHAR(50),
            page_number_sentence    TEXT,
            page_number_value       TEXT,
            printed_page_no         VARCHAR(20),
            printed_page_total      VARCHAR(20),
            confidence              NUMERIC(5,4),
            source                  VARCHAR(20),
            section_headers         JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (page_id)
        );
        CREATE INDEX idx_additional_page_details_chart_id
            ON additional_page_details(chart_id);
        CREATE INDEX idx_additional_page_details_page_id
            ON additional_page_details(page_id);
        CREATE TRIGGER trg_additional_page_details_updated_at
            BEFORE UPDATE ON additional_page_details
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- Accuracy is a view, not a stored score table.
DROP TABLE IF EXISTS accuracy_snapshot;
