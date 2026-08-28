"""Diagnostic-parameter analysis of the conviction-sweep trade CSVs.

Answers: do the *recorded-but-not-gated* diagnostics (PCR, VIX, ATR,
contract OI, and their entry->exit deltas) plus day-of-week carry any
edge — a win-rate / expectancy tilt, a losing-streak signature, or an
extreme-loss signature — that a future gate could exploit?

No new backtest. Reads <name>_current.csv files, pools the ORB-family
signals (dedup by symbol+entry_time), analyses the ATR-breakout family
separately, and cuts everything by day-of-week with an optional
drop-expiry-day (Tuesday, the NIFTY weekly expiry) recut.

Run under ~/an_venv (pandas/numpy):
    ~/an_venv/bin/python scripts/analyze_trade_diagnostics.py \\
        --dir data/historical/backtest_reports/conviction_sweep \\
        --index-csv data/historical/underlyings/NIFTY_alice_index_1min.csv
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd

FLAT_COST_PER_LOT = 40.0
PROPORTIONAL_RATE = 0.0004
STT_RATE = 0.001
SLIP_PCT = 0.005

ORB_FAMILY = [
    "orb_baseline", "orbc_htf", "orbc_atr", "orbc_htf_atr", "orbc_vix20",
    "orbc_htf_atr_r2", "orbc_wide_nogates", "orbc_wide_htf_atr",
]
ATR_FAMILY = ["atrb_baseline", "atrb_r25", "atrb_lb40"]

_SYM_RE = re.compile(r"^NIFTY(\d{2})(\d{2})(\d{2})(\d+)(CE|PE)$")


def _net_per_lot(r: pd.Series) -> float:
    e, x, ls = r["entry_price"], r["exit_price"], r["lot_size"]
    cost = (
        FLAT_COST_PER_LOT
        + PROPORTIONAL_RATE * (e + x) * ls
        + STT_RATE * x * ls
        + SLIP_PCT * (e + x) * ls
    )
    return (r["pnl"] / max(r["qty_lots"], 1)) - cost


def _load_family(dir_: Path, names: list[str]) -> pd.DataFrame:
    frames = []
    for n in names:
        p = dir_ / f"{n}_current.csv"
        if not p.exists():
            continue
        df = pd.read_csv(p)
        df["config"] = n
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.sort_values("entry_time").drop_duplicates(["symbol", "entry_time"], keep="first")
    df["net"] = df.apply(_net_per_lot, axis=1)
    df["win"] = df["net"] > 0
    df["entry_date"] = df["entry_time"].dt.date
    df["dow"] = df["entry_time"].dt.day_name()
    df["hold_min"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 60
    df["opt"] = df["symbol"].str.extract(r"(CE|PE)$")[0]

    def _expiry(sym: str):
        m = _SYM_RE.match(sym)
        if not m:
            return pd.NaT
        yy, mm, dd, *_ = m.groups()
        return pd.Timestamp(2000 + int(yy), int(mm), int(dd)).date()

    df["expiry_date"] = df["symbol"].map(_expiry)
    df["is_expiry_day"] = df["entry_date"] == df["expiry_date"]
    df["pcr_delta"] = df["pcr_exit"] - df["pcr_entry"]
    df["vix_delta"] = df["vix_exit"] - df["vix_entry"]
    df["atr_delta"] = df["atr_exit"] - df["atr_entry"]
    df["oi_delta_pct"] = (df["contract_oi_exit"] - df["contract_oi_entry"]) / df[
        "contract_oi_entry"
    ].replace(0, np.nan)
    return df.reset_index(drop=True)


def _bucket_table(df: pd.DataFrame, col: str, q: int = 3) -> str:
    s = df[col].dropna()
    if s.nunique() < q:
        return f"  {col}: too few distinct values\n"
    try:
        df = df.copy()
        df["_b"] = pd.qcut(df[col], q, duplicates="drop")
    except ValueError:
        return f"  {col}: qcut failed\n"
    g = df.groupby("_b", observed=True).agg(
        n=("net", "size"), win=("win", "mean"), avg_net=("net", "mean"), tot_net=("net", "sum")
    )
    out = [f"  {col} (terciles):"]
    for b, r in g.iterrows():
        out.append(
            f"    {str(b):<28} n={r['n']:>3.0f}  win={r['win']*100:>5.1f}%  "
            f"avgNet={r['avg_net']:>8.0f}  totNet={r['tot_net']:>9.0f}"
        )
    return "\n".join(out) + "\n"


def _corr_block(df: pd.DataFrame, cols: list[str]) -> str:
    out = ["  correlations (r vs net PnL / r vs win 0-1, n):"]
    for c in cols:
        sub = df[[c, "net", "win"]].dropna()
        if len(sub) < 8:
            out.append(f"    {c:<18} (n<8)")
            continue
        r_pnl = np.corrcoef(sub[c], sub["net"])[0, 1]
        r_win = np.corrcoef(sub[c], sub["win"].astype(float))[0, 1]
        out.append(f"    {c:<18} r_pnl={r_pnl:+.3f}  r_win={r_win:+.3f}  n={len(sub)}")
    return "\n".join(out) + "\n"


def _streaks(df: pd.DataFrame) -> str:
    d = df.sort_values("entry_time").reset_index(drop=True)
    wins = d["win"].to_numpy()
    # position within current same-outcome run (1 = first of a run)
    run_pos = np.ones(len(wins), dtype=int)
    for i in range(1, len(wins)):
        if wins[i] == wins[i - 1]:
            run_pos[i] = run_pos[i - 1] + 1
    d["run_pos"] = run_pos
    d["in_loss_streak2"] = (~d["win"]) & (d["run_pos"] >= 2)
    d["first_loss"] = (~d["win"]) & (d["run_pos"] == 1)

    def _mean(sub: pd.DataFrame) -> str:
        return (
            f"n={len(sub):>3}  pcr_e={sub['pcr_entry'].mean():.2f}  "
            f"vix_e={sub['vix_entry'].mean():.2f}  atr_e={sub['atr_entry'].mean():.2f}  "
            f"pcrΔ={sub['pcr_delta'].mean():+.3f}  vixΔ={sub['vix_delta'].mean():+.3f}  "
            f"hold={sub['hold_min'].mean():.0f}m  %PE={ (sub['opt']=='PE').mean()*100:.0f}"
        )

    longest = 0
    cur = 0
    for w in wins:
        cur = cur + 1 if not w else 0
        longest = max(longest, cur)
    out = [
        f"  longest losing streak: {longest} trades",
        f"  wins .............. {_mean(d[d['win']])}",
        f"  losses (all) ...... {_mean(d[~d['win']])}",
        f"  1st loss of a run . {_mean(d[d['first_loss']])}",
        f"  2nd+ loss in a run  {_mean(d[d['in_loss_streak2']])}",
    ]
    return "\n".join(out) + "\n"


def _extremes(df: pd.DataFrame, k: int = 8) -> str:
    d = df.sort_values("net")
    cols = ["entry_date", "dow", "opt", "exit_reason", "net", "pcr_entry", "vix_entry",
            "atr_entry", "pcr_delta", "hold_min", "is_expiry_day"]
    out = [f"  WORST {k}:"]
    out += ["    " + "  ".join(f"{c}={v}" for c, v in row.items())
            for row in d.head(k)[cols].round(2).to_dict("records")]
    out += [f"  BEST {k}:"]
    out += ["    " + "  ".join(f"{c}={v}" for c, v in row.items())
            for row in d.tail(k)[cols].round(2).to_dict("records")]
    return "\n".join(out) + "\n"


def _dow_table(df: pd.DataFrame, label: str) -> str:
    g = df.groupby("dow", observed=True).agg(
        n=("net", "size"), win=("win", "mean"), avg_net=("net", "mean"), tot_net=("net", "sum")
    ).reindex(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]).dropna(how="all")
    out = [f"  {label}:"]
    for d_, r in g.iterrows():
        out.append(
            f"    {d_:<10} n={r['n']:>3.0f}  win={r['win']*100:>5.1f}%  "
            f"avgNet={r['avg_net']:>8.0f}  totNet={r['tot_net']:>9.0f}"
        )
    tot = df["net"]
    out.append(f"    {'TOTAL':<10} n={len(df):>3}  win={df['win'].mean()*100:>5.1f}%  "
               f"avgNet={tot.mean():>8.0f}  totNet={tot.sum():>9.0f}")
    return "\n".join(out) + "\n"


DIAG_COLS = ["pcr_entry", "vix_entry", "atr_entry", "contract_oi_entry",
             "pcr_delta", "vix_delta", "atr_delta", "hold_min"]


def _analyse(df: pd.DataFrame, title: str) -> None:
    print("=" * 96)
    print(f"{title}   (pooled unique trades: {len(df)})")
    print("=" * 96)
    print(_dow_table(df, "By day-of-week (ALL days)"))
    print(_dow_table(df[~df["is_expiry_day"]], "By day-of-week (EXPIRY DAY / Tuesday removed)"))

    for scope_label, sub in (("ALL DAYS", df), ("EX-EXPIRY-DAY", df[~df["is_expiry_day"]])):
        print(f"--- diagnostics — {scope_label} (n={len(sub)}) ---")
        print(_corr_block(sub, DIAG_COLS))
        for c in ["pcr_entry", "vix_entry", "atr_entry", "pcr_delta"]:
            print(_bucket_table(sub, c))
        # option_type x pcr_entry (the "pcr_oi low + PE" tell)
        for ot in ["CE", "PE"]:
            o = sub[sub["opt"] == ot]
            if len(o) >= 6:
                lo = o[o["pcr_entry"] <= o["pcr_entry"].median()]
                hi = o[o["pcr_entry"] > o["pcr_entry"].median()]
                print(f"  {ot}: pcr<=med  n={len(lo):>2} win={lo['win'].mean()*100:4.0f}% "
                      f"avgNet={lo['net'].mean():>7.0f}   |   pcr>med  n={len(hi):>2} "
                      f"win={hi['win'].mean()*100:4.0f}% avgNet={hi['net'].mean():>7.0f}")
        print()
    print("--- winning vs losing streak signatures (ALL days) ---")
    print(_streaks(df))
    print("--- winning vs losing streak signatures (EX-EXPIRY-DAY) ---")
    print(_streaks(df[~df["is_expiry_day"]]))
    print("--- extreme trades (ALL days) ---")
    print(_extremes(df))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--index-csv", type=Path, default=None)
    args = ap.parse_args()

    orb = _load_family(args.dir, ORB_FAMILY)
    atr = _load_family(args.dir, ATR_FAMILY)

    print(f"cost model: flat {FLAT_COST_PER_LOT}/lot + {PROPORTIONAL_RATE:.4%} turnover "
          f"+ {STT_RATE:.3%} STT + {SLIP_PCT:.3%}/side slippage   (net, per 1 lot, INR)\n")
    if not orb.empty:
        _analyse(orb, "ORB FAMILY (orb + orb_conviction variants, deduped)")
    if not atr.empty:
        _analyse(atr, "ATR-BREAKOUT FAMILY (deduped)")


if __name__ == "__main__":
    main()
