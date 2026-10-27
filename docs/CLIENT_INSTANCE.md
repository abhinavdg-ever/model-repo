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

## 0. Runbook: new database, copy the charts, start the client UI

Step 1 is `psql` on the VM; the rest runs on the app VM (172.20.4.170).

Replace these everywhere:

| Placeholder | Means |
|---|---|
| `DBUSER` | the database user our core-pipeline uses (in `core-pipeline/.env` → `DATABASE_URL`) |
| `DBPASS` | its password |
| `DBHOST` | the Postgres server's IP as the app VM reaches it, never `localhost` |

The leading space on the `export` lines keeps passwords out of shell history.

### Step 1. Create the database (psql on the VM)

```sql
-- sudo -u postgres psql   (or however you open psql)
CREATE DATABASE imaging_outputs_client OWNER DBUSER;
\c imaging_outputs_client
\i /path/to/schema/v1.sql
\i /path/to/schema/changes_2026-10-27.sql
SELECT count(*) FROM pipeline_stage;   -- 13

-- our database needs the new ground-truth columns too
\c imaging_outputs
\i /path/to/schema/changes_2026-10-27.sql
\q
```

No repo on that VM: `curl -fsSLO https://raw.githubusercontent.com/abhinavdg-ever/model-repo/main/schema/v1.sql`
(and the same for `changes_2026-10-27.sql`), then `\i` them from that folder.

### Step 2. App VM: the chart list

A Notepad file, `charts.txt`: one chart name per line (the folder name, e.g.
`52743839_44976074`). Blank lines and lines starting with `#` are skipped.

```bash
mkdir -p ~/client-charts
# from your laptop:  scp charts.txt <user>@172.20.4.170:~/client-charts/
```

### Step 3. App VM: copy the charts and our users

```bash
 export SOURCE_DATABASE_URL='postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs'
 export TARGET_DATABASE_URL='postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs_client'

cd /opt/advantmed/advantmed-imaging-pipeline && git pull      # the copy script; restart nothing
cd core-pipeline                                              # its image has psycopg
clone() { docker compose run --rm --no-deps -v "$PWD/../utilities:/app/utilities:ro" \
  -v "$HOME/client-charts:/lists:ro" \
  -e SOURCE_DATABASE_URL -e TARGET_DATABASE_URL api python /app/utilities/clone_client_db.py "$@"; }

clone --dry-run --charts-file /lists/charts.txt --update      # counts only
clone --charts-file /lists/charts.txt --update                # the copy
```

Copied: each listed chart with its pages, every pipeline result and its manifest rows, and
our users (same usernames and passwords). Never copied: ground truth. Names not in our
database are printed and skipped. Then, on the Postgres server:

```bash
psql -h localhost -U DBUSER -d imaging_outputs_client -c "SELECT
  (SELECT count(*) FROM chart_list)        AS charts,
  (SELECT count(*) FROM page_list)         AS pages,
  (SELECT count(*) FROM users)             AS users,
  (SELECT count(*) FROM page_ground_truth) AS ground_truth"     -- ground_truth = 0
```

### Step 4. App VM: the client UI in `~/ui-temp` (clearing the old one)

```bash
# Anything on 5000 / 5001 now? Stop it (never ours on 4000 / 4001 / 8001)
docker ps --format '{{.Names}}  {{.Ports}}' | grep -E ':500[01]->' || echo "5000/5001 free"
(cd ~/ui-temp 2>/dev/null && docker compose down) || true
rm -rf ~/ui-temp

git clone https://github.com/abhinavdg-ever/ui-temp.git ~/ui-temp
# or from DevOps:
# git clone -b imaging-client-ui "https://AdvantmedDevOps@dev.azure.com/AdvantmedDevOps/AI%20Project%20POC/_git/AI%20Project%20POC" ~/ui-temp
cd ~/ui-temp
cp .env.example .env
mkdir -p data/empty
nano .env
```

Set in `.env`:

