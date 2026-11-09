-- Database changes of 2026-11-07 for a database that already has schema/schema.sql
-- applied (a new database gets this from schema.sql itself). Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/migrations/2026-11-07.sql          (macOS / Linux / VM)

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

-- 5. (removed 2026-11-09, user) Part 2's proposal tables are not created by migrations: no
--    running code reads or writes them. schema.sql still defines them for a new database.


-- 6. run_usage (2026-11-07): monitoring, one row per chart run, plus daily / hourly views.
-- Monitoring (2026-11-07): one row per chart run: time, pages, Doc Intel pages and cost, LLM
-- tokens and cost. Written by core-pipeline when a run ends (core-pipeline/apps/worker/usage.py). Prices
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
