# Schema changelog

Newest first. Before 2026-11-07 the schema was two files: `v1.sql` (implemented) and `v2.sql`
(proposals). Both are now `schema.sql`, Part 1 and Part 2. Older entries keep the old names.

```
 * schema/ layout (2026-11-07): the dated changes_*.sql files and temp_changes_to_db.sql
   are now schema/migrations/<date>.sql, run in order by schema/migrate.sql (safe to
   re-run on any v1 database). clear_*.sql moved to schema/tools/. v1.sql no longer
   creates the unused pgcrypto extension. migrations/2026-11-07.sql also brings
   v_accuracy_page, the page_subtype stage label and users NOT NULL in line with v1.sql.
 * chart_list.tags (v1.sql, 2026-11-07): free-text tags on a chart (JSON array), set with
   utilities/tag_chart.py, for review-ui's Tag filter on History and Accuracy
   (migrations/2026-11-07.sql).
 * blank_junk_classification.junk_subtype (v1.sql, 2026-10-31): 'Cards' added to the
   CHECK, for ID / insurance cards found by the blank / junk rule R3C
   (migrations/2026-10-31.sql).
 * chart_review_entry (v1.sql, 2026-10-28): every chart-level review action, append-only
   -- Reviewed marked and cleared, member decision, encounters -- for the Review Log
   (migrations/2026-10-28.sql).
 * chart_review, idx_page_ground_truth_entry_user_id_entry_mode (v1.sql, 2026-10-28): a
   chart marked Reviewed freezes its ground truth; the per-user Review Log
   (migrations/2026-10-28.sql).
 * idx_blank_junk_classification_duplicate_of_page_id, idx_imaging_final_duplicate_of_page_id
   (v1.sql, 2026-10-28): page deletes no longer scan those tables (migrations/2026-10-27.sql).
 * chart_ground_truth (v1.sql, 2026-10-28): the reviewer's member Accept / Reject per chart
   (migrations/2026-10-27.sql).
 * member_extraction_results.other_names (v1.sql, 2026-10-27): the people a page names
   who are not the manifest member (migrations/2026-10-27.sql).
 * page_ground_truth.encounter_label (v1.sql, 2026-10-27): the page's encounter type as
   entered in review-ui; encounter_type keeps the client sheet's page type
   (migrations/2026-10-27.sql).
 * imaging_final.confidences / review_fields (v1.sql, 2026-10-25): every Final value's
   confidence on one scale and the values below the review line (migrations/2026-10-25.sql).
 * Production Mode from Postgres only (v1.sql, 2026-10-25): encounter_type_results gains
   encounter_seq / decided_by / reason / conflict / contenders (the encounter grouping),
   additional_page_details.extraction_fields (the Extraction tab),
   page_continuity_results.label (the continuity label as shown).
 * blank_junk_classification.duplicate_confidence (v1.sql, 2026-10-22): each page's
   highest duplicate similarity, appended to v_page_blank_junk_final
   (migrations/2026-10-22.sql).
 * encounter_review (v1.sql, 2026-10-21): a reviewer's encounters (page ranges with
   page type, encounter type, DOS) from review-ui's Encounter tab; not ground truth
   (migrations/2026-10-21.sql).
 * encounter_type_results (v1.sql, 2026-10-20): encounter_type allows 'not_applicable'
   (encounters that are not Progress Note / Discharge Summary; section 10).
 * page_continuity_results.confidence (v1.sql, 2026-10-20): the 0-1 confidence of each
   page's Break Before decision, beside score (migrations/2026-10-19.sql section 9).
 * page_continuity_results (v1.sql, 2026-10-20): decided_by gains the gap scorer's reasons;
   its scores go to <chart>/staging/continuity.json.
 * chart_deletion (v1.sql, 2026-10-20): charts an admin deleted in review-ui.
 * users moved to v1.sql (review-ui sign-in: username, password_hash, last_login_*,
   login_count; email optional), with user_login (every sign-in).
   page_ground_truth and page_ground_truth_entry gain user_id (who saved it).
   Existing databases: schema/migrations/2026-10-19.sql.
 * manifest_member_list (v1.sql) gains project_scope (2026-10-21; the client's "Project
   Scope", review-ui's Scope filter). Existing databases: schema/migrations/2026-10-21.sql
   (core-pipeline also adds it on the first manifest load).
 * additional_page_details (v1.sql) gains heading_candidates (2026-10-21): every heading
   row the key/value detector found, accepted or not, for review-ui's Production Mode.
 * manifest_member_list (v1.sql) gains client_id, project_id, source_name, rr_method,
   measure and service_note (TEXT): the client's chart metadata. Existing
   databases: schema/migrations/2026-10-19.sql (which also holds the 2026-10-18 changes).
 * provider_signature_results moved to v1.sql (2026-10-18): kv_extract writes it
   and imaging_final reads it, with provider_credentials, signers (every
   accepted signer) and provider_version. imaging_final gains
   provider_credentials, signers and provider_version. Existing databases:
   schema/migrations/2026-10-16.sql.
 * imaging_final.specialty (v1.sql): the page's own specialty from the page
   axes. Existing databases:
     ALTER TABLE imaging_final ADD COLUMN IF NOT EXISTS specialty VARCHAR(100);
 * dos_extraction_results.extraction_method also allows 'kv' (a date the
   key/value ranker supplied). In v1.sql since 2026-10-27; widened below for older databases.
 * pipeline_stage gains kv_extract (seq 56), after section_headers.
 * provider_signature_results follows the electronic-signature extractor
   (key, region, scale, sentence, ner text, provider name, signature date
   converted from the printed form to a DATE, confidence, source).
 * additional_page_details (printed page number + section_headers JSON)
   moved to v1.sql: the key/value stage writes it.
 * page_continuity_results (v1.sql) and the continuity stage (seq 87): each
   page's document, and the Final page type / DOS carried from the
   document's first page. Replaces the DOS progress-note span and the
   page-type span; nothing existing changes shape.
 * imaging_final (v1.sql) and the imaging_final stage (seq 100): the Final
   value of every reviewer field, one row per page. A duplicate page copies
   the page it duplicates: source 'duplicate' + duplicate_of_page_id.
 * page_continuity_results.in_progress_note: the document opens like a
   progress note and ends signed; imaging_final makes each page Progress
   Note with its own type as the sub-type (source 'progress_note').
 * page_list.use_corrected + image_path (workspace image source).
 * page_classification lives in v1.sql (page_subtype writes it).
 * encounter_type_results + page_sequencing_results live in v1.sql.
 * member_verification_summary.final_status also allows 'skipped'
   (all-blank/junk charts). Defined in v1.sql.
 * v_accuracy_page (v1.sql) appends orientation_angle, rotation_known and
   the Final page type / sub-type / category / DOS from imaging_final, so
   the accuracy view can score Orientation and Final ahead of Extracted.
 * ocr_quality_results.orientation_angle holds the text's orientation as
   scanned (degrees clockwise), not the correction applied.
 * ocr_quality_results gains document_type, handwritten_probability,
   is_visible, handwritten_area_pct and review_required (page-tag
   model). Defined in v1.sql, with the ALTER for an existing database.
 * v_accuracy_page (v1.sql) appends gt_provider_credentials. Review UI saves
   Provider Credentials to page_ground_truth.provider_credentials and
   Provider Signature (Yes / No) to provider_signature; older rows with
   credentials in provider_signature are read as both.
 * page_classification (v1.sql) gains axes JSONB: the v24 page axes
   (L1/L2/L3, specialty, codeability, who decided each, the model's raw
   answer). page_type / page_subtype now hold the v24 names built from them.
   ALTER TABLE page_classification ADD COLUMN IF NOT EXISTS axes JSONB;

WHAT CHANGED IN v8 (applies to both files)
---------------------------------------------------------------------
 * schema/migrations/ and the duplicate v6 copy deleted. The schema is
   these two files; a pre-v8 database is recreated, not upgraded.
 * Column names normalised to the conventions above:
     chart_list.blob_container_name        -> blob_container
     chart_list.path                       -> blob_path
     ocr_quality_results.confidence        -> hw_confidence
     ocr_quality_results.quality_tag       -> hw_method (it held the
                                              classifier's method, not a
                                              quality grade)
     page_classification.confidence_score  -> confidence
     invoice_matching_results.similarity_score -> confidence
     chunk_results.chunk_index             -> seq
     page_sequencing_results.sequence_no   -> seq
     member_verification_summary.matched_member_id -> matched_member_list_id
     member_verification_summary.decided_at        -> created_at/updated_at
     rejection_results.reviewed_by         -> user_id
     ground_truth_csv.imported_by/_at      -> user_id/created_at
     field_accuracy_log.compared_at        -> created_at
     model_accuracy_snapshots.evaluated_at -> created_at
     manual_review.review_id               -> user_id
     pipeline_jobs.attempt_number          -> attempt
     manifest_member_list.source_blob_path -> source_path (it also holds
                                              local paths, not just blob)
 * created_at + updated_at + trigger on every table.
 * dos_extraction_dates MERGED into dos_extraction_results as the
   multi-valued `dates` JSONB column. One row per page.
 * ocr_quality_results gained quality_tag / quality_score (a documented
   placeholder) and hw_method / hw_confidence (the real classifier output).
 * member_extraction_results.page_name dropped; join page_list.
 * v_chart_status_legacy dropped — no v6 consumer remains.
```
