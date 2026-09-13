-- =====================================================================
-- AI Imaging & Coding Pipeline — PostgreSQL Schema (v8)
-- =====================================================================
-- THE single canonical DDL for core-pipeline + review-ui. There is no
-- second copy and no migration directory: this file is the schema.
--
--     psql "$DATABASE_URL" -f schema/schema.sql
--
-- The file is idempotent-by-drop: it assumes an empty database (or one you
-- are willing to rebuild). Pre-v8 databases are recreated, not upgraded.
--
-- ---------------------------------------------------------------------
-- NAMING CONVENTIONS — every table in this file obeys these. A new table
-- that breaks one of them is a bug, not a style preference.
-- ---------------------------------------------------------------------
--  1. snake_case everywhere. No camelCase, no abbreviations that are not
--     already domain words (dos = date of service, ocr, ner, hw).
--  2. Primary key is  id BIGSERIAL PRIMARY KEY. The one exception is
--     pipeline_stage, a static registry keyed by its natural key
--     (stage_name, pass_no) — it is referenced by name, never by id.
--  3. A foreign key is named <referenced_table_singular>_id and nothing
--     else:  chart_id, page_id, user_id, model_id, matched_member_list_id.
--     Never reviewed_by / imported_by / review_id for the same idea.
--  4. Every table has  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
--     and                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
--     with a  trg_<table>_updated_at  trigger. No bespoke row-birth names
--     (no imported_at / compared_at / evaluated_at / decided_at).
--     Timestamps that mean something *other* than row lifecycle keep their
--     own name: started_at, completed_at, queued_at, trained_at.
--  5. A model/rule score in [0,1] is  confidence NUMERIC(5,4). When a table
--     carries more than one, each is prefixed: hw_confidence, quality_score.
--     Never confidence_score / similarity_score for the same idea.
--  6. An ordinal within a parent row is  seq INT.  Not chunk_index, not
--     sequence_no, not position.
--  7. Stage identity is always the pair  stage_name VARCHAR(50) +
--     pass_no SMALLINT.  A retry counter is  attempt INT.
--  8. Blob location is  blob_container VARCHAR(150) + blob_path TEXT.
--  9. Booleans read as assertions: is_final, is_active, mirrored,
--     rotation_applied, signature_present.
-- 10. Indexes:  idx_<table>_<columns>.  Views:  v_<name> for helper views;
--     report views keep their business name (consolidated_chart_results).
--
-- ---------------------------------------------------------------------
-- WHAT CHANGED IN v8 (from v7)
-- ---------------------------------------------------------------------
--  * Consolidation. schema/migrations/ and Reference/schema.sql are gone.
--  * Column names normalised to the conventions above:
--      chart_list.blob_container_name        -> blob_container
--      chart_list.path                       -> blob_path
--      ocr_quality_results.confidence        -> hw_confidence
--      page_classification.confidence_score  -> confidence
--      invoice_matching_results.similarity_score -> confidence
--      chunk_results.chunk_index             -> seq
--      page_sequencing_results.sequence_no   -> seq
--      dos_extraction_dates.dos_from/dos_to  -> date_of_service_from/_to
--      member_verification_summary.matched_member_id -> matched_member_list_id
--      member_verification_summary.decided_at        -> created_at/updated_at
--      rejection_results.reviewed_by         -> user_id
--      ground_truth_csv.imported_by/_at      -> user_id/created_at
--      field_accuracy_log.compared_at        -> created_at
--      model_accuracy_snapshots.evaluated_at -> created_at
--      manual_review.review_id               -> user_id
--      pipeline_jobs.attempt_number          -> attempt
--  * created_at + updated_at + trigger on every table.
--  * ocr_quality_results: quality_tag / quality_score are now a real page
--    quality grade, not the handwriting classifier's method string. The
--    method moved to its own hw_method column. Until the quality model is
--    trained the grade is a fixed placeholder written by the stage
--    (printed -> high/0.8, handwritten|mixed -> low/0.5) — see the comment
--    on the table.
--  * member_extraction_results.page_name dropped; join page_list instead.
--  * v_chart_status_legacy dropped — there is no v6 consumer left.
--  * encounter_type_results.page_id / provider_signature_results.page_id
--    now ON DELETE CASCADE like every other page_id.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
SET search_path TO public;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------
-- STAGE REGISTRY
-- ---------------------------------------------------------------------
-- The pipeline's shape as data. Orchestrator reads execution order from
-- here; chart status is derived by joining page_stage_status against it.
-- Adding page-subtype / encounter / sequencing later is an INSERT.

