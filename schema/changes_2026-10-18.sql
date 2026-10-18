-- Database changes of 2026-10-18 for a database that already has schema/v1.sql
-- applied (a new database gets all of this from v1.sql itself). Additive only:
-- nothing is dropped or rewritten, existing rows keep their values. Safe to run
-- more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-10-18.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-10-18.sql        (Windows PowerShell)
--
-- What it is for: docs/PROVIDER_LOGIC.md — the provider signature rule v2
-- (every accepted signer, credentials in their own column, a confidence), stored
-- in provider_signature_results by kv_extract and carried into imaging_final.
-- review-ui reads each row by provider_version (2 = this rule; 1 / NULL = older
-- rows, read the old way), so old and new rows live side by side.

-- 1. provider_signature_results: written by kv_extract, read by imaging_final.
--    (It used to be defined in v2.sql only, and nothing wrote it.)
CREATE TABLE IF NOT EXISTS provider_signature_results (
    id                    BIGSERIAL PRIMARY KEY,
    chart_id              BIGINT NOT NULL REFERENCES chart_list(id) ON DELETE CASCADE,
    page_id               BIGINT NOT NULL REFERENCES page_list(id) ON DELETE CASCADE,
    signature_present     BOOLEAN NOT NULL DEFAULT FALSE,
    signature_key         VARCHAR(200),
    region                VARCHAR(50),
    scale                 VARCHAR(50),
    sentence              TEXT,
    ner_text              TEXT,
    provider_name         TEXT,
    signature_date        DATE,
    confidence            NUMERIC(5,4),
    source                VARCHAR(20),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id)
);
CREATE INDEX IF NOT EXISTS idx_provider_signature_results_chart_id
    ON provider_signature_results(chart_id);
CREATE INDEX IF NOT EXISTS idx_provider_signature_results_page_id
    ON provider_signature_results(page_id);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_provider_signature_results_updated_at'
    ) THEN
        CREATE TRIGGER trg_provider_signature_results_updated_at
            BEFORE UPDATE ON provider_signature_results
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    END IF;
END $$;

-- The rule's columns: the first signer's credentials, every signer
-- ([{"name", "credentials", "signature_date", "confidence"}]), the rule version.
-- confidence (already there) = the signature's score; x0.75 when the name or
-- credentials came from outside it; several signatures: each x0.9, the page their average.
ALTER TABLE provider_signature_results
    ADD COLUMN IF NOT EXISTS provider_credentials TEXT,
    ADD COLUMN IF NOT EXISTS signers JSONB,
    ADD COLUMN IF NOT EXISTS provider_version SMALLINT;

-- 2. imaging_final: the same provider values, plus the confidence.
ALTER TABLE imaging_final
    ADD COLUMN IF NOT EXISTS provider_credentials TEXT,
    ADD COLUMN IF NOT EXISTS signers JSONB,
    ADD COLUMN IF NOT EXISTS provider_version SMALLINT,
    ADD COLUMN IF NOT EXISTS provider_confidence NUMERIC(5,4);

-- 3. v_accuracy_page (review-ui's accuracy view) gains final_provider_credentials,
--    final_signers and final_provider_version. review-ui re-creates the view itself
--    the first time the accuracy report loads; nothing to run here.

-- 4. Continuity: the weighted revamp (docs/CONTINUITY_LOGIC.md). decided_by gains
--    'weighted'; the old ladder's values stay allowed for rows written before.
--    score and review_required (already there) are written again.
ALTER TABLE page_continuity_results DROP CONSTRAINT IF EXISTS page_continuity_results_decided_by_check;
ALTER TABLE page_continuity_results ADD CONSTRAINT page_continuity_results_decided_by_check
    CHECK (decided_by IN (
        'first_page','blank_junk','same_dos','new_dos','pagination',
        'signature','page_number_end','weighted',
        'first_page_marker','page_type','page_end','continue_page','progress_note'
    )) NOT VALID;  -- checks new rows; rows from older engines are left as they are
