"""Robustness checks for the near-week ORB sweep (#3), tuned for a very small
trade count (~15-35/yr).

Formal rolling walk-forward folds are not viable at this n (0-3 trades per
month). Instead, per config:

  * per-expiry-week sign test -- of the Tuesday-expiry weeks that produced a
    trade, what fraction netted positive? exact two-sided binomial p vs 50%.
  * IS / OOS split (entry date </>= --oos-from).
  * two 6-month halves (directional only).
  * bootstrap -- resample trades with replacement 10k times: 5th-percentile
    mean net, and P(mean net <= 0).
  * cost sensitivity -- mean net at 0.3 / 0.5 / 0.7 / 1.0 %/side slippage.

All PnL per 1 lot, net of the same cost model as analyze_conviction_sweep.py.

Run under ~/an_venv on the e4 VM:
    ~/an_venv/bin/python scripts/analyze_walkforward.py \\
        --dir data/historical/backtest_reports/sweep3a_widthridge_XXX \\
        --configs w_25_65,w_20_65,w_25_75
"""

from __future__ import annotations

import argparse
import math
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

FLAT_COST_PER_LOT = 10.0  # Shoonya real brokerage: Rs5/order flat x2 legs (round-trip)
PROPORTIONAL_RATE = 0.0004
STT_RATE = 0.001
_SYM_RE = re.compile(r"^[A-Z]+?(\d{2})(\d{2})(\d{2})(\d+)(CE|PE)$")


def _cost(entry: float, exit_: float, ls: float, slip: float) -> float:
    return (
        FLAT_COST_PER_LOT
        + PROPORTIONAL_RATE * (entry + exit_) * ls
        + STT_RATE * exit_ * ls
        + slip * (entry + exit_) * ls
    )


def _sym_expiry(sym: str):
    m = _SYM_RE.match(str(sym))
    if not m:
        return pd.NaT
    yy, mm, dd, _s, _o = m.groups()
    try:
        return pd.Timestamp(2000 + int(yy), int(mm), int(dd)).date()
    except ValueError:
        return pd.NaT


def _load(path: Path, slip: float) -> pd.DataFrame:
    df = pd.read_csv(path)
    et = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["entry_date"] = et.dt.date
    df["ppl"] = df["pnl"] / df["qty_lots"].clip(lower=1)
    df["cost"] = df.apply(
        lambda r: _cost(r["entry_price"], r["exit_price"], r["lot_size"], slip), axis=1
    )
    df["net"] = df["ppl"] - df["cost"]
    df["expiry_week"] = df["symbol"].map(_sym_expiry)
    return df


def _pf(p: np.ndarray) -> float:
    gl = -p[p < 0].sum()
    return float(p[p > 0].sum() / gl) if gl > 0 else float("inf")


def _seg(p: np.ndarray) -> str:
    if len(p) == 0:
        return "n=0"
    return (f"n={len(p):>3} win={(p > 0).mean() * 100:>5.1f}% "
            f"E={p.mean():>8.1f} PF={_pf(p):>5.2f} tot={p.sum():>9.1f}")


def _binom_two_sided(k: int, n: int) -> float:
    """Exact two-sided binomial p vs p0=0.5."""
    if n == 0:
        return float("nan")
    pmf = [math.comb(n, i) * 0.5 ** n for i in range(n + 1)]
    obs = pmf[k]
    return float(min(1.0, sum(x for x in pmf if x <= obs + 1e-12)))


def _bootstrap(p: np.ndarray, draws: int = 10000, seed: int = 12345) -> tuple[float, float]:
    if len(p) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = p[rng.integers(0, len(p), size=(draws, len(p)))].mean(axis=1)
    return float(np.percentile(means, 5)), float((means <= 0).mean())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--configs", required=True)
    ap.add_argument("--oos-from", type=date.fromisoformat, default=date(2026, 4, 1))
    ap.add_argument("--slippage-pct", type=float, default=0.005)
    ap.add_argument("--glob-suffix", default="_current.csv")
    args = ap.parse_args()

    names = [c.strip() for c in args.configs.split(",") if c.strip()]
    print(f"cost: flat {FLAT_COST_PER_LOT}/lot + {PROPORTIONAL_RATE:.4%} turnover "
          f"+ {STT_RATE:.3%} STT + {args.slippage_pct:.3%}/side slip   (net, per 1 lot)")
    print(f"OOS from {args.oos_from}\n")

    for name in names:
        path = args.dir / f"{name}{args.glob_suffix}"
        if not path.exists():
            print(f"### {name}  MISSING\n")
            continue
        df = _load(path, args.slippage_pct)
        p = df["net"].to_numpy()
        print(f"### {name}   ({len(df)} trades)")
        print(f"  ALL  {_seg(p)}")

        # per-expiry-week sign test
        wk = df.groupby("expiry_week")["net"].sum()
        pos = int((wk > 0).sum())
        tot = int(len(wk))
        pval = _binom_two_sided(pos, tot)
        print(f"  expiry-week sign test: {pos}/{tot} weeks net-positive  "
              f"(two-sided binomial p vs 50% = {pval:.3f})")

        # IS / OOS
        isd = df[df["entry_date"] < args.oos_from]["net"].to_numpy()
        oos = df[df["entry_date"] >= args.oos_from]["net"].to_numpy()
        print(f"  IS   {_seg(isd)}")
        print(f"  OOS  {_seg(oos)}")

        # 6-month halves
        d0, d1 = df["entry_date"].min(), df["entry_date"].max()
        mid = d0 + (d1 - d0) / 2
        h1 = df[df["entry_date"] <= mid]["net"].to_numpy()
        h2 = df[df["entry_date"] > mid]["net"].to_numpy()
        print(f"  H1 (<= {mid})  {_seg(h1)}")
        print(f"  H2 ( > {mid})  {_seg(h2)}")

        # bootstrap
        lo5, p_le0 = _bootstrap(p)
        print(f"  bootstrap (10k): 5th-pctile mean net = {lo5:>8.1f}   P(mean <= 0) = {p_le0:.3f}")

        # cost sensitivity
        cs = []
        for s in (0.003, 0.005, 0.007, 0.010):
            d2 = _load(path, s)
            cs.append(f"{s * 100:.1f}%:{d2['net'].mean():>7.0f}")
        print(f"  slippage sensitivity (mean net/lot): {'  '.join(cs)}")
        print()


if __name__ == "__main__":
    main()
