#!/usr/bin/env bash
# Refined conviction sweep #2 (2026-08-28). NIFTY, full 52-expiry 1-min
# options archive, --exit-mode current, ALL days / full market hours (any
# day/hour slicing happens only in evaluation, never in the run). Each
# config 10-way sharded. Harness fixes applied first: VIX seeded as real
# QuoteTicks, PCR floored (garbage -> None), max_loss_per_lot / time_stop
# as TradeProposal fields consumed by the exit reconstruction.
#
# Results + this config list are timestamped under RESULTS_DIR so learnings
# can be tied back to exactly what was run.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
SHARD_COUNT=10
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/refined_sweep_${STAMP}
STATUS_FILE=/tmp/refined_sweep_status.log
LOG_DIR=/tmp/refined_sweep_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

# name|strategy|underlying-source|params-json
CONFIGS=(
  # --- references
  "ref_orb_baseline|orb|alice_index|{}"
  "ref_orbc_none|orb_conviction|alice_index|{}"
  # --- A. structure-break fidelity A/B
  "sb_persist30|orb_conviction|alice_index|{\"structure_break_persistence_seconds\":30}"
  "sb_persist60|orb_conviction|alice_index|{\"structure_break_persistence_seconds\":60}"
  "sb_persist120|orb_conviction|alice_index|{\"structure_break_persistence_seconds\":120}"
  "sb_persist600|orb_conviction|alice_index|{\"structure_break_persistence_seconds\":600}"
  "sb_buffer_05|orb_conviction|alice_index|{\"structure_break_atr_multiplier\":0.5}"
  "sb_buffer_10|orb_conviction|alice_index|{\"structure_break_atr_multiplier\":1.0}"
  # --- B. findings-driven single gates
  "g_ce_only|orb_conviction|alice_index|{\"ce_only\":true}"
  "g_cutoff_0945|orb_conviction|alice_index|{\"orb_entry_cutoff_time\":\"09:45\"}"
  "g_cutoff_1000|orb_conviction|alice_index|{\"orb_entry_cutoff_time\":\"10:00\"}"
  "g_skip_tue|orb_conviction|alice_index|{\"skip_weekdays\":[\"Tuesday\"]}"
  "g_strength_03|orb_conviction|alice_index|{\"min_breakout_strength_atr\":0.3}"
  "g_strength_06|orb_conviction|alice_index|{\"min_breakout_strength_atr\":0.6}"
  "g_drift_align|orb_conviction|alice_index|{\"require_drift_alignment\":true}"
  # --- C. risk overlays alone
  "r_maxloss_2000|orb_conviction|alice_index|{\"max_loss_per_lot\":2000}"
  "r_maxloss_2500|orb_conviction|alice_index|{\"max_loss_per_lot\":2500}"
  "r_maxloss_3000|orb_conviction|alice_index|{\"max_loss_per_lot\":3000}"
  "r_tstop_60|orb_conviction|alice_index|{\"time_stop_minutes\":60}"
  "r_tstop_90|orb_conviction|alice_index|{\"time_stop_minutes\":90}"
  "r_tstop_120|orb_conviction|alice_index|{\"time_stop_minutes\":120}"
  "r_maxloss2500_tstop90|orb_conviction|alice_index|{\"max_loss_per_lot\":2500,\"time_stop_minutes\":90}"
  # --- D. VIX (now functional)
  "v_vix_max_16|orb_conviction|alice_index|{\"vix_max\":16}"
  "v_vix_max_18|orb_conviction|alice_index|{\"vix_max\":18}"
  "v_vix_max_22|orb_conviction|alice_index|{\"vix_max\":22}"
  # --- E. HTF / ATR gates re-test
  "e_htf|orb_conviction|alice_index|{\"require_htf_ema_trend\":true}"
  "e_atr_exp|orb_conviction|alice_index|{\"require_atr_expansion\":true,\"atr_expansion_min_ratio\":1.1}"
  # --- F. target / trail / stop tuning
  "f_trail_030|orb_conviction|alice_index|{\"trail_activation_fraction\":0.3}"
  "f_trail_045|orb_conviction|alice_index|{\"trail_activation_fraction\":0.45}"
  "f_target_15|orb_conviction|alice_index|{\"target_pct\":0.15}"
  "f_target_30|orb_conviction|alice_index|{\"target_pct\":0.30}"
  "f_stop_10|orb_conviction|alice_index|{\"stop_pct\":0.10}"
  "f_range_tight|orb_conviction|alice_index|{\"min_or_range_nifty_points\":25,\"max_or_range_nifty_points\":65}"
  "f_range_wide|orb_conviction|alice_index|{\"max_or_range_nifty_points\":120}"
  # --- G. 2-way combos
  "c_ce_cutoff|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\"}"
  "c_ce_skiptue|orb_conviction|alice_index|{\"ce_only\":true,\"skip_weekdays\":[\"Tuesday\"]}"
  "c_strength_drift|orb_conviction|alice_index|{\"min_breakout_strength_atr\":0.3,\"require_drift_alignment\":true}"
  # --- H. stacked hypotheses
  "s_A_ce_cut_skip|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"]}"
  "s_B_A_maxloss|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500}"
  "s_C_B_tstop|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90}"
  "s_D_C_sb120|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90,\"structure_break_persistence_seconds\":120}"
  "s_E_C_strength|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90,\"min_breakout_strength_atr\":0.3}"
  "s_F_C_htf|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90,\"require_htf_ema_trend\":true}"
  "s_G_C_vix18|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90,\"vix_max\":18}"
  "s_H_C_trail030|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90,\"trail_activation_fraction\":0.3}"
  "s_I_everything|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90,\"structure_break_persistence_seconds\":120,\"min_breakout_strength_atr\":0.3,\"require_htf_ema_trend\":true,\"trail_activation_fraction\":0.3}"
  "s_J_C_sb120_target30|orb_conviction|alice_index|{\"ce_only\":true,\"orb_entry_cutoff_time\":\"10:00\",\"skip_weekdays\":[\"Tuesday\"],\"max_loss_per_lot\":2500,\"time_stop_minutes\":90,\"structure_break_persistence_seconds\":120,\"target_pct\":0.30}"
)

{
  echo "refined sweep ${STAMP}"
  echo "shards=${SHARD_COUNT}  strategy_source=alice_index  exit_mode=current  all-expiries all-days"
  echo "harness: VIX seeded, PCR floored (${PCR_MIN_SIDE_OI:-see run_backtest.py}), max_loss_per_lot+time_stop via TradeProposal"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== refined sweep ${STAMP} started: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="

for entry in "${CONFIGS[@]}"; do
  IFS='|' read -r name strat src params <<< "$entry"
  cfg_start=$(date +%s)
  log "--- $name ($strat, src=$src) params=$params"
  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py \
      --strategy "$strat" --underlying NIFTY \
      --all-expiries --options-subdir options_1min_past --underlying-source "$src" \
      --exit-mode current --fast --strategy-params "$params" \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "rs_${name}_s${i}" \
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
  [ "$fail" -eq 1 ] && log "*** $name: a shard FAILED (${el}s)" || log "$name OK (${el}s)"
done

log "=== sweep complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