CREATE TABLE pipeline_stage (
    stage_name   VARCHAR(50)  NOT NULL,
    pass_no      SMALLINT     NOT NULL DEFAULT 1,
    seq          INT          NOT NULL,
    label        VARCHAR(80)  NOT NULL,
    is_phase1    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (stage_name, pass_no),
    UNIQUE (seq)
);

CREATE TRIGGER trg_pipeline_stage_updated_at
    BEFORE UPDATE ON pipeline_stage
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO pipeline_stage (stage_name, pass_no, seq, label, is_phase1) VALUES
    ('ocr_prelim',       1, 10, 'Preliminary OCR (Tesseract)',   TRUE),
    ('ocr_quality',      1, 20, 'Rotation + Handwriting',        TRUE),
    ('blank_junk',       1, 30, 'Blank/Junk/Duplicate — pass 1',  TRUE),
    ('ocr_final1',       1, 40, 'Final OCR 1 (RapidOCR)',        TRUE),
    ('ocr_final2',       1, 50, 'Final OCR 2 (Azure DocIntel)',  TRUE),
    ('blank_junk',       2, 60, 'Blank/Junk/Duplicate — pass 2',  TRUE),
    ('member_verify',    1, 70, 'Member Extraction + Verify',    TRUE),
    ('dos_extract',      1, 80, 'Date-of-Service Extraction',    TRUE),
    -- Registered but not orchestrated yet (is_phase1 = FALSE keeps them out
    -- of the chart-status rollup until their stage modules land).
    ('page_subtype',     1, 90, 'Page Subtype Classification',   FALSE),
    ('encounter_type',   1, 100, 'Encounter Type',               FALSE),
    ('page_sequencing',  1, 110, 'Page Sequencing',              FALSE),
    ('rejection_logic',  1, 120, 'Rejection Logic',              FALSE);

-- ---------------------------------------------------------------------
-- MASTER + REFERENCE TABLES
-- ---------------------------------------------------------------------

CREATE TABLE chart_list (
    id                  BIGSERIAL PRIMARY KEY,
    chart_name          VARCHAR(150) NOT NULL,
    page_count          INT,

    -- Lifecycle only. Where the chart *is* lives in current_stage.
    status              VARCHAR(20) NOT NULL DEFAULT 'received'
                        CHECK (status IN (
                            'received','downloading','processing',
                            'completed','failed','needs_review','rejected'
                        )),
    current_stage       VARCHAR(50),
    current_pass        SMALLINT,

    -- How this row came to exist. 'manifest' rows are placeholders created by
    -- the manifest sweeper for charts that may never be ingested; the UI
    -- filters them out until pages arrive.
    source              VARCHAR(20) NOT NULL DEFAULT 'blob'
                        CHECK (source IN ('blob','local','manifest')),

    blob_container      VARCHAR(150),
    blob_path           TEXT,
    run_id              VARCHAR(50),
    batch_id            VARCHAR(50),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One row per chart name. v6 relied on SELECT ... ORDER BY id DESC LIMIT 1,
    -- which let two concurrent ingests create two charts with the same name.
    UNIQUE (chart_name)
);
CREATE INDEX idx_chart_list_status ON chart_list(status);
CREATE INDEX idx_chart_list_run_batch ON chart_list(run_id, batch_id);
CREATE INDEX idx_chart_list_source ON chart_list(source);

CREATE TRIGGER trg_chart_list_updated_at
    BEFORE UPDATE ON chart_list
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE page_list (
    id               BIGSERIAL PRIMARY KEY,
    chart_id         BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_name        VARCHAR(150) NOT NULL,
    page_number      INT,

    -- Image identity: image-level duplicate detection and download idempotency.
    image_sha256     CHAR(64),
    file_size_bytes  BIGINT,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chart_id, page_name)
);
CREATE INDEX idx_page_list_chart_id ON page_list(chart_id);
CREATE INDEX idx_page_list_chart_page_number ON page_list(chart_id, page_number);
CREATE INDEX idx_page_list_image_sha256 ON page_list(image_sha256)
    WHERE image_sha256 IS NOT NULL;

CREATE TRIGGER trg_page_list_updated_at
    BEFORE UPDATE ON page_list
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Per-page, per-stage, per-pass progress. Replaces v6's 11 status columns.
CREATE TABLE page_stage_status (
    id             BIGSERIAL PRIMARY KEY,
    chart_id       BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id        BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    stage_name     VARCHAR(50) NOT NULL,
    pass_no        SMALLINT NOT NULL DEFAULT 1,
    status         VARCHAR(20) NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','processing','completed','failed','skipped')),
    attempt        INT NOT NULL DEFAULT 0,
    skip_reason    VARCHAR(50),
    error_message  TEXT,
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, stage_name, pass_no)
);
CREATE INDEX idx_page_stage_status_chart_stage_pass ON page_stage_status(chart_id, stage_name, pass_no);
CREATE INDEX idx_page_stage_status_chart_status ON page_stage_status(chart_id, status);

