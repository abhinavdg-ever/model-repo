-- 2026-11-28: Readability (was Visibility) -- is_readable per page, from Final OCR 2's routing
-- (core-pipeline modules/ocr/readability.py). Older rows stay NULL: review-ui shows is_visible
-- (Visible / Not visible) for them. Re-runnable.

ALTER TABLE ocr_quality_results ADD COLUMN IF NOT EXISTS is_readable BOOLEAN;
ALTER TABLE imaging_final ADD COLUMN IF NOT EXISTS is_readable BOOLEAN;
