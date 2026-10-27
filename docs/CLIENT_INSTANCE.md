# A second review-ui for the client (own database, no ground truth)

The client reviews charts and enters their own ground truth, without seeing ours.
They get a second review-ui on the same VM, on **port 5001** (API on 5000), reading its
own Postgres database. That database holds:

- the pipeline's outputs for the charts **last run on or after 26 Oct 2026, 00:00 IST**
  (or the charts you list),
- **our users**, so the same usernames and passwords sign in,
- **no ground truth**: nothing typed in Page Details, no CSV upload, no encounter review.

The client UI lives in its **own repository**,
[`abhinavdg-ever/ui-temp`](https://github.com/abhinavdg-ever/ui-temp). Everything else
(the pipeline, the copy script, the schema, this doc) stays in this one.

```
Laptop
├── advantmed-imaging-pipeline/          ← this repo (model-repo)
│     review-ui/                         ← where UI changes are made
│     review-ui-client/                  ← a checkout of ui-temp (git-ignored here)
│         utilities/sync_client_ui.sh copies review-ui → review-ui-client

VM (172.20.4.170)
├── /opt/advantmed/advantmed-imaging-pipeline/   ← ours (as today)
│     core-pipeline  :8001 ──writes──▶ imaging_outputs
│     review-ui      :4000 / :4001 ──reads/writes──▶ imaging_outputs (our ground truth)
│
└── ~/ui-temp/                                   ← a checkout of ui-temp
      client-review  :5000 / :5001 ──reads/writes──▶ imaging_outputs_client
                                                 (charts copied from ours, our users,
                                                  the client's ground truth only)
```

What the client folder fixes, whatever its `.env` says (`docker-compose.yml` there):

| Setting | Value |
|---|---|
| Docker project | `client-review`, so its containers never replace ours |
| Ports | 5000 (API) / 5001 (web) by default |
| Data | Postgres only (`DATA_MODE=production`); the disk folders are empty |
| Playground | off (`CORE_PIPELINE_URLS` is empty), so nobody on 5001 can start a run |
| Connections | 12 kept open, at most 16 (`DB_POOL_MIN` / `DB_POOL_MAX`): 3 reviewers at once |

Paths here are examples; use your VM's layout. `SOURCE_DATABASE_URL` /
`TARGET_DATABASE_URL` are the two connection strings
(`postgresql://USER:PASSWORD@HOST:5432/imaging_outputs` and `…/imaging_outputs_client`).
Keep the passwords out of shell history and out of git.

---

## 0. Quick start: every command, in order

### On the laptop: put a UI change into the client repo

```bash
cd "/Volumes/Extreme SSD/Advantmed/advantmed-imaging-pipeline"
# once: git clone https://github.com/abhinavdg-ever/ui-temp.git review-ui-client
git commit ...                       # commit the review-ui change here first
utilities/sync_client_ui.sh          # copies the committed review-ui/backend + frontend
cd review-ui-client
git add -A && git commit -m "Sync review-ui at <commit>" && git push
```

Only `backend/` and `frontend/` are copied. The client repo's own `docker-compose.yml`,
`.env.example` and `README.md` are kept. Nothing reaches the client until you sync and push.

### On the VM: the database (psql)

Open `psql` against the Postgres server as a user that may create databases (the same
user as our `core-pipeline/.env`):

```sql
CREATE DATABASE imaging_outputs_client;
\c imaging_outputs_client
\i /opt/advantmed/advantmed-imaging-pipeline/schema/v1.sql
SELECT stage_name, pass_no, seq FROM pipeline_stage ORDER BY seq;   -- 13 rows
\q
```

`\i` takes the schema from **our** checkout; `git pull` there first so it is current. If
that user cannot create databases, a Postgres admin runs
`CREATE DATABASE imaging_outputs_client OWNER <that user>;`, then you run the `\c` and
`\i` lines. No `psql` on the VM: see section 2a.

### On the VM: copy the charts and our users

```bash
# The connection strings (the leading space keeps them out of shell history)
 export SOURCE_DATABASE_URL='postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs'
 export TARGET_DATABASE_URL='postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs_client'

cd /opt/advantmed/advantmed-imaging-pipeline/core-pipeline       # OUR folder: its image has psycopg
mkdir -p ~/client-charts      # chart lists (.txt) go here; the container sees it as /lists
clone() { docker compose run --rm --no-deps -v "$PWD/../utilities:/app/utilities:ro" \
  -v "$HOME/client-charts:/lists:ro" \
  -e SOURCE_DATABASE_URL -e TARGET_DATABASE_URL api python /app/utilities/clone_client_db.py "$@"; }
clone --dry-run        # counts only: check the charts and pages look right
clone                  # the copy; ends with "target ground truth rows: 0"
# Other choices of charts (section 2c): --since "YYYY-MM-DD HH:MM", --charts-file /lists/charts.txt
```

`DBHOST` is the Postgres server's IP: the script runs inside a container, where
`localhost` is the container itself.

### On the VM: the client UI in `~/ui-temp` (clearing what is there)

```bash
# 1. Stop whatever runs on 5000 / 5001 now, and remove the old folder
docker ps --format '{{.Names}}  {{.Ports}}' | grep -E ':500[01]->' || echo "nothing on 5000/5001"
(cd ~/ui-temp 2>/dev/null && docker compose down) || true
rm -rf ~/ui-temp

# 2. A fresh checkout of the client repo
git clone https://github.com/abhinavdg-ever/ui-temp.git ~/ui-temp
cd ~/ui-temp
cp .env.example .env
mkdir -p data/empty
```

If step 1 still lists a container on 5000 or 5001, stop it by name
(`docker stop <name> && docker rm <name>`). **Never** stop ours on 4000 / 4001 / 8001.

Edit `~/ui-temp/.env`:

```ini
DATABASE_URL=postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs_client
ALLOWED_ORIGINS=http://172.20.4.170:5001,http://localhost:5001
AUTH_SECRET=<output of: openssl rand -hex 32>
# Page images: copy the AZURE_STORAGE_* / AZURE_* lines from our review-ui/.env
```

```bash
# 3. Start it (project "client-review", fixed in its docker-compose.yml)
docker compose up -d --build
curl -fsS localhost:5000/health && echo OK

# 4. Open http://172.20.4.170:5001 and sign in with your usual username and password.
#    Open port 5001 on the VM's firewall / NSG to the client's network only.
```

**Update the screens** (after a sync and push from the laptop):
`cd ~/ui-temp && git pull && docker compose up -d --build`. **More charts or re-runs**:
`clone --add` / `clone --update` (section 2c). **Stop it**: `docker compose down` in
`~/ui-temp` (ours keeps running).

| Check | Expected |
|---|---|
| `docker ps` | `client-review-backend-1` on 5000, `client-review-frontend-1` on 5001, plus ours on 4000 / 4001 |
| :5001 chart list | only the copied charts |
| :5001 Page Details | no ground truth on any page |
| :5001 profile menu | no Playground |
| A ground truth saved on :5001 | in `imaging_outputs_client`, not in `imaging_outputs` |

## 1. The client repository

`ui-temp` holds only the review UI:

```
ui-temp/
├── backend/            copied from review-ui/backend by utilities/sync_client_ui.sh
├── frontend/           copied from review-ui/frontend
├── docker-compose.yml  the client's own: ports, project name, no Playground
├── .env.example
├── README.md
└── data/empty/         mounted where our disk workspace would be
```

Don't edit `backend/` or `frontend/` there: the next sync overwrites them. Make the
change in this repo's `review-ui/`, commit it, then sync. The script copies what is
committed at `HEAD`, so a half-finished change never reaches the client.

Never start `core-pipeline` for the client. The pipeline keeps writing to our database,
and the client database is a copy (section 5).

## 2. The database

### 2a. Create it, with the schema

With `psql`, as in section 0. Or from a shell:

```bash
psql "$SOURCE_DATABASE_URL" -c 'CREATE DATABASE imaging_outputs_client'
psql "$TARGET_DATABASE_URL" -f /opt/advantmed/advantmed-imaging-pipeline/schema/v1.sql
```

No `psql` on the VM: run it from the Postgres image instead (same two commands):

```bash
docker run --rm -v /opt/advantmed/advantmed-imaging-pipeline/schema:/schema:ro postgres:16 \
  psql "$SOURCE_DATABASE_URL" -c 'CREATE DATABASE imaging_outputs_client'
docker run --rm -v /opt/advantmed/advantmed-imaging-pipeline/schema:/schema:ro postgres:16 \
  psql "$TARGET_DATABASE_URL" -f /schema/v1.sql
```

The database user needs the right to create databases; if it lacks it, ask whoever runs
the Postgres server to create `imaging_outputs_client` owned by that user.

`v1.sql` is the whole current schema; the dated `changes_*.sql` files only upgrade an
old database. It also seeds `pipeline_stage`, so that table's data is left out of the
copy below.

### 2b. Copy the charts and the users

`utilities/clone_client_db.py` does the copy.

- **Charts:** only those whose last run (`chart_list.updated_at`) is at or after the
  cutoff. By default that is 26 Oct 2026 00:00, Asia/Kolkata; change it with
  `--since "YYYY-MM-DD HH:MM"` and `--tz`.
- **What is copied for each chart:** its pages, every pipeline result, its jobs and its
  manifest rows. Ids are kept, so a page has the same `page_list.id` in both databases.
- **Users:** merged in by username, so our passwords and roles work on 5001.
- **Never copied:** `page_ground_truth` and `page_ground_truth_entry` (manual and
  CSV-uploaded), `encounter_review`, `user_login`, `chart_deletion`.

The script needs Python with psycopg. The core-pipeline image already has both, so run it
from our folder, inside that container:

```bash
cd /opt/advantmed/advantmed-imaging-pipeline/core-pipeline
export SOURCE_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/imaging_outputs'
export TARGET_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/imaging_outputs_client'
mkdir -p ~/client-charts      # chart lists (.txt) go here; the container sees it as /lists
clone() { docker compose run --rm --no-deps -v "$PWD/../utilities:/app/utilities:ro" \
  -v "$HOME/client-charts:/lists:ro" \
  -e SOURCE_DATABASE_URL -e TARGET_DATABASE_URL api python /app/utilities/clone_client_db.py "$@"; }

clone --dry-run    # how many charts and pages, by status; writes nothing
clone              # the copy (refuses a target that already has charts)
```

### 2c. Choosing the charts: a date, a list, or both

| You want | Run |
|---|---|
| Charts run on or after a date (default 26 Oct 2026 00:00 IST) | `clone --since "2026-11-01 00:00"` |
| Exactly the charts in a list | `clone --charts-file /lists/charts.txt` |
| Charts in the list **and** run since a date | `clone --charts-file /lists/charts.txt --since "2026-11-01 00:00"` |
| A list of chart ids (`chart_list.id`) instead of names | `clone --charts-file /lists/ids.txt --by-id` |

The list is a plain text file (Notepad is fine) in `~/client-charts/` on the VM: one chart
name per line, as in our chart list (the folder name, e.g. `52743839_44976074`), or one
chart id per line with `--by-id`. Blank lines
are skipped, a `#` starts a comment, and commas or spaces also separate names. With a list
and no `--since`, the date is not checked. Names not in our database are printed and
skipped. Always run the same command with `--dry-run` first.

How the charts land in the client database:

| Flag | The target's existing charts | The chosen charts |
|---|---|---|
| (none) | must be empty, else it stops | copied |
| `--add` | kept as they are | copied if not there yet; one already there is skipped (not updated) |
| `--update` | kept, except the chosen ones | each chosen chart's old copy is deleted (matched by name) and its latest copied; a new one is added |
| `--replace` | all deleted (ground truth, reviews and accounts stay) | copied |

So to give the client more charts later, write their names in a file and run
`clone --dry-run --charts-file /lists/more.txt --add`, then the same without `--dry-run`.
**Batches after re-runs** (the usual way to keep the client up to date): list the charts
you re-ran, by name or by id, and run

```bash
clone --dry-run --charts-file /lists/batch-07.txt --update
clone --charts-file /lists/batch-07.txt --update            # names
clone --charts-file /lists/batch-07-ids.txt --by-id --update  # or chart ids
```

Only those charts change on the client side: their pages and results are replaced by our
latest, the other charts stay, and the client's ground truth on them stays (it is keyed
by chart name and page number). `--since` works with `--update` too (every chart re-run
since a date).

