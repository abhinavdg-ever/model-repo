# A second review-ui for the client (own database, no ground truth)

The client reviews charts and enters their own ground truth, without seeing ours.
They get a second review-ui on the same VM: the same code, in its own folder, reading
its own Postgres database. That database holds a copy of the pipeline's outputs and
none of our ground truth.

```
VM (172.20.4.170)
├── /opt/advantmed/advantmed-imaging-pipeline/   ← ours (as today)
│     core-pipeline  :8001 ──writes──▶ imaging_outputs
│     review-ui      :4000 / :4001 ──reads/writes──▶ imaging_outputs (our ground truth)
│
└── /opt/advantmed/client-review/                ← new folder, same git repo
      review-ui      :4100 / :4101 ──reads/writes──▶ imaging_outputs_client
                                                 (pipeline outputs copied in,
                                                  the client's ground truth only)
```

Nothing new to build. review-ui already runs on Postgres alone in Production Mode, and
everything it writes is in the database: ground truth, encounter reviews, users and
sign-ins. A second copy pointed at a second database is a separate system.

Paths and ports here are examples; use your VM's layout. `$SRC` / `$DST` are the two
connection strings (`postgresql://USER:PASSWORD@HOST:5432/imaging_outputs` and
`…/imaging_outputs_client`). Keep the passwords out of shell history and out of git.

---

## 1. The folder

Clone the repo again into its own folder. Don't copy the working tree (CLAUDE.md §8).

```bash
sudo mkdir -p /opt/advantmed/client-review && sudo chown "$USER" /opt/advantmed/client-review
git clone https://github.com/abhinavdg-ever/model-repo.git /opt/advantmed/client-review
cd /opt/advantmed/client-review/review-ui
```

The client only needs review-ui. Never start `core-pipeline` from this folder: the
pipeline keeps writing to our database, and the client copy is a snapshot (section 5).

To update the client's screens later: `git pull` in this folder, then
`docker compose up -d --build` (section 3). Their data is not touched.

## 2. The database

### 2a. Create it, with the schema

On the same Postgres server, or another one:

```bash
createdb -h HOST -U USER imaging_outputs_client
psql "$DST" -f ../schema/v1.sql
```

`v1.sql` is the whole current schema; the dated `changes_*.sql` files only upgrade an
old database. It also seeds `pipeline_stage`, so that table's data is left out of the
copy below.

### 2b. Copy the pipeline outputs, without our ground truth

Every table except these: our ground truth and reviews, our users, our deletion log, and
the stage registry that `v1.sql` already seeded.

| Left out (data) | Why |
|---|---|
| `page_ground_truth`, `page_ground_truth_entry` | our ground truth (manual and bulk-uploaded) |
| `encounter_review` | our encounter reviews |
| `users`, `user_login` | our accounts and sign-ins; the client gets their own |
| `chart_deletion` | our deletion log |
| `pipeline_stage` | already seeded by `v1.sql` |

```bash
pg_dump "$SRC" --data-only --no-owner --no-privileges -Fc -f /tmp/outputs.dump \
  --exclude-table-data=page_ground_truth \
  --exclude-table-data=page_ground_truth_entry \
  --exclude-table-data=encounter_review \
  --exclude-table-data=users \
  --exclude-table-data=user_login \
  --exclude-table-data=chart_deletion \
  --exclude-table-data=pipeline_stage
pg_restore --data-only --no-owner -d "$DST" /tmp/outputs.dump
rm /tmp/outputs.dump
```

Ids are copied as they are, so a page has the same `page_list.id` in both databases. The
refresh in section 5 depends on that.

Check the copy:

```bash
psql "$DST" -c "SELECT
  (SELECT count(*) FROM chart_list)        AS charts,
  (SELECT count(*) FROM page_list)         AS pages,
  (SELECT count(*) FROM imaging_final)     AS final_rows,
  (SELECT count(*) FROM page_ground_truth) AS ground_truth;   -- must be 0"
```

To give the client only some charts, delete the others in the client database after the
copy, through its own review-ui (admin, Delete chart), or ask Claude Code for a filtered
copy.

## 3. The client's review-ui

```bash
cd /opt/advantmed/client-review/review-ui
cp .env.example .env
```

Set these in the new `.env`. Everything else stays as in our `.env`, including the Azure
Blob block, so page images still load:

