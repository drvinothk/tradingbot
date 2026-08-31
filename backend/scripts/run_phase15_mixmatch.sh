#!/usr/bin/env bash
# Phase 1.5 mix-and-match sweep (2026-08-30): each strategy's best Phase 1
# conviction-gate config stacked against a brand-new, research-grounded
# NATIVE param (not a shared gate) added this same session -- see each
# strategy's own *_conviction.py module docstring for the research behind
# its new param (min_bars_since_open / min_displacement_atr /
# require_oi_price_alignment / min_ema_spread_atr_ratio). All 4 new params
# smoke-tested individually against real VM data before this launch
# (confirmed: off = byte-identical, on = demonstrably blocks/shifts a known
# trade) -- see this session's own smoke-test transcript.
#
# Same sequential-strategy, merge-then-reap-per-config, 28-way-sharded shape
# as run_conviction_sweep_all4.sh (Phase 1) -- reusing its own conventions
# verbatim, not reinventing them.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=28
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s4p15_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/s4p15_mixmatch_${STAMP}
STATUS_FILE=~/s4p15_status.log
LOG_DIR=/tmp/s4p15_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

# Shared gate vocabulary, reused from Phase 1.
PDT='"require_prior_day_trend":true'
ATR='"require_atr_expansion":true'
PCR_TIGHT='"pcr_oi_min":0.7,"pcr_oi_max":1.3'
OI_STUBS='"oi_use_futures_volume_confirmation":false,"oi_use_atm_oi_buildup":false'

# name|strategy_type|underlying_source|strategy_params_json
CONFIGS_LIQ=(
  "l15_disp05|liquidity_sweep_reversal_conviction|alice_index|{\"min_displacement_atr\":0.5}"
  "l15_disp10|liquidity_sweep_reversal_conviction|alice_index|{\"min_displacement_atr\":1.0}"
  "l15_pcrt_disp05|liquidity_sweep_reversal_conviction|alice_index|{${PCR_TIGHT},\"min_displacement_atr\":0.5}"
  "l15_pcrt_disp10|liquidity_sweep_reversal_conviction|alice_index|{${PCR_TIGHT},\"min_displacement_atr\":1.0}"
)

CONFIGS_OI=(
  "o15_align5|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},\"require_oi_price_alignment\":true}"
  "o15_align10|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},\"require_oi_price_alignment\":true,\"oi_alignment_lookback_bars\":10}"
  "o15_atr_align|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},${ATR},\"require_oi_price_alignment\":true}"
  "o15_pdt_atr_align|oi_volume_confirmed_conviction|alice_index|{${OI_STUBS},${PDT},${ATR},\"require_oi_price_alignment\":true}"
)

CONFIGS_VWAP=(
  "v15_open20|vwap_pullback_conviction|futures_proxy|{\"min_bars_since_open\":20}"
  "v15_open50|vwap_pullback_conviction|futures_proxy|{\"min_bars_since_open\":50}"
  "v15_pdt_open20|vwap_pullback_conviction|futures_proxy|{${PDT},\"min_bars_since_open\":20}"
  "v15_pdt_open50|vwap_pullback_conviction|futures_proxy|{${PDT},\"min_bars_since_open\":50}"
)

CONFIGS_EMA=(
  "e15_spread05|ema_micro_pullback_conviction|alice_index|{\"min_ema_spread_atr_ratio\":0.5}"
  "e15_spread10|ema_micro_pullback_conviction|alice_index|{\"min_ema_spread_atr_ratio\":1.0}"
  "e15_atr_spread05|ema_micro_pullback_conviction|alice_index|{${ATR},\"min_ema_spread_atr_ratio\":0.5}"
  "e15_pdt_atr_spread05|ema_micro_pullback_conviction|alice_index|{${PDT},${ATR},\"min_ema_spread_atr_ratio\":0.5}"
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
        --db-suffix "s4p15_${name}_s${i}" \
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
log "Phase 1.5 mix-and-match sweep starting -> $RESULTS_DIR"
log "=========================================="

run_strategy_configs "liquidity_sweep_reversal" CONFIGS_LIQ
run_strategy_configs "oi_volume_confirmed" CONFIGS_OI
run_strategy_configs "vwap_pullback" CONFIGS_VWAP
run_strategy_configs "ema_micro_pullback" CONFIGS_EMA

log "=========================================="
log "S4P15 MIX-AND-MATCH SWEEP COMPLETE -> $RESULTS_DIR"
log "=========================================="