CREATE TRIGGER trg_page_stage_status_updated_at
    BEFORE UPDATE ON page_stage_status
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- MANIFEST (client-provided member roster)
-- ---------------------------------------------------------------------
-- A manifest batch (metadata_R1_B1.csv) is a fact about a client RecordId,
-- not about a chart row we happen to have downloaded. record_id is therefore
-- the identity; chart_id is a nullable convenience link resolved on ingest.
--
-- Name parts are stored separately because the verification rules match
-- first / middle / last independently — a single "member_name" string cannot
-- drive classify_two_word_name / classify_three_word_name.

CREATE TABLE manifest_member_list (
    id                  BIGSERIAL PRIMARY KEY,
    record_id           VARCHAR(150) NOT NULL,
    chart_id            BIGINT REFERENCES chart_list(id) ON DELETE SET NULL,

    member_name         VARCHAR(255) NOT NULL,
    first_name          VARCHAR(100),
    middle_name         VARCHAR(100),
    last_name           VARCHAR(100),
    member_dob          DATE,
    external_member_id  VARCHAR(100),

    run_id              VARCHAR(50),
    batch_id            VARCHAR(50),
    source_file         VARCHAR(255),
    source_blob_path    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_manifest_member_list_record_id ON manifest_member_list(record_id);
CREATE INDEX idx_manifest_member_list_chart_id ON manifest_member_list(chart_id);
CREATE INDEX idx_manifest_member_list_run_batch ON manifest_member_list(run_id, batch_id);

-- Upsert key 1: MemberID present.
CREATE UNIQUE INDEX idx_manifest_member_list_upsert_id
    ON manifest_member_list (record_id, external_member_id)
    WHERE external_member_id IS NOT NULL AND external_member_id <> '';

-- Upsert key 2: no MemberID — fall back to name + DOB. v6 documented this key
-- but never indexed it, so the fallback path was a seq scan and racy.
CREATE UNIQUE INDEX idx_manifest_member_list_upsert_name
    ON manifest_member_list (record_id, lower(member_name), COALESCE(member_dob, 'epoch'::date))
    WHERE external_member_id IS NULL OR external_member_id = '';

CREATE TRIGGER trg_manifest_member_list_updated_at
    BEFORE UPDATE ON manifest_member_list
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE users (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(150) NOT NULL,
    email       VARCHAR(255) NOT NULL UNIQUE,
    role        VARCHAR(20) NOT NULL DEFAULT 'reviewer' CHECK (role IN ('reviewer','admin','viewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO users (name, email, role)
VALUES ('imaging_user', 'imaging_user@advantmed.local', 'admin')
ON CONFLICT (email) DO NOTHING;

-- ---------------------------------------------------------------------
-- PAGE-LEVEL RESULT TABLES
-- ---------------------------------------------------------------------

CREATE TABLE ocr_results (
    id           BIGSERIAL PRIMARY KEY,
    chart_id     BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id      BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    ocr_type     VARCHAR(30) NOT NULL CHECK (ocr_type IN ('tesseract','docling','azuredocintel')),
    raw_text     TEXT,
    char_count   INT,
    text_sha256  CHAR(64),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, ocr_type)
);
CREATE INDEX idx_ocr_results_chart_id ON ocr_results(chart_id);
CREATE INDEX idx_ocr_results_page_id ON ocr_results(page_id);

CREATE TRIGGER trg_ocr_results_updated_at
    BEFORE UPDATE ON ocr_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE ocr_quality_results (
    id                      BIGSERIAL PRIMARY KEY,
    chart_id                BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                 BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    -- PAGE QUALITY GRADE.
    -- No quality model exists yet. Until one is trained, ocr_quality stage
    -- writes a fixed placeholder derived entirely from
    -- printed_or_handwritten:  printed -> ('high', 0.8000),
    -- handwritten|mixed -> ('low', 0.5000). It therefore carries no
    -- information beyond printed_or_handwritten — do not branch on it, and
    -- do not report it as a measured score. When the model lands, the stage
    -- starts writing real values and nothing here has to change.
    quality_tag             VARCHAR(10) CHECK (quality_tag IS NULL OR
                                               quality_tag IN ('high','medium','low')),
    quality_score           NUMERIC(5,4),

    printed_or_handwritten  VARCHAR(20) CHECK (printed_or_handwritten IN ('printed','handwritten','mixed')),
    -- Which classifier produced printed_or_handwritten ('model' | 'heuristic').
    -- In v7 this string was stored in quality_tag, which is why that column
    -- never held a quality grade.
    hw_method               VARCHAR(30),
    hw_confidence           NUMERIC(5,4),

    orientation_angle       NUMERIC(6,2),
    tilt_angle              NUMERIC(6,2),
    mirrored                BOOLEAN,
    rotation_applied        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);
CREATE INDEX idx_ocr_quality_results_chart_id ON ocr_quality_results(chart_id);
CREATE INDEX idx_ocr_quality_results_page_id ON ocr_quality_results(page_id);

CREATE TRIGGER trg_ocr_quality_results_updated_at
    BEFORE UPDATE ON ocr_quality_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One row per page per pass. Exactly one row per page carries is_final, so
-- downstream stages never have to guess which pass won.
CREATE TABLE blank_junk_classification (
    id                BIGSERIAL PRIMARY KEY,
    chart_id          BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id           BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    pass_no           SMALLINT NOT NULL DEFAULT 1,
    blank_junk_flag   VARCHAR(20) CHECK (blank_junk_flag IN (
                          'blank','junk','duplicate','not_blank_junk'
                      )),
    -- Constrained to the label set review-ui renders (imaging_overlays.py).
    junk_subtype      VARCHAR(50) CHECK (junk_subtype IS NULL OR junk_subtype IN (
                          'Invoice','Cover Page','Record Request/Transmittal',
                          'Instructions','Letter/Fax','Others'
                      )),
    duplicate_of_page_id BIGINT REFERENCES page_list(id) ON DELETE SET NULL,
    ocr_source        VARCHAR(30) NOT NULL DEFAULT 'tesseract'
                      CHECK (ocr_source IN ('tesseract','docling','azuredocintel')),
    is_final          BOOLEAN NOT NULL DEFAULT FALSE,
    confidence        NUMERIC(5,4),
    reason            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, pass_no)
);
CREATE INDEX idx_blank_junk_classification_chart_id ON blank_junk_classification(chart_id);
CREATE INDEX idx_blank_junk_classification_page_id ON blank_junk_classification(page_id);
CREATE UNIQUE INDEX idx_blank_junk_classification_one_final
    ON blank_junk_classification (page_id) WHERE is_final;

CREATE TRIGGER trg_blank_junk_classification_updated_at
    BEFORE UPDATE ON blank_junk_classification
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE VIEW v_page_blank_junk_final AS
SELECT chart_id, page_id, blank_junk_flag, junk_subtype, duplicate_of_page_id,
       ocr_source, pass_no, confidence, reason, updated_at
FROM blank_junk_classification
WHERE is_final;

CREATE TABLE page_classification (
    id                        BIGSERIAL PRIMARY KEY,
    chart_id                  BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                   BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    page_subtype              VARCHAR(50),
    classification_category   VARCHAR(20) CHECK (classification_category IN (
                                  'codeable','non_codeable','discharge_summary'
                              )),
    duplicate_flag            BOOLEAN NOT NULL DEFAULT FALSE,
    confidence                NUMERIC(5,4),
    confidence_level          VARCHAR(10) CHECK (confidence_level IN ('high','medium','low')),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);
CREATE INDEX idx_page_classification_chart_id ON page_classification(chart_id);
CREATE INDEX idx_page_classification_page_id ON page_classification(page_id);

CREATE TRIGGER trg_page_classification_updated_at
    BEFORE UPDATE ON page_classification
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE chunk_results (
    id           BIGSERIAL PRIMARY KEY,
    chart_id     BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id      BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    seq          INT NOT NULL,
    chunk_text   TEXT NOT NULL,
    char_start   INT,
    char_end     INT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, seq)
);

CREATE TRIGGER trg_chunk_results_updated_at
    BEFORE UPDATE ON chunk_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_chunk_results_chart_id ON chunk_results(chart_id);
CREATE INDEX idx_chunk_results_page_id ON chunk_results(page_id);

-- Primary (best) DOS per page; the full set lives in dos_extraction_dates.
CREATE TABLE dos_extraction_results (
    id                              BIGSERIAL PRIMARY KEY,
    chart_id                        BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                         BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    date_of_service_from            DATE,
    date_of_service_to              DATE,
    date_of_service_from_doclevel   DATE,
    date_of_service_to_doclevel     DATE,
    date_count                      INT NOT NULL DEFAULT 0,
    extraction_method               VARCHAR(20)
                                    CHECK (extraction_method IS NULL OR extraction_method IN
                                          ('rules','llm','rules+llm')),
    confidence                      NUMERIC(5,4),
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);
CREATE INDEX idx_dos_extraction_results_chart_id ON dos_extraction_results(chart_id);
CREATE INDEX idx_dos_extraction_results_page_id ON dos_extraction_results(page_id);

CREATE TRIGGER trg_dos_extraction_results_updated_at
    BEFORE UPDATE ON dos_extraction_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- A page can carry several dates of service. v6 could hold exactly one pair,
-- while the CSV contract already supported comma-separated lists.
CREATE TABLE dos_extraction_dates (
    id             BIGSERIAL PRIMARY KEY,
    chart_id       BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id        BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    seq                  INT NOT NULL,
    date_of_service_from DATE,
    date_of_service_to   DATE,
    source_keyword       VARCHAR(100),
    confidence           NUMERIC(5,4),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, seq)
);

