-- =====================================================================
-- Clear the imaging-pipeline schema (public) — DESTRUCTIVE
-- =====================================================================
-- Wipes every V1/V2 table, view, function and trigger in ``public``, then
-- leaves an empty schema ready for a fresh apply of schema.sql.
--
--     psql "$DATABASE_URL" -f schema/tools/clear_schema.sql
--     psql "$DATABASE_URL" -f schema/schema.sql
--
-- Or:  ./scripts/reset_db.sh --yes
--
-- Does NOT drop the PostgreSQL database itself — only its public contents.
-- Connect as a role that owns the objects (typically the DATABASE_URL user).
-- =====================================================================

SET client_min_messages TO WARNING;

DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;

-- Restore the usual grants so the next apply of v1/v2 can create objects.
GRANT ALL ON SCHEMA public TO CURRENT_USER;
GRANT ALL ON SCHEMA public TO public;

COMMENT ON SCHEMA public IS 'imaging pipeline — cleared; apply schema/schema.sql next';
