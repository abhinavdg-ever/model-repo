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

-- 2026-10-09 21:11 IST: one ground-truth column for each field the review
-- screen shows that did not already have one. Existing rows stay NULL.
-- Member name, DOB, member id, dates of service, encounter type, page type,
-- codeable, blank/junk, rotation, visibility, provider name, and
-- provider_signature are already on the table and are not changed here.
ALTER TABLE page_ground_truth
    ADD COLUMN IF NOT EXISTS printed_or_handwritten TEXT,
    ADD COLUMN IF NOT EXISTS handwritten_pct TEXT,
    ADD COLUMN IF NOT EXISTS quality TEXT,
    ADD COLUMN IF NOT EXISTS tilt TEXT,
    ADD COLUMN IF NOT EXISTS mirrored TEXT,
    ADD COLUMN IF NOT EXISTS is_duplicate TEXT,
    ADD COLUMN IF NOT EXISTS page_subtype TEXT,
    ADD COLUMN IF NOT EXISTS provider_credentials TEXT;

-- 2026-10-10: document continuity and Final values. Two stages and two
-- tables; nothing existing changes shape. page_classification and
-- dos_extraction_results keep each page's own values; imaging_final holds the
-- Final value of every reviewer field.
INSERT INTO pipeline_stage (stage_name, pass_no, seq, label, is_phase1)
VALUES ('continuity', 1, 87, 'Document Continuity', TRUE),
       ('imaging_final', 1, 100, 'Final Values', TRUE)
ON CONFLICT (stage_name, pass_no) DO NOTHING;


-- Which document each page belongs to (continuity stage). The values carried
-- from a document's first page are in imaging_final.
-- Blank, junk and duplicate pages belong to no document and have no row.
-- The printed page number and section headers are in additional_page_details
-- (created above; the key/value stage now writes it).
CREATE TABLE IF NOT EXISTS page_continuity_results (
    id                   BIGSERIAL PRIMARY KEY,
    chart_id             BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id              BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    document_seq         INT NOT NULL,
    seq                  INT NOT NULL,
    position             VARCHAR(10) NOT NULL CHECK (position IN ('single','first','continue','last')),
    relation             VARCHAR(20) NOT NULL CHECK (relation IN ('new_document','continue','unknown')),
    decided_by           VARCHAR(20) NOT NULL CHECK (decided_by IN (
                             'first_page','blank_junk','signature','pagination','signals','progress_note'
                         )),
    confidence_level     VARCHAR(10) CHECK (confidence_level IS NULL OR confidence_level IN (
                             'high','medium','low'
                         )),
    score                NUMERIC(6,2),
    review_required      BOOLEAN NOT NULL DEFAULT FALSE,
    link_strength        VARCHAR(10) CHECK (link_strength IS NULL OR link_strength IN ('strong','weak')),
    start_confirmed      BOOLEAN NOT NULL DEFAULT FALSE,
    evidence             TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);
CREATE INDEX IF NOT EXISTS idx_page_continuity_results_chart_id ON page_continuity_results(chart_id);
CREATE INDEX IF NOT EXISTS idx_page_continuity_results_page_id ON page_continuity_results(page_id);

DROP TRIGGER IF EXISTS trg_page_continuity_results_updated_at ON page_continuity_results;
CREATE TRIGGER trg_page_continuity_results_updated_at
    BEFORE UPDATE ON page_continuity_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- The reviewer-facing Final value of every field, one row per page, written