CREATE TRIGGER trg_dos_extraction_dates_updated_at
    BEFORE UPDATE ON dos_extraction_dates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_dos_extraction_dates_chart_id ON dos_extraction_dates(chart_id);
CREATE INDEX idx_dos_extraction_dates_page_id ON dos_extraction_dates(page_id);

CREATE TABLE encounter_type_results (
    id              BIGSERIAL PRIMARY KEY,
    chart_id        BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id         BIGINT REFERENCES page_list(id) ON DELETE CASCADE,
    encounter_type  VARCHAR(100),
    confidence      NUMERIC(5,4),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_encounter_type_results_updated_at
    BEFORE UPDATE ON encounter_type_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_encounter_type_results_chart_id ON encounter_type_results(chart_id);

CREATE TABLE page_sequencing_results (
    id                     BIGSERIAL PRIMARY KEY,
    chart_id               BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    original_page_number   INT,
    seq                    INT,
    confidence             NUMERIC(5,4),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);

CREATE TRIGGER trg_page_sequencing_results_updated_at
    BEFORE UPDATE ON page_sequencing_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_page_sequencing_results_chart_id ON page_sequencing_results(chart_id);
CREATE INDEX idx_page_sequencing_results_page_id ON page_sequencing_results(page_id);

-- ---------------------------------------------------------------------
-- VERIFICATION & DECISION
-- ---------------------------------------------------------------------
-- Columns mirror the V1 Member_Verification contract so a ported run is
-- diffable against the reference implementation.

CREATE TABLE member_extraction_results (
    id                             BIGSERIAL PRIMARY KEY,
    chart_id                       BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id                        BIGINT REFERENCES page_list(id) ON DELETE CASCADE,

    extracted_name                 VARCHAR(255),
    extracted_dob                  DATE,
    extracted_member_id            VARCHAR(100),

    -- How each field was found: rule pass, NER pass, or not at all.
    detection_source_name          VARCHAR(20)
                                   CHECK (detection_source_name IS NULL OR
                                          detection_source_name IN ('rule_based','ner','')),
    detection_source_dob           VARCHAR(20)
                                   CHECK (detection_source_dob IS NULL OR
                                          detection_source_dob IN ('rule_based','ner','')),
    detection_source_member_id     VARCHAR(20)
                                   CHECK (detection_source_member_id IS NULL OR
                                          detection_source_member_id IN ('rule_based','ner','')),
    -- Which key sentence the NER pass read the value out of ("Patient Name:").
    ner_key_source_name            VARCHAR(100),
    ner_key_source_dob             VARCHAR(100),
    ner_key_source_member_id       VARCHAR(100),

    -- The V1 per-page verdict. Without wrong_member there is no reject path.
    page_status                    VARCHAR(20)
                                   CHECK (page_status IS NULL OR page_status IN
                                          ('verified','wrong_member','not_verified')),
    page_verified                  BOOLEAN,

    confidence                     NUMERIC(5,4),
    provided_name                  VARCHAR(255),
    provided_dob                   DATE,
    provided_external_member_id    VARCHAR(100),
    matched_member_list_id         BIGINT REFERENCES manifest_member_list(id) ON DELETE SET NULL,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);
CREATE INDEX idx_member_extraction_results_chart_id ON member_extraction_results(chart_id);
CREATE INDEX idx_member_extraction_results_page_id ON member_extraction_results(page_id);
CREATE INDEX idx_member_extraction_results_matched ON member_extraction_results(matched_member_list_id);
CREATE INDEX idx_member_extraction_results_page_status ON member_extraction_results(chart_id, page_status);

CREATE TRIGGER trg_member_extraction_results_updated_at
    BEFORE UPDATE ON member_extraction_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE member_verification_summary (
    id                  BIGSERIAL PRIMARY KEY,
    chart_id            BIGINT NOT NULL UNIQUE REFERENCES chart_list(id) ON DELETE CASCADE,

    final_status        VARCHAR(20) NOT NULL
                        CHECK (final_status IN ('verified','failed','needs_review')),
    -- Accept / Reject from what_if_rules: wrong-member pages >= reject_threshold.
    document_decision   VARCHAR(10)
                        CHECK (document_decision IS NULL OR document_decision IN ('accept','reject')),

    matched_member_list_id BIGINT REFERENCES manifest_member_list(id) ON DELETE SET NULL,
    matched_name        VARCHAR(255),
    name_mode           VARCHAR(2) CHECK (name_mode IS NULL OR name_mode IN ('2','3','')),

    confidence          NUMERIC(5,4),
    pages_checked       INT,
    pages_matched       INT,
    wrong_member_pages  INT NOT NULL DEFAULT 0,
    reject_threshold    INT,

    decision_reason     VARCHAR(50),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_member_verification_summary_updated_at
    BEFORE UPDATE ON member_verification_summary
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE rejection_results (
    id            BIGSERIAL PRIMARY KEY,
    chart_id      BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    decision      VARCHAR(10) NOT NULL CHECK (decision IN ('accept','reject','flag')),
    reason        TEXT,
    user_id       BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_rejection_results_updated_at
    BEFORE UPDATE ON rejection_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_rejection_results_chart_id ON rejection_results(chart_id);

CREATE TABLE provider_signature_results (
    id                  BIGSERIAL PRIMARY KEY,
    chart_id            BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id             BIGINT REFERENCES page_list(id) ON DELETE CASCADE,
    signature_present   BOOLEAN NOT NULL DEFAULT FALSE,
    bounding_box        JSONB,
    signature_date      DATE,
    confidence          NUMERIC(5,4),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_provider_signature_results_updated_at
    BEFORE UPDATE ON provider_signature_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_provider_signature_results_chart_id ON provider_signature_results(chart_id);

CREATE TABLE invoice_matching_results (
    id                  BIGSERIAL PRIMARY KEY,
    chart_id            BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    invoice_reference   VARCHAR(100),
    match_status        VARCHAR(30),
    confidence          NUMERIC(5,4),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_invoice_matching_results_updated_at
    BEFORE UPDATE ON invoice_matching_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_invoice_matching_results_chart_id ON invoice_matching_results(chart_id);

CREATE TABLE ground_truth_csv (
    id              BIGSERIAL PRIMARY KEY,
    chart_id        BIGINT REFERENCES chart_list(id) ON DELETE CASCADE,
    csv_file_name   VARCHAR(255) NOT NULL,
    row_number      INT,
    labeled_fields  JSONB NOT NULL,
    user_id         BIGINT REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_ground_truth_csv_updated_at
    BEFORE UPDATE ON ground_truth_csv
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_ground_truth_csv_chart_id ON ground_truth_csv(chart_id);

-- ---------------------------------------------------------------------
-- OPERATIONAL
-- ---------------------------------------------------------------------

CREATE TABLE model_registry (
    id                 BIGSERIAL PRIMARY KEY,
    model_name         VARCHAR(100) NOT NULL,
    model_type         VARCHAR(20) NOT NULL CHECK (model_type IN ('classification','ner','llm','ocr')),
    model_algorithm    VARCHAR(50),
    version            VARCHAR(30) NOT NULL,
    storage_type       VARCHAR(10) NOT NULL CHECK (storage_type IN ('blob','docker')),
    storage_location   TEXT NOT NULL,
    training_metrics   JSONB,
    trained_at         TIMESTAMPTZ,
    is_active          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (model_name, version)
);

CREATE TRIGGER trg_model_registry_updated_at
    BEFORE UPDATE ON model_registry
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_model_registry_name_active ON model_registry(model_name, is_active);

-- Audit log today; a work queue when a worker process lands. lease_expires_at
-- and heartbeat_at are what a claim/lease loop needs.
CREATE TABLE pipeline_jobs (
    id                BIGSERIAL PRIMARY KEY,
    chart_id          BIGINT REFERENCES chart_list(id) ON DELETE CASCADE,
    stage_name        VARCHAR(50) NOT NULL,
    pass_no           SMALLINT NOT NULL DEFAULT 1,
    status            VARCHAR(20) NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','running','completed','failed','retrying')),
    queue_name        VARCHAR(100),
    attempt           INT NOT NULL DEFAULT 1,
    worker_id         VARCHAR(100),
    lease_expires_at  TIMESTAMPTZ,
    heartbeat_at      TIMESTAMPTZ,
    pages_total       INT,
    pages_done        INT,
    pages_failed      INT,
    pages_skipped     INT,
    queued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    duration_seconds  NUMERIC GENERATED ALWAYS AS (
                          EXTRACT(EPOCH FROM (completed_at - started_at))
                      ) STORED,
    error_message     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_pipeline_jobs_updated_at
    BEFORE UPDATE ON pipeline_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_pipeline_jobs_chart_id ON pipeline_jobs(chart_id);
CREATE INDEX idx_pipeline_jobs_stage_status ON pipeline_jobs(stage_name, status);
CREATE INDEX idx_pipeline_jobs_claim ON pipeline_jobs(status, queue_name, queued_at)
    WHERE status IN ('queued','retrying');

CREATE OR REPLACE VIEW pipeline_stage_performance AS
SELECT
    stage_name,
    COUNT(*)                                                             AS total_jobs,
    COUNT(*) FILTER (WHERE status = 'completed')                         AS completed_jobs,
    COUNT(*) FILTER (WHERE status = 'failed')                            AS failed_jobs,
    ROUND(AVG(duration_seconds) FILTER (WHERE status = 'completed'), 2)  AS avg_duration_seconds,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'completed') / NULLIF(COUNT(*), 0), 2) AS success_rate_pct
FROM pipeline_jobs
GROUP BY stage_name;

CREATE TABLE field_accuracy_log (
    id                BIGSERIAL PRIMARY KEY,
    chart_id          BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    model_id          BIGINT REFERENCES model_registry(id),
    field_name        VARCHAR(100) NOT NULL,
    predicted_value   TEXT,
    actual_value      TEXT,
    is_correct        BOOLEAN,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_field_accuracy_log_updated_at
    BEFORE UPDATE ON field_accuracy_log
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_field_accuracy_log_chart_id ON field_accuracy_log(chart_id);
CREATE INDEX idx_field_accuracy_log_model_id ON field_accuracy_log(model_id);

CREATE TABLE model_accuracy_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    model_id          BIGINT NOT NULL REFERENCES model_registry(id) ON DELETE CASCADE,
    evaluation_batch  VARCHAR(100),
    metric_name       VARCHAR(50) NOT NULL,
    metric_value      NUMERIC(7,4) NOT NULL,
    sample_size       INT,
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_model_accuracy_snapshots_updated_at
    BEFORE UPDATE ON model_accuracy_snapshots
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX idx_model_accuracy_snapshots_model_id ON model_accuracy_snapshots(model_id);

CREATE OR REPLACE VIEW model_accuracy_latest AS
SELECT DISTINCT ON (mr.id, s.metric_name)
    mr.id AS model_id, mr.model_name, mr.version, mr.storage_type,
    s.metric_name, s.metric_value, s.sample_size, s.created_at
FROM model_registry mr
JOIN model_accuracy_snapshots s ON s.model_id = mr.id
ORDER BY mr.id, s.metric_name, s.created_at DESC;

CREATE TABLE manual_review (
    id                BIGSERIAL PRIMARY KEY,
    chart_id          BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id           BIGINT REFERENCES page_list(id) ON DELETE CASCADE,
    user_id           BIGINT REFERENCES users(id),
    module_type       VARCHAR(50) NOT NULL,
    actual_value      TEXT,
    feedback          TEXT,
    suggested_value   TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_manual_review_chart_id ON manual_review(chart_id);
CREATE INDEX idx_manual_review_page_id ON manual_review(page_id);
CREATE INDEX idx_manual_review_user_id ON manual_review(user_id);
CREATE INDEX idx_manual_review_module_type ON manual_review(module_type);

CREATE TRIGGER trg_manual_review_updated_at
    BEFORE UPDATE ON manual_review
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    chart_id    BIGINT REFERENCES chart_list(id) ON DELETE SET NULL,
    user_id     BIGINT REFERENCES users(id),
    action      VARCHAR(50) NOT NULL,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_audit_log_updated_at
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_audit_log_chart_id ON audit_log(chart_id);

-- ---------------------------------------------------------------------
-- DERIVED VIEWS
-- ---------------------------------------------------------------------

-- Per-chart, per-stage page rollup. The orchestrator's chart-status rule
-- ("earliest stage where not every page is completed|skipped") reads this.
CREATE OR REPLACE VIEW v_chart_stage_progress AS
SELECT
    c.id                                                       AS chart_id,
    s.stage_name,
    s.pass_no,
    s.seq,
    s.label,
    s.is_phase1,
    COUNT(pss.id)                                              AS rows_present,
    (SELECT COUNT(*) FROM page_list p WHERE p.chart_id = c.id) AS pages_total,
    COUNT(*) FILTER (WHERE pss.status = 'pending')             AS pending,
    COUNT(*) FILTER (WHERE pss.status = 'processing')          AS processing,
    COUNT(*) FILTER (WHERE pss.status = 'completed')           AS completed,
    COUNT(*) FILTER (WHERE pss.status = 'failed')              AS failed,
    COUNT(*) FILTER (WHERE pss.status = 'skipped')             AS skipped
FROM chart_list c
CROSS JOIN pipeline_stage s
LEFT JOIN page_stage_status pss
       ON pss.chart_id = c.id
      AND pss.stage_name = s.stage_name
      AND pss.pass_no = s.pass_no
GROUP BY c.id, s.stage_name, s.pass_no, s.seq, s.label, s.is_phase1;

CREATE OR REPLACE VIEW consolidated_chart_results AS
SELECT
    c.id                          AS chart_id,
    c.chart_name,
    c.blob_container,
    c.blob_path,
    c.page_count,
    c.status                      AS chart_status,
    c.current_stage,
    c.run_id,
    c.batch_id,

    mv.final_status               AS verification_status,
    mv.document_decision          AS verification_decision,
    mv.matched_name               AS verified_name,
    mv.confidence                 AS verification_confidence,
    mv.wrong_member_pages,
    mv.reject_threshold,

    rj.decision                   AS rejection_decision,
    rj.reason                     AS rejection_reason,

    bj.blank_count,
    bj.junk_count,
    bj.duplicate_count            AS bj_duplicate_count,
    pc.codeable_count,
    pc.non_codeable_count,
    pc.discharge_summary_count,
    pc.duplicate_count,

    seq.sequenced_page_count,
    enc.encounter_count,
    dos.earliest_dos,
    dos.latest_dos,

    COALESCE(sig.any_signature_present, false) AS signature_present,
    im.match_status                AS invoice_match_status,
    (gt.row_count > 0)             AS has_ground_truth,

    c.created_at,
    c.updated_at

FROM chart_list c

LEFT JOIN member_verification_summary mv ON mv.chart_id = c.id

LEFT JOIN LATERAL (
    SELECT * FROM rejection_results r
    WHERE r.chart_id = c.id ORDER BY r.created_at DESC LIMIT 1
) rj ON true

LEFT JOIN LATERAL (
    SELECT
        COUNT(*) FILTER (WHERE blank_junk_flag = 'blank') AS blank_count,
        COUNT(*) FILTER (WHERE blank_junk_flag = 'junk')  AS junk_count,
        COUNT(*) FILTER (WHERE blank_junk_flag = 'duplicate') AS duplicate_count
    FROM v_page_blank_junk_final b WHERE b.chart_id = c.id
) bj ON true

LEFT JOIN LATERAL (
    SELECT
        COUNT(*) FILTER (WHERE classification_category = 'codeable')         AS codeable_count,
        COUNT(*) FILTER (WHERE classification_category = 'non_codeable')     AS non_codeable_count,
        COUNT(*) FILTER (WHERE classification_category = 'discharge_summary') AS discharge_summary_count,
        COUNT(*) FILTER (WHERE duplicate_flag)                                AS duplicate_count
    FROM page_classification p WHERE p.chart_id = c.id
) pc ON true

LEFT JOIN LATERAL (
    SELECT COUNT(*) AS sequenced_page_count FROM page_sequencing_results s WHERE s.chart_id = c.id
) seq ON true

LEFT JOIN LATERAL (
    SELECT COUNT(DISTINCT encounter_type) AS encounter_count FROM encounter_type_results e WHERE e.chart_id = c.id
) enc ON true

LEFT JOIN LATERAL (
    SELECT MIN(date_of_service_from) AS earliest_dos, MAX(date_of_service_to) AS latest_dos
    FROM dos_extraction_results d WHERE d.chart_id = c.id
) dos ON true

LEFT JOIN LATERAL (
    SELECT bool_or(signature_present) AS any_signature_present
    FROM provider_signature_results p WHERE p.chart_id = c.id
) sig ON true

LEFT JOIN LATERAL (
    SELECT * FROM invoice_matching_results i WHERE i.chart_id = c.id ORDER BY i.created_at DESC LIMIT 1
) im ON true

LEFT JOIN LATERAL (
    SELECT COUNT(*) AS row_count FROM ground_truth_csv g WHERE g.chart_id = c.id
) gt ON true;

-- =====================================================================
-- End of schema v7
-- =====================================================================
