#!/usr/bin/env bash
# Loren track -- FUTURES pass (no option Greeks). Standalone: scripts/
# loren_backtest.py + analyze_loren_futures.py are pandas/numpy only, no
# Postgres, no reaper. Runs under ~/an_venv on e4/A1, or ./.venv locally.
#
#   cd ~/trading-bot/backend
#   setsid bash scripts/run_loren_futures_sweep.sh </dev/null >/tmp/lorenfut_nohup.log 2>&1 & disown
#   tail -f /tmp/lorenfut_status.log
#
# Env:
#   MODE       pass0 (default) | pass1 | both
#   SRC        index_proxy (default, 3yr) | shoonya | truedata_stitch
#   DATE_SHARD 1 -> split each 3yr config run into 3 calendar-year sub-runs + merge
#   PY         python to use (default ~/an_venv/bin/python, fallback ./.venv/bin/python)
#
# Pass 0 is the edge gate: base + 6 variants + the real-futures cross-check.
# Read the pass-0 block; if base AND the best variant both have TRAIN E<=0
# (or bootstrap P(mean<=0) > ~0.5), the signal has no edge -> do NOT run pass 1.
set -u

PY="${PY:-$HOME/an_venv/bin/python}"
[ -x "$PY" ] || PY="./.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"   # local/Windows fallback
MODE="${MODE:-pass0}"
SRC="${SRC:-index_proxy}"
DATE_SHARD="${DATE_SHARD:-0}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="data/historical/backtest_reports/lorenfut_${STAMP}"
STATUS="/tmp/lorenfut_status.log"
mkdir -p "$OUT"
echo "loren-futures sweep  stamp=$STAMP  py=$PY  mode=$MODE  src=$SRC  date_shard=$DATE_SHARD" | tee "$STATUS"

# name|config-json   -- Config default exit_mode is classifier_structure, so
# the spec-6 base MUST set exit_mode explicitly.
PASS0=(
  "f_base|{\"exit_mode\":\"kernel_only\"}"
  "f_exit_cls|{\"exit_mode\":\"classifier_only\"}"
  "f_exit_comb|{\"exit_mode\":\"combined\"}"
  "f_cap|{\"exit_mode\":\"kernel_only\",\"risk_exceed_action\":\"cap\"}"
  "f_featB|{\"exit_mode\":\"kernel_only\",\"feature_set\":\"B\"}"
  "f_wick|{\"exit_mode\":\"kernel_only\",\"breakout_confirmation\":\"wick\"}"
  "f_nokernel|{\"exit_mode\":\"kernel_only\",\"trade_with_kernel\":false}"
)
# spec §6 matrix, one axis at a time around f_base, + risk/feature axes + combos
K='"exit_mode":"kernel_only"'
PASS1=(
  "f_w4|{$K,\"breakout_max_wait\":4}"
  "f_w6|{$K,\"breakout_max_wait\":6}"
  "f_adx20|{$K,\"use_adx_filter\":true,\"adx_threshold\":20}"
  "f_adx25|{$K,\"use_adx_filter\":true,\"adx_threshold\":25}"
  "f_ema200|{$K,\"use_ema_filter\":true,\"ema_period\":200}"
  "f_sb0|{$K,\"stop_buffer_atr_frac\":0.0}"
  "f_sb25|{$K,\"stop_buffer_atr_frac\":0.25}"
  "f_caponly|{$K,\"risk_exceed_action\":\"cap\"}"
  "f_comb_featB|{\"exit_mode\":\"combined\",\"feature_set\":\"B\"}"
  "f_comb_cap|{\"exit_mode\":\"combined\",\"risk_exceed_action\":\"cap\"}"
  "f_comb_adx20|{\"exit_mode\":\"combined\",\"use_adx_filter\":true,\"adx_threshold\":20}"
  "f_w4_comb|{\"exit_mode\":\"combined\",\"breakout_max_wait\":4}"
  "f_cap_comb_featB|{\"exit_mode\":\"combined\",\"risk_exceed_action\":\"cap\",\"feature_set\":\"B\"}"
  "f_kernel_sb0_cap|{$K,\"stop_buffer_atr_frac\":0.0,\"risk_exceed_action\":\"cap\"}"
)