-- by the last stage (imaging_final) from every stage's stored output. Most
-- Finals are the page's own value. Page type follows the continuation rules
-- (page_arbitration.json level 3, on page_continuity_results' documents):
-- page_type_source = page | continuation | embedded. DOS is the document's
-- first dated page's (dos_source = document) or the page's own. A duplicate
-- page (skipped after blank/junk) copies its content fields from the page it
-- duplicates: page_type_source = dos_source = 'duplicate', and
-- duplicate_of_page_id names that page.
CREATE TABLE IF NOT EXISTS imaging_final (
    id                       BIGSERIAL PRIMARY KEY,
    chart_id                 BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                  BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    member_name              VARCHAR(255),
    member_dob               DATE,
    member_id                VARCHAR(100),
    printed_or_handwritten   VARCHAR(20),
    handwritten_area_pct     NUMERIC(5,2),
    is_visible               BOOLEAN,
    quality_tag              VARCHAR(10),
    orientation_angle        NUMERIC(6,2),
    tilt_angle               NUMERIC(6,2),
    mirrored                 BOOLEAN,
    blank_junk_flag          VARCHAR(30),
    is_duplicate             BOOLEAN,
    document_seq             INT,
    page_type                VARCHAR(100),
    page_subtype             VARCHAR(200),
    model_type               VARCHAR(100),
    codability               VARCHAR(20),
    classification_category  VARCHAR(20),
    page_type_source         VARCHAR(20) CHECK (page_type_source IS NULL OR
                                                page_type_source IN ('page','continuation','embedded','duplicate')),
    continuation_rule        VARCHAR(30),
    needs_review             BOOLEAN NOT NULL DEFAULT FALSE,
    dos_from                 DATE,
    dos_to                   DATE,
    dos_source               VARCHAR(10) CHECK (dos_source IS NULL OR
                                                dos_source IN ('page','document','duplicate')),
    encounter_type           VARCHAR(30),
    provider_name            TEXT,
    signature_present        BOOLEAN,
    seq                      INT,
    duplicate_of_page_id     BIGINT REFERENCES page_list(id) ON DELETE SET NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);
CREATE INDEX IF NOT EXISTS idx_imaging_final_chart_id ON imaging_final(chart_id);
CREATE INDEX IF NOT EXISTS idx_imaging_final_page_id ON imaging_final(page_id);

DROP TRIGGER IF EXISTS trg_imaging_final_updated_at ON imaging_final;
CREATE TRIGGER trg_imaging_final_updated_at
    BEFORE UPDATE ON imaging_final
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 2026-10-11: page classification revamp (page_taxonomy.json names, BERT +
-- keyword ladder). page_subtype now holds the plain sub-type, not
-- "Family (Type)"; re-run page_subtype to refresh old rows.
ALTER TABLE page_classification
    ADD COLUMN IF NOT EXISTS page_type                 VARCHAR(100),
    ADD COLUMN IF NOT EXISTS model_type                VARCHAR(100),
    ADD COLUMN IF NOT EXISTS decided_by                VARCHAR(30),
    ADD COLUMN IF NOT EXISTS needs_review              BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS bert_model_type           VARCHAR(100),
    ADD COLUMN IF NOT EXISTS bert_confidence           NUMERIC(5,4),
    ADD COLUMN IF NOT EXISTS keyword_page_subtype      VARCHAR(200),
    ADD COLUMN IF NOT EXISTS keyword_score             NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS keyword_margin            NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS keyword_title_hit         BOOLEAN;
ALTER TABLE page_continuity_results
    ADD COLUMN IF NOT EXISTS link_strength VARCHAR(10)
        CHECK (link_strength IS NULL OR link_strength IN ('strong','weak')),
    ADD COLUMN IF NOT EXISTS start_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
-- imaging_final created on 2026-10-10 lacks these; widen its source check.
ALTER TABLE imaging_final
    ADD COLUMN IF NOT EXISTS page_subtype VARCHAR(200),
    ADD COLUMN IF NOT EXISTS model_type VARCHAR(100),
    ADD COLUMN IF NOT EXISTS codability VARCHAR(20),
    ADD COLUMN IF NOT EXISTS continuation_rule VARCHAR(30),
    ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT FALSE,
    ALTER COLUMN page_type_source TYPE VARCHAR(20);
ALTER TABLE imaging_final DROP CONSTRAINT IF EXISTS imaging_final_page_type_source_check;
ALTER TABLE imaging_final ADD CONSTRAINT imaging_final_page_type_source_check
    CHECK (page_type_source IS NULL OR page_type_source IN ('page','continuation','embedded','duplicate'));

-- 2026-10-11: a duplicate page copies its Finals from the page it duplicates.
ALTER TABLE imaging_final
    ADD COLUMN IF NOT EXISTS duplicate_of_page_id BIGINT REFERENCES page_list(id) ON DELETE SET NULL;
ALTER TABLE imaging_final DROP CONSTRAINT IF EXISTS imaging_final_dos_source_check;
ALTER TABLE imaging_final ADD CONSTRAINT imaging_final_dos_source_check
    CHECK (dos_source IS NULL OR dos_source IN ('page','document','duplicate'));

-- 2026-10-11: page type runs before DOS (DOS reads the Extracted sub-type).
UPDATE pipeline_stage SET seq = 75 WHERE stage_name = 'page_subtype' AND pass_no = 1;

-- 2026-10-11: the accuracy report failed with
-- "column pc.page_type does not exist" until the columns above exist.
-- Replace the view only after those columns are added. Same statement as
-- schema/v1.sql and review-ui accuracy_cache.py.
CREATE OR REPLACE VIEW v_accuracy_page AS
SELECT
    g.chart_name,
    g.page_number,
    g.source_page_id,
    c.updated_at                                              AS chart_updated_at,
    g.member_name,
    g.member_dob,
    g.member_id,
    g.dos_from                                                AS gt_dos_from,
    g.dos_to                                                  AS gt_dos_to,
    g.encounter_type,
    g.page_type                                               AS gt_page_type,
    g.codeable                                                AS gt_codeable,
    g.blank_page,
    g.junk_page,
    g.is_invoice,
    g.page_sequence,
    g.rotation,
    g.is_visible,
    g.rendering_provider,
    g.provider_signature,
    pl.page_name,
    m.extracted_name,
    m.extracted_dob,
    m.extracted_member_id,
    d.date_of_service_from,
    d.date_of_service_to,
    b.blank_junk_flag,
    b.junk_subtype,
    -- "Page Type (Sub-type)", the shape review-ui shows; a pre-taxonomy row
    -- already holds that string in page_subtype.
    (CASE WHEN pc.page_type IS NOT NULL AND pc.page_type <> pc.page_subtype
          THEN pc.page_type || ' (' || pc.page_subtype || ')'
          ELSE COALESCE(pc.page_subtype, pc.page_type) END)::VARCHAR(200) AS page_subtype,
    pc.classification_category,
    (
        SELECT mm.external_member_id
          FROM manifest_member_list mm
         WHERE mm.record_id = g.chart_name
         ORDER BY mm.id
         LIMIT 1
    )                                                         AS external_member_id,
    (
        EXISTS (SELECT 1 FROM member_extraction_results mx WHERE mx.chart_id = c.id)
        OR EXISTS (SELECT 1 FROM member_verification_summary vx WHERE vx.chart_id = c.id)
    )                                                         AS member_known,
    EXISTS (SELECT 1 FROM dos_extraction_results dx WHERE dx.chart_id = c.id) AS dos_known,
    EXISTS (SELECT 1 FROM blank_junk_classification bx WHERE bx.chart_id = c.id) AS junk_known,
    EXISTS (SELECT 1 FROM page_classification px WHERE px.chart_id = c.id) AS codeable_known,
    g.updated_at                                              AS ground_truth_updated_at
FROM page_ground_truth g
LEFT JOIN chart_list c
       ON c.chart_name = g.chart_name
LEFT JOIN LATERAL (
    SELECT p.id, p.page_name
      FROM page_list p
     WHERE p.chart_id = c.id
       AND (
            p.page_name = g.source_page_id
         OR split_part(p.page_name, '.', 1) = g.page_number::text
       )
     ORDER BY (p.page_name IS NOT DISTINCT FROM g.source_page_id) DESC, p.id
     LIMIT 1
) pl ON TRUE
LEFT JOIN member_extraction_results m ON m.page_id = pl.id
LEFT JOIN dos_extraction_results d ON d.page_id = pl.id
LEFT JOIN v_page_blank_junk_final b ON b.page_id = pl.id
LEFT JOIN page_classification pc ON pc.page_id = pl.id;

-- 2026-10-11: a blank / junk page in between, and a signed previous page,
-- always start a new document.
ALTER TABLE page_continuity_results DROP CONSTRAINT IF EXISTS page_continuity_results_decided_by_check;
ALTER TABLE page_continuity_results ADD CONSTRAINT page_continuity_results_decided_by_check
    CHECK (decided_by IN ('first_page','blank_junk','signature','pagination','signals','progress_note'));
