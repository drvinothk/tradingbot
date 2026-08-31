from __future__ import annotations
import re
from pathlib import Path
import numpy as np
import pandas as pd

RESULTS_DIR = Path("data/historical/backtest_reports/s4p3_refinement_20260830T164757Z")
FLAT_COST_PER_LOT = 40.0
PROPORTIONAL_RATE = 0.0004
STT_RATE = 0.001


def cost(entry, exit_, ls):
    return FLAT_COST_PER_LOT + PROPORTIONAL_RATE * (entry + exit_) * ls + STT_RATE * exit_ * ls


def pf(p):
    gl = -p[p < 0].sum()
    return float(p[p > 0].sum() / gl) if gl > 0 else float("inf")


def max_drawdown(df, net):
    order = df["entry_time"].astype(str).argsort()
    net_ord = net[order]
    cum = np.cumsum(net_ord)
    running_max = np.maximum.accumulate(cum)
    dd = running_max - cum
    return float(dd.max()) if len(dd) else 0.0


rows = []
for f in sorted(RESULTS_DIR.glob("x3_*_current.csv")):
    name = f.stem.replace("_current", "")
    df = pd.read_csv(f)
    if df.empty:
        rows.append({"config": name, "n": 0, "win_rate": 0, "net_pnl": 0, "net_per_trade": 0, "pf": 0, "max_dd": 0})
        continue
    ppl = df["pnl"] / df["qty_lots"].clip(lower=1)
    c = df.apply(lambda r: cost(r["entry_price"], r["exit_price"], r["lot_size"]), axis=1)
    net = (ppl - c).to_numpy()
    n = len(net)
    wins = net[net > 0]
    rows.append({
        "config": name, "n": n,
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0,
        "net_pnl": round(float(net.sum()), 1),
        "net_per_trade": round(float(net.mean()), 1) if n else 0,
        "pf": round(pf(net), 3),
        "max_dd": round(max_drawdown(df, net), 1),
    })

out = pd.DataFrame(rows)
out.to_csv("/tmp/phase3_summary.csv", index=False)
print(out.to_string(index=False))
