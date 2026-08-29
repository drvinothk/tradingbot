#!/usr/bin/env bash
# Sweep #3, Workstream 3 (2026-08-29) -- BANKNIFTY ORB dip-test, current
# expiry week.
#
# BANKNIFTY = monthly-only since SEBI's 2024 change; the options archive is
# 12 monthly-expiry dirs, each ~11 trading days (last 2 weeks), 22
# contracts/expiry. "Current expiry week" = --near-expiry-days 6 = the last
# week before each monthly. Expect ~5-12 trades/config -- PROBE, to see if
# ORB's weak-entry problem is NIFTY-specific and to size a real BANKNIFTY
# batch, not to make strong claims.
#
# max_or_range_banknifty_points default is 250; max_loss_per_lot scaled to
# BANKNIFTY premium (~2.5-3x NIFTY, lot 35) at 6000. 12-way sharded (only 12
# expiries).
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
SHARD_COUNT=12
NEAR_EXPIRY_DAYS=6
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w3_banknifty_${STAMP}
STATUS_FILE=/tmp/sweep3w3_status.log
LOG_DIR=/tmp/sweep3w3_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

# name|params-json  (strategy=orb_conviction, underlying=BANKNIFTY, src=alice_index)
CONFIGS=(
  "bn_baseline|{}"
  "bn_w90|{\"max_or_range_banknifty_points\":90}"
  "bn_w140|{\"max_or_range_banknifty_points\":140}"
  "bn_w200|{\"max_or_range_banknifty_points\":200}"
  "bn_w280|{\"max_or_range_banknifty_points\":280}"
  "bn_ce_only|{\"ce_only\":true}"
  "bn_cut1000|{\"orb_entry_cutoff_time\":\"10:00\"}"
  "bn_maxloss6k|{\"max_loss_per_lot\":6000}"
  "bn_htf|{\"require_htf_ema_trend\":true}"
  "bn_pdt|{\"require_prior_day_trend\":true}"
  "bn_pdt_w140|{\"require_prior_day_trend\":true,\"max_or_range_banknifty_points\":140}"
)

{
  echo "sweep 3w3 (BANKNIFTY ORB dip-test) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  underlying=BANKNIFTY  src=alice_index"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w3 ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="

for entry in "${CONFIGS[@]}"; do
  IFS='|' read -r name params <<< "$entry"
  cfg_start=$(date +%s)
  log "--- $name  params=$params"
  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py \
      --strategy orb_conviction --underlying BANKNIFTY \
      --all-expiries --options-subdir options_1min_past --underlying-source alice_index \
      --exit-mode current --fast --near-expiry-days "$NEAR_EXPIRY_DAYS" \
      --strategy-params "$params" \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "s3w3_${name}_s${i}" \
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

log "=== sweep 3w3 complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
