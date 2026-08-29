#!/usr/bin/env bash
# Loren track -- OPTIONS pass (jdehorty Lorentzian Classification, "Framework 5").
# NOT ORB W7 -- see LOREN_BACKTEST_PLAN.md's naming note. Standalone: scripts/
# loren_backtest.py is self-contained (pandas/numpy only), no Postgres/reaper.
# Run under ~/an_venv on e4/A1, or system python locally.
#
#   cd ~/trading-bot/backend
#   setsid bash scripts/run_loren_options_sweep.sh </dev/null >/tmp/sweep3w7_nohup.log 2>&1 & disown
#   tail -f /tmp/sweep3w7_status.log
#
# ETA (SHARDS=12): ~2-4 min. Then read the status-log summary, run
# analyze_walkforward.py. (Result 2026-08-29: all 17 configs fail the robust
# bar -- see BACKTEST_LEARNINGS.md. The futures follow-up is
# run_loren_futures_sweep.sh.)
set -u

PY="${PY:-$HOME/an_venv/bin/python}"
[ -x "$PY" ] || PY="./.venv/bin/python"
SHARDS="${SHARDS:-12}"
NEAR_DAYS="${NEAR_DAYS:-6}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="data/historical/backtest_reports/sweep3w7_loren_${STAMP}"
STATUS="/tmp/sweep3w7_status.log"
mkdir -p "$OUT"

echo "W7 Lorentzian sweep  stamp=$STAMP  py=$PY  shards=$SHARDS  near_days=$NEAR_DAYS" | tee "$STATUS"

# name | config-json   (see loren_backtest.py Config for every key)
CAP='"risk_exceed_action":"cap"'
C15='"max_risk_atr_frac":1.5'
COMB='"exit_mode":"combined"'
CONFIGS=(
  # --- fidelity baselines ---
  "l_spec|{}"
  "l_cap|{${CAP}}"
  # --- risk-handling axis ---
  "l_cap15|{${CAP},${C15}}"
  "l_cap25|{${CAP},\"max_risk_atr_frac\":2.5}"
  # --- exit-mechanism axis (spec's 3 exit modes) ---
  "l_combined|{${CAP},${C15},${COMB}}"
  "l_kernel|{${CAP},${C15},\"exit_mode\":\"kernel_only\"}"
  "l_oppo|{${CAP},${C15},\"exit_mode\":\"classifier_only\"}"
  "l_comb_cap075|{${CAP},${COMB}}"
  # --- target overlays ---
  "l_tgt35_comb|{${CAP},${C15},${COMB},\"target_pct\":0.35}"
  "l_tgt50_comb|{${CAP},${C15},${COMB},\"target_pct\":0.50}"
  # --- win-rate / pnl levers (stacked on the combined base) ---
  "l_featB|{${CAP},${C15},${COMB},\"feature_set\":\"B\"}"
  "l_itm|{${CAP},${C15},${COMB},\"strike_rule\":\"1_ITM\"}"
  "l_morning|{${CAP},${C15},${COMB},\"entry_cutoff\":\"12:00\"}"
  "l_k5|{${CAP},${C15},${COMB},\"neighbors\":5}"
  "l_k11|{${CAP},${C15},${COMB},\"neighbors\":11}"
  "l_wait3|{${CAP},${C15},${COMB},\"breakout_max_wait\":3}"
  "l_best|{${CAP},${C15},${COMB},\"feature_set\":\"B\",\"strike_rule\":\"1_ITM\",\"target_pct\":0.40}"
)

echo "--- underlying-only signal validation (3yr, 60/20/20 overfitting check) ---" | tee -a "$STATUS"
echo "[feature-set A / spec default]" | tee -a "$STATUS"
"$PY" scripts/loren_backtest.py --underlying-only 2>&1 | tee -a "$STATUS"
echo "[feature-set B / trend-confirmed]" | tee -a "$STATUS"
"$PY" scripts/loren_backtest.py --underlying-only --config '{"feature_set":"B"}' 2>&1 | tee -a "$STATUS"

for row in "${CONFIGS[@]}"; do
  name="${row%%|*}"; cfg="${row#*|}"
  echo "=== $name  $cfg ===" | tee -a "$STATUS"
  pids=()
  for i in $(seq 0 $((SHARDS-1))); do
    "$PY" scripts/loren_backtest.py \
      --all-expiries --near-expiry-days "$NEAR_DAYS" \
      --config "$cfg" \
      --shard-count "$SHARDS" --shard-index "$i" \
      --out-csv "$OUT/${name}_s${i}.csv" \
      >"$OUT/${name}_s${i}.log" 2>&1 &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p"; done
  "$PY" scripts/merge_backtest_shards.py --glob "$OUT/${name}_s*.csv" --out "$OUT/${name}_current.csv" \
    >>"$OUT/${name}_merge.log" 2>&1
  rm -f "$OUT/${name}_s"*.csv
  # quick summary line from the merged file
  "$PY" - "$OUT/${name}_current.csv" "$name" <<'PYEOF' | tee -a "$STATUS"
import sys, pandas as pd, numpy as np
p, name = sys.argv[1], sys.argv[2]
df = pd.read_csv(p)
df = df[df["pnl"].notna()]
if df.empty:
    print(f"{name:16s} n=0"); sys.exit()
# realistic cost model (flat 40 + 0.04% turnover + 0.1% STT + 0.5%/side slip), per 1 lot
ls = df["lot_size"]; e = df["entry_price"]; x = df["exit_price"]
cost = 40 + 0.0004*(e+x)*ls + 0.001*x*ls + 0.005*(e+x)*ls
net = df["pnl"] - cost
w = net[net > 0]
pf = w.sum()/abs(net[net<=0].sum()) if net[net<=0].sum() != 0 else float("inf")
print(f"{name:16s} n={len(df):4d}  win%={100*len(w)/len(df):5.1f}  "
      f"net/lot={net.mean():7.0f}  total_net={net.sum():9.0f}  PF={pf:.2f}  "
      f"maxDD={((net.cumsum().cummax()-net.cumsum()).max()):.0f}")
PYEOF
done

echo "DONE  $STAMP  -> $OUT" | tee -a "$STATUS"
echo "next: ~/an_venv/bin/python scripts/analyze_walkforward.py --dir $OUT --configs <csv list> --oos-from 2026-03-01" | tee -a "$STATUS"
