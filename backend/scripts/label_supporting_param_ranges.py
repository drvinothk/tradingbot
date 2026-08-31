"""Range-labels the supporting/diagnostic parameters (VIX, ATR, PCR,
contract_oi) already recorded on each strategy's backtest trade CSV
(`run_backtest.py`'s `vix_entry`/`vix_exit`/etc columns — see that script's
module docstring: "diagnostic-only barometers ... never fed into any
strategy's own entry/exit condition").

This is a **retrospective** analysis tool, not a live trading rule. It
answers: "given the value this parameter actually had at entry/exit, which
of 3 bands did it fall into, and does that line up with whether the trade
made money?" — so a strategy's own real entry/exit logic can be judged
against these barometers after the fact.

**Design decisions (2026-08-24, explicit user instructions)**:

- **VIX / ATR** are volatility-*magnitude* measures with no inherent CE/PE
  direction, so they get a plain Low/Mid/High band only — never a
  CE/PE-preferred label.
- **PCR** (put OI / call OI, chain-wide — confirmed via `_pcr_at` in
  `run_backtest.py`, this is OI-based PCR only, volume-based PCR is never
  mixed in per the user's own PCR-instructions doc) is the one parameter
  with a real directional convention: low PCR (relatively more call
  activity) reads bullish/CE-leaning, high PCR (relatively more put
  activity) reads bearish/PE-leaning, middle is flat. Per the user's PCR
  instructions ("Avoid universal rules such as PCR < 0.7 -> buy CE"), the
  three bands are **not** fixed cutoffs — they're tertiles computed from
  this dataset's own pooled entry-time PCR distribution ("use extreme PCR
  values only relative to the instrument's own history").
- **contract_oi** is the *traded contract's own* OI (liquidity/build-up of
  the specific strike already picked), not a chain-wide call-vs-put signal
  — so instead of binning a static level, it's classified by entry->exit
  %% change: building / flat / unwinding.
- VIX/ATR/PCR bands are derived once from pooled **entry**-time values
  across every strategy, then the exact same cut points are reused to
  label the **exit** value too — so entry vs exit for the same trade is
  directly comparable (did the regime shift during the trade?), and bands
  stay consistent across strategies/files rather than each file getting
  its own private thresholds.

Run: `python scripts/label_supporting_param_ranges.py` (no DB/broker
needed — pure CSV in, CSV out.) Writes the new columns back onto each of
the 5 canonical `*_NIFTY_trades.csv` files in place (not the `_BASELINE_*`
snapshots or `_smoke_*` files) and writes a pooled + per-strategy summary
to `supporting_param_range_summary.csv` in the same directory.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "historical" / "backtest_reports"

# Canonical per-strategy trade files (excludes _BASELINE_* snapshots, which
# were confirmed byte-identical to these on 2026-08-24, and _smoke_* files,
# which are throwaway fast/slow-path comparison runs, not real results).
TRADE_FILES = [
    "orb_NIFTY_trades.csv",
    "vwap_pullback_NIFTY_trades.csv",
    "ema_micro_pullback_NIFTY_trades.csv",
    "oi_volume_confirmed_NIFTY_trades.csv",
    "liquidity_sweep_reversal_NIFTY_trades.csv",
]

OI_FLAT_BAND_PCT = 5.0  # |entry->exit change| below this % counts as "flat", not building/unwinding

NEW_COLUMNS = [
    "vix_entry_band", "vix_exit_band",
    "atr_entry_band", "atr_exit_band",
    "pcr_entry_range", "pcr_exit_range",
    "option_type", "pcr_entry_alignment",
    "oi_change_pct", "oi_change_signal",
]


def _to_float(raw: str) -> float | None:
    if raw is None or raw == "":
        return None
    return float(raw)


def _tertile_cuts(values: list[float]) -> tuple[float, float]:
    """Returns (low_cut, high_cut) such that roughly a third of `values`
    fall below low_cut, a third above high_cut, a third between."""
    if len(values) < 3:
        # Too few points for a meaningful tertile split; fall back to the
        # single value repeated so everything lands in the middle band.
        v = values[0] if values else 0.0
        return v, v
    lo, hi = statistics.quantiles(values, n=3, method="inclusive")
    return lo, hi


def _low_mid_high(value: float | None, low_cut: float, high_cut: float) -> str:
    if value is None:
        return ""
    if value <= low_cut:
        return "Low"
    if value >= high_cut:
        return "High"
    return "Mid"


def _pcr_range(value: float | None, low_cut: float, high_cut: float) -> str:
    if value is None:
        return ""
    if value <= low_cut:
        return "CE_preferred"
    if value >= high_cut:
        return "PE_preferred"
    return "Flat"


def _option_type(symbol: str) -> str:
    if symbol.endswith("CE"):
        return "CE"
    if symbol.endswith("PE"):
        return "PE"
    return ""


def _alignment(option_type: str, pcr_range: str) -> str:
    if not option_type or not pcr_range:
        return ""
    if pcr_range == "Flat":
        return "N/A"
    expected = "CE" if pcr_range == "CE_preferred" else "PE"
    return "Aligned" if option_type == expected else "Contrarian"


def _oi_change(entry_oi: float | None, exit_oi: float | None) -> tuple[str, str]:
    if entry_oi is None or exit_oi is None or entry_oi == 0:
        return "", ""
    pct = (exit_oi - entry_oi) / entry_oi * 100.0
    if pct >= OI_FLAT_BAND_PCT:
        signal = "OI_Building"
    elif pct <= -OI_FLAT_BAND_PCT:
        signal = "OI_Unwinding"
    else:
        signal = "OI_Flat"
    return f"{pct:.2f}", signal


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    per_file_rows: dict[str, list[dict[str, str]]] = {}
    all_rows: list[dict[str, str]] = []
    strategy_of: dict[int, str] = {}

    for fname in TRADE_FILES:
        path = REPORTS_DIR / fname
        if not path.exists():
            print(f"  (skip, not found) {fname}")
            continue
        rows = _read_rows(path)
        per_file_rows[fname] = rows
        strategy = fname.replace("_NIFTY_trades.csv", "")
        for row in rows:
            strategy_of[id(row)] = strategy
            all_rows.append(row)

    vix_pool = [v for r in all_rows if (v := _to_float(r.get("vix_entry", ""))) is not None]
    atr_pool = [v for r in all_rows if (v := _to_float(r.get("atr_entry", ""))) is not None]
    pcr_pool = [v for r in all_rows if (v := _to_float(r.get("pcr_entry", ""))) is not None]

    vix_low, vix_high = _tertile_cuts(vix_pool)
    atr_low, atr_high = _tertile_cuts(atr_pool)
    pcr_low, pcr_high = _tertile_cuts(pcr_pool)

    print("Pooled tertile cut points (from entry-time values, all strategies):")
    print(f"  VIX : Low <= {vix_low:.3f} < Mid < {vix_high:.3f} <= High   (n={len(vix_pool)})")
    print(f"  ATR : Low <= {atr_low:.3f} < Mid < {atr_high:.3f} <= High   (n={len(atr_pool)})")
    print(
        f"  PCR : CE_preferred <= {pcr_low:.3f} < Flat < {pcr_high:.3f} "
        f"<= PE_preferred   (n={len(pcr_pool)})"
    )
    print()

    for row in all_rows:
        vix_e = _to_float(row.get("vix_entry", ""))
        vix_x = _to_float(row.get("vix_exit", ""))
        atr_e = _to_float(row.get("atr_entry", ""))
        atr_x = _to_float(row.get("atr_exit", ""))
        pcr_e = _to_float(row.get("pcr_entry", ""))
        pcr_x = _to_float(row.get("pcr_exit", ""))
        oi_e = _to_float(row.get("contract_oi_entry", ""))
        oi_x = _to_float(row.get("contract_oi_exit", ""))

        row["vix_entry_band"] = _low_mid_high(vix_e, vix_low, vix_high)
        row["vix_exit_band"] = _low_mid_high(vix_x, vix_low, vix_high)
        row["atr_entry_band"] = _low_mid_high(atr_e, atr_low, atr_high)
        row["atr_exit_band"] = _low_mid_high(atr_x, atr_low, atr_high)
        row["pcr_entry_range"] = _pcr_range(pcr_e, pcr_low, pcr_high)
        row["pcr_exit_range"] = _pcr_range(pcr_x, pcr_low, pcr_high)

        opt_type = _option_type(row.get("symbol", ""))
        row["option_type"] = opt_type
        row["pcr_entry_alignment"] = _alignment(opt_type, row["pcr_entry_range"])

        oi_pct, oi_signal = _oi_change(oi_e, oi_x)
        row["oi_change_pct"] = oi_pct
        row["oi_change_signal"] = oi_signal

    # Write labeled columns back onto each source file.
    for fname, rows in per_file_rows.items():
        path = REPORTS_DIR / fname
        fieldnames = list(rows[0].keys()) if rows else []
        _write_rows(path, fieldnames, rows)
        print(f"  labeled -> {fname} ({len(rows)} trades)")

    _write_summary(all_rows, strategy_of)


def _write_summary(all_rows: list[dict[str, str]], strategy_of: dict[int, str]) -> None:
    def is_win(row: dict[str, str]) -> bool:
        pnl = _to_float(row.get("pnl", ""))
        return pnl is not None and pnl > 0

    def pnl_of(row: dict[str, str]) -> float:
        return _to_float(row.get("pnl", "")) or 0.0

    def summarize(rows: list[dict[str, str]], key: str) -> list[tuple[str, int, float, float]]:
        groups: dict[str, list[dict[str, str]]] = {}
        for r in rows:
            label = r.get(key, "")
            if not label:
                continue
            groups.setdefault(label, []).append(r)
        out = []
        for label, grp in groups.items():
            wins = sum(1 for r in grp if is_win(r))
            win_rate = wins / len(grp) * 100.0
            total_pnl = sum(pnl_of(r) for r in grp)
            out.append((label, len(grp), win_rate, total_pnl))
        return sorted(out, key=lambda t: t[0])

    lines: list[str] = []

    def add_section(title: str, key: str, rows: list[dict[str, str]]) -> None:
        lines.append(f"\n=== {title} (pooled, all strategies) ===")
        lines.append(f"{'range/band':<16}{'trades':>8}{'win_rate%':>12}{'total_pnl':>14}")
        for label, n, win_rate, total_pnl in summarize(rows, key):
            lines.append(f"{label:<16}{n:>8}{win_rate:>11.1f}%{total_pnl:>14.2f}")

    add_section("VIX entry band", "vix_entry_band", all_rows)
    add_section("ATR entry band", "atr_entry_band", all_rows)
    add_section("PCR entry range", "pcr_entry_range", all_rows)
    add_section(
        "PCR entry alignment (actual CE/PE vs PCR-recommended side)",
        "pcr_entry_alignment",
        all_rows,
    )
    add_section("Contract OI entry->exit change", "oi_change_signal", all_rows)

    lines.append("\n=== Per-strategy trade counts ===")
    per_strategy: dict[str, list[dict[str, str]]] = {}
    for r in all_rows:
        per_strategy.setdefault(strategy_of[id(r)], []).append(r)
    for strategy, rows in sorted(per_strategy.items()):
        wins = sum(1 for r in rows if is_win(r))
        lines.append(f"  {strategy:<28} {len(rows):>3} trades, {wins}/{len(rows)} wins")

    summary_text = "\n".join(lines)
    print(summary_text)

    out_path = REPORTS_DIR / "supporting_param_range_summary.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["parameter", "range_or_band", "trades", "win_rate_pct", "total_pnl"])
        sections = [
            ("vix_entry_band", "vix_entry_band"),
            ("atr_entry_band", "atr_entry_band"),
            ("pcr_entry_range", "pcr_entry_range"),
            ("pcr_entry_alignment", "pcr_entry_alignment"),
            ("oi_change_signal", "oi_change_signal"),
        ]
        for param_name, key in sections:
            for label, n, win_rate, total_pnl in summarize(all_rows, key):
                writer.writerow([param_name, label, n, f"{win_rate:.1f}", f"{total_pnl:.2f}"])

    print(f"\nSummary written -> {out_path}")


if __name__ == "__main__":
    main()
