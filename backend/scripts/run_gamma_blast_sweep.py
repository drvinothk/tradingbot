#!/usr/bin/env python3
"""Phase 0/1 sweep driver for Gamma Blast — smoke baseline + single-axis
entry-conviction sweep (see chat plan: "Phase 1 (entry conviction)").

Each config is run across every discovered NIFTY expiry directory, in a
separate process (multiprocessing.Pool — cheap per-config, so this parallelizes
by CONFIG not by expiry-shard, unlike the ORB/Loren sweeps which needed
per-expiry sharding because each config was itself expensive).

Writes one raw trade CSV per config (`--out-dir/<name>.csv`) plus a single
ranked summary CSV (`--out-dir/summary.csv`) via `analyze_gamma_blast.py`'s
cost model + robustness checks.

Usage:
    python scripts/run_gamma_blast_sweep.py \
        --out-dir data/historical/backtest_reports/gamma_blast_p1 --phase 1
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
from datetime import date
from pathlib import Path

import analyze_gamma_blast as ag
import gamma_blast_backtest as gb
import pandas as pd

DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent
    / "data" / "historical" / "backtest_reports" / "gamma_blast"
)


def _baseline() -> dict:
    return {}  # Config() defaults == JSON defaults, system-cutoff-corrected


def phase0_configs() -> list[tuple[str, dict]]:
    return [("p0_baseline", _baseline())]


def phase1_configs() -> list[tuple[str, dict]]:
    b = _baseline
    cfgs: list[tuple[str, dict]] = [("p1_baseline", b())]

    # precondition threshold
    for thr in (0.4, 0.8):
        cfgs.append((f"p1_pcThr{thr}", {**b(), "precondition_threshold_pct": thr}))
    cfgs.append(("p1_pcOff", {**b(), "precondition_measure": "off"}))

    # precondition measure
    cfgs.append(("p1_pcDayRange", {**b(), "precondition_measure": "day_range"}))

    # recheck at trigger time
    cfgs.append(("p1_noRecheck", {**b(), "recheck_at_trigger_time": False}))

    # trigger type
    cfgs.append(("p1_trigEma", {**b(), "trigger_type": "ema_cross"}))

    # arm mode / threshold
    for g in (0.001, 0.004):
        cfgs.append((f"p1_gamma{g}", {**b(), "gamma_threshold": g}))
    for lo, hi in ((3, 40), (10, 80)):
        cfgs.append((f"p1_premBand{lo}_{hi}", {
            **b(), "arm_mode": "premium_band", "premium_band": [lo, hi]
        }))
    cfgs.append(("p1_armOff", {**b(), "arm_mode": "off"}))

    # strike distance
    cfgs.append(("p1_dist0", {**b(), "max_distance_points": 0}))
    cfgs.append(("p1_dist100_negctrl", {**b(), "max_distance_points": 100}))

    # volume confirm
    for mult in (None, 1.5, 3.0):
        cfgs.append((f"p1_volMult{mult}", {**b(), "volume_spike_mult": mult}))

    # entry window bounds
    for earliest in ("13:15", "14:15"):
        cfgs.append((f"p1_earliest{earliest.replace(':','')}", {**b(), "entry_earliest": earliest}))
    for latest in ("14:30", "14:45"):
        cfgs.append((f"p1_latest{latest.replace(':','')}", {**b(), "entry_latest": latest}))

    # system-cutoff reference: JSON-literal force_exit (NOT deployable live)
    cfgs.append(("p1_forceExit1520_JSONliteral_NOTDEPLOYABLE", {**b(), "force_exit_time": "15:20"}))

    return cfgs


def phase2_configs() -> list[tuple[str, dict]]:
    """Exit optimization, layered on the plain baseline -- Phase 1 found every
    entry-conviction axis either inert (arm/distance -- ATM-heuristic mode
    means max_distance never binds; gamma is essentially always above the
    0.001-0.004 threshold range near expiry) or an IS/OOS overfit trap
    (pcThr0.4, pcDayRange: positive IS, deeply negative OOS). The one real
    lead from Phase 1 is the exit MIX itself: hard_stop losses average
    -673/lot (12 of them) against momentum_stall wins averaging only
    +100..+185/lot (16-19 of them) -- badly asymmetric risk:reward, the
    opposite shape from `orb_conviction`'s own fix (which needed a LOOSER
    stop). Here the hypothesis is a TIGHTER stop and/or a slower-to-fire
    stall exit (let winners run further before calling it a stall).
    """
    b = _baseline
    cfgs: list[tuple[str, dict]] = [("p2_baseline", b())]

    for stop in (20, 25, 30, 40):
        cfgs.append((f"p2_hardStop{stop}", {**b(), "hard_stop_pct": stop}))

    for stall in (20, 40, 50):
        cfgs.append((f"p2_stallPct{stall}", {**b(), "momentum_stall_pct": stall}))

    for n in (2, 5):
        cfgs.append((f"p2_stallN{n}", {**b(), "momentum_stall_n_ticks": n}))

    # combined: tight stop + slower stall
    for stop in (20, 25, 30):
        for stall in (40, 50):
            cfgs.append((f"p2_combo_stop{stop}_stall{stall}", {
                **b(), "hard_stop_pct": stop, "momentum_stall_pct": stall,
            }))

    # fixed-mode alternative (no momentum_stall at all)
    for target in (100, 150, 200, 300):
        for stop in (20, 30, 40, 50):
            cfgs.append((f"p2_fixed_t{target}_s{stop}", {
                **b(), "exit_mode": "fixed", "fixed_target_pct": target, "fixed_stop_pct": stop,
            }))

    # force_exit_time -- system-realistic sweep (see module docstring)
    for fx in ("15:00", "15:05"):
        cfgs.append((f"p2_forceExit{fx.replace(':','')}", {**b(), "force_exit_time": fx}))

    # re-entry attempts
    for att in (1, 3):
        cfgs.append((f"p2_maxAttempts{att}", {**b(), "max_attempts_per_expiry": att}))

    return cfgs


def _run_one(args: tuple[str, dict, Path, list]) -> tuple[str, Path, int]:
    name, overrides, out_dir, expiries = args
    cfg = gb.Config.from_json(json.dumps(overrides))
    trades = gb.run_all(
        gb.DEFAULT_DATA_DIR, "options_1min_past", "NIFTY_alice_index_1min.csv", cfg, expiries
    )
    out_csv = out_dir / f"{name}.csv"
    gb.write_csv(trades, out_csv)
    return name, out_csv, len(trades)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--phase", type=int, choices=[0, 1, 2], default=1)
    ap.add_argument("--processes", type=int, default=max(1, (mp.cpu_count() or 2) - 1))
    ap.add_argument("--oos-from", type=date.fromisoformat, default=date(2026, 4, 1))
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    opt_base = gb.DEFAULT_DATA_DIR / "options_1min_past" / "NIFTY"
    expiries = gb._discover_expiries(opt_base)
    print(f"expiries discovered: {len(expiries)} ({expiries[0]}..{expiries[-1]})")

    configs = {0: phase0_configs, 1: phase1_configs, 2: phase2_configs}[args.phase]()
    tasks = [(name, overrides, args.out_dir, expiries) for name, overrides in configs]

    print(f"running {len(tasks)} configs across {len(expiries)} expiries "
          f"with {args.processes} processes...")
    with mp.Pool(processes=args.processes) as pool:
        results = pool.map(_run_one, tasks)

    summary_rows = []
    for name, out_csv, n_trades in results:
        print(f"  [{name}] {n_trades} raw trades -> {out_csv.name}")
        df = ag.load(out_csv)
        row = ag.report(df, name, args.oos_from)
        summary_rows.append(row)

    summary_df = pd.DataFrame(summary_rows).sort_values(
        "expectancy", ascending=False, na_position="last"
    )
    summary_path = args.out_dir / "summary.csv"
    summary_df.to_csv(summary_path, index=False)
    print("\n=== RANKED SUMMARY (by net expectancy/lot) ===")
    print(summary_df.to_string(index=False))
    print(f"\nwrote {summary_path}")


if __name__ == "__main__":
    main()
