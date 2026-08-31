from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

RESULTS_DIR = Path("data/historical/backtest_reports/s4p2_exittuning_20260830T104615Z")
FLAT_COST_PER_LOT = 40.0
PROPORTIONAL_RATE = 0.0004
STT_RATE = 0.001

FILES = {
    "o3_atr_pcrl_lock06": "x2_o3_atr_pcrl_mid",
    "o3_atr_pcrt_lock06": "x2_o3_atr_pcrt_min",
    "o3_pdt_atr_pcrt_lock06": "x2_o3_pdt_atr_pcrt_min",
    "o_pdt_atr_lock06": "x2_o_pdt_atr_mid",
    "o_pcrl_lock06": "x2_o_pcrl_mid",
    "o_pcrt_lock06": "x2_o_pcrt_min",
    "e_pdt_atr_lock06": "x2_e_pdt_atr_max",
    "e3_atr_pcrl_lock06": "x2_e3_atr_pcrl_min",
    "v_pdt_lock06": "x2_v_pdt_mid",
    "v_atr_lock06": "x2_v_atr_min",
    "l_pcrt_lock06": "x2_l_pcrt_max",
    "l15_pcrt_disp10_lock06": "x2_l15_pcrt_disp10_min",
}


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
for label, fname in FILES.items():
    f = RESULTS_DIR / f"{fname}_current.csv"
    df = pd.read_csv(f)
    ppl = df["pnl"] / df["qty_lots"].clip(lower=1)
    c = df.apply(lambda r: cost(r["entry_price"], r["exit_price"], r["lot_size"]), axis=1)
    net = (ppl - c).to_numpy()
    n = len(net)
    wins = net[net > 0]
    rows.append({
        "config": label, "n": n,
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0,
        "net_pnl": round(float(net.sum()), 1),
        "net_per_trade": round(float(net.mean()), 1) if n else 0,
        "pf": round(pf(net), 3),
        "max_dd": round(max_drawdown(df, net), 1),
    })

out = pd.DataFrame(rows)
out.to_csv("/tmp/phase2_lock06_baseline.csv", index=False)
print(out.to_string(index=False))
