-- 2026-11-25: qa_ground_truth -- review-ui's Manual Review upserts every QA Ground Truth file's
-- rows here (chart_name, page_number). Re-runnable.

CREATE TABLE IF NOT EXISTS qa_ground_truth (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    page_number             INT NOT NULL,
    member_name             TEXT,
    member_dob              TEXT,
    member_id               TEXT,
    printed_or_handwritten  TEXT,
    dos_from                TEXT,
    dos_to                  TEXT,
    is_blank_or_junk        TEXT,
    is_duplicate            TEXT,
    page_type               TEXT,
    page_subtype            TEXT,
    codeable                TEXT,
    provider_credentials    TEXT,
    current_sequence        TEXT,
    actual_sequence         TEXT,
    labels                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_file             TEXT,
    user_id                 BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_qa_ground_truth_chart_page UNIQUE (chart_name, page_number)
);
CREATE INDEX IF NOT EXISTS idx_qa_ground_truth_chart_name ON qa_ground_truth(chart_name);

DROP TRIGGER IF EXISTS trg_qa_ground_truth_updated_at ON qa_ground_truth;
CREATE TRIGGER trg_qa_ground_truth_updated_at
    BEFORE UPDATE ON qa_ground_truth
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