```ini
# Its own ports, so it runs beside ours (docker-compose.yml reads them).
BACKEND_PORT=4100
FRONTEND_PORT=4101
ALLOWED_ORIGINS=http://172.20.4.170:4101,http://localhost:4101

# Database only: the client never sees our disk workspace.
DATA_MODE=production
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/imaging_outputs_client
# An empty folder: Local mode (top bar) shows nothing.
DATA_HOST_PATH=./data/empty
PIPELINE_HOST_PATH=./data/empty

# Its own sign-in secret: our tokens don't work here, theirs don't work on ours.
AUTH_SECRET=<a new long random value>
# No shared env admin account (or the client's own admin, not ours).
LOGIN_USERNAME=
LOGIN_PASSWORD=

# No Playground: the client cannot start pipeline runs.
CORE_PIPELINE_URLS=
```

Then start it under its own Docker project name, so its containers never replace ours:

```bash
mkdir -p data/empty
docker compose -p client-review up -d --build
curl -fsS localhost:4100/health
```

Open `http://172.20.4.170:4101`. If a firewall or NSG sits in front of the VM, open port
4101 there; 4100 can stay internal.

## 4. The client's accounts

Their users exist only in their database. `--sql` needs plain `python3` only (no
pipeline virtualenv in this folder): it asks for each password, prints the SQL with the
password hashed, and `psql` runs it against the client database:

```bash
cd /opt/advantmed/client-review
python3 utilities/create_user.py client.reviewer1 --name "Reviewer One" --role reviewer --sql > /tmp/u.sql
python3 utilities/create_user.py client.lead --name "Client Lead" --role admin --sql >> /tmp/u.sql
psql "$DST" -f /tmp/u.sql && rm /tmp/u.sql
```

Run it in your own terminal, since it prompts. Roles:
`reviewer` enters ground truth, `viewer` only looks, `admin` can also delete charts. Give
`admin` sparingly.

## 5. Refreshing the outputs later

The copy is a snapshot: new runs land only in our database. To bring new charts or
re-runs across without losing what the client entered:

1. **Save the client's work** (their ground truth, reviews and users):

   ```bash
   pg_dump "$DST" --data-only --no-owner -Fc -f /tmp/client_work.dump \
     -t page_ground_truth -t page_ground_truth_entry -t encounter_review \
     -t users -t user_login -t chart_deletion
   ```

2. **Recreate the database and copy the outputs again** (2a and 2b):

   ```bash
   docker compose -p client-review stop backend
   dropdb -h HOST -U USER imaging_outputs_client && createdb -h HOST -U USER imaging_outputs_client
   psql "$DST" -f ../schema/v1.sql
   # … the pg_dump / pg_restore of 2b …
   ```

3. **Put the client's work back**, then restart:

   ```bash
   pg_restore --data-only --no-owner -d "$DST" /tmp/client_work.dump
   docker compose -p client-review start backend
   ```

The client's ground truth points at pages by `page_list.id`, which the copy keeps.
Re-running an existing chart keeps its page ids too. A chart deleted and re-ingested
under new ids would orphan that chart's client ground truth, and step 3 then fails on
that row: re-attach it by chart name and page number. Ask Claude Code for the query if
it happens.

Take a backup before each refresh (`pg_dump "$DST" -Fc -f client_backup_<date>.dump`).

## 6. Checks before handing it over

- [ ] `SELECT count(*) FROM page_ground_truth` is 0 on the client database before the
      client starts.
- [ ] Signing in with one of our accounts on :4101 fails.
- [ ] A chart opens on :4101 with images, and Page Details shows no ground truth.
- [ ] A ground truth saved on :4101 appears in `imaging_outputs_client` and not in
      `imaging_outputs`.
- [ ] Our instance (:4001) is unchanged.
- [ ] No Playground in the client's profile menu.

## 7. What the client sees, and what to keep in mind

- **Same features as ours:** Page Details, Encounter, Sequencing, Search, Accuracy (once
  they enter ground truth), Manual Review.
- **Accuracy and Manual Review** score against the client's own ground truth only. With
  none entered yet, the Accuracy view has nothing to score.
- **Postgres connections:** each review-ui keeps up to `DB_POOL_MAX` (20) connections,
  and core-pipeline up to 20. Keep the total under the server's `max_connections`
  (100 by default), or lower `DB_POOL_MAX` in the client `.env` (e.g. 10).
- **Authentication covers ground-truth saves only** (CLAUDE.md §10): other routes are
  open to anyone who can reach the port. Expose :4101 only to the client's network (an
  NSG rule on their IP range, or a VPN), not to the internet.
- **One codebase:** both folders track the same `main`. Pull the client folder when you
  want the client to get a change, and only then.