If the database host is `localhost` on the VM, use `host.docker.internal` (or the VM's
IP) in the two URLs, since the script runs inside a container. Prefix the `export` lines
with a space to keep them out of shell history.

The script prints a count per table and ends with the target's ground truth rows, which
must be 0. Check the copy:

```bash
psql "$TARGET_DATABASE_URL" -c "SELECT
  (SELECT count(*) FROM chart_list)              AS charts,
  (SELECT min(updated_at) FROM chart_list)       AS earliest_run,   -- on or after the cutoff
  (SELECT count(*) FROM page_list)               AS pages,
  (SELECT count(*) FROM users)                   AS users,
  (SELECT count(*) FROM page_ground_truth)       AS ground_truth,    -- 0
  (SELECT count(*) FROM page_ground_truth_entry) AS ground_truth_csv -- 0"
```

## 3. The client's review-ui

Section 0 has the commands. What its `.env` needs:

| Setting | Value |
|---|---|
| `DATABASE_URL` | the client database, `…/imaging_outputs_client` |
| `ALLOWED_ORIGINS` | the address the client's browser uses, `http://172.20.4.170:5001` |
| `AUTH_SECRET` | a new long random value: the same usernames and passwords work (the users were copied), but a session from our UI does not open this one |
| `AZURE_STORAGE_*`, `AZURE_*` | as in our `review-ui/.env`, so page images load |
| `LOGIN_USERNAME` / `LOGIN_PASSWORD` | the same env admin as ours if you use one, else empty |
| `BACKEND_PORT` / `FRONTEND_PORT` | only to move off 5000 / 5001 |
| `SEMANTIC_MODEL_HOST_PATH` | optional: our `core-pipeline/models/semantic-model`, for Accuracy's semantic page-type match |

