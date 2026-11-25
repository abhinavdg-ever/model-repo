#!/usr/bin/env bash
# Memory-profile scenarios: several stage_memory.py processes (each its own container in
# compose mode, like separate worker pods), plus a host / container memory sampler every second.
#
#   cd utilities/memory_profile
#   cp scenarios.example.env scenarios.env      # fill in the chart names
#   ./run_scenarios.sh setup                    # once: psutil (+ matplotlib) into .pydeps
#   ./run_scenarios.sh selftest                 # fake stages: checks the tooling, no DB
#   ./run_scenarios.sh cold-single warm-repeat
#   ./run_scenarios.sh -n 4 parallel
#   ./run_scenarios.sh report                   # summary over every session in results/
#
# THE CHARTS RUN FOR REAL: each run replaces that chart's results in the database (and, with
# OUTPUT_PATH, its Processed folder in Blob). See README.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UTIL_ABS="$(cd "$HERE/.." && pwd)"
IN_CONTAINER_HERE=/app/utilities/memory_profile

usage() {
  cat <<'EOF'
Usage: run_scenarios.sh [-c CONFIG] [-n N] [-s SESSION] [-y] COMMAND [COMMAND ...]

Commands
  setup         install psutil (+ matplotlib) into utilities/memory_profile/.pydeps (once)
  selftest      stage_memory.py --self-test (fake stages; no pipeline, no DB)
  cold-single   CHART_SMALL in a fresh process: model-load cost + one chart
  warm-repeat   CHART_SMALL twice in one process: cold vs warm, what stays resident
  size-ladder   CHART_SMALL, CHART_MEDIUM, CHART_LARGE one after another, fresh process each
  parallel      N distinct charts (PARALLEL_CHARTS) at once, N processes: sizes worker pods
  skip-ocr      CHART_SMALL full OCR, then with --skip-ocr, fresh process each
                (the full-OCR run bills Document Intelligence unless NO_DOCINTEL=true; asks first)
  all           cold-single warm-repeat size-ladder parallel (not skip-ocr)
  report        report.py over results/ (or over this SESSION with -s)

Options
  -c CONFIG     scenarios file (default: scenarios.env, else scenarios.example.env)
  -n N          parallel: number of processes (default PARALLEL_N)
  -s SESSION    results/<SESSION>/ (default: the current time)
  -y            do not ask before the billed full-OCR run
EOF
}

CONFIG=""
N_OVERRIDE=""
SESSION="$(date +%Y%m%d-%H%M%S)"
ASSUME_YES=false
while getopts ":c:n:s:yh" opt; do
  case "$opt" in
    c) CONFIG="$OPTARG" ;;
    n) N_OVERRIDE="$OPTARG" ;;
    s) SESSION="$OPTARG" ;;
    y) ASSUME_YES=true ;;
    h) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
