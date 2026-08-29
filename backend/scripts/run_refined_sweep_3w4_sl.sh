#!/usr/bin/env bash
# Sweep #3 W4 -- SL-axis follow-up on d_pdt_w65 (2026-08-29).
#
# The first W4 pass found the SL is the real lever, not the target:
#   stop_pct 0.10 -> win 58% E +229    (tighter = worse)
#   stop_pct 0.12 -> win 65% E +274    (current d_pdt_w65 default)
#   stop_pct 0.15 -> win 73% E +400 PF 5.11 P(mean<=0) 0.002   (looser = much better)
# Loosening the premium stop lets trades that dipped ~-13% then recovered
# survive to trail/target out green, at a small maxDD cost (+~140/lot).
#
# This batch: does stop15 stack with the early-trail-arm changes, does an
# even looser stop18 keep helping or break, and does stop15 + no-target work.
# All carry the d_pdt_w65 gate G.
#
# Isolated DB prefix (s3w4s_) so this can run alongside the other W4 batches
# without their reapers colliding.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s3w4s_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w4_sl_${STAMP}
STATUS_FILE=/tmp/sweep3w4_sl_status.log
LOG_DIR=/tmp/sweep3w4_sl_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

G='"require_prior_day_trend":true,"max_or_range_nifty_points":65'

# name|params-json  (strategy=orb_conviction, src=alice_index)
CONFIGS=(
  "x_stop15_arm06|{$G,\"stop_pct\":0.15,\"trail_activation_fraction\":0.30}"
  "x_stop15_arm06_lock06|{$G,\"stop_pct\":0.15,\"trail_activation_fraction\":0.30,\"trail_lock_fraction\":0.6}"
  "x_stop15_arm08_lock06|{$G,\"stop_pct\":0.15,\"trail_activation_fraction\":0.40,\"trail_lock_fraction\":0.6}"
  "x_stop18|{$G,\"stop_pct\":0.18}"
  "x_stop18_arm06_lock06|{$G,\"stop_pct\":0.18,\"trail_activation_fraction\":0.30,\"trail_lock_fraction\":0.6}"
  "x_stop15_tgt30|{$G,\"stop_pct\":0.15,\"target_pct\":0.30}"
  "x_notgt_stop15_arm06_lock06|{$G,\"stop_pct\":0.15,\"target_pct\":1.0,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.6}"
)

{
  echo "sweep 3w4 SL follow-up ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index"
  echo "entry gate G (every config): {$G}"
  echo "NB arm06 = trail_activation_fraction 0.30 (target dist 0.20*E -> arms +6% of E)"
  echo "   arm08 = trail_activation_fraction 0.40 -> arms +8% of E"
  echo "   no-target arm06 = target_pct 1.0, trail_activation_fraction 0.06 -> arms +6% of E"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w4 SL ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
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
      --db-suffix "s3w4s_${name}_s${i}" \
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

log "=== sweep 3w4 SL complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
