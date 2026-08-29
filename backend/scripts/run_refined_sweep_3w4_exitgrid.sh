#!/usr/bin/env bash
# Sweep #3, Workstream 4 (2026-08-29) -- exit-overlay grid on d_pdt_w65,
# NIFTY current expiry week.
#
# d_pdt_w65 (orb_conviction + require_prior_day_trend + max_or_range 65) is
# the one config from sweeps #1-#3 that clears the full robust bar (65% win,
# +274/lot, boot P(mean<=0)=0.028, survives 1% slip; n=26). The near-week ORB
# exit mix is carried entirely by `trail` (100% win) -- the fixed +20% target
# is hit ~once/yr and `stop` is 0% win. This grid asks: does arming the trail
# earlier and/or removing the fixed target (let winners run to structure-break
# / EOD) raise expectancy without dropping win rate below 60%?
#
# EVERY config carries the identical entry gate G, so entries are byte-
# identical across the whole grid -- this is a pure exit-overlay re-slice of
# the same ~26 trades. Judge as a plateau test (analyze_walkforward.py), not a
# point-estimate hunt.
#
# "No target" is expressed as target_pct=1.0 (+100% intrabar target, ~never
# hit for an ATM weekly scalp) WITH trail_activation_fraction compensated so
# the trail still arms at a sane +6..12% premium move -- the arm distance in
# _reconstruct_exit_current is abs(target-entry)*trail_activation_fraction,
# i.e. a fraction of the TARGET distance, so it must be lowered when the
# target is pushed out. No harness code change.
#
#   target_pct 0.20, taf 0.60 -> arms +12% (baseline d_pdt_w65)
#   target_pct 0.20, taf 0.30 -> arms +6%
#   target_pct 0.20, taf 0.20 -> arms +4%
#   target_pct 1.0,  taf 0.12 -> arms +12%
#   target_pct 1.0,  taf 0.08 -> arms +8%
#   target_pct 1.0,  taf 0.06 -> arms +6%
#   target_pct 0.30/0.40/0.50, taf 0.40/0.30/0.24 -> arms +12% (abs held)
#
# --near-expiry-days 6, --exit-mode current, ALL days / full hours, VIX
# seeded + PCR floored (automatic). 18-way sharded. strategy=orb_conviction,
# src=alice_index always.
set -uo pipefail
cd "$(dirname "$0")/.."   # backend/

PY=./.venv/bin/python
SHARD_COUNT=18
NEAR_EXPIRY_DAYS=6
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=data/historical/backtest_reports/sweep3w4_exitgrid_${STAMP}
STATUS_FILE=/tmp/sweep3w4_status.log
LOG_DIR=/tmp/sweep3w4_logs
mkdir -p "$RESULTS_DIR" "$LOG_DIR"
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$STATUS_FILE"; }

# Disk hygiene: run_backtest.py CREATEs a per-`--db-suffix` database and NEVER
# drops it, so a sweep leaks ~20-70 MB x (configs x shards) of Postgres
# scratch DBs forever (filled the VM disk 2026-08-29). Reap this sweep's own
# `trading_bot_backtest_s3w4_*` DBs at start, after each config, and on EXIT.
PSQL="sudo -u postgres psql"
DB_PREFIX="trading_bot_backtest_s3w4_"
reap_dbs() {
  $PSQL -tAc "SELECT 'DROP DATABASE IF EXISTS \"'||datname||'\" WITH (FORCE);'
    FROM pg_database WHERE datname LIKE '${DB_PREFIX}%'" 2>/dev/null | $PSQL -q 2>/dev/null || true
}
trap reap_dbs EXIT

# The d_pdt_w65 entry gate -- present on every config, unchanged.
G='"require_prior_day_trend":true,"max_or_range_nifty_points":65'

# name|params-json  (strategy=orb_conviction, src=alice_index always)
CONFIGS=(
  "x_baseline|{$G}"
  "x_tgt_arm06|{$G,\"trail_activation_fraction\":0.30}"
  "x_tgt_arm04|{$G,\"trail_activation_fraction\":0.20}"
  "x_tgt_arm06_lock06|{$G,\"trail_activation_fraction\":0.30,\"trail_lock_fraction\":0.6}"
  "x_notgt_arm12|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.12}"
  "x_notgt_arm08|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.08}"
  "x_notgt_arm06|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.06}"
  "x_notgt_arm08_lock06|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.08,\"trail_lock_fraction\":0.6}"
  "x_tgt30_arm12|{$G,\"target_pct\":0.30,\"trail_activation_fraction\":0.40}"
  "x_tgt40_arm12|{$G,\"target_pct\":0.40,\"trail_activation_fraction\":0.30}"
  "x_tgt50_arm12|{$G,\"target_pct\":0.50,\"trail_activation_fraction\":0.24}"
  "x_tgt40_armrel|{$G,\"target_pct\":0.40}"
  "x_baseline_stop10|{$G,\"stop_pct\":0.10}"
  "x_baseline_stop15|{$G,\"stop_pct\":0.15}"
  "x_notgt_arm08_stop10|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.08,\"stop_pct\":0.10}"
  "x_notgt_arm08_stop15|{$G,\"target_pct\":1.0,\"trail_activation_fraction\":0.08,\"stop_pct\":0.15}"
)

{
  echo "sweep 3w4 (d_pdt_w65 exit-overlay grid) ${STAMP}"
  echo "shards=${SHARD_COUNT}  near_expiry_days=${NEAR_EXPIRY_DAYS}  strategy=orb_conviction  src=alice_index"
  echo "entry gate G (every config): {$G}"
  printf '%s\n' "${CONFIGS[@]}"
} > "${RESULTS_DIR}/SWEEP_META.txt"

overall_start=$(date +%s)
log "=== sweep 3w4 ${STAMP}: ${#CONFIGS[@]} configs x ${SHARD_COUNT} shards ==="
log "reaping any stale ${DB_PREFIX}* DBs before start"; reap_dbs

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
      --db-suffix "s3w4_${name}_s${i}" \
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
  reap_dbs   # drop this config's 18 scratch DBs before the next config
  el=$(( $(date +%s) - cfg_start ))
  n=$(($(wc -l < "${RESULTS_DIR}/${name}_current.csv" 2>/dev/null || echo 1) - 1))
  [ "$fail" -eq 1 ] && log "*** $name: a shard FAILED (${el}s, ${n} trades)" \
                    || log "$name OK (${el}s, ${n} trades)"
done

log "=== sweep 3w4 complete: $(( ($(date +%s) - overall_start) / 60 ))min -> ${RESULTS_DIR} ==="