run_one() {   # $1=name  $2=cfg
  local name="$1" cfg="$2"
  if [ "$DATE_SHARD" = "1" ] && [ "$SRC" = "index_proxy" ]; then
    for i in 0 1 2; do
      "$PY" scripts/loren_backtest.py --futures --futures-source "$SRC" \
        --config "$cfg" --shard-count 3 --shard-index "$i" \
        --out-csv "$OUT/${name}_s${i}.csv" >"$OUT/${name}_s${i}.log" 2>&1 &
    done
    wait
    "$PY" scripts/merge_backtest_shards.py --glob "$OUT/${name}_s*.csv" \
      --out "$OUT/${name}_current.csv" >>"$OUT/${name}_merge.log" 2>&1
    rm -f "$OUT/${name}_s"*.csv
  else
    "$PY" scripts/loren_backtest.py --futures --futures-source "$SRC" \
      --config "$cfg" --out-csv "$OUT/${name}_current.csv" \
      >"$OUT/${name}.log" 2>&1
  fi
}

summ() {   # $1=name
  "$PY" - "$OUT/${1}_current.csv" "$1" <<'PYEOF' | tee -a "$STATUS"
import sys, pandas as pd, numpy as np
p, name = sys.argv[1], sys.argv[2]
try:
    df = pd.read_csv(p)
except Exception:
    print(f"{name:18s} MISSING"); sys.exit()
df = df[df["pnl"].notna()]
if df.empty:
    print(f"{name:18s} n=0"); sys.exit()
# futures cost model, per 1 lot RT (matches analyze_loren_futures.py defaults)
ls=df["lot_size"]; e=df["entry_price"]; x=df["exit_price"]; side=df["side"]
nb=np.where(side=="BUY", e*ls, x*ls); ns=np.where(side=="BUY", x*ls, e*ls)
to=nb+ns
cost = 40 + 0.0000188*to + 1e-6*to + 0.18*(40+0.0000188*to+1e-6*to) + 0.0002*ns + 0.00002*nb + 1*0.05*ls*2
net = df["pnl"]/df["qty_lots"].clip(lower=1) - cost
d0,d1 = pd.to_datetime(df["entry_time"],utc=True).dt.tz_convert("Asia/Kolkata").dt.date.agg(["min","max"])
span=(d1-d0).days; te=d0+pd.Timedelta(days=int(span*0.6))
ed=pd.to_datetime(df["entry_time"],utc=True).dt.tz_convert("Asia/Kolkata").dt.date
tr = net[ed<=te]
w=net[net>0]
pf=w.sum()/abs(net[net<=0].sum()) if net[net<=0].sum()!=0 else float("inf")
print(f"{name:18s} n={len(df):4d}  win%={100*len(w)/len(df):5.1f}  "
      f"net/lot={net.mean():8.0f}  TRAIN_E={tr.mean() if len(tr) else float('nan'):8.0f}  "
      f"tot={net.sum():10.0f}  PF={pf:.2f}  maxDD={((net.cumsum().cummax()-net.cumsum()).max()):.0f}")
PYEOF
}

run_pass() {   # $1=label  $2..=rows
  local label="$1"; shift
  echo "=== $label ===" | tee -a "$STATUS"
  local names=()
  for row in "$@"; do
    name="${row%%|*}"; cfg="${row#*|}"
    run_one "$name" "$cfg" &
    names+=("$name")
  done
  wait
  for n in "${names[@]}"; do summ "$n"; done
  echo "$label configs: $(IFS=,; echo "${names[*]}")" >>"$STATUS"
}

if [ "$MODE" = "pass0" ] || [ "$MODE" = "both" ]; then
  run_pass "PASS 0 (edge gate)" "${PASS0[@]}"
  echo "--- real-futures cross-check (shoonya vs index_proxy, 2026-07..08) ---" | tee -a "$STATUS"
  "$PY" scripts/loren_backtest.py --futures --futures-source shoonya \
    --config '{"exit_mode":"kernel_only"}' --out-csv "$OUT/xchk_shoonya_current.csv" \
    2>&1 | grep -E "^trades=|TRAIN|ALL " | tee -a "$STATUS"
  "$PY" scripts/loren_backtest.py --futures --futures-source index_proxy \
    --from 2026-07-01 --to 2026-08-26 --config '{"exit_mode":"kernel_only"}' \
    --out-csv "$OUT/xchk_indexproxy_current.csv" \
    2>&1 | grep -E "^trades=|TRAIN|ALL " | tee -a "$STATUS"
  echo "GATE: if f_base AND the best variant both have TRAIN_E<=0 -> signal has no edge, STOP." | tee -a "$STATUS"
fi

if [ "$MODE" = "pass1" ] || [ "$MODE" = "both" ]; then
  run_pass "PASS 1 (spec §6 matrix)" "${PASS1[@]}"
fi

echo "DONE  $STAMP  -> $OUT" | tee -a "$STATUS"
echo "next: $PY scripts/analyze_loren_futures.py --dir $OUT --configs <csv list> --split 0.6,0.8" | tee -a "$STATUS"
