-- 2026-11-24: confidence_calibration -- review-ui's Page Type / Codability confidence curves
-- (fitted in the Confidence Analysis, saved by an admin, applied on read). Re-runnable.

CREATE TABLE IF NOT EXISTS confidence_calibration (
    id          BIGSERIAL PRIMARY KEY,
    feature     VARCHAR(40) NOT NULL UNIQUE,
    knots       JSONB NOT NULL,
    pages       INT NOT NULL,
    user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_confidence_calibration_updated_at ON confidence_calibration;
CREATE TRIGGER trg_confidence_calibration_updated_at
    BEFORE UPDATE ON confidence_calibration
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
