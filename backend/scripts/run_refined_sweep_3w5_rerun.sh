#!/usr/bin/env bash
# Sweep #3 W5 -- RERUN of the 19 configs that failed on the first pass when the
# VM disk hit 100% (2026-08-29). Same params as
# run_refined_sweep_3w5_frameworks_deep.sh, only the not-yet-completed configs
# (3 ema + all 8 oi + all 8 liq). vwap x8 and ema x5 already completed.
#
# Same disk hygiene as run_refined_sweep_3w4_rerun.sh: reaps its own
# `trading_bot_backtest_s3w5_*` scratch DBs at start, after every config, and
# on EXIT -- run_backtest.py creates per-suffix DBs and never drops them.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s3w5_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w5_frameworks_deep_rerun_${STAMP}
STATUS_FILE=/tmp/sweep3w5_rerun_status.log
LOG_DIR=/tmp/sweep3w5_rerun_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

OFF='"oi_use_futures_volume_confirmation":false,"oi_use_atm_oi_buildup":false'

# name|strategy|underlying-source|params-json  -- the 19 that failed
CONFIGS=(
  "w5_ema_exp5|ema_micro_pullback|alice_index|{\"ema_expansion_lookback\":5}"
  "w5_ema_am|ema_micro_pullback|alice_index|{\"ema_morning_window_end\":\"11:00\",\"ema_afternoon_window_start\":\"11:00\",\"ema_afternoon_window_end\":\"11:00\"}"
  "w5_ema_body55_exp4|ema_micro_pullback|alice_index|{\"min_body_ratio\":0.55,\"ema_expansion_lookback\":4}"
  "w5_oi_base|oi_volume_confirmed|alice_index|{$OFF}"
  "w5_oi_lb3|oi_volume_confirmed|alice_index|{$OFF,\"lookback_bars\":3}"
  "w5_oi_lb8|oi_volume_confirmed|alice_index|{$OFF,\"lookback_bars\":8}"
  "w5_oi_maxr45|oi_volume_confirmed|alice_index|{$OFF,\"max_range_nifty_points\":45}"
  "w5_oi_maxr50|oi_volume_confirmed|alice_index|{$OFF,\"max_range_nifty_points\":50}"
  "w5_oi_body55|oi_volume_confirmed|alice_index|{$OFF,\"min_body_ratio\":0.55}"
  "w5_oi_am|oi_volume_confirmed|alice_index|{$OFF,\"oi_morning_window_end\":\"11:00\",\"oi_afternoon_window_start\":\"11:00\",\"oi_afternoon_window_end\":\"11:00\"}"
  "w5_oi_lb8_maxr50_body55|oi_volume_confirmed|alice_index|{$OFF,\"lookback_bars\":8,\"max_range_nifty_points\":50,\"min_body_ratio\":0.55}"
  "w5_liq_base|liquidity_sweep_reversal|alice_index|{}"
  "w5_liq_dist8|liquidity_sweep_reversal|alice_index|{\"min_sweep_distance_nifty_points\":8}"
  "w5_liq_dist12|liquidity_sweep_reversal|alice_index|{\"min_sweep_distance_nifty_points\":12}"
  "w5_liq_lb12|liquidity_sweep_reversal|alice_index|{\"lookback_bars\":12}"
  "w5_liq_lb15|liquidity_sweep_reversal|alice_index|{\"lookback_bars\":15}"
  "w5_liq_maxr90|liquidity_sweep_reversal|alice_index|{\"sweep_max_range_width_nifty_points\":90}"
  "w5_liq_body50|liquidity_sweep_reversal|alice_index|{\"min_body_ratio\":0.50}"
  "w5_liq_dist10_maxr100_lb15|liquidity_sweep_reversal|alice_index|{\"min_sweep_distance_nifty_points\":10,\"sweep_max_range_width_nifty_points\":100,\"lookback_bars\":15}"
)

{
  echo "sweep 3w5 RERUN ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  exit_mode=current  --all-expiries"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w5 RERUN ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
log "reaping any stale ${DB_PREFIX}* DBs before start"; reap_dbs
log "disk: $(df -h / | awk 'NR==2 {print $4" free ("$5" used)"}')"

for entry in "${CONFIGS[@]}"; do
  IFS='|' read -r name strat src params <<< "$entry"
  cfg_start=$(date +%s)
  log "--- $name ($strat, src=$src) params=$params"
  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py \
      --strategy "$strat" --underlying NIFTY \
      --all-expiries --options-subdir options_1min_past --underlying-source "$src" \
      --exit-mode current --fast --near-expiry-days "$NEAR_EXPIRY_DAYS" \
      --strategy-params "$params" \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "s3w5_${name}_s${i}" \
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
  n=$(($(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1))
  [ "$fail" -eq 1 ] && log "*** $name: a shard FAILED (${el}s, ${n} trades, $(df -h / | awk 'NR==2{print $4}') free)" \
                    || log "$name OK (${el}s, ${n} trades, $(df -h / | awk 'NR==2{print $4}') free)"
done

log "=== sweep 3w5 RERUN complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
