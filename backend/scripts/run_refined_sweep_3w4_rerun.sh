#!/usr/bin/env bash
# Sweep #3 W4 -- RERUN of the 6 configs that failed on the first pass when the
# VM disk hit 100% (2026-08-29). Same params as run_refined_sweep_3w4_exitgrid.sh,
# only the not-yet-completed configs.
#
# Adds disk hygiene the base driver lacks: run_backtest.py CREATEs a
# per-`--db-suffix` database and NEVER drops it, so every sweep leaks
# ~20-70 MB x (configs x shards) of Postgres scratch DBs forever. This script
# reaps its own `trading_bot_backtest_s3w4_*` DBs (a) at start, (b) after each
# config's shards + merge finish, and (c) on EXIT (success or fail).
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s3w4_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w4_exitgrid_rerun_${STAMP}
STATUS_FILE=/tmp/sweep3w4_rerun_status.log
LOG_DIR=/tmp/sweep3w4_rerun_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

G='"require_prior_day_trend":true,"max_or_range_nifty_points":65'

# name|params-json  (strategy=orb_conviction, src=alice_index)  -- the 6 that failed
CONFIGS=(
  "x_tgt50_arm12|{$G,\"target_pct\":0.50,\"trail_activation_fraction\":0.24}"
  "x_tgt40_armrel|{$G,\"target_pct\":0.40}"
  "x_baseline_stop10|{$G,\"stop_pct\":0.10}"
  "x_baseline_stop15|{$G,\"stop_pct\":0.15}"
  "x_notgt_arm08_stop10|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.08,\"stop_pct\":0.10}"
  "x_notgt_arm08_stop15|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.08,\"stop_pct\":0.15}"
)

{
  echo "sweep 3w4 RERUN ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index"
  echo "entry gate G (every config): {$G}"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w4 RERUN ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
log "reaping any stale ${DB_PREFIX}* DBs before start"; reap_dbs
log "disk: $(df -h / | awk 'NR==2 {print $4" free ("$5" used)"}')"

for entry in "${CONFIGS[@]}"; do
  IFS='|' read -r name params <<< "$entry"
  cfg_start=$(date +%s)
  log "--- $name  params=$params"
  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py \
      --strategy orb_conviction --underlying NIFTY \
      --all-expiries --options-subdir options_1min_past --underlying-source alice_index \
      --exit-mode current --fast --near-expiry-days "$NEAR_EXPIRY_DAYS" \
      --strategy-params "$params" \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "s3w4_${name}_s${i}" \
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
  reap_dbs   # <-- drop this config's 18 scratch DBs before the next config
  el=$(( $(date +%s) - cfg_start ))
  n=$(($(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1))
  [ "$fail" -eq 1 ] && log "*** $name: a shard FAILED (${el}s, ${n} trades, $(df -h / | awk 'NR==2{print $4}') free)" \
                    || log "$name OK (${el}s, ${n} trades, $(df -h / | awk 'NR==2{print $4}') free)"
done

log "=== sweep 3w4 RERUN complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
