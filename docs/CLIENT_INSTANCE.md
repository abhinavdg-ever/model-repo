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
createdb -h HOST -U USER imaging_outputs_client
psql "$TARGET_DATABASE_URL" -f ../schema/v1.sql
```

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
clone() { docker compose run --rm --no-deps -v "$PWD/../utilities:/app/utilities:ro" -e SOURCE_DATABASE_URL -e TARGET_DATABASE_URL api python /app/utilities/clone_client_db.py "$@"; }

clone --dry-run    # how many charts and pages, by status; writes nothing
clone              # the copy (refuses a target that already has charts)
```

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

# No Playground: the client cannot start pipeline runs.
CORE_PIPELINE_URLS=
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
again (move the cutoff with `--since` if you want). Untouched: the client's ground
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
