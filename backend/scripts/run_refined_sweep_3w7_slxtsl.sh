#!/usr/bin/env bash
# Sweep #3 W7 -- stop x TSL grid on d_pdt_w65 (2026-08-29). NO code change.
#
# W4 found looser premium stop is the dominant lever (-10 -> -12 -> -15 -> -18
# all improving) and trail-arm timing is a win% <-> PnL dial, but only tested
# arm +6/+8 at stop 15/18 and stop 18 has no arm ladder at all. This fills the
# grid: stop {0.15,0.18} x trail_activation_fraction (= +X% of entry, since
# target_pct=1.0 -> arm dist = entry*frac) {0.06,0.08,0.10,0.12,0.14} x
# trail_lock_fraction {0.4,0.6} = 20 configs, no fixed target. Plus 4
# stop x target anchors (zero stop18 x target data exists).
#
# Every config = the SAME 26 d_pdt_w65 entries (gate G unchanged) -> pure
# exit-overlay re-slice. Isolated DB prefix s3w7t_ (NOT s3w7_ -- that clashes
# with the separate "W7 Lorentzian" sweep) so it runs alongside W6 and that.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s3w7t_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w7tsl_${STAMP}
STATUS_FILE=/tmp/sweep3w7tsl_status.log
LOG_DIR=/tmp/sweep3w7tsl_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

G='"require_prior_day_trend":true,"max_or_range_nifty_points":65'
T='"target_pct":1.0'

# name|params-json  (strategy=orb_conviction, src=alice_index)
CONFIGS=(
  # --- grid: stop {0.15,0.18} x arm {0.06,0.08,0.10,0.12,0.14} x lock {0.4,0.6}, no target ---
  "w7_s15_a06_l04|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.4}"
  "w7_s15_a06_l06|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.6}"
  "w7_s15_a08_l04|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.08,\"trail_lock_fraction\":0.4}"
  "w7_s15_a08_l06|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.08,\"trail_lock_fraction\":0.6}"
  "w7_s15_a10_l04|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.10,\"trail_lock_fraction\":0.4}"
  "w7_s15_a10_l06|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.10,\"trail_lock_fraction\":0.6}"
  "w7_s15_a12_l04|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.4}"
  "w7_s15_a12_l06|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.6}"
  "w7_s15_a14_l04|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.4}"
  "w7_s15_a14_l06|{$G,$T,\"stop_pct\":0.15,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.6}"
  "w7_s18_a06_l04|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.4}"
  "w7_s18_a06_l06|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.6}"
  "w7_s18_a08_l04|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.08,\"trail_lock_fraction\":0.4}"
  "w7_s18_a08_l06|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.08,\"trail_lock_fraction\":0.6}"
  "w7_s18_a10_l04|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.10,\"trail_lock_fraction\":0.4}"
  "w7_s18_a10_l06|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.10,\"trail_lock_fraction\":0.6}"
  "w7_s18_a12_l04|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.4}"
  "w7_s18_a12_l06|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.6}"
  "w7_s18_a14_l04|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.4}"
  "w7_s18_a14_l06|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.6}"
  # --- stop x target anchors (default trail unless noted) ---
  "w7_s15_tgt40|{$G,\"stop_pct\":0.15,\"target_pct\":0.40}"
  "w7_s18_tgt30|{$G,\"stop_pct\":0.18,\"target_pct\":0.30}"
  "w7_s18_tgt40|{$G,\"stop_pct\":0.18,\"target_pct\":0.40}"
  # target 0.30 so trail_activation_fraction 0.42 -> arms at 0.30*0.42 = +12.6% of entry
  "w7_s18_tgt30_a14_l06|{$G,\"stop_pct\":0.18,\"target_pct\":0.30,\"trail_activation_fraction\":0.42,\"trail_lock_fraction\":0.6}"
)

{
  echo "sweep 3w7 (stop x TSL grid) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index"
  echo "entry gate G (every config): {$G}   grid: no fixed target ({$T}), arm frac = +X% of entry"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w7 ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
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
      --db-suffix "s3w7t_${name}_s${i}" \
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

log "=== sweep 3w7 complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
