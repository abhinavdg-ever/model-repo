-- Database changes of 2026-11-07 for a database that already has schema/v1.sql
-- applied (a new database gets this from v1.sql itself). Safe to run more than once.
--
--   psql "$DATABASE_URL" -f schema/migrations/2026-11-07.sql          (macOS / Linux / VM)
--   psql $env:DATABASE_URL -f schema/migrations/2026-11-07.sql        (Windows PowerShell)

-- 1. chart_list.tags (2026-11-07): free-text tags on a chart, a JSON array of strings, set
--    with utilities/tag_chart.py. review-ui's History and Accuracy filter on them.
ALTER TABLE chart_list ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 2. pipeline_stage label (2026-11-07): the spelling v1.sql has used since 2026-10-27.
UPDATE pipeline_stage SET label = 'Codable / Non Codable (TF)'
 WHERE stage_name = 'page_subtype' AND pass_no = 1 AND label <> 'Codable / Non Codable (TF)';

-- 3. users.username and password_hash are NOT NULL in v1.sql. A database that got users
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

-- 4. v_accuracy_page as in v1.sql. review-ui re-creates it when the accuracy report first
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
