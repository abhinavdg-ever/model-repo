# Memory profile: RAM per stage, per chart, in parallel

Measures how much RAM each pipeline stage takes for real charts, and what N charts at once
cost, so worker containers / AKS pods can be sized. Nothing in core-pipeline is changed: the
scripts wrap the stage functions at runtime.

| File | What it is |
|---|---|
| `stage_memory.py` | Runs one or more charts through the real stage chain in one process and records RAM per stage |
| `run_scenarios.sh` | Starts named scenarios (one process per container, several at once) and samples host / container memory every second |
| `report.py` | Summary per stage across all runs, plus plots (RSS over time with stage bands) when matplotlib is there |
| `scenarios.example.env` | The settings the scenarios read. Copy it to `scenarios.env` and fill in real chart names |

## Read this first: the charts run for real

- **Database writes are real.** Each run replaces that chart's results in the database
  (Postgres or Cosmos, whatever `DB_BACKEND` in `core-pipeline/.env` says), exactly as a worker
  run would. Use charts whose results you are happy to have re-run.
- **Document Intelligence is billed.** With `--skip-ocr` (the scenarios' default, `SKIP_OCR=true`)
  a chart that already has OCR reuses it and sends nothing to Azure. A chart with no OCR yet, or a
  run without `--skip-ocr`, bills stage 5 for its routed pages. `--no-docintel`
  (`NO_DOCINTEL=true`) turns stage 5 off for that process. Its own RAM is small (it is an HTTP
  call), but the chart's stored results then have no Final OCR 2.
- **Blob writes only if asked.** Without `--output-path` (`OUTPUT_PATH=` empty) nothing is written
  to Processed/. With it, that chart's Processed folder is overwritten.
- **The LLM is always off.** Every run sets `BLANK_JUNK_LLM=false`, whatever `.env` says.
- Do not run the same chart twice at once: the runs would share its working folder.
  `parallel` refuses duplicates.

## What is measured

The runs go through the worker's own path: `apps.worker.chart_run.execute_run` with a
`RunRequest` built from the API's fields (`input_type`, `container_name`, `input_path`,
`chart_name`, `output_path`, `skip_ocr`, `only`, `run_through`). Before the first chart, each
entry of `apps.worker.runner.STAGE_CHAIN` (the 15 stages, `stage_name:pass_no`) is replaced in
place by a wrapper. `run_pipeline_for_chart` reads that list each time it runs, so it calls the
wrappers. Intake (`intake_blob` / `intake_local`), page hydration (`ensure_images`) and the
write-out (`write_out`) are wrapped the same way. Each wrapper writes one row:

| Column | Meaning |
|---|---|
| `rss_before_mb` / `rss_after_mb` | RSS of this process when the stage starts / ends |
| `delta_mb` | after − before: what the stage **left resident** (models loaded, caches, allocator arenas) |
| `peak_rss_mb` | highest RSS during the stage: the 50 ms samples, or the kernel's own high-water mark when higher |
| `spike_mb` | peak − before: the **extra headroom** the stage needs while it runs |
| `hwm_mb` | Linux VmHWM, reset at stage start (`/proc/self/clear_refs`). Catches spikes shorter than one sample |
| `children_peak_mb`, `total_peak_mb` | RSS of child processes, and this process plus its children |
| `cgroup_peak_mb`, `cgroup_ws_peak_mb` | the whole container (`memory.current`), and its working set (minus inactive page cache, as `kubectl top` counts it) |
| `threads_max` | OS threads (torch / ONNX / OpenCV pools, `STAGE_WORKERS` page threads) |
| `wall_s`, `gc_collections` (gen0/1/2), `tracemalloc_peak_mb` (with `--tracemalloc`), `error` | |

Two rows come before the charts. `prepare_environment` covers `.env`, logging and models from
Blob. `import_pipeline` covers importing the stage modules. The **cold vs warm** effect: models
load lazily, the first time a stage needs them, so the first chart's spike in a stage includes
loading its model. With `--repeat 2` the second pass shows the stage alone, and
`_summary.json → cold_vs_warm` sets the two side by side.

Files per process, in `--out` (default `utilities/memory_profile/results/`):

- `<run-id>_stages.csv`: one row per stage call. Written row by row, so an OOM kill still leaves
  everything up to the kill.
