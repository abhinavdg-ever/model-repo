-- Bring an existing imaging-pipeline database up to schema/schema.sql.
--
-- Runs every file in schema/migrations/ in date order. Each file only adds or replaces
-- (ADD COLUMN IF NOT EXISTS, CREATE ... IF NOT EXISTS, CREATE OR REPLACE VIEW,
-- ON CONFLICT DO NOTHING), so this is safe to run on any database built from schema.sql, at
-- any age, as often as you like. Stops at the first error.
-- A NEW database does not need this: apply schema.sql alone.
--
--   Ubuntu VM / macOS:   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema/migrate.sql
--   Windows PowerShell:  psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f schema\migrate.sql
--
-- Run it as the owner of the tables (the DATABASE_URL user). \ir resolves each file
-- next to this one, so it works from any folder.
-- When you add a migration: a new dated file in migrations/, a new line below, the same
-- change in schema.sql, and a line at the top of schema/CHANGELOG.md.

\set ON_ERROR_STOP on
SET client_min_messages TO WARNING;

\echo '2026-10-16  ground truth Member ID, kv_extract stage, page-tag columns, accuracy view'
\ir migrations/2026-10-16.sql
\echo '2026-10-19  provider signature v2, continuity gap scorer, manifest metadata, users, encounter type'
\ir migrations/2026-10-19.sql
\echo '2026-10-21  encounter review, project scope, heading candidates'
\ir migrations/2026-10-21.sql
\echo '2026-10-22  duplicate confidence'
\ir migrations/2026-10-22.sql
\echo '2026-10-25  imaging_final confidences, Production Mode columns'
\ir migrations/2026-10-25.sql
\echo '2026-10-27  ground-truth encounter label, member other_names'
\ir migrations/2026-10-27.sql
\echo '2026-10-28  chart_review'
\ir migrations/2026-10-28.sql
\echo '2026-10-31  junk subtype Cards'
\ir migrations/2026-10-31.sql
\echo '2026-11-07  chart tags, stage label, accuracy view, run_usage monitoring'
\ir migrations/2026-11-07.sql
\echo 'Done: the database matches schema/schema.sql.'