```ini
DATABASE_URL=postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs_client
ALLOWED_ORIGINS=http://172.20.4.170:5001,http://localhost:5001
AUTH_SECRET=<the output of: openssl rand -hex 32>
DB_POOL_MIN=12
DB_POOL_MAX=16
CLIENT_VIEW=true
# Page images: copy every AZURE_STORAGE_* and AZURE_* line from our review-ui/.env
```

Its `docker-compose.yml` fixes, whatever `.env` says: ports 5000 / 5001, the database
only, no Playground, the Algorithm column only.

### Step 5. App VM: start it

```bash
cd ~/ui-temp
docker compose up -d --build
docker ps --format '{{.Names}}  {{.Ports}}' | grep client-review    # backend :5000, frontend :5001
curl -fsS localhost:5000/health && echo OK
```

Open **http://172.20.4.170:5001** and sign in with your usual username and password. In
the firewall / NSG, open 5001 to the client's network only; 5000 stays internal.

### Step 6. Checks before the three reviewers start

| Check | Expected |
|---|---|
| Chart list on :5001 | only the charts in `charts.txt` |
| Page Details | Field · Algorithm · Confidence · Ground Truth, none filled in |
| Profile menu | no Playground |
| A ground truth saved on :5001 | in `imaging_outputs_client`, not in `imaging_outputs` (query below) |
| Our UI on :4001 | unchanged |

```bash
# Postgres server: the latest saves on the client side
psql -h localhost -U DBUSER -d imaging_outputs_client -c "SELECT chart_name, page_number,
  encounter_label, quality, updated_at FROM page_ground_truth ORDER BY updated_at DESC LIMIT 5"
```

Split the work by chart: two saves of the same page at the same moment keep the later one.

### Later: more charts, or charts re-run

Add the names to `charts.txt` (or write a new file), then:

```bash
# Postgres server: a backup first
sudo -u postgres pg_dump -Fc -d imaging_outputs_client -f /tmp/client_backup_$(date +%F).dump

# App VM: the exports and clone() from step 3, then
clone --dry-run --charts-file /lists/charts.txt --update
clone --charts-file /lists/charts.txt --update
```

| Chart in the list | In the client DB already? | Result |
|---|---|---|
| new | no | copied |
| re-run since the last copy | yes | its old copy deleted, our latest copied |
| not in the list | | left as it is |

The client's ground truth always stays. The UI shows the new data at once; nothing to
restart. Use `--add` instead of `--update` to never re-copy a chart that is already there.

### Later: new screens, stopping, a client-only account

```bash
# App VM: after a UI change is synced and pushed (laptop steps below)
cd ~/ui-temp && git pull && docker compose up -d --build

# App VM: stop the client UI (ours keeps running)
cd ~/ui-temp && docker compose down

# An account only the client has (it asks for the password)
cd /opt/advantmed/advantmed-imaging-pipeline
python3 utilities/create_user.py client.lead --name "Client Lead" --role reviewer --sql > /tmp/u.sql
psql "$TARGET_DATABASE_URL" -f /tmp/u.sql && rm /tmp/u.sql
```

### If something goes wrong

| Symptom | Fix |
|---|---|
| `clone`: "the target already has N charts" | add `--update` |
| `clone`: "no schema / pipeline_stage" | step 1 was not run |
| `clone` or the UI: `connection refused` / `no pg_hba.conf entry` | `DBHOST` is `localhost`, or `pg_hba.conf` names only `imaging_outputs`: add a line for `imaging_outputs_client`, reload |
| `permission denied for table …` | the schema was applied as `postgres`: in the new DB run `REASSIGN OWNED BY postgres TO DBUSER;` |
| Page loads, no charts or no sign-in | `DATABASE_URL` in `~/ui-temp/.env`; `docker compose logs backend` |
| Pages show no images | the Azure lines are missing from `~/ui-temp/.env` |
| Saving ground truth fails with a column error | `changes_2026-10-27.sql` was not run (step 1) |

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

---

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