`DATA_MODE`, the empty disk folders and the Playground switch are set in its
`docker-compose.yml`, not in `.env`, so a copied `.env` cannot turn them back on.

If a firewall or NSG sits in front of the VM, open port 5001 there; 5000 can stay internal.

## 4. Accounts

Our users were copied, so everyone signs in as they do on ours. A user added or
re-passworded on our side reaches 5001 at the next refresh (section 5). An account only
the client should have is made against the client database alone. `--sql` needs plain
`python3`: it asks for the password, prints the SQL with it hashed, and `psql` runs it.

```bash
cd /opt/advantmed/advantmed-imaging-pipeline
python3 utilities/create_user.py client.lead --name "Client Lead" --role reviewer --sql > /tmp/u.sql
psql "$TARGET_DATABASE_URL" -f /tmp/u.sql && rm /tmp/u.sql
```

Run it in your own terminal, since it prompts. Roles: `reviewer` enters ground truth,
`viewer` only looks, `admin` can also delete charts.

## 5. Refreshing the outputs later

The copy is a snapshot: new runs land only in our database. To bring them across:

```bash
clone --dry-run --replace
clone --replace
```

`--replace` deletes the target's chart data and copies the charts run since the cutoff
again (move the cutoff with `--since`, or pass the list with `--charts-file`). To add new
charts without touching the ones there, use `--add` instead (section 2c). Untouched: the client's ground
truth, encounter reviews and client-only accounts. The client's ground truth is keyed by
chart name and page number, not by pipeline row, so it lines up with the refreshed pages.

