-- =====================================================================
-- AI Imaging & Coding Pipeline — PostgreSQL Schema  ·  V2 (NEXT PHASE)
-- =====================================================================
-- NOTHING IN THIS FILE IS IMPLEMENTED YET. Every table here is a proposal:
-- defined so the design is reviewable, but no running code writes or reads
-- any of it (except the consolidated_chart_results view, which joins V1
-- tables that already exist after v1.sql).
--
-- SETUP
-- ---------------------------------------------------------------------
--     psql "$DATABASE_URL" -f schema/v1.sql        # FIRST — required
--     psql "$DATABASE_URL" -f schema/v2.sql        # optional
--
-- Do not apply v2.sql before v1.sql: every table here FKs into V1.
-- An existing database is recreated from v1.sql, then this file.
--
-- WHAT IS IN HERE (proposals only)
-- ---------------------------------------------------------------------
--   users                        reviewer identity (V2 tables FK to it)
--   chunk_results                OCR text chunking
--   rejection_results            reviewer accept/reject/flag
--   provider_signature_results   selected electronic signature
--   invoice_matching_results     invoice reconciliation
--   ground_truth_csv             labelled data import
--   model_registry               model versions + storage
--   field_accuracy_log           per-field prediction vs truth
--   model_accuracy_snapshots     aggregate metrics per model
--   manual_review                reviewer corrections
--   audit_log                    who did what
--   consolidated_chart_results   one wide reporting row per chart
--   pipeline_stage_performance   stage timings and success rates
--   model_accuracy_latest        newest metric per model
--
-- Registers rejection_logic (is_phase1 = FALSE) and kv_extract (seq 56).
-- page_classification / encounter_type_results / page_sequencing_results
-- live in v1.sql now.
--
-- ---------------------------------------------------------------------
-- NAMING CONVENTIONS — every table in BOTH files obeys these. A new table
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
--     rotation_applied, signature_present, use_corrected.
-- 10. Indexes:  idx_<table>_<columns>.  Views:  v_<name> for helper views;
--     report views keep their business name (consolidated_chart_results).
--
-- WHAT CHANGED
-- ---------------------------------------------------------------------
--  * dos_extraction_results.extraction_method also allows 'kv' (a date the
--    key/value ranker supplied). The CHECK is widened below; v1.sql is unchanged.
--  * pipeline_stage gains kv_extract (seq 56), after section_headers.
--  * provider_signature_results follows the electronic-signature extractor
--    (key, region, scale, sentence, ner text, provider name, signature date
--    converted from the printed form to a DATE, confidence, source).
--  * additional_page_details (printed page number + section_headers JSON)
--    moved to v1.sql: the key/value stage writes it.
--  * page_continuity_results (v1.sql) and the continuity stage (seq 87): each
--    page's document, and the Final page type / DOS carried from the
--    document's first page. Replaces the DOS progress-note span and the
--    page-type span; nothing existing changes shape.
--  * imaging_final (v1.sql) and the imaging_final stage (seq 100): the Final
--    value of every reviewer field, one row per page. A duplicate page copies
--    the page it duplicates: source 'duplicate' + duplicate_of_page_id.
--  * page_list.use_corrected + image_path (workspace image source).
--  * page_classification lives in v1.sql (page_subtype writes it).
--  * encounter_type_results + page_sequencing_results live in v1.sql.
--  * member_verification_summary.final_status also allows 'skipped'
--    (all-blank/junk charts). Defined in v1.sql.
--  * ocr_quality_results gains document_type, handwritten_probability,
--    is_visible, handwritten_area_pct and review_required (page-tag
--    model). Defined in v1.sql, with the ALTER for an existing database.
--
-- WHAT CHANGED IN v8 (applies to both files)
-- ---------------------------------------------------------------------
--  * schema/migrations/ and the duplicate v6 copy deleted. The schema is
--    these two files; a pre-v8 database is recreated, not upgraded.
--  * Column names normalised to the conventions above:
--      chart_list.blob_container_name        -> blob_container
--      chart_list.path                       -> blob_path
--      ocr_quality_results.confidence        -> hw_confidence
--      ocr_quality_results.quality_tag       -> hw_method (it held the
--                                               classifier's method, not a
--                                               quality grade)
--      page_classification.confidence_score  -> confidence
--      invoice_matching_results.similarity_score -> confidence
--      chunk_results.chunk_index             -> seq
--      page_sequencing_results.sequence_no   -> seq
--      member_verification_summary.matched_member_id -> matched_member_list_id
--      member_verification_summary.decided_at        -> created_at/updated_at
--      rejection_results.reviewed_by         -> user_id
--      ground_truth_csv.imported_by/_at      -> user_id/created_at
--      field_accuracy_log.compared_at        -> created_at
--      model_accuracy_snapshots.evaluated_at -> created_at
--      manual_review.review_id               -> user_id
--      pipeline_jobs.attempt_number          -> attempt
--      manifest_member_list.source_blob_path -> source_path (it also holds
--                                               local paths, not just blob)
--  * created_at + updated_at + trigger on every table.
--  * dos_extraction_dates MERGED into dos_extraction_results as the
--    multi-valued `dates` JSONB column. One row per page.
--  * ocr_quality_results gained quality_tag / quality_score (a documented
--    placeholder) and hw_method / hw_confidence (the real classifier output).
--  * member_extraction_results.page_name dropped; join page_list.
--  * v_chart_status_legacy dropped — no v6 consumer remains.
-- =====================================================================

SET search_path TO public;


-- ---------------------------------------------------------------------
-- STAGE REGISTRY — unorchestrated stages only
-- ---------------------------------------------------------------------
-- v1.sql seeds the phase-1 stages. rejection_logic stays here with
-- is_phase1 = FALSE so registering it cannot stall a chart.

INSERT INTO pipeline_stage (stage_name, pass_no, seq, label, is_phase1) VALUES
    ('kv_extract',       1, 56,  'Key/Value Extraction',         TRUE),
    ('rejection_logic',  1, 120, 'Rejection Logic',              FALSE)
ON CONFLICT (stage_name, pass_no) DO NOTHING;

-- v1.sql allows rules / llm / rules+llm. A date the key/value ranker supplied
-- is 'kv'. Widen the check without editing v1.sql.
ALTER TABLE dos_extraction_results
    DROP CONSTRAINT IF EXISTS dos_extraction_results_extraction_method_check;
ALTER TABLE dos_extraction_results
    ADD CONSTRAINT dos_extraction_results_extraction_method_check
    CHECK (extraction_method IS NULL OR extraction_method IN
           ('rules','llm','rules+llm','kv'));

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
-- page_classification lives in v1.sql (codeable / non-codeable / discharge).

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

-- encounter_type_results + page_sequencing_results live in v1.sql now.

-- ---------------------------------------------------------------------
-- VERIFICATION & DECISION
-- ---------------------------------------------------------------------
-- Columns mirror the V1 Member_Verification contract so a ported run is
-- diffable against the reference implementation.


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


-- Selected electronic signature. Columns follow the extractor row
-- (Key, Region, Scale, Sentence, Ner_Text, ProviderName, SignatureDate,
-- Score, Source). The printed date (03/15/2024) is stored converted.
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
CREATE INDEX idx_provider_signature_results_chart_id ON provider_signature_results(chart_id);
CREATE INDEX idx_provider_signature_results_page_id ON provider_signature_results(page_id);

CREATE TRIGGER trg_provider_signature_results_updated_at
    BEFORE UPDATE ON provider_signature_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


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
