# A second review-ui for the client (own database, no ground truth)

The client reviews charts and enters their own ground truth, without seeing ours.
They get a second review-ui on the same VM, on **port 5001**: the same code, in its own
folder, reading its own Postgres database. That database holds:

- the pipeline's outputs for the charts **last run on or after 26 Oct 2026, 00:00 IST**,
- **our users**, so the same usernames and passwords sign in,
- **no ground truth**: nothing typed in Page Details, no CSV upload, no encounter review.

```
VM (172.20.4.170)
├── /opt/advantmed/advantmed-imaging-pipeline/   ← ours (as today)
│     core-pipeline  :8001 ──writes──▶ imaging_outputs
│     review-ui      :4000 / :4001 ──reads/writes──▶ imaging_outputs (our ground truth)
│
└── /opt/advantmed/client-review/                ← new folder, same git repo
      review-ui      :5000 / :5001 ──reads/writes──▶ imaging_outputs_client
                                                 (charts run since 26 Oct, our users,
                                                  the client's ground truth only)
```

Nothing new to build. review-ui already runs on Postgres alone in Production Mode, and
everything it writes is in the database: ground truth, encounter reviews, users and
sign-ins. A second copy pointed at a second database is a separate system.

Paths and ports here are examples; use your VM's layout. `SOURCE_DATABASE_URL` / `TARGET_DATABASE_URL` are the two
connection strings (`postgresql://USER:PASSWORD@HOST:5432/imaging_outputs` and
`…/imaging_outputs_client`). Keep the passwords out of shell history and out of git.

---

## 0. Quick start: every command, in order

