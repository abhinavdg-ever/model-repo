-- Database changes of 2026-11-07 for a database that already has schema/schema.sql
-- applied (a new database gets this from schema.sql itself). Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/migrations/2026-11-07.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/migrations/2026-11-07.sql        (Windows PowerShell)

-- 1. chart_list.tags (2026-11-07): free-text tags on a chart, a JSON array of strings, set
--    with utilities/tag_chart.py. review-ui's History and Accuracy filter on them.
ALTER TABLE chart_list ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 2. pipeline_stage label (2026-11-07): the spelling schema.sql has used since 2026-10-27.
UPDATE pipeline_stage SET label = 'Codable / Non Codable (TF)'
 WHERE stage_name = 'page_subtype' AND pass_no = 1 AND label <> 'Codable / Non Codable (TF)';

-- 3. users.username and password_hash are NOT NULL in schema.sql. A database that got users
--    from migrations/2026-10-19.sql allowed NULL; tightened only when no row has one.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE username IS NULL OR password_hash IS NULL) THEN
        ALTER TABLE users ALTER COLUMN username SET NOT NULL;
        ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;
    ELSE
        RAISE WARNING 'users has rows without a username or password_hash: left nullable';
    END IF;
END $$;

-- 4. v_accuracy_page as in schema.sql. review-ui re-creates it when the accuracy report first
--    loads; a database brought up through these files had the 2026-10-16 version until then
--    (no Final, rotation or provider-credential columns). Columns are only appended, so the
--    view is replaced in place.
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
    g.updated_at                                              AS ground_truth_updated_at,
    -- Appended (a replaced view keeps its earlier columns in order): the
    -- stored text orientation, and the Final values the accuracy view scores
    -- ahead of the Extracted ones.
    q.orientation_angle,
    EXISTS (SELECT 1 FROM ocr_quality_results qx WHERE qx.chart_id = c.id) AS rotation_known,
    f.page_type                                               AS final_page_type,
    f.page_subtype                                            AS final_page_subtype,
    f.classification_category                                 AS final_category,
    f.dos_from                                                AS final_dos_from,
    f.dos_to                                                  AS final_dos_to,
    -- Provider Credentials ground truth (provider_signature is Yes / No).
    g.provider_credentials                                    AS gt_provider_credentials,
    -- The signature rows' provider and credentials, as review-ui shows them.
    f.provider_name                                           AS final_provider_name,
    to_jsonb(f) ->> 'provider_credentials'                    AS final_provider_credentials,
    to_jsonb(f) -> 'signers'                                  AS final_signers,
    to_jsonb(f) ->> 'provider_version'                        AS final_provider_version,
    -- Provider Signature accuracy: did the pipeline find a signature ('true' / 'false';
    -- NULL on a database whose imaging_final has no such column).
    to_jsonb(f) ->> 'signature_present'                       AS final_signature_present
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
LEFT JOIN page_classification pc ON pc.page_id = pl.id
LEFT JOIN ocr_quality_results q ON q.page_id = pl.id
LEFT JOIN imaging_final f ON f.page_id = pl.id;

-- 5. One database (2026-11-07): v1.sql and v2.sql became schema/schema.sql, Part 1
--    (implemented) and Part 2 (not implemented). Part 2's tables used to be optional;
--    they are now created everywhere, empty, so every database has the same shape.
--    Same statements as schema.sql's Part 2 (tests/test_schema_migrations.py checks).
-- >>> PART 2 START
-- ---------------------------------------------------------------------
-- STAGE REGISTRY — unorchestrated stages only
-- ---------------------------------------------------------------------
-- rejection_logic stays out of the chain (is_phase1 = FALSE), so registering it
-- cannot stall a chart.

INSERT INTO pipeline_stage (stage_name, pass_no, seq, label, is_phase1) VALUES
    ('rejection_logic',  1, 120, 'Rejection Logic',              FALSE)
ON CONFLICT (stage_name, pass_no) DO NOTHING;

-- ---------------------------------------------------------------------
-- PAGE-LEVEL RESULT TABLES
-- ---------------------------------------------------------------------
-- page_classification lives in Part 1 (codeable / non-codable / discharge).