[ $# -ge 1 ] || { usage; exit 2; }

if [ -z "$CONFIG" ]; then
  if [ -f "$HERE/scenarios.env" ]; then CONFIG="$HERE/scenarios.env"; else CONFIG="$HERE/scenarios.example.env"; fi
fi
# shellcheck disable=SC1090
source "$CONFIG"
MODE="${MODE:-compose}"
SERVICE="${SERVICE:-worker}"
PYTHON="${PYTHON:-python}"
INPUT_TYPE="${INPUT_TYPE:-blob}"
INTERVAL="${INTERVAL:-0.05}"
HOST_INTERVAL="${HOST_INTERVAL:-1}"
PARALLEL_N="${N_OVERRIDE:-${PARALLEL_N:-3}}"
case "${COMPOSE_DIR:-../../core-pipeline}" in
  /*) COMPOSE_ABS="${COMPOSE_DIR}" ;;
  *)  COMPOSE_ABS="$(cd "$HERE/${COMPOSE_DIR:-../../core-pipeline}" 2>/dev/null && pwd || true)" ;;
esac
if [ "$MODE" = compose ] && [ ! -f "${COMPOSE_ABS}/docker-compose.yml" ]; then
  echo "COMPOSE_DIR does not hold docker-compose.yml: ${COMPOSE_DIR:-} (resolved: ${COMPOSE_ABS:-none})" >&2
  exit 2
fi

SAMPLER_STOPS=()
cleanup() { local f; for f in ${SAMPLER_STOPS[@]+"${SAMPLER_STOPS[@]}"}; do touch "$f" 2>/dev/null || true; done; }
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

need_chart() {  # refuse the placeholders from scenarios.example.env
  local name="$1" value="$2"
  if [ -z "$value" ] || [[ "$value" =~ ^CHART_[A-Z_]+$ ]]; then
    echo "$name is not set to a real chart name in $CONFIG (is: '${value}')" >&2
    return 2
  fi
}

# --------------------------------------------------------------------------- one process

# python_cmd OUTDIR_REL ARGS... : runs stage_memory.py (or report/self-test) in the chosen mode.
python_in_mode() {
  local label="$1"; shift
  # shellcheck disable=SC2206
  local run_args=(${COMPOSE_RUN_ARGS:-})
  if [ "$MODE" = compose ]; then
    (cd "$COMPOSE_ABS" && docker compose run --rm --no-deps -T \
        --label "memprof.session=${SESSION}" --label "memprof.run=${label}" \
        -e BLANK_JUNK_LLM=false \
        -v "${UTIL_ABS}:/app/utilities" \
        ${run_args[@]+"${run_args[@]}"} \
        "$SERVICE" python "$@")
  else
    BLANK_JUNK_LLM=false "$PYTHON" "$@"
  fi
}

script_path() {  # stage_memory.py / report.py as the process sees it
  if [ "$MODE" = compose ]; then echo "$IN_CONTAINER_HERE/$1"; else echo "$HERE/$1"; fi
}

out_path() {  # results/<...> as the process sees it
  if [ "$MODE" = compose ]; then echo "$IN_CONTAINER_HERE/$1"; else echo "$HERE/$1"; fi
}

# profile RUN_ID OUTDIR_REL [stage_memory args...] : one profiled process, log beside its CSVs.
profile() {
  local run_id="$1" outrel="$2"; shift 2
  mkdir -p "$HERE/$outrel"
  local common=(--input-type "$INPUT_TYPE" --input-path "$INPUT_PATH" --interval "$INTERVAL"
                --out "$(out_path "$outrel")" --run-id "$run_id")
  [ -n "${CONTAINER:-}" ] && common+=(--container "$CONTAINER")
  [ -n "${OUTPUT_PATH:-}" ] && common+=(--output-path "$OUTPUT_PATH")
  [ "${NO_DOCINTEL:-false}" = true ] && common+=(--no-docintel)
  # shellcheck disable=SC2206
  local extra=(${EXTRA_ARGS:-})
  log "start $run_id: $*"
  if python_in_mode "$run_id" "$(script_path stage_memory.py)" "${common[@]}" ${extra[@]+"${extra[@]}"} "$@" \
       >"$HERE/$outrel/${run_id}.log" 2>&1; then
    log "done  $run_id"
  else
    log "FAILED $run_id (exit $?): see $outrel/${run_id}.log"
    return 1
  fi
}

# --------------------------------------------------------------------------- host sampler

to_mb() {  # "1.5GiB" / "512MiB" / "800kB" -> MB (docker stats units)
  awk -v v="$1" 'BEGIN{
    n=v; sub(/[A-Za-z]+$/,"",n); u=v; sub(/^[0-9.]+/,"",u);
    f=1/1048576; if(u=="KiB"||u=="kB"||u=="KB")f=1/1024; if(u=="MiB"||u=="MB")f=1; if(u=="GiB"||u=="GB")f=1024; if(u=="TiB"||u=="TB")f=1048576;
    printf "%.1f", n*f }'
}

container_mb() {  # full container id -> "current_mb working_set_mb", from the host's cgroup files
  local id="$1" dir="" f
  for f in "/sys/fs/cgroup/system.slice/docker-${id}.scope" "/sys/fs/cgroup/docker/${id}" \
           "/sys/fs/cgroup/memory/docker/${id}" "/sys/fs/cgroup/memory/system.slice/docker-${id}.scope"; do
    [ -d "$f" ] && { dir="$f"; break; }
  done
  if [ -n "$dir" ] && [ -r "$dir/memory.current" ]; then
    awk -v cur="$(cat "$dir/memory.current")" '$1=="inactive_file"{ia=$2} END{printf "%.1f %.1f", cur/1048576, (cur-ia)/1048576}' "$dir/memory.stat"
  elif [ -n "$dir" ] && [ -r "$dir/memory.usage_in_bytes" ]; then
    awk -v cur="$(cat "$dir/memory.usage_in_bytes")" '$1=="total_inactive_file"{ia=$2} END{printf "%.1f %.1f", cur/1048576, (cur-ia)/1048576}' "$dir/memory.stat"
  else  # no cgroup path visible: docker stats (slower, already excludes cache)
    local used
    used="$(docker stats --no-stream --format '{{.MemUsage}}' "$id" 2>/dev/null | awk '{print $1}')"
    [ -n "$used" ] && { local mb; mb="$(to_mb "$used")"; echo "$mb $mb"; } || echo " "
  fi
}

# sample_host CSV SCENARIO STOPFILE : one row per HOST_INTERVAL until STOPFILE exists.
sample_host() {
  local csv="$1" scenario="$2" stop="$3"
  echo "ts,scenario,host_total_mb,host_used_mb,host_available_mb,swap_used_mb,self_cgroup_mb,containers,containers_mb,containers_ws_mb,per_container" >"$csv"
  local names="${csv%.csv}.names"   # "<container id> <run label>" cache (no bash-4 maps)
  : >"$names"
  while [ ! -e "$stop" ]; do
    local ts total used avail swap self_cg="" n=0 sum=0 sumws=0 per="" id mbs mb ws
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    read -r total used avail < <(free -m 2>/dev/null | awk '/^Mem:/{print $2, $3, $7}') || true
    swap="$(free -m 2>/dev/null | awk '/^Swap:/{print $3}' || true)"
    if [ "$MODE" = local ]; then
      for f in /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory/memory.usage_in_bytes; do
        [ -r "$f" ] && { self_cg="$(awk '{printf "%.1f", $1/1048576}' "$f")"; break; }
      done
    else
      for id in $(docker ps -q --no-trunc --filter "label=memprof.session=${SESSION}" 2>/dev/null); do
        local label
        label="$(awk -v id="$id" '$1==id{print $2}' "$names")"
        if [ -z "$label" ]; then
          label="$(docker inspect -f '{{ index .Config.Labels "memprof.run" }}' "$id" 2>/dev/null || echo "${id:0:12}")"
          echo "$id $label" >>"$names"
        fi
        read -r mb ws <<<"$(container_mb "$id")" || true
        [ -z "${mb:-}" ] && continue
        n=$((n + 1))
        sum="$(awk -v a="$sum" -v b="$mb" 'BEGIN{printf "%.1f", a+b}')"
        sumws="$(awk -v a="$sumws" -v b="$ws" 'BEGIN{printf "%.1f", a+b}')"
        per="${per:+$per;}${label}=${ws}"
      done
    fi
    echo "${ts},${scenario},${total},${used},${avail},${swap},${self_cg},${n},${sum},${sumws},\"${per}\"" >>"$csv"
    sleep "$HOST_INTERVAL"
  done
}

# scenario NAME FUNCTION : results dir + host sampler around FUNCTION; prints the peaks.
scenario() {
  local name="$1" fn="$2"
  local outrel="results/${SESSION}/${name}"
  mkdir -p "$HERE/$outrel"
  local stop="$HERE/$outrel/.stop"
  rm -f "$stop"
  SAMPLER_STOPS+=("$stop")
  sample_host "$HERE/$outrel/host_memory.csv" "$name" "$stop" &
  local sampler=$!
  log "=== $name -> $outrel"
  local rc=0
  "$fn" "$outrel" || rc=$?
  touch "$stop"; wait "$sampler" 2>/dev/null || true; rm -f "$stop" "$HERE/$outrel/host_memory.names"
  awk -F, 'NR>1{ if($4>u)u=$4; if($10>w)w=$10; if($9>c)c=$9; if($7>s)s=$7 }
           END{printf "    host used peak %s MB; containers peak %s MB (working set %s MB); own cgroup peak %s MB\n", u+0, c+0, w+0, s+0}' \
      "$HERE/$outrel/host_memory.csv"
  if command -v python3 >/dev/null 2>&1; then
    python3 "$HERE/report.py" "$HERE/$outrel" --no-plots >"$HERE/$outrel/report.txt" 2>&1 || true
    log "    report: $outrel/report.txt"
  fi
  return $rc
}

# --------------------------------------------------------------------------- scenarios

run_cold_single() {
  need_chart CHART_SMALL "${CHART_SMALL:-}" || return 2
  local ocr=(); [ "${SKIP_OCR:-true}" = true ] && ocr=(--skip-ocr)
  profile "cold-single-${CHART_SMALL}" "$1" --chart "$CHART_SMALL" ${ocr[@]+"${ocr[@]}"}
}

run_warm_repeat() {
  need_chart CHART_SMALL "${CHART_SMALL:-}" || return 2
  local ocr=(); [ "${SKIP_OCR:-true}" = true ] && ocr=(--skip-ocr)
  profile "warm-repeat-${CHART_SMALL}" "$1" --chart "$CHART_SMALL" --repeat 2 ${ocr[@]+"${ocr[@]}"}
}

run_size_ladder() {
  need_chart CHART_SMALL "${CHART_SMALL:-}" || return 2; need_chart CHART_MEDIUM "${CHART_MEDIUM:-}" || return 2; need_chart CHART_LARGE "${CHART_LARGE:-}" || return 2
  local ocr=(); [ "${SKIP_OCR:-true}" = true ] && ocr=(--skip-ocr)
  local rc=0 size chart
  for size in small medium large; do
    case "$size" in small) chart="$CHART_SMALL" ;; medium) chart="$CHART_MEDIUM" ;; large) chart="$CHART_LARGE" ;; esac
    profile "size-${size}-${chart}" "$1" --chart "$chart" ${ocr[@]+"${ocr[@]}"} || rc=1
  done
  return $rc
}

run_parallel() {
  # shellcheck disable=SC2206
  local charts=(${PARALLEL_CHARTS:-})
  [ "${#charts[@]}" -ge "$PARALLEL_N" ] || { echo "PARALLEL_CHARTS has ${#charts[@]} chart(s); parallel-$PARALLEL_N needs $PARALLEL_N distinct ones" >&2; return 2; }
  charts=("${charts[@]:0:$PARALLEL_N}")
  local c seen=" "
  for c in "${charts[@]}"; do
    need_chart PARALLEL_CHARTS "$c" || return 2
    case "$seen" in *" $c "*) echo "PARALLEL_CHARTS repeats $c: parallel runs need distinct charts (one working folder each)" >&2; return 2 ;; esac
    seen="$seen$c "
  done
  local ocr=(); [ "${SKIP_OCR:-true}" = true ] && ocr=(--skip-ocr)
  local pids=() i=0 rc=0
  for c in "${charts[@]}"; do
    i=$((i + 1))
    profile "parallel${PARALLEL_N}-p${i}-${c}" "$1" --chart "$c" ${ocr[@]+"${ocr[@]}"} &
    pids+=($!)
  done
  for i in "${pids[@]}"; do wait "$i" || rc=1; done
  return $rc
}

run_skip_ocr() {
  need_chart CHART_SMALL "${CHART_SMALL:-}" || return 2
  if [ "${NO_DOCINTEL:-false}" != true ] && [ "$ASSUME_YES" != true ]; then
    read -r -p "skip-ocr runs $CHART_SMALL with full OCR first: Azure Document Intelligence bills its routed pages. Continue? [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]] || { echo "skipped (set NO_DOCINTEL=true, or -y)"; return 0; }
  fi
  local rc=0
  profile "full-ocr-${CHART_SMALL}" "$1" --chart "$CHART_SMALL" || rc=1
  profile "skip-ocr-${CHART_SMALL}" "$1" --chart "$CHART_SMALL" --skip-ocr || rc=1
  return $rc
}

# --------------------------------------------------------------------------- commands

do_setup() {
  if [ "$MODE" = compose ]; then
    (cd "$COMPOSE_ABS" && docker compose run --rm --no-deps -T -v "${UTIL_ABS}:/app/utilities" "$SERVICE" \
        pip install --quiet --disable-pip-version-check --target "$IN_CONTAINER_HERE/.pydeps" psutil matplotlib)
  else
    "$PYTHON" -m pip install --quiet --disable-pip-version-check --target "$HERE/.pydeps" psutil matplotlib
  fi
  log "installed into $HERE/.pydeps"
}

rc=0
for cmd in "$@"; do
  case "$cmd" in
    setup)        do_setup ;;
    selftest)     mkdir -p "$HERE/results/selftest"
                  python_in_mode selftest "$(script_path stage_memory.py)" --self-test \
                      --out "$(out_path results/selftest)" --run-id "selftest-$SESSION" || rc=1 ;;
    cold-single)  scenario cold-single run_cold_single || rc=1 ;;
    warm-repeat)  scenario warm-repeat run_warm_repeat || rc=1 ;;
    size-ladder)  scenario size-ladder run_size_ladder || rc=1 ;;
    parallel|parallel-*)
                  [[ "$cmd" =~ ^parallel-([0-9]+)$ ]] && PARALLEL_N="${BASH_REMATCH[1]}"
                  scenario "parallel-${PARALLEL_N}" run_parallel || rc=1 ;;
    skip-ocr)     scenario skip-ocr run_skip_ocr || rc=1 ;;
    all)          for s in cold-single warm-repeat size-ladder; do scenario "$s" "run_${s//-/_}" || rc=1; done
                  scenario "parallel-${PARALLEL_N}" run_parallel || rc=1 ;;
    report)       target="$HERE/results"; [ -d "$HERE/results/$SESSION" ] && target="$HERE/results/$SESSION"
                  if command -v python3 >/dev/null 2>&1; then python3 "$HERE/report.py" "$target"
                  else python_in_mode report "$(script_path report.py)" "$(out_path "${target#"$HERE"/}")"; fi ;;
    *)            echo "unknown command: $cmd" >&2; usage; exit 2 ;;
  esac
done
exit $rc
