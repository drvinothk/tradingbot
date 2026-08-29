#!/usr/bin/env bash
# Sweep #3, Workstream 1 (2026-08-29) -- directional-regime filter on ORB,
# NIFTY current expiry week.
#
# Motivation: 3a proved ORB fires long and short mechanically and the PE
# (short) side is a structural loser on a net-up year (CE E +17..+250 vs
# PE E -43..-127 across every 3a config). A trend filter is also framework-
# faithful (Strategy A = "ORB + trend + volume").
#
# New gate: require_prior_day_trend (+ prior_day_trend_buffer_pts) -- CE only
# if underlying trades above prior-day close + buffer, PE only if below
# prior-day close - buffer. Also re-tests the existing require_htf_ema_trend
# and require_drift_alignment gates (weak/inert in sweep #2's DTE-artifact
# data, deserve a fair near-week re-run).
#
# --near-expiry-days 6, --exit-mode current, ALL days / full hours, VIX
# seeded + PCR floored (automatic). 18-way sharded.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w1_directional_${STAMP}
STATUS_FILE=/tmp/sweep3w1_status.log
LOG_DIR=/tmp/sweep3w1_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

# name|params-json  (strategy=orb_conviction, src=alice_index always)
CONFIGS=(
  "ref_orb_baseline|{}"
  "d_ce_only|{\"ce_only\":true}"
  "d_htf|{\"require_htf_ema_trend\":true}"
  "d_htf_slope10|{\"require_htf_ema_trend\":true,\"htf_ema_slope_lookback\":10}"
  "d_drift|{\"require_drift_alignment\":true}"
  "d_pdt|{\"require_prior_day_trend\":true}"
  "d_pdt_buf15|{\"require_prior_day_trend\":true,\"prior_day_trend_buffer_pts\":15}"
  "d_pdt_buf30|{\"require_prior_day_trend\":true,\"prior_day_trend_buffer_pts\":30}"
  "d_pdt_htf|{\"require_prior_day_trend\":true,\"require_htf_ema_trend\":true}"
  "d_pdt_w65|{\"require_prior_day_trend\":true,\"max_or_range_nifty_points\":65}"
  "d_htf_w65|{\"require_htf_ema_trend\":true,\"max_or_range_nifty_points\":65}"
  "d_pdt_cut1000|{\"require_prior_day_trend\":true,\"orb_entry_cutoff_time\":\"10:00\"}"
  "d_pdt_skipfri|{\"require_prior_day_trend\":true,\"skip_weekdays\":[\"Friday\"]}"
)

{
  echo "sweep 3w1 (directional ORB) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w1 ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="

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
      --db-suffix "s3w1_${name}_s${i}" \
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

log "=== sweep 3w1 complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