CREATE TABLE IF NOT EXISTS chunk_results (
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

DROP TRIGGER IF EXISTS trg_chunk_results_updated_at ON chunk_results;
CREATE TRIGGER trg_chunk_results_updated_at
    BEFORE UPDATE ON chunk_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX IF NOT EXISTS idx_chunk_results_chart_id ON chunk_results(chart_id);
CREATE INDEX IF NOT EXISTS idx_chunk_results_page_id ON chunk_results(page_id);

-- encounter_type_results + page_sequencing_results live in Part 1 now.

-- ---------------------------------------------------------------------
-- VERIFICATION & DECISION
-- ---------------------------------------------------------------------
-- Columns mirror the Part 1 Member_Verification contract so a ported run is
-- diffable against the reference implementation.


CREATE TABLE IF NOT EXISTS rejection_results (
    id            BIGSERIAL PRIMARY KEY,
    chart_id      BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    decision      VARCHAR(10) NOT NULL CHECK (decision IN ('accept','reject','flag')),
    reason        TEXT,
    user_id       BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_rejection_results_updated_at ON rejection_results;
CREATE TRIGGER trg_rejection_results_updated_at
    BEFORE UPDATE ON rejection_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX IF NOT EXISTS idx_rejection_results_chart_id ON rejection_results(chart_id);




CREATE TABLE IF NOT EXISTS invoice_matching_results (
    id                  BIGSERIAL PRIMARY KEY,
    chart_id            BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    invoice_reference   VARCHAR(100),
    match_status        VARCHAR(30),
    confidence          NUMERIC(5,4),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_invoice_matching_results_updated_at ON invoice_matching_results;
CREATE TRIGGER trg_invoice_matching_results_updated_at
    BEFORE UPDATE ON invoice_matching_results
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX IF NOT EXISTS idx_invoice_matching_results_chart_id ON invoice_matching_results(chart_id);


CREATE TABLE IF NOT EXISTS ground_truth_csv (
    id              BIGSERIAL PRIMARY KEY,
    chart_id        BIGINT REFERENCES chart_list(id) ON DELETE CASCADE,
    csv_file_name   VARCHAR(255) NOT NULL,
    row_number      INT,
    labeled_fields  JSONB NOT NULL,
    user_id         BIGINT REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_ground_truth_csv_updated_at ON ground_truth_csv;
CREATE TRIGGER trg_ground_truth_csv_updated_at
    BEFORE UPDATE ON ground_truth_csv
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX IF NOT EXISTS idx_ground_truth_csv_chart_id ON ground_truth_csv(chart_id);

-- ---------------------------------------------------------------------
-- OPERATIONAL
-- ---------------------------------------------------------------------


-- ---------------------------------------------------------------------
-- OPERATIONAL
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_registry (
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

DROP TRIGGER IF EXISTS trg_model_registry_updated_at ON model_registry;
CREATE TRIGGER trg_model_registry_updated_at
    BEFORE UPDATE ON model_registry
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX IF NOT EXISTS idx_model_registry_name_active ON model_registry(model_name, is_active);

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


CREATE TABLE IF NOT EXISTS field_accuracy_log (
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

DROP TRIGGER IF EXISTS trg_field_accuracy_log_updated_at ON field_accuracy_log;
CREATE TRIGGER trg_field_accuracy_log_updated_at
    BEFORE UPDATE ON field_accuracy_log
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX IF NOT EXISTS idx_field_accuracy_log_chart_id ON field_accuracy_log(chart_id);
CREATE INDEX IF NOT EXISTS idx_field_accuracy_log_model_id ON field_accuracy_log(model_id);


CREATE TABLE IF NOT EXISTS model_accuracy_snapshots (
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

DROP TRIGGER IF EXISTS trg_model_accuracy_snapshots_updated_at ON model_accuracy_snapshots;
CREATE TRIGGER trg_model_accuracy_snapshots_updated_at
    BEFORE UPDATE ON model_accuracy_snapshots
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX IF NOT EXISTS idx_model_accuracy_snapshots_model_id ON model_accuracy_snapshots(model_id);


CREATE OR REPLACE VIEW model_accuracy_latest AS
SELECT DISTINCT ON (mr.id, s.metric_name)
    mr.id AS model_id, mr.model_name, mr.version, mr.storage_type,
    s.metric_name, s.metric_value, s.sample_size, s.created_at
FROM model_registry mr
JOIN model_accuracy_snapshots s ON s.model_id = mr.id
ORDER BY mr.id, s.metric_name, s.created_at DESC;


CREATE TABLE IF NOT EXISTS manual_review (
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
CREATE INDEX IF NOT EXISTS idx_manual_review_chart_id ON manual_review(chart_id);
CREATE INDEX IF NOT EXISTS idx_manual_review_page_id ON manual_review(page_id);
CREATE INDEX IF NOT EXISTS idx_manual_review_user_id ON manual_review(user_id);
CREATE INDEX IF NOT EXISTS idx_manual_review_module_type ON manual_review(module_type);

DROP TRIGGER IF EXISTS trg_manual_review_updated_at ON manual_review;
CREATE TRIGGER trg_manual_review_updated_at
    BEFORE UPDATE ON manual_review
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    chart_id    BIGINT REFERENCES chart_list(id) ON DELETE SET NULL,
    user_id     BIGINT REFERENCES users(id),
    action      VARCHAR(50) NOT NULL,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_audit_log_updated_at ON audit_log;
CREATE TRIGGER trg_audit_log_updated_at
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_audit_log_chart_id ON audit_log(chart_id);

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
-- <<< PART 2 END

-- 6. run_usage (2026-11-07): monitoring, one row per chart run, plus daily / hourly views.
-- Monitoring (2026-11-07): one row per chart run: time, pages, Doc Intel pages and cost, LLM
-- tokens and cost. Written by core-pipeline when a run ends (orchestrator/usage.py). Prices
-- come from .env (PRICE_*) and are stored with the row, so a later price change never
-- rewrites history. v_run_usage_daily / v_run_usage_hourly add them up.
CREATE TABLE IF NOT EXISTS run_usage (
    id                       BIGSERIAL PRIMARY KEY,
    chart_id                 BIGINT REFERENCES chart_list(id) ON DELETE SET NULL,
    chart_name               VARCHAR(255) NOT NULL,
    trigger_id               VARCHAR(40),
    trigger_kind             VARCHAR(20),
    status                   VARCHAR(20) NOT NULL,
    partial                  TEXT,
    started_at               TIMESTAMPTZ NOT NULL,
    completed_at             TIMESTAMPTZ NOT NULL,
    seconds                  NUMERIC(10,1) NOT NULL,
    pages                    INT NOT NULL DEFAULT 0,
    ocr_ran                  BOOLEAN NOT NULL DEFAULT FALSE,
    stage_seconds            JSONB NOT NULL DEFAULT '{}'::jsonb,
    docintel_pages           INT NOT NULL DEFAULT 0,
    docintel_price_per_1000  NUMERIC(10,4),
    docintel_cost            NUMERIC(12,4) NOT NULL DEFAULT 0,
    llm_prompt_tokens        BIGINT NOT NULL DEFAULT 0,
    llm_completion_tokens    BIGINT NOT NULL DEFAULT 0,
    llm_cost                 NUMERIC(12,4) NOT NULL DEFAULT 0,
    llm_usage                JSONB NOT NULL DEFAULT '{}'::jsonb,
    currency                 VARCHAR(3) NOT NULL DEFAULT 'USD',
    error_message            TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_run_usage_updated_at ON run_usage;
CREATE TRIGGER trg_run_usage_updated_at
    BEFORE UPDATE ON run_usage
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_run_usage_started_at ON run_usage(started_at);
CREATE INDEX IF NOT EXISTS idx_run_usage_chart_id ON run_usage(chart_id);
CREATE INDEX IF NOT EXISTS idx_run_usage_trigger_id ON run_usage(trigger_id);


-- Daily and hourly totals of run_usage (by the hour a run started).
CREATE OR REPLACE VIEW v_run_usage_daily AS
SELECT date_trunc('day', started_at)::date               AS day,
       count(*)                                          AS runs,
       count(*) FILTER (WHERE status = 'failed')          AS failed,
       sum(pages)                                        AS pages,
       round(sum(seconds), 1)                            AS seconds,
       round(sum(seconds) / NULLIF(sum(pages), 0), 2)    AS seconds_per_page,
       sum(docintel_pages)                               AS docintel_pages,
       round(sum(docintel_cost), 2)                      AS docintel_cost,
       sum(llm_prompt_tokens)                            AS llm_prompt_tokens,
       sum(llm_completion_tokens)                        AS llm_completion_tokens,
       round(sum(llm_cost), 2)                           AS llm_cost,
       round(sum(docintel_cost + llm_cost), 2)           AS total_cost,
       min(currency)                                     AS currency
  FROM run_usage
 GROUP BY 1;

CREATE OR REPLACE VIEW v_run_usage_hourly AS
SELECT date_trunc('hour', started_at)                    AS hour,
       count(*)                                          AS runs,
       count(*) FILTER (WHERE status = 'failed')          AS failed,
       sum(pages)                                        AS pages,
       round(sum(seconds), 1)                            AS seconds,
       sum(docintel_pages)                               AS docintel_pages,
       round(sum(docintel_cost), 2)                      AS docintel_cost,
       sum(llm_prompt_tokens + llm_completion_tokens)    AS llm_tokens,
       round(sum(llm_cost), 2)                           AS llm_cost
  FROM run_usage
 GROUP BY 1;