Run these on the VM, as the user that runs our Docker containers. Replace the four
CAPITALISED values first: `DBUSER`, `DBPASS`, `DBHOST` (the Postgres server; from inside a
container use the VM's IP, not `localhost`) and `VMIP` (172.20.4.170). Sections 1–7 explain
each step.

```bash
# 1. A second copy of the code, in its own folder
sudo mkdir -p /opt/advantmed/client-review && sudo chown "$USER" /opt/advantmed/client-review
git clone https://github.com/abhinavdg-ever/model-repo.git /opt/advantmed/client-review

# 2. The connection strings (the leading space keeps them out of shell history)
 export SOURCE_DATABASE_URL='postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs'
 export TARGET_DATABASE_URL='postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs_client'

# 3. The client's database, with the schema (no psql on the VM? see section 2a)
psql "$SOURCE_DATABASE_URL" -c 'CREATE DATABASE imaging_outputs_client'
psql "$TARGET_DATABASE_URL" -f /opt/advantmed/client-review/schema/v1.sql

# 4. Copy the charts run since 26 Oct 00:00 IST, and our users; no ground truth
cd /opt/advantmed/advantmed-imaging-pipeline/core-pipeline       # OUR folder: its image has psycopg
mkdir -p ~/client-charts      # chart lists (.txt) go here; the container sees it as /lists
clone() { docker compose run --rm --no-deps -v "$PWD/../utilities:/app/utilities:ro" \
  -v "$HOME/client-charts:/lists:ro" \
  -e SOURCE_DATABASE_URL -e TARGET_DATABASE_URL api python /app/utilities/clone_client_db.py "$@"; }
clone --dry-run        # counts only: check the charts and pages look right
clone                  # the copy; ends with "target ground truth rows: 0"
# Other choices of charts (section 2c): --since "YYYY-MM-DD HH:MM", --charts-file /lists/charts.txt

# 5. The client's review-ui settings
cd /opt/advantmed/client-review/review-ui
cp .env.example .env
mkdir -p data/empty
```

Edit `/opt/advantmed/client-review/review-ui/.env` and set exactly these (the rest stays as in
**our** `review-ui/.env`, so copy the `BLOB_*` / Azure lines from there for page images):

```ini
BACKEND_PORT=5000
FRONTEND_PORT=5001
ALLOWED_ORIGINS=http://VMIP:5001,http://localhost:5001
DATA_MODE=production
DATABASE_URL=postgresql://DBUSER:DBPASS@DBHOST:5432/imaging_outputs_client
DATA_HOST_PATH=./data/empty
PIPELINE_HOST_PATH=./data/empty
AUTH_SECRET=<output of: openssl rand -hex 32>
CORE_PIPELINE_URLS=
DB_POOL_MAX=10
```

`CORE_PIPELINE_URLS=` left empty turns the Playground off, so nobody on 5001 can start a
pipeline run (needs this repo at or after 27 Oct 2026; older checkouts filled in a default).

```bash
# 6. Start it under its own project name, so it never replaces our containers
docker compose -p client-review up -d --build
curl -fsS localhost:5000/health && echo OK

# 7. Open http://VMIP:5001 and sign in with your usual username and password.
#    Open port 5001 on the VM's firewall / NSG to the client's network only.
```

**Refresh later** (new runs since the copy): `pg_dump` the client DB first, then in our
`core-pipeline` folder define `clone` again (step 4) and run `clone --replace`. The client's
ground truth stays. **Update the screens**: `git pull` in `/opt/advantmed/client-review`, then
step 6 again. **Stop it**: `docker compose -p client-review down` in its `review-ui` folder
(ours keeps running).

| Check | Expected |
|---|---|
| `docker ps` | `client-review-backend-1` on 5000, `client-review-frontend-1` on 5001, plus ours on 4000 / 4001 |
| :5001 chart list | only charts run since the cutoff |
| :5001 Page Details | no ground truth on any page |
| :5001 profile menu | no Playground |
| A ground truth saved on :5001 | in `imaging_outputs_client`, not in `imaging_outputs` |

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

On the same Postgres server, or another one (`TARGET_DATABASE_URL` as set in 2b):

```bash
psql "$SOURCE_DATABASE_URL" -c 'CREATE DATABASE imaging_outputs_client'
psql "$TARGET_DATABASE_URL" -f /opt/advantmed/client-review/schema/v1.sql
```

No `psql` on the VM: run it from the Postgres image instead (same two commands):

```bash
docker run --rm -v /opt/advantmed/client-review/schema:/schema:ro postgres:16 \
  psql "$SOURCE_DATABASE_URL" -c 'CREATE DATABASE imaging_outputs_client'
docker run --rm -v /opt/advantmed/client-review/schema:/schema:ro postgres:16 \
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

```bash
cd /opt/advantmed/client-review/review-ui
cp .env.example .env
```

Set these in the new `.env`. Everything else stays as in our `.env`, including the Azure
Blob block, so page images still load:

```ini
# Its own ports, so it runs beside ours (docker-compose.yml reads them).
BACKEND_PORT=5000
FRONTEND_PORT=5001
ALLOWED_ORIGINS=http://172.20.4.170:5001,http://localhost:5001

# Database only: the client never sees our disk workspace.
DATA_MODE=production
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/imaging_outputs_client
# An empty folder: Local mode (top bar) shows nothing.
DATA_HOST_PATH=./data/empty
PIPELINE_HOST_PATH=./data/empty

# Its own sign-in secret: the same usernames and passwords work (the users were
# copied), but a session from one instance does not open the other.
AUTH_SECRET=<a new long random value>
# The same env admin account as ours, if you use one there; else leave empty.
LOGIN_USERNAME=
LOGIN_PASSWORD=

# No Playground: the client cannot start pipeline runs (empty turns it off).
CORE_PIPELINE_URLS=
DB_POOL_MAX=10
```

Then start it under its own Docker project name, so its containers never replace ours:

```bash
mkdir -p data/empty
docker compose -p client-review up -d --build
curl -fsS localhost:5000/health
```

Open `http://172.20.4.170:5001`. If a firewall or NSG sits in front of the VM, open port
5001 there; 5000 can stay internal.

## 4. Accounts

Our users were copied, so everyone signs in as they do on ours. A user added or
re-passworded on our side reaches 5001 at the next refresh (section 5). An account only
the client should have is made against the client database alone. `--sql` needs plain
`python3`: it asks for the password, prints the SQL with it hashed, and `psql` runs it.

```bash
cd /opt/advantmed/client-review
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
- **Postgres connections:** each review-ui keeps up to `DB_POOL_MAX` (20) connections,
  and core-pipeline up to 20. Keep the total under the server's `max_connections`
  (100 by default), or lower `DB_POOL_MAX` in the client `.env` (e.g. 10).
- **Authentication covers ground-truth saves only** (CLAUDE.md §10): other routes are
  open to anyone who can reach the port. Expose :5001 only to the client's network (an
  NSG rule on their IP range, or a VPN), not to the internet.
- **One codebase:** both folders track the same `main`. Pull the client folder when you
  want the client to get a change, and only then.
