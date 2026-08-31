#!/usr/bin/env bash
# Phase 6 generic driver (2026-08-31): same shape as run_phase4_armlock.sh /
# run_phase5_followup.sh, but appends $EXTRA_BT_ARGS (env var, e.g.
# '--min-minutes-before-trail-arm 2' or '--structure-stop-mode swing
# --swing-lookback 15') uniformly to every backtest invocation in this run.
# Usage: RUN_TAG=<tag> EXTRA_BT_ARGS='...' ./run_phase6_generic.sh <config_list_file>
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=28
NEAR_EXPIRY_DAYS=6
RUN_TAG="${RUN_TAG:?set RUN_TAG}"
EXTRA_BT_ARGS="${EXTRA_BT_ARGS:-}"
DB_PREFIX="trading_bot_backtest_s6_${RUN_TAG}_"
RESULTS_DIR="data/historical/backtest_reports/s6_${RUN_TAG}"
STATUS_FILE=~/s6_status.log
LOG_DIR="/tmp/s6_logs/${RUN_TAG}"
CONFIG_FILE="${1:?usage: run_phase6_generic.sh <config_list_file>}"
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [$RUN_TAG] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

n_configs=$(grep -cv '^#' "$CONFIG_FILE")
log "=========================================="
log "Phase 6 [$RUN_TAG] starting -> $RESULTS_DIR ($n_configs configs) extra_args='$EXTRA_BT_ARGS'"
log "=========================================="
sweep_start=$(date +%s)

while IFS='|' read -r name strategy_type source params; do
  [ -z "$name" ] && continue
  case "$name" in \#*) continue ;; esac
  cfg_start=$(date +%s)
  log "--- $name ($strategy_type, src=$source) params=$params"

  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py       --strategy "$strategy_type" --underlying NIFTY       --all-expiries --options-subdir options_1min_past       --underlying-source "$source"       --exit-mode current --fast --near-expiry-days "$NEAR_EXPIRY_DAYS"       --strategy-params "$params"       $EXTRA_BT_ARGS       --shard-count "$SHARD_COUNT" --shard-index "$i"       --db-suffix "s6_${RUN_TAG}_${name}_s${i}"       --out-csv "${RESULTS_DIR}/${name}_s${i}.csv"       > "${LOG_DIR}/${name}_s${i}.log" 2>&1 &
    pids+=($!)
  done

  fail=0
  for pid in "${pids[@]}"; do wait "$pid" || fail=1; done

  "$PY" scripts/merge_backtest_shards.py     --glob "${RESULTS_DIR}/${name}_s*.csv"     --out "${RESULTS_DIR}/${name}_current.csv"     >> "${LOG_DIR}/${name}_merge.log" 2>&1 || log "*** $name merge FAILED"

  reap_dbs

  el=$(( $(date +%s) - cfg_start ))
  n=$(( $(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1 ))
  free=$(df -h --output=avail / | tail -1 | tr -d ' ')
  log "$name $([ $fail -eq 0 ] && echo OK || echo 'HAD SHARD FAILURES') (${el}s, ${n} trades, ${free} free)"
done < "$CONFIG_FILE"

log "=========================================="
log "Phase 6 [$RUN_TAG] COMPLETE -> $RESULTS_DIR ($(( ($(date +%s) - sweep_start) / 60 ))min)"
log "=========================================="
