#!/usr/bin/env bash
# Sweep #3, Workstream 5 (2026-08-29) -- deep entry-tightening pass on the
# other 4 framework strategies, NIFTY current expiry week.
#
# W2 tried base + ~6 mostly-EXIT variants each and all 4 were net-negative on
# near-week NIFTY. This pass tightens ENTRY quality only, using constructor
# kwargs that already exist -- NO strategy code changes. If any variant clears
# the loose gate (>=55% win AND positive OOS AND positive in both 6-mo halves
# AND boot P(mean<=0) <= ~0.20 in analyze_walkforward.py) it earns a
# "paper-trade candidate" label; otherwise the strategy is permanently parked
# with the rationale written to BACKTEST_LEARNINGS.md.
#
# Per-config source:
#   vwap_pullback -> futures_proxy  (index feed volume=0 -> VWAP never forms).
#   all others    -> alice_index.
# oi_volume_confirmed always runs with its futures-volume / OI-buildup
# confirmation knobs OFF (single-snapshot-per-run backtest can't support the
# temporal modes).
#
# --near-expiry-days 6, --exit-mode current, ALL days / full hours, VIX
# seeded + PCR floored (automatic). 18-way sharded. Expect thin n (~8-47
# /config over 52 expiries) -- PROBE.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w5_frameworks_deep_${STAMP}
STATUS_FILE=/tmp/sweep3w5_status.log
LOG_DIR=/tmp/sweep3w5_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

# Disk hygiene: run_backtest.py CREATEs a per-`--db-suffix` database and NEVER
# drops it, so a sweep leaks ~20-70 MB x (configs x shards) of Postgres
# scratch DBs forever (filled the VM disk 2026-08-29). Reap this sweep's own
# `trading_bot_backtest_s3w5_*` DBs at start, after each config, and on EXIT.
PSQL="sudo -u postgres psql"
DB_PREFIX="trading_bot_backtest_s3w5_"
reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

OFF='"oi_use_futures_volume_confirmation":false,"oi_use_atm_oi_buildup":false'

# name|strategy|underlying-source|params-json
CONFIGS=(
  # --- vwap_pullback (futures_proxy) -- probe around W2's lone trendlb40 (+86) ---
  "w5_vwap_base|vwap_pullback|futures_proxy|{}"
  "w5_vwap_lb30|vwap_pullback|futures_proxy|{\"trend_lookback_bars\":30}"
  "w5_vwap_lb50|vwap_pullback|futures_proxy|{\"trend_lookback_bars\":50}"
  "w5_vwap_side80|vwap_pullback|futures_proxy|{\"min_trend_side_fraction\":0.80}"
  "w5_vwap_side85|vwap_pullback|futures_proxy|{\"min_trend_side_fraction\":0.85}"
  "w5_vwap_cross2|vwap_pullback|futures_proxy|{\"max_vwap_crosses_in_lookback\":2}"
  "w5_vwap_tol10|vwap_pullback|futures_proxy|{\"pullback_tolerance_frac\":0.0010}"
  "w5_vwap_lb40_side80|vwap_pullback|futures_proxy|{\"trend_lookback_bars\":40,\"min_trend_side_fraction\":0.80}"
  # --- ema_micro_pullback (alice_index) ---
  "w5_ema_base|ema_micro_pullback|alice_index|{}"
  "w5_ema_body50|ema_micro_pullback|alice_index|{\"min_body_ratio\":0.50}"
  "w5_ema_body55|ema_micro_pullback|alice_index|{\"min_body_ratio\":0.55}"
  "w5_ema_body60|ema_micro_pullback|alice_index|{\"min_body_ratio\":0.60}"
  "w5_ema_exp4|ema_micro_pullback|alice_index|{\"ema_expansion_lookback\":4}"
  "w5_ema_exp5|ema_micro_pullback|alice_index|{\"ema_expansion_lookback\":5}"
  "w5_ema_am|ema_micro_pullback|alice_index|{\"ema_morning_window_end\":\"11:00\",\"ema_afternoon_window_start\":\"11:00\",\"ema_afternoon_window_end\":\"11:00\"}"
  "w5_ema_body55_exp4|ema_micro_pullback|alice_index|{\"min_body_ratio\":0.55,\"ema_expansion_lookback\":4}"
  # --- oi_volume_confirmed (alice_index, confirmation OFF) -- mirror the ORB maxOR finding ---
  "w5_oi_base|oi_volume_confirmed|alice_index|{$OFF}"
  "w5_oi_lb3|oi_volume_confirmed|alice_index|{$OFF,\"lookback_bars\":3}"
  "w5_oi_lb8|oi_volume_confirmed|alice_index|{$OFF,\"lookback_bars\":8}"
  "w5_oi_maxr45|oi_volume_confirmed|alice_index|{$OFF,\"max_range_nifty_points\":45}"
  "w5_oi_maxr50|oi_volume_confirmed|alice_index|{$OFF,\"max_range_nifty_points\":50}"
  "w5_oi_body55|oi_volume_confirmed|alice_index|{$OFF,\"min_body_ratio\":0.55}"
  "w5_oi_am|oi_volume_confirmed|alice_index|{$OFF,\"oi_morning_window_end\":\"11:00\",\"oi_afternoon_window_start\":\"11:00\",\"oi_afternoon_window_end\":\"11:00\"}"
  "w5_oi_lb8_maxr50_body55|oi_volume_confirmed|alice_index|{$OFF,\"lookback_bars\":8,\"max_range_nifty_points\":50,\"min_body_ratio\":0.55}"
  # --- liquidity_sweep_reversal (alice_index) -- probe around W2's dist10 (-104) ---
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
  echo "sweep 3w5 (frameworks deep entry) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  exit_mode=current  --all-expiries"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w5 ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
log "reaping any stale ${DB_PREFIX}* DBs before start"; reap_dbs

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
  reap_dbs   # drop this config's 18 scratch DBs before the next config
  el=$(( $(date +%s) - cfg_start ))
  n=$(($(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1))
  [ "$fail" -eq 1 ] && log "*** $name: a shard FAILED (${el}s, ${n} trades)" \
                    || log "$name OK (${el}s, ${n} trades)"
done

log "=== sweep 3w5 complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
