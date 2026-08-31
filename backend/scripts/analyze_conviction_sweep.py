"""Analyse the conviction-strategy sweep output (scripts/run_conviction_sweep.sh).

Reads every ``<name>_current.csv`` trades file in --dir and reports, per
config, the framework's KPIs (expectancy first, not win rate) with a
realistic-cost haircut and an in-sample / out-of-sample split.

Cost model (applied per trade, per lot, then scaled by that trade's own
qty_lots): a flat brokerage+GST component plus premium-proportional
exchange/STT/stamp charges plus a configurable per-side slippage fraction
of each leg's premium -- since run_backtest.py itself models zero costs and
zero slippage (fills at the option LTP on both sides). See the module
docstring of run_backtest.py for the other, un-normalisable data limits
(1-min bars, synthetic spread, single chain snapshot per run, ~1yr depth).

Run under .venv-backtest (the only env with pandas/numpy):
    ./.venv-backtest/Scripts/python scripts/analyze_conviction_sweep.py \\
        --dir data/historical/backtest_reports/conviction_sweep \\
        --oos-from 2026-04-01 --slippage-pct 0.005
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# Shoonya real brokerage: Rs5/order flat x2 legs (round-trip), corrected 2026-08-31
FLAT_COST_PER_LOT = 10.0
PROPORTIONAL_RATE = 0.0004  # exchange txn + stamp + SEBI, fraction of (entry+exit) premium
STT_RATE = 0.001  # sell-side STT, fraction of exit premium


def _round_trip_cost_per_lot(entry: float, exit_: float, lot_size: float, slip_pct: float) -> float:
    proportional = PROPORTIONAL_RATE * (entry + exit_) * lot_size
    stt = STT_RATE * exit_ * lot_size
    slippage = slip_pct * (entry + exit_) * lot_size
    return FLAT_COST_PER_LOT + proportional + stt + slippage


def _load(path: Path, slip_pct: float) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["entry_date"] = df["entry_time"].dt.tz_convert("Asia/Kolkata").dt.date
    df["dow"] = df["entry_time"].dt.tz_convert("Asia/Kolkata").dt.day_name()
    df["pnl_per_lot"] = df["pnl"] / df["qty_lots"].clip(lower=1)
    df["cost_per_lot"] = df.apply(
        lambda r: _round_trip_cost_per_lot(
            r["entry_price"], r["exit_price"], r["lot_size"], slip_pct
        ),
        axis=1,
    )
    df["net_per_lot"] = df["pnl_per_lot"] - df["cost_per_lot"]
    return df


def _streak(losses: list[bool]) -> int:
    best = cur = 0
    for is_loss in losses:
        cur = cur + 1 if is_loss else 0
        best = max(best, cur)
    return best


def _metrics(df: pd.DataFrame, col: str) -> dict[str, float]:
    if df.empty:
        return {k: float("nan") for k in _METRIC_KEYS}
    pnl = df[col].to_numpy()
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(equity)
    max_dd = float((peak - equity).max()) if len(equity) else 0.0
    n_days = max(df["entry_date"].nunique(), 1)
    return {
        "n": float(len(df)),
        "per_day": len(df) / n_days,
        "win_rate": len(wins) / len(df),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "expectancy": float(pnl.mean()),
        "total": float(pnl.sum()),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_dd": max_dd,
        "loss_streak": float(_streak([bool(x < 0) for x in pnl])),
    }


_METRIC_KEYS = [
    "n", "per_day", "win_rate", "avg_win", "avg_loss",
    "expectancy", "total", "profit_factor", "max_dd", "loss_streak",
]


def _fmt_row(name: str, seg: str, m: dict[str, float]) -> str:
    return (
        f"{name:<22} {seg:<4} "
        f"n={m['n']:>4.0f} /day={m['per_day']:>4.2f} "
        f"win={m['win_rate']*100:>5.1f}% "
        f"avgW={m['avg_win']:>8.1f} avgL={m['avg_loss']:>8.1f} "
        f"E={m['expectancy']:>8.1f} tot={m['total']:>10.1f} "
        f"PF={m['profit_factor']:>5.2f} maxDD={m['max_dd']:>9.1f} "
        f"LlossStrk={m['loss_streak']:>3.0f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--oos-from", type=date.fromisoformat, default=date(2026, 4, 1))
    ap.add_argument("--slippage-pct", type=float, default=0.005,
                    help="Per-side slippage as a fraction of each leg's premium (default 0.005).")
    ap.add_argument("--glob", default="*_current.csv")
    args = ap.parse_args()

    paths = sorted(args.dir.glob(args.glob))
    if not paths:
        raise SystemExit(f"no files matching {args.glob} in {args.dir}")

    print(f"cost model: flat {FLAT_COST_PER_LOT}/lot + {PROPORTIONAL_RATE:.4%} premium turnover "
          f"+ {STT_RATE:.3%} STT + {args.slippage_pct:.3%}/side slippage")
    print(f"OOS = entry_date >= {args.oos_from.isoformat()}   (all figures per 1 lot, INR)\n")

    summary: list[tuple[str, dict[str, float], dict[str, float]]] = []
    for path in paths:
        name = path.stem.replace("_current", "")
        df = _load(path, args.slippage_pct)
        is_df = df[df["entry_date"] < args.oos_from]
        oos_df = df[df["entry_date"] >= args.oos_from]

        print(f"### {name}   ({len(df)} trades)")
        for seg, seg_df in (("ALL", df), ("IS", is_df), ("OOS", oos_df)):
            print("  gross " + _fmt_row(name, seg, _metrics(seg_df, "pnl_per_lot")))
            print("  net   " + _fmt_row(name, seg, _metrics(seg_df, "net_per_lot")))
        # day-of-week net expectancy
        dow = (
            df.groupby("dow")["net_per_lot"].agg(["count", "mean"]).reindex(
                ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
            )
        )
        print("  net by DOW: " + "  ".join(
            f"{d[:3]}={r['mean']:.0f}(n{int(r['count'])})"
            for d, r in dow.dropna().iterrows()
        ))
        print()
        summary.append((name, _metrics(df, "net_per_lot"), _metrics(oos_df, "net_per_lot")))

    print("=" * 100)
    print("NET expectancy ranking (per 1 lot):")
    for name, allm, oosm in sorted(summary, key=lambda x: -x[1]["expectancy"]):
        print(f"  {name:<22} ALL E={allm['expectancy']:>8.1f} PF={allm['profit_factor']:>5.2f} "
              f"| OOS E={oosm['expectancy']:>8.1f} PF={oosm['profit_factor']:>5.2f} "
              f"n(OOS)={oosm['n']:.0f}")


if __name__ == "__main__":
    main()
