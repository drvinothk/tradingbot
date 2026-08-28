#!/usr/bin/env bash
# One-off local driver: backtests the conviction-strategy candidate matrix
# (orb baseline, orb_conviction gate variants, atr_breakout variants) over
# the full 52-expiry NIFTY 1-min options archive, --exit-mode current only,
# each config 10-way sharded across local cores. Results land as one merged
# per-config trades CSV under RESULTS_DIR/conviction_sweep/. Analyse with
# scripts/analyze_conviction_sweep.py afterwards.
#
# Deliberately NOT `set -e` -- one shard/config failure must not kill the
# rest; failures are logged to STATUS_FILE.
set -uo pipefail

cd "$(dirname "$0")/.."   # backend/

PY=./.venv/Scripts/python
SHARD_COUNT=10
RESULTS_DIR=data/historical/backtest_reports/conviction_sweep
STATUS_FILE=/tmp/conviction_sweep_status.log
LOG_DIR=/tmp/conviction_sweep_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$STATUS_FILE"; }

# name|strategy|underlying-source|strategy-params-json
CONFIGS=(
  "orb_baseline|orb|alice_index|{}"
  "orbc_nogates|orb_conviction|alice_index|{}"
  "orbc_htf|orb_conviction|alice_index|{\"require_htf_ema_trend\":true}"
  "orbc_atr|orb_conviction|alice_index|{\"require_atr_expansion\":true}"
  "orbc_vix20|orb_conviction|alice_index|{\"vix_max\":20}"
  "orbc_htf_atr|orb_conviction|alice_index|{\"require_htf_ema_trend\":true,\"require_atr_expansion\":true}"
  "orbc_htf_atr_r2|orb_conviction|alice_index|{\"require_htf_ema_trend\":true,\"require_atr_expansion\":true,\"target_r_multiple\":2.0}"
  "orbc_wide_nogates|orb_conviction|alice_index|{\"max_or_range_nifty_points\":130,\"orb_entry_cutoff_time\":\"11:00\"}"
  "orbc_wide_htf_atr|orb_conviction|alice_index|{\"max_or_range_nifty_points\":130,\"orb_entry_cutoff_time\":\"11:00\",\"require_htf_ema_trend\":true,\"require_atr_expansion\":true}"
  "atrb_baseline|atr_breakout|alice_index|{}"
  "atrb_r25|atr_breakout|alice_index|{\"target_r_multiple\":2.5}"
  "atrb_lb40|atr_breakout|alice_index|{\"breakout_lookback_bars\":40}"
  # Volume-surge gate can only be exercised on a real-volume source; the
  # futures proxy only has real volume ~1 week/month (near each monthly
  # expiry) -- treat this run as indicative, not comparable to the above.
  "orbc_htf_atr_vol_fut|orb_conviction|futures_proxy|{\"require_htf_ema_trend\":true,\"require_atr_expansion\":true,\"require_volume_surge\":true}"
)

overall_start=$(date +%s)
log "=== conviction sweep started: ${#CONFIGS[@]} configs, ${SHARD_COUNT} shards each ==="

for entry in "${CONFIGS[@]}"; do
  IFS='|' read -r name strat src params <<< "$entry"
  cfg_start=$(date +%s)
  log "--- $name ($strat, source=$src, params=$params) ---"

  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py \
      --strategy "$strat" --underlying NIFTY \
      --all-expiries --options-subdir options_1min_past \
      --underlying-source "$src" \
      --exit-mode current --fast \
      --strategy-params "$params" \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "cs_${name}_shard${i}" \
      --out-csv "${RESULTS_DIR}/${name}_shard${i}.csv" \
      > "${LOG_DIR}/${name}_shard${i}.log" 2>&1 &
    pids+=($!)
  done

  fail=0
  for pid in "${pids[@]}"; do wait "$pid" || fail=1; done

  # Single-mode runs write --out-csv verbatim (no _<mode> suffix), so the
  # shard files are <name>_shard<N>.csv, not <name>_shard<N>_current.csv.
  if ! "$PY" scripts/merge_backtest_shards.py \
      --glob "${RESULTS_DIR}/${name}_shard*.csv" \
      --out "${RESULTS_DIR}/${name}_current.csv" \
      >> "${LOG_DIR}/${name}_merge.log" 2>&1; then
    log "*** $name: merge FAILED -- see ${LOG_DIR}/${name}_merge.log ***"
  fi

  cfg_elapsed=$(( $(date +%s) - cfg_start ))
  if [ "$fail" -eq 1 ]; then
    log "*** $name: one or more shards FAILED (${cfg_elapsed}s) -- see ${LOG_DIR}/${name}_shard*.log ***"
  else
    log "$name: OK (${cfg_elapsed}s), merged -> ${RESULTS_DIR}/${name}_current.csv"
  fi
done

overall_elapsed=$(( $(date +%s) - overall_start ))
log "=== sweep complete -- ${overall_elapsed}s ($((overall_elapsed / 60))min) ==="
