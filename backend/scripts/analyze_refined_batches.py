"""Batch comparison for refined sweep #2 (2026-08-28).

Given a --dir of ``<name>_current.csv`` trade files and a --configs list
(comma-separated config names, in the order to display), prints one
compact comparison block:

  * per-config KPI table (ALL / IS / OOS): n, win%, net expectancy,
    net profit factor, net total, max drawdown, worst losing streak
  * pooled-and-per-config breakdowns by entry hour (IST), by
    days-to-expiry bucket, and by exit_reason

All PnL is per 1 lot, net of the same realistic cost model as
analyze_conviction_sweep.py (flat 40/lot + 0.04% premium turnover +
0.1% STT + 0.5%/side premium slippage). run_backtest.py --exit-mode
current models zero costs itself.

Run under ~/an_venv (pandas/numpy) on the backtest VM:
    ~/an_venv/bin/python scripts/analyze_refined_batches.py \\
        --dir data/historical/backtest_reports/refined_sweep_XXX \\
        --configs ref_orb_baseline,sb_persist60,sb_persist120 \\
        --label "Batch 1 - exit mechanics"
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

FLAT_COST_PER_LOT = 40.0
PROPORTIONAL_RATE = 0.0004
STT_RATE = 0.001

_SYM_RE = re.compile(r"^[A-Z]+?(\d{2})(\d{2})(\d{2})(\d+)(CE|PE)$")


def _round_trip_cost_per_lot(entry: float, exit_: float, lot_size: float, slip: float) -> float:
    return (
        FLAT_COST_PER_LOT
        + PROPORTIONAL_RATE * (entry + exit_) * lot_size
        + STT_RATE * exit_ * lot_size
        + slip * (entry + exit_) * lot_size
    )


def _expiry_from_symbol(sym: str):
    m = _SYM_RE.match(str(sym))
    if not m:
        return pd.NaT
    yy, mm, dd, _strike, _ot = m.groups()
    try:
        return pd.Timestamp(2000 + int(yy), int(mm), int(dd)).date()
    except ValueError:
        return pd.NaT


def _load(path: Path, slip: float) -> pd.DataFrame:
    df = pd.read_csv(path)
    et = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    xt = pd.to_datetime(df["exit_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["entry_ist"] = et
    df["entry_date"] = et.dt.date
    df["entry_hour"] = et.dt.hour
    df["dow"] = et.dt.day_name()
    df["hold_min"] = (xt - et).dt.total_seconds() / 60
    df["pnl_per_lot"] = df["pnl"] / df["qty_lots"].clip(lower=1)
    df["cost_per_lot"] = df.apply(
        lambda r: _round_trip_cost_per_lot(r["entry_price"], r["exit_price"], r["lot_size"], slip),
        axis=1,
    )
    df["net_per_lot"] = df["pnl_per_lot"] - df["cost_per_lot"]
    exp = df["symbol"].map(_expiry_from_symbol)
    df["dte"] = [
        (e - d).days if not pd.isna(e) else np.nan
        for e, d in zip(exp, df["entry_date"], strict=False)
    ]
    return df


def _worst_streak(pnl: np.ndarray) -> int:
    best = cur = 0
    for x in pnl:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def _kpi(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return dict(n=0, win=float("nan"), E=float("nan"), PF=float("nan"),
                    tot=float("nan"), dd=float("nan"), strk=float("nan"))
    p = df["net_per_lot"].to_numpy()
    wins, losses = p[p > 0], p[p < 0]
    gl = -losses.sum()
    eq = np.cumsum(p)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    return dict(
        n=len(p),
        win=len(wins) / len(p) * 100,
        E=float(p.mean()),
        PF=(wins.sum() / gl) if gl > 0 else float("inf"),
        tot=float(p.sum()),
        dd=dd,
        strk=_worst_streak(p),
    )


def _kpi_row(name: str, seg: str, k: dict[str, float]) -> str:
    return (
        f"  {name:<24} {seg:<3} n={k['n']:>4.0f} win={k['win']:>5.1f}% "
        f"E={k['E']:>8.1f} PF={k['PF']:>5.2f} tot={k['tot']:>10.1f} "
        f"maxDD={k['dd']:>9.1f} Lstrk={k['strk']:>3.0f}"
    )


def _grp(df: pd.DataFrame, col: str, order=None) -> str:
    d = df.assign(_w=(df["net_per_lot"] > 0).astype(float))
    g = d.groupby(col, observed=True).agg(
        n=("net_per_lot", "size"),
        w=("_w", "mean"),
        E=("net_per_lot", "mean"),
        tot=("net_per_lot", "sum"),
    )
    if order is not None:
        g = g.reindex(order).dropna(how="all")
    parts = []
    for k, r in g.iterrows():
        parts.append(f"{k}: n={r['n']:.0f} w={r['w'] * 100:.0f}% "
                     f"E={r['E']:.0f} tot={r['tot']:.0f}")
    return "   ".join(parts)


def _dte_bucket(d) -> str:
    if pd.isna(d):
        return "?"
    d = int(d)
    if d <= 0:
        return "0 (expiry day)"
    if d == 1:
        return "1"
    if d <= 3:
        return "2-3"
    if d <= 6:
        return "4-6"
    return "7+"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--configs", required=True,
                    help="comma-separated config names in display order")
    ap.add_argument("--label", default="batch")
    ap.add_argument("--oos-from", type=date.fromisoformat, default=date(2026, 4, 1))
    ap.add_argument("--slippage-pct", type=float, default=0.005)
    args = ap.parse_args()

    names = [c.strip() for c in args.configs.split(",") if c.strip()]
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    dte_order = ["0 (expiry day)", "1", "2-3", "4-6", "7+", "?"]

    print("=" * 104)
    print(f"### {args.label}    (OOS = entry >= {args.oos_from};  per 1 lot, net of costs)")
    print("=" * 104)

    pooled = []
    print("\n-- KPI table --")
    for name in names:
        path = args.dir / f"{name}_current.csv"
        if not path.exists():
            print(f"  {name:<24} MISSING ({path.name})")
            continue
        df = _load(path, args.slippage_pct)
        df["config"] = name
        pooled.append(df)
        is_df = df[df["entry_date"] < args.oos_from]
        oos_df = df[df["entry_date"] >= args.oos_from]
        print(_kpi_row(name, "ALL", _kpi(df)))
        print(_kpi_row(name, "IS ", _kpi(is_df)))
        print(_kpi_row(name, "OOS", _kpi(oos_df)))

    print("\n-- net expectancy by entry hour (IST) --")
    for name in names:
        sub = next((d for d in pooled if d["config"].iloc[0] == name), None)
        if sub is None:
            continue
        print(f"  {name:<24} {_grp(sub, 'entry_hour')}")

    print("\n-- net expectancy by days-to-expiry --")
    for name in names:
        sub = next((d for d in pooled if d["config"].iloc[0] == name), None)
        if sub is None:
            continue
        sub = sub.assign(dteb=sub["dte"].map(_dte_bucket))
        print(f"  {name:<24} {_grp(sub, 'dteb', dte_order)}")

    print("\n-- net expectancy by day-of-week --")
    for name in names:
        sub = next((d for d in pooled if d["config"].iloc[0] == name), None)
        if sub is None:
            continue
        print(f"  {name:<24} {_grp(sub, 'dow', dow_order)}")

    print("\n-- exit_reason mix (count / win% / avg net) --")
    for name in names:
        sub = next((d for d in pooled if d["config"].iloc[0] == name), None)
        if sub is None:
            continue
        d = sub.assign(_w=(sub["net_per_lot"] > 0).astype(float))
        g = d.groupby("exit_reason", observed=True).agg(
            size=("net_per_lot", "size"), w=("_w", "mean"), mean=("net_per_lot", "mean")
        )
        parts = [f"{k}: {r['size']:.0f}/{r['w'] * 100:.0f}%/{r['mean']:.0f}"
                 for k, r in g.iterrows()]
        print(f"  {name:<24} {'   '.join(parts)}")

    if pooled:
        allrows = pd.concat(pooled, ignore_index=True)
        print("\n-- batch pooled OOS ranking (net expectancy) --")
        rank = []
        for name in names:
            sub = allrows[(allrows["config"] == name) & (allrows["entry_date"] >= args.oos_from)]
            k = _kpi(sub)
            rank.append((name, k))
        for name, k in sorted(rank, key=lambda x: -(x[1]["E"] if x[1]["E"] == x[1]["E"] else -1e9)):
            print(f"  {name:<24} OOS E={k['E']:>8.1f} PF={k['PF']:>5.2f} "
                  f"win={k['win']:>5.1f}% n={k['n']:>3.0f}")


if __name__ == "__main__":
    main()
