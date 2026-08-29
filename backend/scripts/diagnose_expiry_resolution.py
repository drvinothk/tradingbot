"""Part-0 gate for backtest sweep #3: what expiry / DTE does each backtested
trade actually sit on?

`run_backtest.py --all-expiries` iterates one expiry DIRECTORY at a time and
replays that directory's entire multi-day data window. Because real NIFTY
weeklies are listed ~2 weeks before their own expiry, that window opens early,
and ORB (firing ~once per directory) tends to trigger early in it -> trades
skew to high days-to-expiry. This script quantifies that from the sweep-#2
trade CSVs, so we know whether the `f_range_tight` edge is being measured on
roughly the instrument a live near-week ORB would trade, or something further
out.

Read-only. No DB, no backtest. Run under ~/an_venv on the e4 VM:
    ~/an_venv/bin/python scripts/diagnose_expiry_resolution.py \\
        --dir data/historical/backtest_reports/refined_sweep_20260828T072024Z \\
        --configs ref_orb_baseline,g_ce_only,f_range_wide,r_maxloss_2000
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

# {UND}{yy}{mm}{dd}{strike}{CE|PE} -- TrueData / Shoonya real option symbol.
_SYM_RE = re.compile(r"^([A-Z]+?)(\d{2})(\d{2})(\d{2})(\d+)(CE|PE)$")


def _symbol_expiry(sym: str) -> pd.Timestamp | None:
    m = _SYM_RE.match(str(sym))
    if not m:
        return None
    _und, yy, mm, dd, _strike, _ot = m.groups()
    try:
        return pd.Timestamp(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None


def _load(path: Path, config: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    et = pd.to_datetime(df["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["config"] = config
    df["entry_date"] = et.dt.normalize().dt.tz_localize(None)
    df["sym_expiry"] = df["symbol"].map(_symbol_expiry)
    df["dte"] = (df["sym_expiry"] - df["entry_date"]).dt.days
    df["exp_dow"] = df["sym_expiry"].dt.day_name()
    return df[["config", "symbol", "entry_date", "sym_expiry", "dte", "exp_dow"]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--configs", required=True, help="comma-separated config names")
    args = ap.parse_args()

    names = [c.strip() for c in args.configs.split(",") if c.strip()]
    frames = []
    for n in names:
        p = args.dir / f"{n}_current.csv"
        if not p.exists():
            print(f"  MISSING: {p.name}")
            continue
        frames.append(_load(p, n))
    if not frames:
        raise SystemExit("no config CSVs loaded")
    allrows = pd.concat(frames, ignore_index=True)

    unparsed = allrows[allrows["sym_expiry"].isna()]
    print(f"total rows: {len(allrows)}   unparsed symbols: {len(unparsed)}")
    if len(unparsed):
        print("  e.g.", unparsed["symbol"].head(5).tolist())
    print()

    print("=== DTE histogram (calendar days entry_date -> symbol expiry), pooled ===")
    dte = allrows["dte"].dropna().astype(int)
    bins = [(-999, 0), (1, 1), (2, 3), (4, 6), (7, 9), (10, 13), (14, 20), (21, 40), (41, 999)]
    for lo, hi in bins:
        m = dte[(dte >= lo) & (dte <= hi)]
        if len(m):
            if lo == hi:
                label = f"{lo}"
            elif lo == -999:
                label = "<=0"
            elif hi == 999:
                label = f"{lo}+"
            else:
                label = f"{lo}-{hi}"
            print(f"  {label:>8}  n={len(m):>4}  {'#' * len(m)}")
    print(f"  DTE  min={dte.min()}  median={dte.median():.0f}  "
          f"mean={dte.mean():.1f}  max={dte.max()}")
    print()

    print("=== expiry weekday split (NIFTY weekly = Tuesday for the whole archive) ===")
    dow = allrows["exp_dow"].value_counts()
    for d, c in dow.items():
        flag = "  <- weekly" if d == "Tuesday" else ("  <- NOT a weekly expiry" if c else "")
        print(f"  {d:<10} n={c:>4}{flag}")
    n_tue = int(dow.get("Tuesday", 0))
    print(f"  weekly (Tue) share: {n_tue}/{len(allrows)} = {n_tue / len(allrows) * 100:.0f}%")
    print()

    print("=== per-config summary ===")
    for n, g in allrows.groupby("config"):
        d = g["dte"].dropna().astype(int)
        wk = (g["exp_dow"] == "Tuesday").mean() * 100
        print(f"  {n:<22} n={len(g):>3}  "
              f"DTE min/med/max={d.min():>2}/{d.median():>4.0f}/{d.max():>3}  "
              f"weekly={wk:>3.0f}%")
    print()

    print("=== cross-directory duplication: same entry_date, different symbol expiry ===")
    dup = (
        allrows.dropna(subset=["sym_expiry"])
        .groupby("entry_date")["sym_expiry"]
        .nunique()
    )
    multi = dup[dup > 1]
    if multi.empty:
        print("  none -- every traded calendar day maps to a single expiry across these configs")
    else:
        print(f"  {len(multi)} calendar day(s) traded against >1 distinct expiry:")
        for d, k in multi.items():
            rows = allrows[(allrows["entry_date"] == d)][["config", "symbol", "sym_expiry", "dte"]]
            print(f"    {d.date()}  ({k} distinct expiries)")
            for _, r in rows.iterrows():
                print(f"       {r['config']:<20} {r['symbol']:<22} "
                      f"exp={r['sym_expiry'].date()} dte={r['dte']}")


if __name__ == "__main__":
    main()