Take a backup before each refresh: `pg_dump "$TARGET_DATABASE_URL" -Fc -f client_backup_<date>.dump`.

## 6. Checks before handing it over

- [ ] `page_ground_truth` and `page_ground_truth_entry` are 0 on the client database
      before the client starts.
- [ ] Only charts run since the cutoff are listed on :5001.
- [ ] Our usernames and passwords sign in on :5001.
- [ ] A chart opens on :5001 with images, and Page Details shows no ground truth.
- [ ] A ground truth saved on :5001 appears in `imaging_outputs_client` and not in
      `imaging_outputs`.
- [ ] Our instance (:4001) is unchanged.
- [ ] No Playground in the client's profile menu.

## 7. What the client sees, and what to keep in mind

- **Same features as ours:** Page Details, Encounter, Sequencing, Search, Accuracy (once
  they enter ground truth), Manual Review.
- **Accuracy and Manual Review** score against the client's own ground truth only. With
  none entered yet, the Accuracy view has nothing to score.
- **Three reviewers at once:** the client UI keeps 12 connections open and uses at most
  16, so three people opening charts together never wait for one. Every save is one
  transaction, and two reviewers on different pages never touch each other's rows. Two
  saving the **same page** at the same moment: the later save wins for that page, so
  split the work by chart. Check the server has room before handing it over:
  `psql "$TARGET_DATABASE_URL" -c "SHOW max_connections" -c "SELECT count(*) FROM pg_stat_activity"`
  (the second number plus 16 must stay well under the first).
- **Postgres connections:** each review-ui keeps up to `DB_POOL_MAX` (20) connections,
  and core-pipeline up to 20. Keep the total under the server's `max_connections`
  (100 by default), or lower `DB_POOL_MAX` in the client `.env` (e.g. 10).
- **Authentication covers ground-truth saves only** (CLAUDE.md §10): other routes are
  open to anyone who can reach the port. Expose :5001 only to the client's network (an
  NSG rule on their IP range, or a VPN), not to the internet.
- **Two repositories, one codebase:** the client UI is a copy of our `review-ui`. It
  changes only when you run `utilities/sync_client_ui.sh`, push `ui-temp`, and
  `git pull` on the VM.