- `<run-id>_samples.csv`: every sample (time, chart, stage running, RSS, children, cgroup,
  threads). Use it to plot.
- `<run-id>_summary.json`: arguments, the relevant non-secret environment (`OMP_NUM_THREADS`,
  `STAGE_WORKERS`, `DB_BACKEND`, …), the cgroup limit, per-chart totals (pages, seconds,
  start / end / peak RSS, RSS after gc), cold vs warm, container peak, and the tracemalloc top
  allocators.

## On the Ubuntu VM (Docker)

`utilities/` is not in the image. Each profiled process is a one-off container of the worker
service (`docker compose run --rm worker …`), with the same image, `.env`, environment and
mounts (models, outputs, logs). `utilities/` is mounted at `/app/utilities`. Results land on the
host in `utilities/memory_profile/results/`.

psutil is not in core-pipeline's requirements. `setup` installs it, with matplotlib, into
`utilities/memory_profile/.pydeps` from inside the image, once. The scripts pick that folder up,
so the image needs no rebuild.

```bash
cd ~/advantmed-imaging-pipeline/utilities/memory_profile        # wherever the repo is
cp scenarios.example.env scenarios.env
nano scenarios.env            # CHART_SMALL / _MEDIUM / _LARGE, PARALLEL_CHARTS, INPUT_PATH, CONTAINER

bash run_scenarios.sh setup                  # once: psutil + matplotlib into .pydeps
bash run_scenarios.sh selftest               # fake stages: checks psutil, the hook, the files (no DB)

bash run_scenarios.sh cold-single warm-repeat
bash run_scenarios.sh size-ladder
bash run_scenarios.sh -n 4 parallel          # or: bash run_scenarios.sh parallel-4
bash run_scenarios.sh skip-ocr               # asks first: the full-OCR run bills Document Intelligence
bash run_scenarios.sh report                 # summary over everything in results/
bash run_scenarios.sh -s 20261125-101500 report   # one session
```

One chart by hand, the same way (from `core-pipeline/`):

```bash
cd ~/advantmed-imaging-pipeline/core-pipeline
docker compose run --rm --no-deps -T -e BLANK_JUNK_LLM=false \
  -v "$PWD/../utilities:/app/utilities" worker \
  python /app/utilities/memory_profile/stage_memory.py \
    --chart <CHART_NAME> --input-path Raw_Input/Run1/Batch1/DEID_PNGs --container imaging-pipeline \
    --skip-ocr --repeat 2 --gc-between --out /app/utilities/memory_profile/results/manual
```

Use `worker-large` in place of `worker` to profile with the large-chart service's settings.
`docker compose exec worker …` also works, but only after the files are copied in
(`docker compose cp ../utilities/memory_profile worker:/app/utilities/memory_profile`). It then
runs next to the worker's own process, which may be running a chart and holding its own models.
In that case `cgroup_*` covers both.

Without Docker, in the core-pipeline venv:

```bash
cd ~/advantmed-imaging-pipeline/core-pipeline && source .venv/bin/activate
pip install psutil                           # into the venv; or MODE=local + `run_scenarios.sh setup`
python ../utilities/memory_profile/stage_memory.py --chart <CHART_NAME> \
  --input-path Raw_Input/Run1/Batch1/DEID_PNGs --skip-ocr
```

### AKS (later)

Set `MODE=local` in `scenarios.env` and run inside a pod that uses the worker image, so the
processes share the pod's memory limit as workers would:

```bash
kubectl cp utilities/memory_profile <pod>:/app/utilities/memory_profile
kubectl exec -it <pod> -- bash -c 'cd /app/utilities/memory_profile && \
  cp scenarios.example.env scenarios.env && sed -i "s/^MODE=compose/MODE=local/" scenarios.env && \
  bash run_scenarios.sh setup selftest'
```

Edit the chart names in the pod's `scenarios.env`, run the scenarios, then
`kubectl cp <pod>:/app/utilities/memory_profile/results ./results`. One process per pod, to size
a single worker pod: `kubectl exec <pod> -- python /app/utilities/memory_profile/stage_memory.py …`.

## Scenarios

