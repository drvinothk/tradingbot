#!/usr/bin/env bash
# Sweep #3 W4 -- EXTRA no-fixed-target TSL ladder on d_pdt_w65 (2026-08-29,
# user request: "TSL at 8%, 10% and 14% too, without fixed target").
#
# Fills the gaps in the first W4 pass so the no-target grid is a clean
# arm x lock matrix, all with target_pct=1.0 (no fixed target):
#   arm  in {6, 8, 10, 12, 14} %   x   trail_lock_fraction in {0.4, 0.6}
# Already have from sweep3w4_exitgrid: arm06/l04, arm08/l04, arm08/l06,
# arm12/l04.  This script adds the remaining 6 cells.
#
# "No target, arm at X%": target_pct=1.0 (+100% intrabar, ~never hit for an
# ATM weekly scalp) so target distance = 1.0*entry, hence
# trail_activation_fraction = X/100 arms the trail at exactly +X% of entry.
#
# Same disk hygiene as the other W4/W5 drivers: reaps its own
# trading_bot_backtest_s3w4x_* scratch DBs at start / per-config / on EXIT.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
# Distinct prefix (s3w4x_, not s3w4_) so this script's reaper never collides
# with a concurrently-running run_refined_sweep_3w4_rerun.sh (which owns s3w4_).
DB_PREFIX="trading_bot_backtest_s3w4x_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w4_exitgrid_extra_${STAMP}
STATUS_FILE=/tmp/sweep3w4_extra_status.log
LOG_DIR=/tmp/sweep3w4_extra_logs
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
  "x_notgt_arm06_lock06|{$G,$T,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.6}"
  "x_notgt_arm10|{$G,$T,\"trail_activation_fraction\":0.10}"
  "x_notgt_arm10_lock06|{$G,$T,\"trail_activation_fraction\":0.10,\"trail_lock_fraction\":0.6}"
  "x_notgt_arm12_lock06|{$G,$T,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.6}"
  "x_notgt_arm14|{$G,$T,\"trail_activation_fraction\":0.14}"
  "x_notgt_arm14_lock06|{$G,$T,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.6}"
  # redo: this config's first-pass run in sweep3w4_exitgrid_rerun was corrupted
  # when the extra sweep's start-reap dropped its shard DBs mid-run (merged
  # only 20/26 trades). "raise target to +40%, leave trail at default 0.6
  # (arms +24%)" -- pure raise-target-only.
  "x_tgt40_armrel|{$G,\"target_pct\":0.40}"
)

{
  echo "sweep 3w4 EXTRA (no-target TSL ladder) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index"
  echo "entry gate G (every config): {$G}   +  no fixed target ({$T})"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w4 EXTRA ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
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
      --db-suffix "s3w4x_${name}_s${i}" \
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

log "=== sweep 3w4 EXTRA complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
