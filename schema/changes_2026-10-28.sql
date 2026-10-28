-- Database changes of 2026-10-28 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Additive only: nothing is
-- dropped or rewritten. Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/changes_2026-10-28.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/changes_2026-10-28.sql        (Windows PowerShell)

-- 1. chart_review (2026-10-28): a chart marked Reviewed in review-ui's History. While the
--    row exists the chart's ground truth is frozen. review-ui also creates the table on its
--    first read (without the users(id) reference).
CREATE TABLE IF NOT EXISTS chart_review (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    user_id                 BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_chart_review_chart_name UNIQUE (chart_name)
);

DROP TRIGGER IF EXISTS trg_chart_review_updated_at ON chart_review;
CREATE TRIGGER trg_chart_review_updated_at
    BEFORE UPDATE ON chart_review
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 2. The Review Log reads one user's ground-truth saves.
CREATE INDEX IF NOT EXISTS idx_page_ground_truth_entry_user_id_entry_mode
    ON page_ground_truth_entry(user_id, entry_mode);
