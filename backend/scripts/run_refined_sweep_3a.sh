#!/usr/bin/env bash
# Sweep #3, Batch 3a (2026-08-28) -- opening-range-WIDTH ridge, NIFTY.
#
# Anchored on the Part-0 finding: sweep #1/#2's "f_range_tight edge" was a
# 10-14-DTE artifact + day double-counting. This batch re-tests the width
# axis on the CURRENT EXPIRY WEEK only (--near-expiry-days 6 = NIFTY's
# Wed->Tue weekly cycle), which also collapses the multi-expiry-directory
# overlap so each calendar day is traded once against the near-week
# contract a live run would use.
#
# Vary ONLY min/max_or_range_nifty_points; default everything else. Dense
# grid to reveal whether positive expectancy forms a contiguous ridge
# across neighbouring bands (real) or a lone spike (noise). Its result
# picks the 1-3 bands that anchor Batch 3b's broad grid.
#
# ALL days / full market hours in the run; day/hour/DTE slicing happens
# only in evaluation. Results timestamped under RESULTS_DIR.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3a_widthridge_${STAMP}
STATUS_FILE=/tmp/sweep3a_status.log
LOG_DIR=/tmp/sweep3a_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

# name|params-json   (strategy is always orb_conviction, src always alice_index)
CONFIGS=(
  "ref_orb_baseline|{}"
  "ref_w2080|{\"min_or_range_nifty_points\":20,\"max_or_range_nifty_points\":80}"
  # min 15
  "w_15_55|{\"min_or_range_nifty_points\":15,\"max_or_range_nifty_points\":55}"
  "w_15_65|{\"min_or_range_nifty_points\":15,\"max_or_range_nifty_points\":65}"
  "w_15_75|{\"min_or_range_nifty_points\":15,\"max_or_range_nifty_points\":75}"
  # min 20
  "w_20_45|{\"min_or_range_nifty_points\":20,\"max_or_range_nifty_points\":45}"
  "w_20_55|{\"min_or_range_nifty_points\":20,\"max_or_range_nifty_points\":55}"
  "w_20_65|{\"min_or_range_nifty_points\":20,\"max_or_range_nifty_points\":65}"
  "w_20_75|{\"min_or_range_nifty_points\":20,\"max_or_range_nifty_points\":75}"
  "w_20_85|{\"min_or_range_nifty_points\":20,\"max_or_range_nifty_points\":85}"
  "w_20_100|{\"min_or_range_nifty_points\":20,\"max_or_range_nifty_points\":100}"
  # min 25
  "w_25_45|{\"min_or_range_nifty_points\":25,\"max_or_range_nifty_points\":45}"
  "w_25_55|{\"min_or_range_nifty_points\":25,\"max_or_range_nifty_points\":55}"
  "w_25_65|{\"min_or_range_nifty_points\":25,\"max_or_range_nifty_points\":65}"
  "w_25_75|{\"min_or_range_nifty_points\":25,\"max_or_range_nifty_points\":75}"
  "w_25_85|{\"min_or_range_nifty_points\":25,\"max_or_range_nifty_points\":85}"
  # min 30
  "w_30_65|{\"min_or_range_nifty_points\":30,\"max_or_range_nifty_points\":65}"
  "w_30_75|{\"min_or_range_nifty_points\":30,\"max_or_range_nifty_points\":75}"
  "w_30_90|{\"min_or_range_nifty_points\":30,\"max_or_range_nifty_points\":90}"
  # min 35
  "w_35_75|{\"min_or_range_nifty_points\":35,\"max_or_range_nifty_points\":75}"
  "w_35_90|{\"min_or_range_nifty_points\":35,\"max_or_range_nifty_points\":90}"
)

{
  echo "sweep 3a (width ridge) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction"
  echo "src=alice_index  exit_mode=current  --all-expiries (current expiry week only)"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3a ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="

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
      --db-suffix "s3a_${name}_s${i}" \
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

log "=== sweep 3a complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
