#!/usr/bin/env python3
"""Cost model + robustness checks for Gamma Blast backtest CSVs.

Precise cost model per `gamma_blast_config_v2_1.json` (`execution_costs`) --
deliberately NOT the generic flat-₹40+0.04%+0.1% model
`analyze_walkforward.py` uses for ORB/Loren, because this spec explicitly
cares about the 2026-04-01 STT hike and the dataset spans both sides of it.

**Slippage is NOT re-applied here** -- `gamma_blast_backtest.py` already bakes
entry/exit slippage into `entry_price`/`exit_price` at the fill-model level
(spec: `entry_fill: next_bar_open_plus_slippage`, `hard_stop_intrabar: ...
minus slippage`), since slippage there changes which stop/target price
actually executes, not just a post-hoc cost line. Re-applying it here would
double-count it. This script adds only: brokerage, STT (date-switched),
exchange transaction charge, GST on (brokerage+txn), stamp duty on the buy
leg.

Robustness checks (per spec `test_methodology.selection_rules` + this
project's own established bar for a thin-sample options strategy):
  * IS/OOS split (--oos-from)
  * tail-dependence guard: expectancy excluding the top-2 trades by pnl;
    FAILS if the sign flips
  * bootstrap: 10k resamples, 5th-percentile mean, P(mean<=0)
  * per-exit-reason / per-weekday / per-precondition-measure breakdown

Usage:
    python scripts/analyze_gamma_blast.py --csv out/gb_baseline.csv
    python scripts/analyze_gamma_blast.py --dir out/ --glob 'gb_*.csv' --summary-only
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# flat, real Shoonya rate (user-confirmed 2026-08-30) -- per round-trip trade, not per leg
BROKERAGE_PER_TRADE = 5.0
EXCHANGE_TXN_PCT = 0.0005                  # of premium, each side
GST_PCT = 0.18                             # on (brokerage + exchange txn)
STAMP_DUTY_BUY_PCT = 0.00003               # of buy-side premium only


def _stt_sell_pct(trade_date: date) -> float:
    return 0.0010 if trade_date < date(2026, 4, 1) else 0.0015


def _cost_per_trade(entry: float, exit_: float, lot_size: int, entry_date: date) -> float:
    brokerage = BROKERAGE_PER_TRADE
    exch_txn = EXCHANGE_TXN_PCT * (entry + exit_) * lot_size
    gst = GST_PCT * (brokerage + exch_txn)
    stt = _stt_sell_pct(entry_date) * exit_ * lot_size   # STT on the SELL (exit) leg only
    stamp = STAMP_DUTY_BUY_PCT * entry * lot_size
    return brokerage + exch_txn + gst + stt + stamp


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["entry_date"] = df["entry_time"].dt.date
    df["cost"] = df.apply(
        lambda r: _cost_per_trade(
            r["entry_price"], r["exit_price"], int(r["lot_size"]), r["entry_date"]
        ),
        axis=1,
    )
    df["net_pnl"] = df["pnl"] - df["cost"]
    return df


def _pf(p: np.ndarray) -> float:
    gl = -p[p < 0].sum()
    return float(p[p > 0].sum() / gl) if gl > 0 else float("inf")


def _seg(p: np.ndarray) -> str:
    if len(p) == 0:
        return "n=0"
    return (f"n={len(p):>3} win={(p > 0).mean() * 100:>5.1f}% "
            f"E={p.mean():>8.1f} PF={_pf(p):>5.2f} tot={p.sum():>9.1f}")


def _bootstrap(p: np.ndarray, draws: int = 10000, seed: int = 12345) -> tuple[float, float]:
    if len(p) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = p[rng.integers(0, len(p), size=(draws, len(p)))].mean(axis=1)
    return float(np.percentile(means, 5)), float((means <= 0).mean())


def _tail_dependence_guard(p: np.ndarray) -> tuple[bool, float, float]:
    """Recompute expectancy excluding the top-2 trades by pnl. Returns
    (survives, expectancy_all, expectancy_excl_top2). Per spec: discard a
    config if the sign flips.
    """
    if len(p) <= 2:
        return True, float(p.mean()) if len(p) else 0.0, float("nan")
    e_all = float(p.mean())
    trimmed = np.sort(p)[:-2]
    e_trim = float(trimmed.mean())
    survives = (e_all > 0) == (e_trim > 0)
    return survives, e_all, e_trim


def report(df: pd.DataFrame, label: str, oos_from: date, slippage_extra_pct: float = 0.0) -> dict:
    if df.empty:
        print(f"{label}: no trades")
        return {"label": label, "n": 0}

    net = df["net_pnl"].to_numpy()
    if slippage_extra_pct:
        # sensitivity only: extra adverse move on top of the fill model's own
        # baked-in slippage, applied to notional (entry+exit)*lot_size
        extra = slippage_extra_pct / 100.0 * (df["entry_price"] + df["exit_price"]) * df["lot_size"]
        net = net - extra.to_numpy()

    is_mask = df["entry_date"] < oos_from
    survives, e_all, e_trim = _tail_dependence_guard(net)
    p5, p_le0 = _bootstrap(net)

    print(
        f"\n=== {label} ===  "
        f"({len(df)} trades, {df['entry_date'].min()}..{df['entry_date'].max()})"
    )
    print("  ALL   ", _seg(net))
    print("  IS    ", _seg(net[is_mask.to_numpy()]))
    print("  OOS   ", _seg(net[~is_mask.to_numpy()]))
    print(f"  tail-dependence guard: {'PASS' if survives else 'FAIL'}  "
          f"(E_all={e_all:.1f}, E_excl_top2={e_trim:.1f})")
    print(f"  bootstrap: 5th-pctile mean={p5:.1f}  P(mean<=0)={p_le0:.3f}")
    by_reason = df.groupby("exit_reason")["net_pnl"].agg(["count", "mean"]).round(1)
    print("  by exit reason:", by_reason.to_dict("index"))
    by_weekday = (
        df.assign(wd=df["entry_time"].dt.day_name())
        .groupby("wd")["net_pnl"].agg(["count", "mean"]).round(1)
    )
    print("  by weekday:    ", by_weekday.to_dict("index"))
    if "trigger_type" in df.columns:
        by_trigger = df.groupby("trigger_type")["net_pnl"].agg(["count", "mean"]).round(1)
        print("  by trigger:    ", by_trigger.to_dict("index"))

    oos_net = net[~is_mask.to_numpy()]
    is_net = net[is_mask.to_numpy()]
    return {
        "label": label, "n": len(df), "win_pct": round(100 * (net > 0).mean(), 1),
        "expectancy": round(float(net.mean()), 1), "total": round(float(net.sum()), 1),
        "pf": round(_pf(net), 2), "tail_guard": survives,
        "boot_p5": round(p5, 1), "boot_p_le0": round(p_le0, 3),
        "oos_expectancy": round(float(oos_net.mean()), 1) if (~is_mask).any() else float("nan"),
        "is_expectancy": round(float(is_net.mean()), 1) if is_mask.any() else float("nan"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--dir", type=Path, default=None)
    ap.add_argument("--glob", default="*.csv")
    ap.add_argument("--oos-from", type=date.fromisoformat, default=date(2026, 4, 1))
    ap.add_argument("--slippage-extra-pct", type=float, default=0.0,
                     help="extra adverse %% on top of the fill model's own baked-in slippage")
    ap.add_argument("--summary-csv", type=Path, default=None)
    args = ap.parse_args()

    rows: list[dict] = []
    if args.csv:
        df = load(args.csv)
        rows.append(report(df, args.csv.stem, args.oos_from, args.slippage_extra_pct))
    elif args.dir:
        for p in sorted(args.dir.glob(args.glob)):
            df = load(p)
            rows.append(report(df, p.stem, args.oos_from, args.slippage_extra_pct))
    else:
        raise SystemExit("pass --csv or --dir")

    if args.summary_csv and rows:
        pd.DataFrame(rows).to_csv(args.summary_csv, index=False)
        print(f"\nwrote summary -> {args.summary_csv}")


if __name__ == "__main__":
    main()
