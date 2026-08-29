#!/usr/bin/env bash
# Sweep #3 W7b -- trail_lock_fraction ladder on d_pdt_w65 (2026-08-29). NO code change.
#
# W4+W7 tested lock 0.4 and 0.6 only; 0.4 -> 0.6 was a consistent free gain
# (~+15..25/lot, PF up, P(mean<=0) down) in all 4 sweeps. This fills 0.7 / 0.8
# / 0.9 at the W7 lead configs (all stop_pct 0.18) to see if the trend
# continues or over-locking clips winners. Same 26 d_pdt_w65 entries -> pure
# exit-overlay re-slice.  Isolated DB prefix s3w7t2_.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s3w7t2_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w7tsl2_${STAMP}
STATUS_FILE=/tmp/sweep3w7tsl2_status.log
LOG_DIR=/tmp/sweep3w7tsl2_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

G='"require_prior_day_trend":true,"max_or_range_nifty_points":65'
T='"target_pct":1.0'

# name|params-json  (strategy=orb_conviction, src=alice_index, stop_pct 0.18 throughout)
CONFIGS=(
  # no-target, arm +12% (balanced lead) -- lock ladder
  "w7b_s18_a12_l07|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.7}"
  "w7b_s18_a12_l08|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.8}"
  "w7b_s18_a12_l09|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.12,\"trail_lock_fraction\":0.9}"
  # no-target, arm +14% (max-E no-target) -- lock ladder
  "w7b_s18_a14_l07|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.7}"
  "w7b_s18_a14_l08|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.8}"
  "w7b_s18_a14_l09|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.14,\"trail_lock_fraction\":0.9}"
  # no-target, arm +6% (max-win) -- does more lock still help at a tight arm?
  "w7b_s18_a06_l07|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.7}"
  "w7b_s18_a06_l08|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.06,\"trail_lock_fraction\":0.8}"
  # no-target, arm +10%
  "w7b_s18_a10_l07|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.10,\"trail_lock_fraction\":0.7}"
  "w7b_s18_a10_l08|{$G,$T,\"stop_pct\":0.18,\"trail_activation_fraction\":0.10,\"trail_lock_fraction\":0.8}"
  # stop18 + target30 (default trail arm) -- lock 0.6 & 0.8 (its own default is 0.4, already have)
  "w7b_s18_tgt30_l06|{$G,\"stop_pct\":0.18,\"target_pct\":0.30,\"trail_lock_fraction\":0.6}"
  "w7b_s18_tgt30_l08|{$G,\"stop_pct\":0.18,\"target_pct\":0.30,\"trail_lock_fraction\":0.8}"
)

{
  echo "sweep 3w7b (trail_lock_fraction ladder) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index  stop_pct=0.18"
  echo "have from W7: lock 0.4 & 0.6 at arm 6/8/10/12/14; this adds 0.7/0.8/0.9"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w7b ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
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
      --db-suffix "s3w7t2_${name}_s${i}" \
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

log "=== sweep 3w7b complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
