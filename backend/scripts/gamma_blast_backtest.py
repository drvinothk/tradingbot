#!/usr/bin/env python3
"""Standalone Gamma Blast v2.1 backtest.

SELF-CONTAINED. Imports only pandas + numpy + stdlib `math`. Does NOT touch the
app pipeline, the DB, migrations, or `run_backtest.py` — same rationale as
`loren_backtest.py`: a brand-new signal with no representation in the
production `strategy_engine`, evaluated first as a pure backtest question.

Ported from `D:\\Documents\\Trading Bot_Oracle\\gamma_blast_config_v2_1.json`
(user-supplied spec). Full research/evaluation writeup: see chat + memory
`project_gamma_blast_research_2026_08_30` (data-availability audit — no
broker anywhere provides live Greeks; this script's `black_scholes_iv_greeks`
is the same calculation a live implementation would need to run itself).

**Trades ONLY on expiry day itself** (unlike ORB/Loren, which trade the whole
expiry week) — every expiry-day directory in `options_1min_past/NIFTY/<date>/`
IS the trading day; no multi-day warmup or classifier history needed at all,
which makes this strategy trivially parallelizable per-expiry (unlike Loren's
2000-bar lookback requirement).

**System-cutoff correction (deliberate deviation from the JSON)**: the JSON's
`force_exit_time` (15:15/15:20/15:25 sweep) and `entry_window.latest` (15:15)
sit at or past this system's own real EOD cutoff (`app.core.clock` /
`TradingSession.cutoff_time` default **15:09 IST** — see
`BACKTEST_TIME_CONVENTIONS.md`). Anything later would never actually fire
live; PositionManager force-squares-off at 15:09 regardless of what a
strategy config says. `force_exit_time` here defaults to `"15:09"` and the
JSON's own values are only reachable via explicit `--config` override, kept
for A/B reference, never as the recommended config.

**Greeks**: nobody (Shoonya, Alice Blue) returns IV/delta/gamma anywhere —
confirmed by reading both brokers' normalizer/tick code. `black_scholes_iv_greeks`
below is the actual calculation, exactly per spec (`iv_method:
black_scholes_inversion`, `time_convention: trading_year_252d_375min`,
`time_floor_minutes: 30`, `risk_free_rate: 0.065`, `iv_clip_pct: [1,150]`).
Newton-Raphson with a bisection fallback; returns `None` (skip_candidate) on
non-convergence or a premium outside the no-arbitrage band — never a guessed
value. This is the same function a live strategy would call each cycle; there
is no vendor shortcut to build here, on this broker set.

**Lot size / STT are date-dependent across the ~1yr dataset** (Sep'25-Sep'26
crosses both the 2025-12-30/2026-01-06 lot-size boundary and the
2026-04-01 STT hike) — `_lot_size_for_expiry` / `_stt_rate_for_date` apply the
JSON's own tables per-trade, not a single constant for the whole run.

Usage
-----
    # single expiry smoke test
    python scripts/gamma_blast_backtest.py --expiry 2026-08-18 \\
        --out-csv /tmp/gb_smoke.csv

    # full year, one config
    python scripts/gamma_blast_backtest.py --all-expiries \\
        --config '{"precondition_threshold_pct":0.6}' \\
        --out-csv out/gb_baseline.csv
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
STRIKE_STEP = 50
SESSION_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)          # real NSE close -- what T (time-to-expiry) is measured to
SYSTEM_EOD_CUTOFF = time(15, 9)      # this system's own real force-square-off (PositionManager)
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "historical"
RISK_FREE_RATE = 0.065
TRADING_MINUTES_PER_YEAR = 252 * 375

_TRADE_CSV_HEADER = [
    "symbol", "side", "leg", "entry_time", "entry_price", "exit_time", "exit_price",
    "exit_reason", "qty_lots", "lot_size", "pnl",
    "vix_entry", "vix_exit", "atr_entry", "atr_exit",
    "pcr_entry", "pcr_exit", "contract_oi_entry", "contract_oi_exit",
]
# gamma_blast-specific extra columns appended after the shared header (ignored
# by analyze_walkforward.py, read by analyze_gamma_blast.py):
_EXTRA_COLS = [
    "expiry", "precondition_measure", "precondition_move_pct", "trigger_type",
    "arm_mode", "iv_at_entry", "delta_at_entry", "gamma_at_entry",
    "attempt_no", "rejected_candidates_count_same_day",
]


# =========================================================================
# Config
# =========================================================================
@dataclass
class Config:
    # precondition (range compression, measured off 09:15 spot open)
    precondition_measure: str = "net_move"          # "net_move" | "day_range" | "off"
    precondition_threshold_pct: float | None = 0.6
    recheck_at_trigger_time: bool = True

    # entry window (IST "HH:MM")
    entry_earliest: str = "13:45"
    entry_latest: str = "15:00"                       # JSON default 15:15; capped, see module docstring

    # strike selection
    max_distance_points: float = 50.0                 # 100 = negative control, never "best"

    # arm condition
    arm_mode: str = "gamma_threshold"                 # "gamma_threshold" | "premium_band" | "off"
    gamma_threshold: float = 0.002
    premium_band: tuple[float, float] = (5.0, 60.0)

    # trigger
    trigger_type: str = "morning_range_break"         # "morning_range_break" | "ema_cross"
    ema_fast: int = 9
    ema_slow: int = 21

    # volume confirm (option's OWN volume column; None/0 = off)
    volume_spike_mult: float | None = 2.0
    volume_spike_lookback_bars: int = 20

    # exit
    exit_mode: str = "momentum_stall"                 # "momentum_stall" | "fixed"
    momentum_stall_n_ticks: int = 3
    momentum_stall_pct: float = 30.0
    fixed_target_pct: float = 200.0
    fixed_stop_pct: float = 50.0
    hard_stop_pct: float = 50.0                       # always-on backstop regardless of exit_mode
    hard_target_pct: float = 200.0                    # always-on backstop regardless of exit_mode
    force_exit_time: str = "15:09"                    # system-realistic default; see module docstring

    # re-entry
    max_attempts_per_expiry: int = 2                  # total entries = 1 + this
    cooldown_minutes_after_exit: int = 3               # "cooldown_bars_after_exit" on 1-min spot bars
    direction_flip_allowed: bool = True

    # fill / costs (costs applied in analyze_gamma_blast.py, not here -- same
    # convention as run_backtest.py / loren_backtest.py: this script writes
    # RAW pnl, per 1 lot fixed sizing)
    entry_slippage_pct: float = 2.5
    exit_slippage_pct: float = 2.5

    @staticmethod
    def from_json(s: str | None) -> Config:
        cfg = Config()
        if not s:
            return cfg
        import json

        d = json.loads(s)
        for k, v in d.items():
            if not hasattr(cfg, k):
                raise SystemExit(f"unknown config key: {k}")
            cur = getattr(cfg, k)
            if isinstance(cur, tuple) and isinstance(v, list):
                v = tuple(v)
            setattr(cfg, k, v)
        return cfg


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


# =========================================================================
# Black-Scholes IV / Greeks -- see module docstring
# =========================================================================
_SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot: float, strike: float, t_years: float, sigma: float, is_ce: bool) -> float:
    if t_years <= 0 or sigma <= 0:
        intrinsic = max(spot - strike, 0.0) if is_ce else max(strike - spot, 0.0)
        return intrinsic
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * sigma * sigma) * t_years) / (
        sigma * math.sqrt(t_years)
    )
    d2 = d1 - sigma * math.sqrt(t_years)
    disc_k = strike * math.exp(-RISK_FREE_RATE * t_years)
    if is_ce:
        return spot * _norm_cdf(d1) - disc_k * _norm_cdf(d2)
    return disc_k * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def time_to_expiry_years(now_ist: datetime, expiry: date, time_floor_minutes: float = 30.0) -> float:
    """Minutes from `now_ist` to real market close (15:30 IST) on `expiry`,
    floored at `time_floor_minutes` (prevents T->0 blowups seconds before
    close), converted via the spec's 252-trading-day x 375-min/day year.
    """
    close_dt = datetime.combine(expiry, MARKET_CLOSE, tzinfo=IST)
    minutes = max((close_dt - now_ist).total_seconds() / 60.0, time_floor_minutes)
    return minutes / TRADING_MINUTES_PER_YEAR


def black_scholes_iv_greeks(
    premium: float,
    spot: float,
    strike: float,
    t_years: float,
    is_ce: bool,
    *,
    iv_clip: tuple[float, float] = (0.01, 1.50),
    max_iter: int = 50,
    tol: float = 1e-4,
) -> tuple[float, float, float] | None:
    """Returns `(iv, delta, gamma)` or `None` on inversion failure (spec:
    `on_inversion_failure: skip_candidate_and_log`). Newton-Raphson seeded at
    30% IV, falls back to bisection across `iv_clip` if Newton doesn't
    converge (near-zero vega at very short T is exactly where Newton is
    fragile -- the regime this strategy trades in).
    """
    intrinsic = max(spot - strike, 0.0) if is_ce else max(strike - spot, 0.0)
    if premium < intrinsic - 1e-6 or premium <= 0 or t_years <= 0:
        return None

    lo, hi = iv_clip
    price_lo = _bs_price(spot, strike, t_years, lo, is_ce)
    price_hi = _bs_price(spot, strike, t_years, hi, is_ce)
    if not (price_lo - 1e-6 <= premium <= price_hi + 1e-6):
        return None  # premium outside what any IV in [1%,150%] can produce

    sigma = 0.30
    converged = False
    for _ in range(max_iter):
        price = _bs_price(spot, strike, t_years, sigma, is_ce)
        diff = price - premium
        if abs(diff) < tol:
            converged = True
            break
        d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * sigma * sigma) * t_years) / (
            sigma * math.sqrt(t_years)
        )
        vega = spot * _norm_pdf(d1) * math.sqrt(t_years)
        if vega < 1e-8:
            break
        sigma -= diff / vega
        if sigma <= 0 or sigma > 5:
            break

    if not converged:
        # bisection fallback across the clipped band
        a, b = lo, hi
        fa = price_lo - premium
        for _ in range(100):
            m = (a + b) / 2
            fm = _bs_price(spot, strike, t_years, m, is_ce) - premium
            if abs(fm) < tol:
                sigma, converged = m, True
                break
            if (fa < 0) == (fm < 0):
                a, fa = m, fm
            else:
                b = m
        else:
            sigma = (a + b) / 2
            converged = True  # best-effort after 100 bisection steps

    sigma = min(max(sigma, lo), hi)
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * sigma * sigma) * t_years) / (
        sigma * math.sqrt(t_years)
    )
    gamma = _norm_pdf(d1) / (spot * sigma * math.sqrt(t_years))
    delta = _norm_cdf(d1) if is_ce else _norm_cdf(d1) - 1.0
    return sigma, delta, gamma


# =========================================================================
# Contract spec tables (per JSON `contract_spec` / `execution_costs`)
# =========================================================================
def lot_size_for_expiry(expiry: date) -> int:
    return 75 if expiry <= date(2025, 12, 30) else 65


def stt_sell_pct_for_date(trade_date: date) -> float:
    return 0.0010 if trade_date < date(2026, 4, 1) else 0.0015


# =========================================================================
# Data loading
# =========================================================================
def _load_1min(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def _yymmdd(d: date) -> str:
    return f"{d.year % 100:02d}{d.month:02d}{d.day:02d}"


def _option_symbol(expiry: date, strike: int, is_ce: bool) -> str:
    return f"NIFTY{_yymmdd(expiry)}{strike}{'CE' if is_ce else 'PE'}"


def _load_option(expiry_dir: Path, expiry: date, strike: int, is_ce: bool) -> pd.DataFrame | None:
    p = expiry_dir / f"{_option_symbol(expiry, strike, is_ce)}.csv"
    if not p.is_file():
        return None
    return _load_1min(p)


def _first_at_or_after(df: pd.DataFrame, ts: datetime, max_gap_min: int = 6) -> pd.Series | None:
    sub = df[df["timestamp"] >= ts]
    if sub.empty:
        return None
    row = sub.iloc[0]
    if (row["timestamp"] - ts) > timedelta(minutes=max_gap_min):
        return None
    return row


# =========================================================================
# Trade record
# =========================================================================
@dataclass
class Trade:
    symbol: str
    expiry: date
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str
    precondition_measure: str
    precondition_move_pct: float
    trigger_type: str
    arm_mode: str
    iv_at_entry: float | None
    delta_at_entry: float | None
    gamma_at_entry: float | None
    attempt_no: int
    rejected_candidates_count_same_day: int
    lot_size: int
    pnl: float | None = None


# =========================================================================
# Precondition / trigger helpers (all operate on the expiry day's own spot
# 1-min bars -- no cross-day history needed, see module docstring)
# =========================================================================
def _precondition_ok(
    cfg: Config, spot_open: float, day_high: float, day_low: float, spot_now: float
) -> tuple[bool, float]:
    """Returns (ok, move_pct_used)."""
    if cfg.precondition_measure == "off" or cfg.precondition_threshold_pct is None:
        return True, 0.0
    if cfg.precondition_measure == "net_move":
        move = abs(spot_now - spot_open) / spot_open * 100.0
    else:  # day_range
        move = (day_high - day_low) / spot_open * 100.0
    return move <= cfg.precondition_threshold_pct, move


def run_expiry(
    expiry: date, expiry_dir: Path, spot_day: pd.DataFrame, cfg: Config
) -> list[Trade]:
    """`spot_day` = that calendar date's own 1-min spot bars (any columns
    open/high/low/close; 09:15+ only expected but not required -- filtered
    below). Returns every trade (including re-entries) for this expiry day.
    """
    day_bars = spot_day[
        (spot_day["timestamp"].dt.time >= SESSION_OPEN)
        & (spot_day["timestamp"].dt.date == expiry)
    ].reset_index(drop=True)
    if day_bars.empty:
        return []

    earliest = _parse_hhmm(cfg.entry_earliest)
    latest = _parse_hhmm(cfg.entry_latest)
    force_exit = _parse_hhmm(cfg.force_exit_time)

    spot_open = float(day_bars.iloc[0]["open"])
    close = day_bars["close"].astype(float).to_numpy()
    high = day_bars["high"].astype(float).to_numpy()
    low = day_bars["low"].astype(float).to_numpy()
    ts = day_bars["timestamp"].to_numpy()

    # frozen morning range: 09:15 .. entry_earliest (per trigger definition)
    pre_entry_mask = pd.to_datetime(ts).time < earliest
    if pre_entry_mask.any():
        morning_high = float(high[pre_entry_mask].max())
        morning_low = float(low[pre_entry_mask].min())
    else:
        morning_high = float(high[0])
        morning_low = float(low[0])

    ema_fast = pd.Series(close).ewm(span=cfg.ema_fast, adjust=False).mean().to_numpy()
    ema_slow = pd.Series(close).ewm(span=cfg.ema_slow, adjust=False).mean().to_numpy()

    # running day high/low for day_range precondition + (post-entry_window)
    # range extension used by re-entry's "new_trigger_required"
    running_high = np.maximum.accumulate(high)
    running_low = np.minimum.accumulate(low)

    avail_strikes: set[int] = set()
    for f in expiry_dir.glob(f"NIFTY{_yymmdd(expiry)}*CE.csv"):
        try:
            avail_strikes.add(int(f.stem[len(f"NIFTY{_yymmdd(expiry)}"):-2]))
        except ValueError:
            pass
    if not avail_strikes:
        return []

    opt_cache: dict[tuple[int, bool], pd.DataFrame | None] = {}

    def opt(strike: int, is_ce: bool) -> pd.DataFrame | None:
        key = (strike, is_ce)
        if key not in opt_cache:
            opt_cache[key] = _load_option(expiry_dir, expiry, strike, is_ce)
        return opt_cache[key]

    trades: list[Trade] = []
    attempts_used = 0
    rejected_count = 0
    active_range_hi = morning_high
    active_range_lo = morning_low
    last_exit_ts: datetime | None = None
    lot_size = lot_size_for_expiry(expiry)

    i = 0
    n = len(day_bars)
    while i < n:
        bts = pd.Timestamp(ts[i]).to_pydatetime()
        btime = bts.time()
        i += 1
        if btime < earliest or btime > latest or btime >= force_exit:
            continue
        if attempts_used > cfg.max_attempts_per_expiry:
            break
        if last_exit_ts is not None and (bts - last_exit_ts) < timedelta(
            minutes=cfg.cooldown_minutes_after_exit
        ):
            continue

        idx = i - 1
        # precondition
        if cfg.recheck_at_trigger_time:
            ok, move_pct = _precondition_ok(
                cfg, spot_open, float(running_high[idx]), float(running_low[idx]), close[idx]
            )
        else:
            ok, move_pct = _precondition_ok(
                cfg, spot_open, morning_high, morning_low, close[idx]
            )
        if not ok:
            continue

        # trigger
        direction = 0
        if cfg.trigger_type == "morning_range_break":
            if close[idx] > active_range_hi:
                direction = 1
            elif close[idx] < active_range_lo:
                direction = -1
        elif cfg.trigger_type == "ema_cross":
            if idx >= 1 and ema_fast[idx] > ema_slow[idx] and ema_fast[idx - 1] <= ema_slow[idx - 1]:
                direction = 1
            elif idx >= 1 and ema_fast[idx] < ema_slow[idx] and ema_fast[idx - 1] >= ema_slow[idx - 1]:
                direction = -1
        if direction == 0:
            continue
        if attempts_used > 0 and not cfg.direction_flip_allowed and direction != trades[-1].__dict__.get(
            "_direction", direction
        ):
            continue

        is_ce = direction == 1
        atm = int(round(close[idx] / STRIKE_STEP) * STRIKE_STEP)
        if not avail_strikes:
            continue
        strike = min(avail_strikes, key=lambda s: abs(s - atm))
        if abs(strike - atm) > cfg.max_distance_points:
            rejected_count += 1
            continue

        odf = opt(strike, is_ce)
        if odf is None or odf.empty:
            rejected_count += 1
            continue
        trigger_ts = bts
        cand_row = _first_at_or_after(odf, trigger_ts)
        if cand_row is None:
            rejected_count += 1
            continue
        cand_premium = float(cand_row["close"])

        # arm condition
        arm_ok = True
        iv = delta = gamma = None
        if cfg.arm_mode == "gamma_threshold":
            t_years = time_to_expiry_years(trigger_ts.replace(tzinfo=IST), expiry)
            greeks = black_scholes_iv_greeks(cand_premium, close[idx], float(strike), t_years, is_ce)
            if greeks is None:
                arm_ok = False
            else:
                iv, delta, gamma = greeks
                arm_ok = gamma >= cfg.gamma_threshold
        elif cfg.arm_mode == "premium_band":
            lo_b, hi_b = cfg.premium_band
            arm_ok = lo_b <= cand_premium <= hi_b
        if not arm_ok:
            rejected_count += 1
            continue

        # volume confirm
        if cfg.volume_spike_mult and "volume" in odf.columns:
            hist = odf[odf["timestamp"] < trigger_ts].tail(cfg.volume_spike_lookback_bars)
            cur_vol = float(cand_row.get("volume", 0.0))
            if len(hist) >= 5:
                avg_vol = float(hist["volume"].mean())
                if avg_vol <= 0 or cur_vol < cfg.volume_spike_mult * avg_vol:
                    rejected_count += 1
                    continue

        entry_row = _first_at_or_after(odf, trigger_ts + timedelta(minutes=1))
        if entry_row is None:
            rejected_count += 1
            continue
        entry_ts = pd.Timestamp(entry_row["timestamp"]).to_pydatetime()
        entry_premium = float(entry_row["open"]) * (1 + cfg.entry_slippage_pct / 100.0)
        if entry_premium <= 0:
            continue

        attempts_used += 1
        if cfg.arm_mode != "gamma_threshold":
            t_years = time_to_expiry_years(entry_ts.replace(tzinfo=IST), expiry)
            greeks = black_scholes_iv_greeks(
                entry_premium, close[idx], float(strike), t_years, is_ce
            )
            if greeks is not None:
                iv, delta, gamma = greeks

        trade = _walk_exit(
            odf, entry_ts, entry_premium, strike, is_ce, expiry, cfg, force_exit,
            cfg_precondition_measure=cfg.precondition_measure, move_pct=move_pct,
            trigger_type=cfg.trigger_type, arm_mode=cfg.arm_mode,
            iv=iv, delta=delta, gamma=gamma, attempt_no=attempts_used,
            rejected_count=rejected_count, lot_size=lot_size,
        )
        trades.append(trade)
        active_range_hi = max(active_range_hi, close[idx])
        active_range_lo = min(active_range_lo, close[idx])
        if trade.exit_time is not None:
            last_exit_ts = trade.exit_time
            i = int(np.searchsorted(ts, np.datetime64(trade.exit_time)))
        rejected_count = 0

    return trades


def _walk_exit(
    odf: pd.DataFrame,
    entry_ts: datetime,
    entry_premium: float,
    strike: int,
    is_ce: bool,
    expiry: date,
    cfg: Config,
    force_exit: time,
    *,
    cfg_precondition_measure: str,
    move_pct: float,
    trigger_type: str,
    arm_mode: str,
    iv: float | None,
    delta: float | None,
    gamma: float | None,
    attempt_no: int,
    rejected_count: int,
    lot_size: int,
) -> Trade:
    """Fill/exit model per JSON `fill_model`: signal at bar close (handled by
    caller), entry at next bar open+slippage (handled by caller). Exit
    priority hard_stop -> hard_target -> momentum_stall -> force_exit_time,
    same-bar stop&target resolves as the stop (conservative).
    """
    hard_stop = entry_premium * (1 - cfg.hard_stop_pct / 100.0)
    hard_target = entry_premium * (1 + cfg.hard_target_pct / 100.0)
    fixed_stop = entry_premium * (1 - cfg.fixed_stop_pct / 100.0)
    fixed_target = entry_premium * (1 + cfg.fixed_target_pct / 100.0)
    use_stop = fixed_stop if cfg.exit_mode == "fixed" else hard_stop
    use_target = fixed_target if cfg.exit_mode == "fixed" else hard_target

    ov = odf[odf["timestamp"] > entry_ts].reset_index(drop=True)
    exit_time = exit_price = None
    reason = "no_further_data"

    closes: list[float] = []
    peak_roc = 0.0

    if not ov.empty:
        for j in range(len(ov)):
            row = ov.iloc[j]
            bts = pd.Timestamp(row["timestamp"]).to_pydatetime()
            btime = bts.time()
            bar_low = float(row["low"])
            bar_high = float(row["high"])
            bar_close = float(row["close"])

            if bar_low <= use_stop:
                exit_time = bts
                exit_price = min(float(row["open"]), use_stop) * (1 - cfg.exit_slippage_pct / 100.0)
                reason = "hard_stop"
                break
            if bar_high >= use_target:
                exit_time = bts
                exit_price = max(float(row["open"]), use_target) * (1 - cfg.exit_slippage_pct / 100.0)
                reason = "hard_target"
                break

            closes.append(bar_close)
            if cfg.exit_mode == "momentum_stall" and len(closes) > cfg.momentum_stall_n_ticks:
                roc = (
                    (closes[-1] - closes[-1 - cfg.momentum_stall_n_ticks])
                    / closes[-1 - cfg.momentum_stall_n_ticks]
                    * 100.0
                )
                peak_roc = max(peak_roc, roc)
                if peak_roc > 0 and roc < (cfg.momentum_stall_pct / 100.0) * peak_roc:
                    exit_time = bts
                    exit_price = bar_close * (1 - cfg.exit_slippage_pct / 100.0)
                    reason = "momentum_stall"
                    break

            if btime >= force_exit:
                exit_time = bts
                exit_price = bar_close * (1 - cfg.exit_slippage_pct / 100.0)
                reason = "force_exit_time"
                break

        if exit_price is None:
            last = ov.iloc[-1]
            exit_time = pd.Timestamp(last["timestamp"]).to_pydatetime()
            exit_price = float(last["close"]) * (1 - cfg.exit_slippage_pct / 100.0)
            reason = "eod_no_exit_hit"

    t = Trade(
        symbol=_option_symbol(expiry, strike, is_ce),
        expiry=expiry,
        entry_time=entry_ts,
        entry_price=entry_premium,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=reason,
        precondition_measure=cfg_precondition_measure,
        precondition_move_pct=round(move_pct, 3),
        trigger_type=trigger_type,
        arm_mode=arm_mode,
        iv_at_entry=iv,
        delta_at_entry=delta,
        gamma_at_entry=gamma,
        attempt_no=attempt_no,
        rejected_candidates_count_same_day=rejected_count,
        lot_size=lot_size,
    )
    if exit_price is not None:
        t.pnl = (exit_price - entry_premium) * lot_size
    return t


# =========================================================================
# CSV / summary
# =========================================================================
def write_csv(trades: list[Trade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_TRADE_CSV_HEADER + _EXTRA_COLS)
        for t in sorted(trades, key=lambda x: x.entry_time):
            et = t.entry_time.replace(tzinfo=IST).isoformat()
            xt = t.exit_time.replace(tzinfo=IST).isoformat() if t.exit_time else ""
            w.writerow([
                t.symbol, "BUY", "current", et, round(t.entry_price, 2),
                xt, "" if t.exit_price is None else round(t.exit_price, 2),
                t.exit_reason, 1, t.lot_size,
                "" if t.pnl is None else round(t.pnl, 2),
                "", "", "", "", "", "", "", "",
                t.expiry.isoformat(), t.precondition_measure, t.precondition_move_pct,
                t.trigger_type, t.arm_mode,
                "" if t.iv_at_entry is None else round(t.iv_at_entry, 4),
                "" if t.delta_at_entry is None else round(t.delta_at_entry, 4),
                "" if t.gamma_at_entry is None else round(t.gamma_at_entry, 6),
                t.attempt_no, t.rejected_candidates_count_same_day,
            ])


def summary(trades: list[Trade]) -> str:
    xs = [t for t in trades if t.pnl is not None]
    if not xs:
        return "no trades"
    pnl = np.array([t.pnl for t in xs])
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    by_reason: dict[str, int] = {}
    for t in xs:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    return (
        f"trades={len(xs)} win%={100*len(wins)/len(xs):.1f} "
        f"gross_pnl/lot={pnl.sum():.0f} avg/lot={pnl.mean():.0f} "
        f"avg_win={wins.mean() if len(wins) else 0:.0f} "
        f"avg_loss={losses.mean() if len(losses) else 0:.0f} "
        f"PF={pf:.2f}  exits={by_reason}"
    )


def _discover_expiries(base: Path) -> list[date]:
    out = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            d = date.fromisoformat(child.name)
        except ValueError:
            continue
        if any(child.glob("*.csv")):
            out.append(d)
    return out


def run_all(
    data_dir: Path,
    options_subdir: str,
    underlying_file: str,
    cfg: Config,
    expiries: list[date],
) -> list[Trade]:
    spot = _load_1min(data_dir / "underlyings" / underlying_file)
    opt_base = data_dir / options_subdir / "NIFTY"
    all_trades: list[Trade] = []
    for e in expiries:
        edir = opt_base / e.isoformat()
        if not edir.is_dir():
            continue
        day_spot = spot[spot["timestamp"].dt.date == e]
        if day_spot.empty:
            continue
        tr = run_expiry(e, edir, day_spot, cfg)
        all_trades.extend(tr)
    return all_trades


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--options-subdir", default="options_1min_past")
    ap.add_argument("--underlying-file", default="NIFTY_alice_index_1min.csv")
    ap.add_argument("--expiry", type=date.fromisoformat, default=None)
    ap.add_argument("--all-expiries", action="store_true")
    ap.add_argument("--from", dest="from_date", type=date.fromisoformat, default=None)
    ap.add_argument("--to", dest="to_date", type=date.fromisoformat, default=None)
    ap.add_argument("--config", default=None, help="JSON overrides for Config")
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--out-csv", type=Path, default=None)
    args = ap.parse_args()

    cfg = Config.from_json(args.config)
    opt_base = args.data_dir / args.options_subdir / "NIFTY"

    if args.expiry:
        expiries = [args.expiry]
    elif args.all_expiries:
        expiries = _discover_expiries(opt_base)
    else:
        raise SystemExit("pass --expiry or --all-expiries")

    if args.from_date:
        expiries = [e for e in expiries if e >= args.from_date]
    if args.to_date:
        expiries = [e for e in expiries if e <= args.to_date]
    if args.shard_count > 1:
        expiries = [e for k, e in enumerate(expiries) if k % args.shard_count == args.shard_index]

    trades = run_all(args.data_dir, args.options_subdir, args.underlying_file, cfg, expiries)
    print(f"expiries processed: {len(expiries)}")
    print(summary(trades))
    if args.out_csv:
        write_csv(trades, args.out_csv)
        print(f"wrote {args.out_csv}")


if __name__ == "__main__":
    main()
