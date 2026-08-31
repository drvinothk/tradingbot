#!/usr/bin/env bash
# Round 3 (2026-08-30): targeted gate-combo refinement for the two
# strategies whose Phase 1/1.5 champions showed real (if thin) promise
# after real-cost robustness checking (oi_volume_confirmed's o_pdt_atr,
# ema_micro_pullback's e_pdt_atr) -- vwap_pullback and liquidity_sweep_
# reversal are deliberately excluded this round: their best configs (v_pdt,
# l_pcrt) were net LOSERS after real transaction costs (PF 0.82 / 0.74),
# with a negative IS or negative half each -- more gate-stacking on a
# structurally negative entry doesn't manufacture an edge (the same lesson
# ORB's own W4 already established), so this round doesn't burn VM time
# chasing that.
#
# Only untested PAIRWISE/TRIPLE combos of Phase 1's own already-validated
# shared gates (atr, pcr_tight, pdt) -- no new code, nothing to unit-test,
# every one of these gates individually already proven correct in Phase 1.
# Goal: find a combo with a better IS half and/or more OOS trades than
# o_pdt_atr/e_pdt_atr's current thin (7/6-trade) OOS windows, before
# deciding what (if anything) goes to Phase 2 exit-tuning.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=28
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s4p16_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/s4p16_round3_${STAMP}
STATUS_FILE=~/s4p16_status.log
LOG_DIR=/tmp/s4p16_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

PDT='"require_prior_day_trend":true'
ATR='"require_atr_expansion":true'
PCR_TIGHT='"pcr_oi_min":0.7,"pcr_oi_max":1.3'
PCR_LOOSE='"pcr_oi_min":0.4,"pcr_oi_max":2.5'
OI_STUBS='"oi_use_futures_volume_confirmation":false,"oi_use_atm_oi_buildup":false'

# name|strategy_type|underlying_source|strategy_params_json
CONFIGS_OI=(
  "o3_atr_pcrt|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},${ATR},${PCR_TIGHT}}"
  "o3_atr_pcrl|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},${ATR},${PCR_LOOSE}}"
  "o3_pdt_pcrt|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},${PDT},${PCR_TIGHT}}"
  "o3_pdt_atr_pcrt|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},${PDT},${ATR},${PCR_TIGHT}}"
)

CONFIGS_EMA=(
  "e3_atr_pcrt|ema_micro_pullback_conviction|alice_index|{${ATR},${PCR_TIGHT}}"
  "e3_atr_pcrl|ema_micro_pullback_conviction|alice_index|{${ATR},${PCR_LOOSE}}"
  "e3_pdt_pcrt|ema_micro_pullback_conviction|alice_index|{${PDT},${PCR_TIGHT}}"
  "e3_pdt_atr_pcrt|ema_micro_pullback_conviction|alice_index|{${PDT},${ATR},${PCR_TIGHT}}"
)

run_strategy_configs() {
  local label="$1"
  shift
  local -n configs_ref=$1
  log "=== $label: ${#configs_ref[@]} configs x ${SHARD_COUNT} shards ==="
  local strat_start
  strat_start=$(date +%s)

  for entry in "${configs_ref[@]}"; do
    IFS='|' read -r name strategy_type source params <<< "$entry"
    local cfg_start
    cfg_start=$(date +%s)
    log "--- $name ($strategy_type, src=$source) params=$params"

    local pids=()
    for i in $(seq 0 $((SHARD_COUNT - 1))); do
      "$PY" scripts/run_backtest.py \
        --strategy "$strategy_type" --underlying NIFTY \
        --all-expiries --options-subdir options_1min_past \
        --underlying-source "$source" \
        --exit-mode current --fast --near-expiry-days "$NEAR_EXPIRY_DAYS" \
        --strategy-params "$params" \
        --shard-count "$SHARD_COUNT" --shard-index "$i" \
        --db-suffix "s4p16_${name}_s${i}" \
        --out-csv "${RESULTS_DIR}/${name}_s${i}.csv" \
        > "${LOG_DIR}/${name}_s${i}.log" 2>&1 &
      pids+=($!)
    done

    local fail=0
    for pid in "${pids[@]}"; do wait "$pid" || fail=1; done

    "$PY" scripts/merge_backtest_shards.py \
      --glob "${RESULTS_DIR}/${name}_s*.csv" \
      --out "${RESULTS_DIR}/${name}_current.csv" \
      >> "${LOG_DIR}/${name}_merge.log" 2>&1 || log "*** $name merge FAILED"

    reap_dbs

    local el n
    el=$(( $(date +%s) - cfg_start ))
    n=$(( $(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1 ))
    local free
    free=$(df -h --output=avail / | tail -1 | tr -d ' ')
    log "$name $([ $fail -eq 0 ] && echo OK || echo 'HAD SHARD FAILURES') (${el}s, ${n} trades, ${free} free)"
  done

  log "=== $label done: $(( ($(date +%s) - strat_start) / 60 ))min ==="
}

log "=========================================="
log "Round 3 refinement sweep starting -> $RESULTS_DIR"
log "=========================================="

run_strategy_configs "oi_volume_confirmed" CONFIGS_OI
run_strategy_configs "ema_micro_pullback" CONFIGS_EMA

log "=========================================="
log "S4P16 ROUND 3 SWEEP COMPLETE -> $RESULTS_DIR"
log "=========================================="
