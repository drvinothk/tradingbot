#!/usr/bin/env bash
# Phase 1 entry-conviction sweep for the 4 non-ORB strategies (2026-08-30).
# VM-only (dedicated backtest box, e4-16ocpu-128gb) -- not synced to any
# production OCI box, same scope discipline as every run_refined_sweep_3w*.sh
# driver before it.
#
# Runs all 4 strategies' entry-conviction config lists SEQUENTIALLY (not
# concurrent -- the 92GB orphaned-DB disk-fill incident from sweep #3 is real
# precedent, and there's no slack before this VM terminates to clean up a
# repeat), each strategy's own configs one after another, merging + saving
# each config's CSV immediately after its shards finish (never waiting for
# the whole strategy or the whole sweep) and reaping any orphaned
# `trading_bot_backtest_s4p1_*` DBs after every single config, not just at
# the end. A single config/shard failure logs and continues -- one bad
# config must not stall the rest of this unattended loop.
#
# Canonical reliable-backtest setup throughout (see BACKTEST_LEARNINGS.md's
# own opening section): --near-expiry-days 6, --exit-mode current,
# alice_index source except vwap_pullback (always futures_proxy -- the index
# feed reports zero volume) and liquidity_sweep_reversal's one
# volume-surge config (also needs futures_proxy for that gate to be
# non-inert). oi_volume_confirmed always carries
# oi_use_futures_volume_confirmation:false,oi_use_atm_oi_buildup:false (the
# single-snapshot-per-run backtest can't support the temporal confirmation
# modes).
#
# 28-way sharded (up from 18 in sweep #3) -- 32 vCPU threads on this
# 16-OCPU/128GB box, ~4 threads/~2 cores held back as buffer.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=28
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s4p1_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/s4p1_conviction_${STAMP}
STATUS_FILE=~/s4p1_status.log
LOG_DIR=/tmp/s4p1_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

# Shared gate vocabulary (JSON fragments), reused across all 4 strategies'
# config lists below -- kept as named fragments so a threshold change only
# has to happen once, not four times.
PDT='"require_prior_day_trend":true'
VIX='"vix_max":20'
ATR='"require_atr_expansion":true'
HTF='"require_htf_ema_trend":true'
PCR_TIGHT='"pcr_oi_min":0.7,"pcr_oi_max":1.3'
PCR_LOOSE='"pcr_oi_min":0.4,"pcr_oi_max":2.5'
VOLSURGE='"require_volume_surge":true'
OI_STUBS='"oi_use_futures_volume_confirmation":false,"oi_use_atm_oi_buildup":false'

# name|strategy_type|underlying_source|params-json
CONFIGS_VWAP=(
  "v_base|vwap_pullback_conviction|futures_proxy|{}"
  "v_pdt|vwap_pullback_conviction|futures_proxy|{$PDT}"
  "v_vix|vwap_pullback_conviction|futures_proxy|{$VIX}"
  "v_atr|vwap_pullback_conviction|futures_proxy|{$ATR}"
  "v_htf|vwap_pullback_conviction|futures_proxy|{$HTF}"
  "v_pcrt|vwap_pullback_conviction|futures_proxy|{$PCR_TIGHT}"
  "v_pcrl|vwap_pullback_conviction|futures_proxy|{$PCR_LOOSE}"
  "v_pdt_vix|vwap_pullback_conviction|futures_proxy|{$PDT,$VIX}"
  "v_pdt_atr|vwap_pullback_conviction|futures_proxy|{$PDT,$ATR}"
)

CONFIGS_EMA=(
  "e_base|ema_micro_pullback_conviction|alice_index|{}"
  "e_pdt|ema_micro_pullback_conviction|alice_index|{$PDT}"
  "e_vix|ema_micro_pullback_conviction|alice_index|{$VIX}"
  "e_atr|ema_micro_pullback_conviction|alice_index|{$ATR}"
  "e_pcrt|ema_micro_pullback_conviction|alice_index|{$PCR_TIGHT}"
  "e_pcrl|ema_micro_pullback_conviction|alice_index|{$PCR_LOOSE}"
  "e_pdt_vix|ema_micro_pullback_conviction|alice_index|{$PDT,$VIX}"
  "e_pdt_atr|ema_micro_pullback_conviction|alice_index|{$PDT,$ATR}"
)

CONFIGS_OI=(
  "o_base|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS}"
  "o_pdt|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$PDT}"
  "o_vix|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$VIX}"
  "o_atr|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$ATR}"
  "o_htf|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$HTF}"
  "o_pcrt|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$PCR_TIGHT}"
  "o_pcrl|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$PCR_LOOSE}"
  "o_pdt_vix|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$PDT,$VIX}"
  "o_pdt_atr|oi_volume_confirmed_conviction|alice_index|{$OI_STUBS,$PDT,$ATR}"
)

CONFIGS_LIQ=(
  "l_base|liquidity_sweep_reversal_conviction|alice_index|{}"
  "l_pdt|liquidity_sweep_reversal_conviction|alice_index|{$PDT}"
  "l_vix|liquidity_sweep_reversal_conviction|alice_index|{$VIX}"
  "l_atr|liquidity_sweep_reversal_conviction|alice_index|{$ATR}"
  "l_htf|liquidity_sweep_reversal_conviction|alice_index|{$HTF}"
  "l_pcrt|liquidity_sweep_reversal_conviction|alice_index|{$PCR_TIGHT}"
  "l_pcrl|liquidity_sweep_reversal_conviction|alice_index|{$PCR_LOOSE}"
  "l_volsurge|liquidity_sweep_reversal_conviction|futures_proxy|{$VOLSURGE}"
  "l_pdt_vix|liquidity_sweep_reversal_conviction|alice_index|{$PDT,$VIX}"
  "l_pdt_atr|liquidity_sweep_reversal_conviction|alice_index|{$PDT,$ATR}"
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
        --db-suffix "s4p1_${name}_s${i}" \
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
    n=$(($(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1))
    if [ "$fail" -eq 1 ]; then
      log "*** $name: a shard FAILED (${el}s, ${n} trades, $(df -h / | awk 'NR==2{print $4}') free) ***"
    else
      log "$name OK (${el}s, ${n} trades, $(df -h / | awk 'NR==2{print $4}') free)"
    fi
  done

  log "=== $label done: $(( ($(date +%s) - strat_start) / 60 ))min ==="
}

log "=========================================="
log "S4P1 CONVICTION SWEEP STARTED -- ${STAMP}"
log "order: liquidity_sweep_reversal -> oi_volume_confirmed -> vwap_pullback -> ema_micro_pullback"
log "=========================================="
log "reaping any stale ${DB_PREFIX}* DBs before start"; reap_dbs
overall_start=$(date +%s)

run_strategy_configs "liquidity_sweep_reversal" CONFIGS_LIQ
run_strategy_configs "oi_volume_confirmed" CONFIGS_OI
run_strategy_configs "vwap_pullback" CONFIGS_VWAP
run_strategy_configs "ema_micro_pullback" CONFIGS_EMA

log "=========================================="
log "S4P1 CONVICTION SWEEP COMPLETE -- $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR}"
log "=========================================="
