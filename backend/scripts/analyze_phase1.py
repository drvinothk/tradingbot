from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

RESULTS_DIR = Path(sys.argv[1])
FLAT_COST_PER_LOT = 40.0
PROPORTIONAL_RATE = 0.0004
STT_RATE = 0.001


def cost(entry, exit_, ls):
    return FLAT_COST_PER_LOT + PROPORTIONAL_RATE * (entry + exit_) * ls + STT_RATE * exit_ * ls


def pf(p):
    gl = -p[p < 0].sum()
    return float(p[p > 0].sum() / gl) if gl > 0 else float("inf")


rows = []
for f in sorted(RESULTS_DIR.glob("*_current.csv")):
    name = f.stem.replace("_current", "")
    df = pd.read_csv(f)
    if df.empty:
        rows.append({"config": name, "n": 0, "win_rate": 0, "net_pnl": 0, "pf": 0})
        continue
    ppl = df["pnl"] / df["qty_lots"].clip(lower=1)
    c = df.apply(lambda r: cost(r["entry_price"], r["exit_price"], r["lot_size"]), axis=1)
    net = (ppl - c).to_numpy()
    n = len(net)
    wins = net[net > 0]
    rows.append({
        "config": name, "n": n,
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0,
        "net_pnl": round(float(net.sum()), 0),
        "net_per_trade": round(float(net.mean()), 1) if n else 0,
        "pf": round(pf(net), 2),
    })

out = pd.DataFrame(rows)
print(out.to_string(index=False))
