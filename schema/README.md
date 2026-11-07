# schema/

```
schema/
├── schema.sql          the whole database. A NEW database needs only this.
│                         Part 1: implemented (core-pipeline / review-ui use every table)
│                         Part 2: not implemented yet (created empty; nothing uses them)
├── CHANGELOG.md        what changed, newest first
├── migrate.sql         upgrades an EXISTING database to schema.sql. Safe to run again and again.
├── migrations/         one dated file per change day; migrate.sql runs them in order.
│   └── 2026-10-16.sql … 2026-11-07.sql
└── tools/              destructive helpers. Read the file before running it.
    ├── clear_schema.sql                 drops everything in `public`
    └── clear_results_keep_manifest.sql  deletes results, keeps roster, ground truth, stages
```

## Which one do I run?

| Situation | Run |
|---|---|
| New, empty database | `schema.sql` |
| Existing database, any age since 2026-10-07 | `migrate.sql` |
| Not sure | `migrate.sql`: it skips whatever is already there |
| Start over (data lost) | dump first, then `tools/clear_schema.sql`, then `schema.sql` |

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
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema/schema.sql
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
`Done: the database matches schema/schema.sql.` Any error stops the run at that file. Fix the
cause and run it again; the files that already ran are skipped harmlessly.

## Adding a change

1. Edit `schema.sql`: Part 1 if code uses it, Part 2 if it is only a proposal. When a
   Part 2 module ships, move its block into Part 1.
2. Add the same change to `migrations/<today>.sql`, written so it can run twice
   (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS`
   before `ADD CONSTRAINT`, `CREATE OR REPLACE VIEW` with columns only appended).
3. Add one `\ir migrations/<today>.sql` line to `migrate.sql`.
4. Add a line at the top of `CHANGELOG.md`.
5. Run `python -m pytest tests/ -q`. `tests/test_schema_migrations.py` checks that every
   migration is listed in `migrate.sql`.

Verified on 2026-11-07 against Postgres 16. A database built from the 2026-10-07 `schema.sql`,
plus `migrate.sql`, matches a fresh `schema.sql`. Running `migrate.sql` twice, or on a fresh
`schema.sql` database, gives no errors.

## Postgres → Cosmos

These files keep the **Postgres** database current while it is still in use
(`DB_BACKEND=postgres`). Moving to Azure Cosmos DB (platform plan, phase 7) is a
different kind of migration: a data copy by `utilities/migrate_postgres_to_cosmos.py`, which
reads a Postgres database at the `schema.sql` shape. So run `migrate.sql` first, then copy.
