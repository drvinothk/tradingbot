#!/usr/bin/env bash
# One-off overnight driver: runs all 5 strategies sequentially, each fully
# 10-way sharded across the full 52-expiry NIFTY archive, all 6 exit modes
# in one pass. Deliberately NOT `set -e` -- a single shard/strategy failure
# must not kill the rest of the overnight run; failures are logged to
# STATUS_FILE instead so they're visible without stopping progress.
set -uo pipefail

cd ~/trading-bot/backend

SHARD_COUNT=10
RESULTS_DIR=data/historical/backtest_reports
STATUS_FILE=~/trading-bot/backtest_status.log
LOG_DIR=/tmp/backtest_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"

STRATEGIES="orb vwap_pullback ema_micro_pullback oi_volume_confirmed liquidity_sweep_reversal"
MODES="legacy near_only far_only no_target_only split_30_30_40 target_mult"

declare -A SOURCE
SOURCE[orb]=alice_index
SOURCE[vwap_pullback]=futures_proxy
SOURCE[ema_micro_pullback]=alice_index
SOURCE[oi_volume_confirmed]=alice_index
SOURCE[liquidity_sweep_reversal]=alice_index

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S IST')] $*" | tee -a "$STATUS_FILE"
}

log "=========================================="
log "FULL 5-STRATEGY BACKTEST RUN STARTED"
log "shards=$SHARD_COUNT strategies=[$STRATEGIES]"
log "=========================================="

overall_start=$(date +%s)

for strat in $STRATEGIES; do
  src=${SOURCE[$strat]}
  strat_start=$(date +%s)
  log "--- $strat: starting ($SHARD_COUNT shards, source=$src) ---"

  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    ./.venv/bin/python scripts/run_backtest.py \
      --strategy "$strat" --underlying NIFTY \
      --all-expiries --options-subdir options_1min_past \
      --underlying-source "$src" \
      --exit-mode all --total-lots 10 --fast \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "${strat}_NIFTY_shard${i}" \
      --out-csv "${RESULTS_DIR}/${strat}_NIFTY_trades_shard${i}.csv" \
      > "${LOG_DIR}/${strat}_shard${i}.log" 2>&1 &
    pids+=($!)
  done

  fail=0
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      fail=1
    fi
  done

  strat_elapsed=$(( $(date +%s) - strat_start ))
  if [ "$fail" -eq 1 ]; then
    log "*** $strat: one or more shards FAILED (${strat_elapsed}s elapsed) -- check ${LOG_DIR}/${strat}_shard*.log ***"
  else
    log "$strat: all $SHARD_COUNT shards finished OK (${strat_elapsed}s elapsed)"
  fi

  merge_fail=0
  for mode in $MODES; do
    if ! ./.venv/bin/python scripts/merge_backtest_shards.py \
      --glob "${RESULTS_DIR}/${strat}_NIFTY_trades_shard*_${mode}.csv" \
      --out "${RESULTS_DIR}/${strat}_NIFTY_trades_${mode}.csv" \
      >> "${LOG_DIR}/${strat}_merge.log" 2>&1; then
      merge_fail=1
      log "*** $strat: merge FAILED for mode=$mode -- check ${LOG_DIR}/${strat}_merge.log ***"
    fi
  done
  if [ "$merge_fail" -eq 0 ]; then
    log "$strat: all 6 modes merged OK"
  fi

  if ./.venv/bin/python scripts/summarize_exit_modes.py \
    --strategy "$strat" --underlying NIFTY --total-lots 10 \
    --data-dir "$RESULTS_DIR" > "${RESULTS_DIR}/${strat}_NIFTY_summary.txt" 2>&1; then
    log "$strat: summary written -> ${RESULTS_DIR}/${strat}_NIFTY_summary.txt"
  else
    log "*** $strat: summarize_exit_modes.py FAILED -- check ${RESULTS_DIR}/${strat}_NIFTY_summary.txt ***"
  fi

  log "--- $strat: DONE ---"
done

overall_elapsed=$(( $(date +%s) - overall_start ))
log "=========================================="
log "FULL RUN COMPLETE -- total elapsed ${overall_elapsed}s ($((overall_elapsed / 60))min)"
log "=========================================="
