#!/usr/bin/env python3
"""Walk-forward + robustness analysis for the Loren **futures** backtest
(`loren_backtest.py --futures`).

Mirrors `analyze_walkforward.py`'s stats (IS/OOS, two 6-month halves, 10k
bootstrap, slippage sensitivity) but with a real Indian-F&O **futures** cost
model instead of the option 0.5%/side model, plus the spec-§8 breakdowns
(by weekday / entry-hour / exit_reason / direction), the 60/20/20 calendar
split with explicit TRAIN->VALID->TEST deltas, and an optional risk-%
position-sizing overlay (spec §5).

Input CSV columns are `loren_backtest.py`'s `_TRADE_CSV_HEADER` (byte-identical
to `run_backtest.py`). Two option-only columns are repurposed in --futures
mode: `pcr_entry` carries the stop price, `pcr_exit` the entry->stop risk in
points. `symbol` is `NIFTYFUT` (no expiry-week sign test -- that check needs an
option symbol; see `analyze_walkforward.py` for the option version).

Usage:
    python scripts/analyze_loren_futures.py --csv out/f_base_current.csv
    python scripts/analyze_loren_futures.py --dir out/lorenfut_XXX \\
        --configs f_base,f_exit_comb,f_cap --split 0.6,0.8 --oos-from 2026-03-01
    # pre-2024-10 STT rate, custom capital for the sizing overlay:
    python scripts/analyze_loren_futures.py --csv f.csv --stt-rate 0.000125 \\
        --capital 1500000 --risk-pct 1.0
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# --- futures cost model defaults (per 1 lot, round trip; all --overridable) ---
BROKERAGE_RT = 40.0            # flat brokerage per lot, both legs combined
STT_RATE = 0.0002             # 0.02% sell-side notional (current NSE FUT; 0.000125 pre-2024-10)
EXCH_TXN_RATE = 0.0000188     # ~0.00188%/side NSE FUT, applied to full turnover
SEBI_RATE = 1e-6             # SEBI turnover fee ~Rs.10/crore, both sides
STAMP_RATE = 0.00002         # 0.002% buy-side notional
GST_RATE = 0.18             # on (brokerage + exch_txn + sebi)
TICK = 0.05
SLIP_TICKS = 1.0            # 1 tick each way
MIN_STOP_PTS = 2.0          # floor for the risk-% sizing overlay


def _round_trip_cost(entry: float, exit_: float, side: str, lot: float,
                     stt_rate: float, slip_ticks: float) -> float:
    if side == "BUY":
        notional_buy, notional_sell = entry * lot, exit_ * lot
    else:
        notional_buy, notional_sell = exit_ * lot, entry * lot
    turnover = notional_buy + notional_sell
    brokerage = BROKERAGE_RT
    exch = EXCH_TXN_RATE * turnover
    sebi = SEBI_RATE * turnover
    gst = GST_RATE * (brokerage + exch + sebi)
    stt = stt_rate * notional_sell
    stamp = STAMP_RATE * notional_buy
    slippage = slip_ticks * TICK * lot * 2.0
    return brokerage + exch + sebi + gst + stt + stamp + slippage


def _pf(p: np.ndarray) -> float:
    g = p[p > 0].sum()
    ll = -p[p <= 0].sum()
    return float("inf") if ll == 0 else g / ll


def _worst_streak(p: np.ndarray) -> int:
    worst = cur = 0
    for x in p:
        cur = cur + 1 if x <= 0 else 0
        worst = max(worst, cur)
    return worst


def _max_dd(p: np.ndarray) -> float:
    eq = np.cumsum(p)
    return float((np.maximum.accumulate(eq) - eq).max()) if len(eq) else 0.0


def _seg(p: np.ndarray) -> str:
    if len(p) == 0:
        return "n=0"
    return (f"n={len(p):>4} win={(p > 0).mean() * 100:>5.1f}% E={p.mean():>8.1f} "
            f"PF={_pf(p):>5.2f} tot={p.sum():>9.0f} maxDD={_max_dd(p):>8.0f} "
            f"wStreak={_worst_streak(p)}")


def _bootstrap(p: np.ndarray, draws: int = 10000, seed: int = 12345) -> tuple[float, float]:
    if len(p) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = p[rng.integers(0, len(p), size=(draws, len(p)))].mean(axis=1)
    return float(np.percentile(means, 5)), float((means <= 0).mean())


def _load(path: Path, stt_rate: float, slip_ticks: float) -> pd.DataFrame:
    df = pd.read_csv(path)
    et = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["entry_date"] = et.dt.date
    df["entry_hour"] = et.dt.hour
    df["dow"] = et.dt.day_name()
    df["ppl"] = df["pnl"] / df["qty_lots"].clip(lower=1)
    df["cost"] = df.apply(
        lambda r: _round_trip_cost(r["entry_price"], r["exit_price"], r["side"],
                                   r["lot_size"], stt_rate, slip_ticks), axis=1)
    df["net"] = df["ppl"] - df["cost"]
    df["risk_pts"] = pd.to_numeric(df["pcr_exit"], errors="coerce")
    return df


def _kpi_table(df: pd.DataFrame, split: tuple[float, float]) -> None:
    d0, d1 = df["entry_date"].min(), df["entry_date"].max()
    span = (d1 - d0).days
    tr_end = d0 + pd.Timedelta(days=int(span * split[0]))
    va_end = d0 + pd.Timedelta(days=int(span * split[1]))
    segs = {
        "ALL": df,
        "TRAIN(60)": df[df["entry_date"] <= tr_end],
        "VALID(20)": df[(df["entry_date"] > tr_end) & (df["entry_date"] <= va_end)],
        "TEST(20)": df[df["entry_date"] > va_end],
    }
    e = {}
    for name, seg in segs.items():
        p = seg["net"].to_numpy()
        print(f"  {name:10s} {_seg(p)}")
        e[name] = p.mean() if len(p) else float("nan")
    print(f"  delta  TRAIN->VALID E {e['VALID(20)'] - e['TRAIN(60)']:+.1f}   "
          f"VALID->TEST E {e['TEST(20)'] - e['VALID(20)']:+.1f}   "
          f"(small + stable = no overfit)")


def _breakdowns(df: pd.DataFrame) -> None:
    def g(col: str) -> dict:
        return (df.groupby(col)["net"].agg(n="count", win=lambda s: (s > 0).mean() * 100,
                                           avg="mean").round(1).to_dict("index"))
    print("  by weekday :", g("dow"))
    print("  by hour    :", g("entry_hour"))
    print("  by exit    :", g("exit_reason"))
    print("  by side    :", g("side"))


def _risk_overlay(df: pd.DataFrame, capital: float, risk_pct: float) -> None:
    rp = df["risk_pts"].to_numpy()
    if np.isnan(rp).all():
        print("  risk-% overlay: skipped (no risk_pts in CSV)")
        return
    lot = df["lot_size"].to_numpy()
    risk_rs = np.maximum(np.nan_to_num(rp, nan=MIN_STOP_PTS), MIN_STOP_PTS) * lot
    n_lots = np.floor((capital * risk_pct / 100.0) / risk_rs).clip(min=0)
    pnl_curve = df["net"].to_numpy() * n_lots
    eq = np.cumsum(pnl_curve)
    tot = eq[-1] if len(eq) else 0.0
    dd = _max_dd(pnl_curve)
    print(f"  risk-% overlay (capital {capital:,.0f}, {risk_pct}%/trade, "
          f"MIN_STOP {MIN_STOP_PTS}pt): total Rs.{tot:,.0f}  maxDD Rs.{dd:,.0f}  "
          f"return {100 * tot / capital:.1f}%  avg_lots {n_lots.mean():.1f}")


def _analyse(path: Path, name: str, args: argparse.Namespace,
             split: tuple[float, float]) -> None:
    if not path.exists():
        print(f"### {name}  MISSING ({path})\n")
        return
    df = _load(path, args.stt_rate, args.slippage_ticks)
    if df.empty:
        print(f"### {name}  0 trades\n")
        return
    p = df["net"].to_numpy()
    gross = df["ppl"].to_numpy()
    lot = float(df["lot_size"].iloc[0])
    print(f"### {name}   ({len(df)} trades)   "
          f"gross E Rs.{gross.mean():+.0f}/lot ({gross.mean() / lot:+.1f} pts)   "
          f"net E Rs.{p.mean():+.0f}/lot ({p.mean() / lot:+.1f} pts)")
    print("  (all Rs. figures are per 1 lot; E/tot/maxDD in Rs.)")
    _kpi_table(df, split)

    oos_from = args.oos_from
    if oos_from is None:
        d0, d1 = df["entry_date"].min(), df["entry_date"].max()
        oos_from = d0 + pd.Timedelta(days=int((d1 - d0).days * split[1]))
    isd = df[df["entry_date"] < oos_from]["net"].to_numpy()
    oos = df[df["entry_date"] >= oos_from]["net"].to_numpy()
    print(f"  IS  (< {oos_from})  {_seg(isd)}")
    print(f"  OOS (>= {oos_from})  {_seg(oos)}")

    d0, d1 = df["entry_date"].min(), df["entry_date"].max()
    mid = d0 + (d1 - d0) / 2
    h1 = df[df["entry_date"] <= mid]["net"].to_numpy()
    h2 = df[df["entry_date"] > mid]["net"].to_numpy()
    print(f"  H1 (<= {mid})  {_seg(h1)}")
    print(f"  H2 ( > {mid})  {_seg(h2)}")

    lo5, p_le0 = _bootstrap(p)
    print(f"  bootstrap (10k): 5th-pctile mean net = {lo5:>8.1f}   P(mean <= 0) = {p_le0:.3f}")

    cs = []
    for st in (0.5, 1.0, 2.0, 3.0):
        d2 = _load(path, args.stt_rate, st)
        cs.append(f"{st:g}t:{d2['net'].mean():>7.0f}")
    print(f"  slippage sensitivity (mean net/lot): {'  '.join(cs)}")

    _breakdowns(df)
    _risk_overlay(df, args.capital, args.risk_pct)
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path)
    src.add_argument("--dir", type=Path)
    ap.add_argument("--configs", default="")
    ap.add_argument("--glob-suffix", default="_current.csv")
    ap.add_argument("--split", default="0.6,0.8")
    ap.add_argument("--oos-from", type=date.fromisoformat, default=None,
                    help="default = the VALID/TEST boundary date")
    ap.add_argument("--stt-rate", type=float, default=STT_RATE)
    ap.add_argument("--slippage-ticks", type=float, default=SLIP_TICKS)
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    args = ap.parse_args()

    a, b = (float(x) for x in args.split.split(","))
    split = (a, b)

    print(f"futures cost model (per 1 lot RT): brokerage {BROKERAGE_RT} + "
          f"STT {args.stt_rate:.4%} sell + txn {EXCH_TXN_RATE:.5%} + stamp {STAMP_RATE:.3%} buy "
          f"+ GST 18% + slippage {args.slippage_ticks:g} tick/side\n")

    if args.csv:
        _analyse(args.csv, args.csv.stem, args, split)
        return
    names = [c.strip() for c in args.configs.split(",") if c.strip()]
    if not names:
        raise SystemExit("--dir requires --configs")
    for name in names:
        _analyse(args.dir / f"{name}{args.glob_suffix}", name, args, split)


if __name__ == "__main__":
    main()
