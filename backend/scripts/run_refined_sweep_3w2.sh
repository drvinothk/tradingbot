#!/usr/bin/env bash
# Sweep #3, Workstream 2 (2026-08-29) -- the other 4 framework strategies on
# the near-week-corrected harness.
#
# ORB (sweeps #1-#3) has no robust edge on current-week NIFTY weeklies. This
# probes whether vwap_pullback / ema_micro_pullback / oi_volume_confirmed /
# liquidity_sweep_reversal do. All --near-expiry-days 6 (current expiry week
# only), --exit-mode current, ALL days / full hours, VIX seeded + PCR floored
# (automatic in run_backtest). 18-way sharded.
#
# Per-config source:
#   vwap_pullback -> futures_proxy  (index feed reports volume=0, so VWAP
#   never forms; futures_proxy is the volume-bearing series, matching the
#   production set_volume_proxy fix). All others -> alice_index.
# oi_volume_confirmed always runs with its futures-volume / OI-buildup
# confirmation knobs OFF -- the chain-participation-weighted ranking mode is
# the only one the single-snapshot-per-run backtest supports.
#
# Smoke-gate result (all 4 fire on near-week data): ema/oi ~1 trade / few
# expiries, liquidity_sweep ~1/expiry, vwap ~1 / few expiries on futures_proxy.
# Expect thin n (~8-25/config over 52 expiries) -- PROBE, judged on the same
# robust bar as everything else (analyze_walkforward.py).
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w2_frameworks_${STAMP}
STATUS_FILE=/tmp/sweep3w2_status.log
LOG_DIR=/tmp/sweep3w2_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

OFF='"oi_use_futures_volume_confirmation":false,"oi_use_atm_oi_buildup":false'

# name|strategy|underlying-source|params-json
CONFIGS=(
  # --- vwap_pullback (futures_proxy) ---
  "w2_vwap_base|vwap_pullback|futures_proxy|{}"
  "w2_vwap_stop15|vwap_pullback|futures_proxy|{\"stop_pct\":0.15}"
  "w2_vwap_tgt30|vwap_pullback|futures_proxy|{\"target_pct\":0.30}"
  "w2_vwap_trail03|vwap_pullback|futures_proxy|{\"trail_activation_fraction\":0.3}"
  "w2_vwap_trendlb40|vwap_pullback|futures_proxy|{\"trend_lookback_bars\":40}"
  "w2_vwap_side08|vwap_pullback|futures_proxy|{\"min_trend_side_fraction\":0.8}"
  # --- ema_micro_pullback (alice_index) ---
  "w2_ema_base|ema_micro_pullback|alice_index|{}"
  "w2_ema_morning|ema_micro_pullback|alice_index|{\"ema_morning_window_end\":\"11:00\",\"ema_afternoon_window_start\":\"11:00\",\"ema_afternoon_window_end\":\"11:00\"}"
  "w2_ema_stop14|ema_micro_pullback|alice_index|{\"stop_pct\":0.14}"
  "w2_ema_tgt24|ema_micro_pullback|alice_index|{\"target_pct\":0.24}"
  "w2_ema_body05|ema_micro_pullback|alice_index|{\"min_body_ratio\":0.5}"
  "w2_ema_exp5|ema_micro_pullback|alice_index|{\"ema_expansion_lookback\":5}"
  # --- oi_volume_confirmed (alice_index, confirmation OFF) ---
  "w2_oi_base|oi_volume_confirmed|alice_index|{$OFF}"
  "w2_oi_morning|oi_volume_confirmed|alice_index|{$OFF,\"oi_morning_window_end\":\"11:00\",\"oi_afternoon_window_start\":\"11:00\",\"oi_afternoon_window_end\":\"11:00\"}"
  "w2_oi_stop16|oi_volume_confirmed|alice_index|{$OFF,\"stop_pct\":0.16}"
  "w2_oi_tgt30|oi_volume_confirmed|alice_index|{$OFF,\"target_pct\":0.30}"
  "w2_oi_range90|oi_volume_confirmed|alice_index|{$OFF,\"max_range_nifty_points\":90}"
  "w2_oi_body05|oi_volume_confirmed|alice_index|{$OFF,\"min_body_ratio\":0.5}"
  # --- liquidity_sweep_reversal (alice_index) ---
  "w2_liq_base|liquidity_sweep_reversal|alice_index|{}"
  "w2_liq_morning|liquidity_sweep_reversal|alice_index|{\"sweep_morning_window_end\":\"11:00\",\"sweep_afternoon_window_start\":\"11:00\",\"sweep_afternoon_window_end\":\"11:00\"}"
  "w2_liq_stop15|liquidity_sweep_reversal|alice_index|{\"stop_pct\":0.15}"
  "w2_liq_tgt30|liquidity_sweep_reversal|alice_index|{\"target_pct\":0.30}"
  "w2_liq_dist10|liquidity_sweep_reversal|alice_index|{\"min_sweep_distance_nifty_points\":10}"
  "w2_liq_lb15|liquidity_sweep_reversal|alice_index|{\"lookback_bars\":15}"
)

{
  echo "sweep 3w2 (frameworks) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  exit_mode=current  --all-expiries"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w2 ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="

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
      --db-suffix "s3w2_${name}_s${i}" \
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
  el=$(( $(date +%s) - cfg_start ))
  n=$(($(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1))
  [ "$fail" -eq 1 ] && log "*** $name: a shard FAILED (${el}s, ${n} trades)" \
                    || log "$name OK (${el}s, ${n} trades)"
done

log "=== sweep 3w2 complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