| Name | What runs | Answers |
|---|---|---|
| `cold-single` | `CHART_SMALL`, fresh process | model-load cost plus one chart: the minimum a worker needs |
| `warm-repeat` | `CHART_SMALL` twice in one process (`--repeat 2`) | what stays resident after a chart, and each stage's spike without loading its model |
| `size-ladder` | small, medium and large charts, a fresh process each, one after another | how the spikes grow with page count (`pages` is in the summary) |
| `parallel` / `parallel-N` | N distinct charts at once (`PARALLEL_CHARTS`), N processes / containers | the combined peak of N workers on one host: the `host_memory.csv` columns `containers_ws_mb` and `host_used_mb` |
| `skip-ocr` | `CHART_SMALL` with full OCR, then with `--skip-ocr` (fresh processes) | what the OCR engines cost against a reuse run. Bills Document Intelligence unless `NO_DOCINTEL=true`; asks first (`-y` skips the question) |
| `all` | cold-single, warm-repeat, size-ladder, parallel | |

Each scenario writes `results/<session>/<scenario>/`: the CSVs and JSON of each process, its
log (`<run-id>.log`), `host_memory.csv` and `report.txt`. `host_memory.csv` holds `free -m`
plus each profiled container's memory, read from the host's cgroup files (else from
`docker stats`), once per second. In `MODE=local` it holds the current container's
`memory.current` in place of the per-container memory.

## Reading the output

```bash
python3 report.py results/                         # host python is enough (stdlib only)
python3 report.py results/<session>/parallel-3 --csv stage_summary.csv
```

- **Size a worker** from `peak_rss_mb` (largest across stages and charts) plus margin. Use the
  container working set (`cgroup_ws_peak_mb`, or `contPeak` in the report) for the pod limit:
  it is what the OOM killer and `kubectl top` count.
- **`spike` vs `delta`.** A large `spike` with a small `delta` is temporary headroom (page
  images, OCR buffers). A large `delta` on the first chart that is near 0 on the second is a model
  being loaded. A `delta` that keeps growing chart after chart is a leak or a growing cache: run
  `warm-repeat` or `--repeat 3` to see it.
- **Parallel**: combined peak ≈ N × single peak, because separate processes do not share
  Python-loaded model weights. Compare `parallel-N`'s `containers_ws_mb` peak with
  `cold-single`'s to check.
- `--tracemalloc` (slow, off by default) lists the Python lines that allocated most per stage
  (`_summary.json → tracemalloc_top`). It sees only the Python heap, not torch / ONNX / OpenCV
  native buffers, which are usually most of the RSS.

## Caveats

- **RSS counts shared pages in full.** Memory-mapped model files (ONNX, safetensors) and shared
  libraries appear in each process's RSS, but the kernel may hold one copy. The sum of RSS over
  processes can therefore exceed the real use. The cgroup / `free -m` columns are the real total.
- **Freed memory may not leave RSS.** glibc keeps freed blocks in its arenas, so `delta` can stay
  high after a stage even though Python freed everything. `--gc-between` (on in the example
  config) runs `gc.collect()` and `malloc_trim(0)` after each chart and records
  `rss_after_gc_mb`. `MALLOC_ARENA_MAX=2` often lowers the RSS of threaded stages. glibc reads it
  when the process starts, so set it with `COMPOSE_RUN_ARGS="-e MALLOC_ARENA_MAX=2"` in
  `scenarios.env` (or `-e` on a manual `docker compose run`), not with `--env`.
- **Threads.** Docling / torch / ONNX Runtime start their own thread pools. Compose sets
  `OMP_NUM_THREADS=1`; the summary records it. Pages run `STAGE_WORKERS` at a time (default 4),
  so a stage's spike grows with `STAGE_WORKERS`. Try `STAGE_WORKERS=2` to see how much.
  Thread-pool sizes are read when the pipeline imports, so set them with `COMPOSE_RUN_ARGS`
  (`-e STAGE_WORKERS=2`) or with `--env NAME=VALUE` in `EXTRA_ARGS`, which is applied before
  the pipeline is imported.
- **The first chart in a process loads models.** Compare stages only cold-with-cold or
  warm-with-warm. `report.py` shows both columns.
- **Sampling.** Samples are 50 ms apart (`--interval`). The kernel high-water mark (`hwm_mb`)
  closes the gap on Linux for this process; child processes are sampled only.
- **Other load on the host.** Workers or the api running charts at the same time show up in
  `host_used_mb` (and, with `docker compose exec`, in the cgroup columns), not in the per-process
  RSS.
- The `run_stats` monitoring files record these runs with trigger kind `memprof`.
