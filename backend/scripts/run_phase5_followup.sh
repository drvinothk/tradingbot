#!/usr/bin/env bash
# Phase 5 refinement sweep (2026-08-30): trail_lock_fraction bracket (0.4,
# 0.8 -- reusing existing 0.6 data from Phase 2) on the 12 strongest
# candidates across all 4 strategies, plus 4 new untested triple/pair
# entry-gate combos (ATR expansion stacked with the loose/tight PCR band,
# motivated by today's finding that ATR expansion is the single most
# consistently valuable lever) run through the same 3-stop bracket Phase 2
# used. Config list comes from stdin, same reasoning as run_phase2_exit_tuning.sh.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=28
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s4p5_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/s4p5_refinement_${STAMP}
STATUS_FILE=~/s4p5_status.log
LOG_DIR=/tmp/s4p5_logs
CONFIG_FILE="${1:?usage: run_phase5_followup.sh <config_list_file>}"
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

n_configs=$(grep -cv '^#' "$CONFIG_FILE")
log "=========================================="
log "Phase 5 refinement sweep starting -> $RESULTS_DIR ($n_configs configs)"
log "=========================================="
sweep_start=$(date +%s)

while IFS='|' read -r name strategy_type source params; do
  [ -z "$name" ] && continue
  case "$name" in \#*) continue ;; esac
  cfg_start=$(date +%s)
  log "--- $name ($strategy_type, src=$source) params=$params"

  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py \
      --strategy "$strategy_type" --underlying NIFTY \
      --all-expiries --options-subdir options_1min_past \
      --underlying-source "$source" \
      --exit-mode current --fast --near-expiry-days "$NEAR_EXPIRY_DAYS" \
      --strategy-params "$params" \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "s4p5_${name}_s${i}" \
      --out-csv "${RESULTS_DIR}/${name}_s${i}.csv" \
      > "${LOG_DIR}/${name}_s${i}.log" 2>&1 &
    pids+=($!)
  done

  fail=0
  for pid in "${pids[@]}"; do wait "$pid" || fail=1; done

  "$PY" scripts/merge_backtest_shards.py \
    --glob "${RESULTS_DIR}/${name}_s*.csv" \
    --out "${RESULTS_DIR}/${name}_current.csv" \
    >> "${LOG_DIR}/${name}_merge.log" 2>&1 || log "*** $name merge FAILED"

  reap_dbs

  el=$(( $(date +%s) - cfg_start ))
  n=$(( $(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1 ))
  free=$(df -h --output=avail / | tail -1 | tr -d ' ')
  log "$name $([ $fail -eq 0 ] && echo OK || echo 'HAD SHARD FAILURES') (${el}s, ${n} trades, ${free} free)"
done < "$CONFIG_FILE"

log "=========================================="
log "S4P4 REFINEMENT SWEEP COMPLETE -> $RESULTS_DIR ($(( ($(date +%s) - sweep_start) / 60 ))min total)"
log "=========================================="
