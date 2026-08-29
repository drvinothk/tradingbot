#!/usr/bin/env bash
# Sweep #3 W6 -- chart-based structure stop on d_pdt_w65 (2026-08-29).
#
# Uses the new run_backtest.py --structure-stop-mode {or_boundary,swing,
# pivot_s1r1,pivot_s2r2} + --swing-lookback flags (W6 harness change): anchor
# the --exit-mode current structure-break exit to a recent swing candle
# low/high or a classic floor pivot S1/R1/S2/R2, instead of orb_conviction's
# hard-wired opening-range boundary. or_boundary = byte-identical to today
# (smoke-verified against x_baseline_s0.csv).
#
# Base gate G (every config): the d_pdt_w65 entry gate + stop_pct 0.15 (the
# W4 best premium stop, kept as a disaster backstop). G_noStop swaps in
# stop_pct 0.9 (-90% ~ "no premium stop"; NOT 2.0 -> negative price) so the
# chart level + trail + EOD do all the work.
#
# Every config = the SAME 26 d_pdt_w65 entries -> pure exit-overlay re-slice.
# Isolated DB prefix s3w6_ (distinct from s3w7t_ / s3w7_) so it runs alongside
# the W7 sweeps. CONFIGS rows are 3-field: name|extraflags|params-json.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
PSQL="sudo -u postgres psql"
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
DB_PREFIX="trading_bot_backtest_s3w6_"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w6_structstop_${STAMP}
STATUS_FILE=/tmp/sweep3w6_status.log
LOG_DIR=/tmp/sweep3w6_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

G='"require_prior_day_trend":true,"max_or_range_nifty_points":65,"stop_pct":0.15'
G_noStop='"require_prior_day_trend":true,"max_or_range_nifty_points":65,"stop_pct":0.9'

# name|extraflags|params-json  (strategy=orb_conviction, src=alice_index)
CONFIGS=(
  "s_or_baseline|--structure-stop-mode or_boundary|{$G}"
  "s_swing05|--structure-stop-mode swing --swing-lookback 5|{$G}"
  "s_swing10|--structure-stop-mode swing --swing-lookback 10|{$G}"
  "s_swing15|--structure-stop-mode swing --swing-lookback 15|{$G}"
  "s_swing20|--structure-stop-mode swing --swing-lookback 20|{$G}"
  "s_swing30|--structure-stop-mode swing --swing-lookback 30|{$G}"
  "s_piv_s1r1|--structure-stop-mode pivot_s1r1|{$G}"
  "s_piv_s2r2|--structure-stop-mode pivot_s2r2|{$G}"
  "s_swing10_buf0|--structure-stop-mode swing --swing-lookback 10|{$G,\"structure_break_atr_multiplier\":0}"
  "s_swing10_buf10|--structure-stop-mode swing --swing-lookback 10|{$G,\"structure_break_atr_multiplier\":1.0}"
  "s_swing10_nostop|--structure-stop-mode swing --swing-lookback 10|{$G_noStop}"
  "s_piv_s1r1_nostop|--structure-stop-mode pivot_s1r1|{$G_noStop}"
  "s_or_nostop|--structure-stop-mode or_boundary|{$G_noStop}"
)

{
  echo "sweep 3w6 (chart-based structure stop) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index"
  echo "G       = {$G}"
  echo "G_noStop= {$G_noStop}"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w6 ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
log "reaping any stale ${DB_PREFIX}* DBs before start"; reap_dbs
log "disk: $(df -h / | awk 'NR==2 {print $4" free ("$5" used)"}')"

for entry in "${CONFIGS[@]}"; do
  IFS='|' read -r name extraflags params <<< "$entry"
  cfg_start=$(date +%s)
  log "--- $name  extraflags=[$extraflags]  params=$params"
  pids=()
  for i in $(seq 0 $((SHARD_COUNT - 1))); do
    "$PY" scripts/run_backtest.py \
      --strategy orb_conviction --underlying NIFTY \
      --all-expiries --options-subdir options_1min_past --underlying-source alice_index \
      --exit-mode current --fast --near-expiry-days "$NEAR_EXPIRY_DAYS" \
      $extraflags \
      --strategy-params "$params" \
      --shard-count "$SHARD_COUNT" --shard-index "$i" \
      --db-suffix "s3w6_${name}_s${i}" \
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

log "=== sweep 3w6 complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
