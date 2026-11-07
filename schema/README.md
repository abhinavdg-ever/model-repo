# schema/

```
schema/
├── v1.sql              the full current schema. A NEW database needs only this.
├── v2.sql              proposals for later; nothing reads or writes these tables. Optional.
├── migrate.sql         upgrades an EXISTING database to v1.sql. Safe to run again and again.
├── migrations/         one dated file per change day; migrate.sql runs them in order.
│   └── 2026-10-16.sql … 2026-11-07.sql
└── tools/              destructive helpers. Read the file before running it.
    ├── clear_schema.sql                 drops everything in `public`
    └── clear_results_keep_manifest.sql  deletes results, keeps roster, ground truth, stages
```

## Which one do I run?

| Situation | Run |
|---|---|
| New, empty database | `v1.sql` (then `v2.sql` only if you want the proposals) |
| Existing database, any age since 2026-10-07 | `migrate.sql` |
| Not sure | `migrate.sql`: it skips whatever is already there |
| Start over (data lost) | dump first, then `tools/clear_schema.sql`, then `v1.sql` |

`migrate.sql` only adds columns, tables and indexes, widens CHECK lists and replaces views.
It never drops a column or deletes a row. The only tables it drops are two retired ones that
nothing ever read (`accuracy_snapshot`, `encounter_results`). Run it as the database user that owns the tables
(the `DATABASE_URL` user).

## Commands

**Ubuntu VM (bash)**, from the repo folder:

```bash
# existing database
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema/migrate.sql
# new database
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema/v1.sql
# check: 15 rows
psql "$DATABASE_URL" -c "SELECT stage_name, pass_no, seq FROM pipeline_stage ORDER BY seq;"
```

No `psql` on the VM? `sudo apt install -y postgresql-client`. Or use the one inside a
Postgres container, from the repo folder:
`docker run --rm -v "$PWD/schema:/schema" postgres:16 psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f /schema/migrate.sql`.

**Windows laptop (PowerShell)**, from the extracted repo folder (psql comes with the
PostgreSQL installer, in `C:\Program Files\PostgreSQL\16\bin`):

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DBNAME"
psql $env:DATABASE_URL -v ON_ERROR_STOP=1 -f schema\migrate.sql
```

The output prints one line per migration and ends with
`Done: the database matches schema/v1.sql.` Any error stops the run at that file. Fix the
cause and run it again; the files that already ran are skipped harmlessly.

## Adding a change

1. Edit `v1.sql` (the full schema).
2. Add the same change to `migrations/<today>.sql`, written so it can run twice
   (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS`
   before `ADD CONSTRAINT`, `CREATE OR REPLACE VIEW` with columns only appended).
3. Add one `\ir migrations/<today>.sql` line to `migrate.sql`.
4. Add a line to the WHAT CHANGED block at the top of `v2.sql`.
5. Run `python -m pytest tests/ -q`. `tests/test_schema_migrations.py` checks that every
   migration is listed in `migrate.sql`.

Verified on 2026-11-07 against Postgres 16. A database built from the 2026-10-07 `v1.sql`,
plus `migrate.sql`, matches a fresh `v1.sql`. Running `migrate.sql` twice, or on a fresh
`v1.sql` database, gives no errors.

## Later: Cosmos

Moving to Azure Cosmos DB (docs/PLATFORM_PLAN.md) is a data copy, not a SQL migration.
While both stores run side by side, `DB_BACKEND=postgres|cosmos` picks one, and these files
stay the Postgres side.
