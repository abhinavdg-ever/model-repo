-- 2026-11-13: v_accuracy_page gains gt_is_duplicate (page_ground_truth.is_duplicate) for the
-- Accuracy View's Duplicate accuracy. Added at the end: CREATE OR REPLACE keeps the rest.
-- review-ui also re-creates the view on its first accuracy report. Re-runnable.

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
