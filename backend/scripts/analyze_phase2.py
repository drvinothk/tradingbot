"""Phase 2 exit-tuning summary: per-config net-of-cost metrics, grouped by
strategy + entry-cohort, one row per stop variant (min/mid/max)."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

RESULTS_DIR = Path("data/historical/backtest_reports/s4p2_exittuning_20260830T104615Z")

FLAT_COST_PER_LOT = 40.0
PROPORTIONAL_RATE = 0.0004
STT_RATE = 0.001

STRATEGY_PREFIX = {
    "v": "vwap_pullback",
    "e": "ema_micro_pullback",
    "o": "oi_volume_confirmed",
    "l": "liquidity_sweep_reversal",
}


def cost(entry: float, exit_: float, ls: float) -> float:
    return FLAT_COST_PER_LOT + PROPORTIONAL_RATE * (entry + exit_) * ls + STT_RATE * exit_ * ls


def pf(p: np.ndarray) -> float:
    gl = -p[p < 0].sum()
    return float(p[p > 0].sum() / gl) if gl > 0 else float("inf")


def analyze(path: Path) -> dict:
    df = pd.read_csv(path)
    if df.empty:
        return {
            "n": 0,
            "win_rate": 0.0,
            "raw_pnl": 0.0,
            "net_pnl": 0.0,
            "net_per_trade": 0.0,
            "pf": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        }
    ppl = df["pnl"] / df["qty_lots"].clip(lower=1)
    c = df.apply(lambda r: cost(r["entry_price"], r["exit_price"], r["lot_size"]), axis=1)
    net = (ppl - c).to_numpy()
    n = len(net)
    wins = net[net > 0]
    losses = net[net <= 0]
    return {
        "n": n,
        "win_rate": 100.0 * len(wins) / n if n else 0.0,
        "raw_pnl": float(df["pnl"].sum()),
        "net_pnl": float(net.sum()),
        "net_per_trade": float(net.mean()),
        "pf": pf(net),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
    }


def main():
    rows = []
    for f in sorted(RESULTS_DIR.glob("x2_*_current.csv")):
        name = f.stem.replace("_current", "")
        m = re.match(r"^x2_([veol])(\d*)_?(.*)_(min|mid|max)$", name)
        if not m:
            print(f"UNPARSED: {name}")
            continue
        strat_letter, _digit, cohort_raw, stop_variant = m.groups()
        strategy = STRATEGY_PREFIX[strat_letter]
        cohort = (
            f"{strat_letter}{_digit}_{cohort_raw}" if cohort_raw else f"{strat_letter}{_digit}_base"
        )
        stats = analyze(f)
        row = {"strategy": strategy, "cohort": cohort, "stop_variant": stop_variant, "config": name}
        rows.append({**row, **stats})

    out = pd.DataFrame(rows)
    out.to_csv("/tmp/phase2_summary.csv", index=False)
    print(f"Total configs parsed: {len(out)}")
    for strat in out["strategy"].unique():
        print(f"\n=== {strat} ===")
        sub = out[out["strategy"] == strat].sort_values(["cohort", "stop_variant"])
        cols = ["cohort", "stop_variant", "n", "win_rate", "net_pnl", "net_per_trade", "pf"]
        print(sub[cols].to_string(index=False))


if __name__ == "__main__":
    main()
