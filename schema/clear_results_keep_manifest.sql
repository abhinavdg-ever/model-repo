-- =====================================================================
-- Clear pipeline results — keep the client manifest
-- =====================================================================
-- Wipes every chart / page / OCR / imaging / job row in V1, and leaves:
--   * manifest_member_list   (client roster — needed for member_verify)
--   * pipeline_stage         (static stage registry — needed for /ready)
--
-- Does NOT drop the schema. Does NOT re-apply v1.sql.
--
--     psql "$DATABASE_URL" -f schema/clear_results_keep_manifest.sql
--
-- After this, re-run charts (or batch-run). Sweep the manifest again only
-- if you also need fresh chart_list placeholders; roster rows stay.
--
-- For a full wipe including the roster, use schema/clear_schema.sql instead.
-- =====================================================================

SET client_min_messages TO WARNING;

BEGIN;

-- Child → parent order is unnecessary with CASCADE, but listing every
-- chart-scoped table makes the intent obvious in reviews.
TRUNCATE TABLE
    member_verification_summary,
    member_extraction_results,
    dos_extraction_results,
    blank_junk_classification,
    ocr_quality_results,
    ocr_results,
    page_stage_status,
    page_list,
    chart_list,
    pipeline_jobs
    RESTART IDENTITY CASCADE;

-- Optional V2 result tables (no-op when v2.sql was never applied).
DO $$
BEGIN
    IF to_regclass('public.page_classification') IS NOT NULL THEN
        EXECUTE 'TRUNCATE TABLE
            page_classification,
            chunk_results,
            encounter_type_results,
            page_sequencing_results,
            rejection_results,
            provider_signature_results,
            invoice_matching_results,
            ground_truth_csv,
            field_accuracy_log,
            model_accuracy_snapshots,
            manual_review,
            audit_log
            RESTART IDENTITY CASCADE';
    END IF;
END $$;

COMMIT;

-- Sanity: roster + stage registry must still be present.
DO $$
DECLARE
    stages INT;
    roster INT;
BEGIN
    SELECT count(*) INTO stages FROM pipeline_stage;
    SELECT count(*) INTO roster FROM manifest_member_list;
    IF stages = 0 THEN
        RAISE EXCEPTION
            'pipeline_stage is empty after clear — re-apply schema/v1.sql';
    END IF;
    RAISE NOTICE
        'Cleared chart results. Kept pipeline_stage=% row(s), manifest_member_list=% row(s).',
        stages, roster;
END $$;
