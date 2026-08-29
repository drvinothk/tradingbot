#!/usr/bin/env python3
"""Standalone Lorentzian Classification backtest (Sweep #3 W7 / "Framework 5").

SELF-CONTAINED. Imports only pandas + numpy. Does NOT touch the app pipeline,
the DB, migrations, or `run_backtest.py`. Runs under `~/an_venv` on the e4
backtest VM (or any pandas/numpy env). This is the deliberate approach for a
brand-new signal engine that has no representation in the production
`strategy_engine` and a hard e4 deadline (night 2026-08-31) — see
`SWEEP_3W7_LORENTZIAN_PLAN.md`. A "proper" `Strategy`-subclass integration is
the A1 follow-up if results warrant it.

Algorithm: jdehorty's "Machine Learning: Lorentzian Classification"
(TradingView, PineScript "Most Valuable" 2023), ported from the maintained
Python reference `advanced-ta` 0.1.8 (`LorentzianClassification/*.py`) and
cross-checked against the TradingView indicator defaults and the v2 spec at
`D:\\Documents\\Trading Bot_Oracle\\Loren setup.txt`. Full ported formulas:
`SWEEP_3W7_LORENTZIAN_PLAN.md` Appendix A.

Two deliberate, documented divergences from the `advanced-ta` reference:

  1. **Sliding-window neighbour pool.** The reference compares each eval bar
     against the *first* `max_bars_back` bars of the whole dataset (a batch-port
     artifact). This uses a causal sliding window `[t - max_bars_back, t)` —
     the standard, sensible reading of "max_bars_back: 2000" and the only one
     that makes sense in a rolling backtest.
  2. **Causal `normalize()`** for the WT / CCI features: expanding min/max
     (`series.expanding().min()/.max()`), not the reference's whole-series
     `MinMaxScaler` (which is lookahead). Verified non-repainting by
     `--selfcheck`.

The label is kept EXACTLY as the reference / Pine has it (looks inverted;
`src[i-4] < src[i]` -> SHORT). Combined with the furthest-neighbour selection
(`d >= lastDistance`) this is jdehorty's actual algorithm — not "fixed".

Faithful-exit stack (per spec §4, adverse-first on same-bar ties):
    1. premium backstop stop (loose, on the option premium)
    2. underlying structure stop  (signal hi/lo +/- stop_buffer_atr_frac*ATR14)
    3. fixed target               (optional, on the option premium)
    4. kernel reversal transition (5m close; exit_mode kernel_only/combined)
    5. opposite Lorentzian signal (5m close; exit_mode classifier_only/combined)
    6. 15:15 IST forced exit
Costs are NOT modelled here (matches `run_backtest.py` convention) — apply the
cost model in `analyze_walkforward.py` / `analyze_refined_batches.py`. The
per-trade CSV columns are byte-compatible with both analysers.

Usage
-----
    # local smoke on 3 recent expiries
    python scripts/loren_backtest.py --expiry 2026-08-18 --out-csv /tmp/w7_smoke.csv
    python scripts/loren_backtest.py --all-expiries --near-expiry-days 6 \
        --config '{"exit_mode":"combined","premium_backstop_pct":0.6}' \
        --shard-count 18 --shard-index 0 --out-csv out/l_base_s0.csv

    # non-repainting self-check (do this once before trusting any run)
    python scripts/loren_backtest.py --selfcheck

    # underlying-only signal validation over the full ~3yr series + 60/20/20 split
    python scripts/loren_backtest.py --underlying-only
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY_LOT_SIZE = 65          # user-confirmed 2026-08-27 (run_backtest.py UNDERLYING_META)
STRIKE_STEP = 50
SESSION_OPEN = time(9, 15)
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "historical"

_TRADE_CSV_HEADER = [
    "symbol", "side", "leg", "entry_time", "entry_price", "exit_time", "exit_price",
    "exit_reason", "qty_lots", "lot_size", "pnl",
    "vix_entry", "vix_exit", "atr_entry", "atr_exit",
    "pcr_entry", "pcr_exit", "contract_oi_entry", "contract_oi_exit",
]


# =========================================================================
# Config
# =========================================================================
@dataclass
class Config:
    signal_tf_min: int = 5
    neighbors: int = 8
    max_bars_back: int = 2000
    feature_set: str = "A"                       # "A" spec 5-feature; "B" adds trend confirm
    # feature params [length, ema_smoothing]
    rsi1: tuple[int, int] = (9, 1)
    wt: tuple[int, int] = (10, 11)
    cci: tuple[int, int] = (20, 1)
    adx1: tuple[int, int] = (20, 2)
    rsi2: tuple[int, int] = (9, 1)

    use_volatility_filter: bool = True
    use_regime_filter: bool = True
    regime_threshold: float = -0.1
    use_adx_filter: bool = False
    adx_threshold: float = 20.0
    use_ema_filter: bool = False
    ema_period: int = 200

    kernel_h: int = 8
    kernel_r: float = 8.0
    kernel_x: int = 25
    kernel_lag: int = 2
    use_kernel_smoothing: bool = False
    trade_with_kernel: bool = True

    breakout_max_wait: int = 5
    breakout_confirmation: str = "close"          # "close" | "wick"
    breakout_buffer_atr_frac: float = 0.05
    breakout_buffer_min_pts: float = 1.0

    stop_buffer_atr_frac: float = 0.10
    max_risk_atr_frac: float = 0.75
    # what to do when |entry - structure_stop| > max_risk_atr_frac*ATR14:
    #   "reject" = skip the trade (spec §4 literal)
    #   "cap"    = pull the stop in to exactly max_risk_atr_frac*ATR14 from entry
    risk_exceed_action: str = "reject"
    premium_backstop_pct: float = 0.40            # loose premium stop
    target_pct: float | None = None              # fixed premium target; None = off

    # classifier_structure | kernel_only | classifier_only | combined
    exit_mode: str = "classifier_structure"

    session_start: str = "09:20"
    session_end: str = "15:15"
    entry_cutoff: str = "15:00"                    # no new entries after this
    skip_expiry_day: bool = True
    max_trades_per_day: int = 3
    reentry_same_dir_after_stop: bool = False
    strike_rule: str = "ATM"                       # "ATM" | "1_ITM"
    skip_weekdays: tuple[str, ...] = ()            # e.g. ("Friday",)
    atr_period: int = 14

    # --- futures mode (--futures) ---
    # PnL written to the CSV is RAW (gross points x lot). Slippage / brokerage /
    # STT are applied in analyze_loren_futures.py, not here -- these two fields
    # are only documentation of the intended defaults.
    fut_lot_size: int = 65                         # current NSE NIFTY-FUT lot
    fut_tick_size: float = 0.05
    skip_last_trading_day_of_month: bool = True    # monthly-expiry-day skip (spec §0/§5)

    @staticmethod
    def from_json(s: str | None) -> Config:
        cfg = Config()
        if not s:
            return cfg
        d = json.loads(s)
        for k, v in d.items():
            if not hasattr(cfg, k):
                raise SystemExit(f"unknown config key: {k}")
            cur = getattr(cfg, k)
            if isinstance(cur, tuple) and isinstance(v, list):
                v = tuple(v)
            setattr(cfg, k, v)
        return cfg


# =========================================================================
# Indicators  (pure, causal — Wilder RMA family)
# =========================================================================
def _rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = _rma(d.clip(lower=0), n)
    dn = _rma((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _true_range(h: pd.Series, lo: pd.Series, c: pd.Series) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([(h - lo), (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)


def _atr(h: pd.Series, lo: pd.Series, c: pd.Series, n: int) -> pd.Series:
    return _rma(_true_range(h, lo, c), n)


def _cci(h: pd.Series, lo: pd.Series, c: pd.Series, n: int) -> pd.Series:
    tp = (h + lo + c) / 3.0
    ma = tp.rolling(n, min_periods=n).mean()
    md = (tp - ma).abs().rolling(n, min_periods=n).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def _adx(h: pd.Series, lo: pd.Series, c: pd.Series, n: int) -> pd.Series:
    up = h.diff()
    dn = -lo.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = _rma(_true_range(h, lo, c), n)
    plus_di = 100 * _rma(plus_dm, n) / tr.replace(0, np.nan)
    minus_di = 100 * _rma(minus_dm, n) / tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _rma(dx, n)


def _rescale(
    x: pd.Series, omin: float, omax: float, nmin: float = 0.0, nmax: float = 1.0
) -> pd.Series:
    return nmin + (nmax - nmin) * (x - omin) / max(omax - omin, 1e-10)


def _normalize_expanding(x: pd.Series) -> pd.Series:
    mn = x.expanding().min()
    mx = x.expanding().max()
    return ((x - mn) / (mx - mn).replace(0, np.nan)).fillna(0.0)


def n_rsi(close: pd.Series, a: int, b: int) -> pd.Series:
    return _rescale(_ema(_rsi(close, a), b), 0.0, 100.0)


def n_cci(h: pd.Series, lo: pd.Series, c: pd.Series, a: int, b: int) -> pd.Series:
    return _normalize_expanding(_ema(_cci(h, lo, c, a), b))


def n_wt(hlc3: pd.Series, a: int, b: int) -> pd.Series:
    e1 = _ema(hlc3, a)
    e2 = _ema((hlc3 - e1).abs(), a)
    ci = (hlc3 - e1) / (0.015 * e2.replace(0, np.nan))
    wt1 = _ema(ci, b)
    wt2 = _sma(wt1, 4)
    return _normalize_expanding(wt1 - wt2)


def n_adx(h: pd.Series, lo: pd.Series, c: pd.Series, a: int) -> pd.Series:
    return _rescale(_adx(h, lo, c, a), 0.0, 100.0)


# =========================================================================
# Kernels  (Nadaraya-Watson, non-repainting — backward-only)
# =========================================================================
def _kernel(src: np.ndarray, x: int, w_of_i) -> np.ndarray:
    src = np.asarray(src, dtype=float)
    n = len(src)
    num = np.zeros(n)
    den = 0.0
    for i in range(x + 2):
        w = w_of_i(i)
        y = src if i == 0 else np.concatenate([np.zeros(i), src[: n - i]])
        num += y * w
        den += w
    val = num / den
    val[: x + 1] = np.nan
    return val


def rational_quadratic(src: np.ndarray, h: int, r: float, x: int) -> np.ndarray:
    return _kernel(src, x, lambda i: (1 + (i * i) / (h * h * 2 * r)) ** (-r))


def gaussian(src: np.ndarray, h: int, x: int) -> np.ndarray:
    return _kernel(src, x, lambda i: math.exp(-(i * i) / (2 * h * h)))


# =========================================================================
# Filters
# =========================================================================
def filter_volatility(h: pd.Series, lo: pd.Series, c: pd.Series, use: bool) -> np.ndarray:
    if not use:
        return np.ones(len(c), dtype=bool)
    recent = _atr(h, lo, c, 1)
    hist = _atr(h, lo, c, 10)
    return (recent > hist).fillna(False).to_numpy()


def regime_filter(
    src: pd.Series, high: pd.Series, low: pd.Series, threshold: float, use: bool
) -> np.ndarray:
    if not use:
        return np.ones(len(src), dtype=bool)
    s = src.to_numpy(float)
    hi = high.to_numpy(float)
    lo = low.to_numpy(float)
    n = len(s)
    v1 = np.zeros(n)
    v2 = np.zeros(n)
    klmf = np.zeros(n)
    for i in range(n):
        p = i - 1 if i >= 1 else 0
        if hi[i] - lo[i] != 0:
            v1[i] = 0.2 * (s[i] - s[p]) + 0.8 * v1[p]
            v2[i] = 0.1 * (hi[i] - lo[i]) + 0.8 * v2[p]
        else:                       # ref does `continue` -> v1[i]=v2[i] stay 0
            v1[i] = 0.0
            v2[i] = 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        omega = np.nan_to_num(np.abs(np.divide(v1, v2)))
    alpha = (-(omega**2) + np.sqrt(omega**4 + 16 * omega**2)) / 8
    for i in range(n):
        p = i - 1 if i >= 1 else 0
        klmf[i] = alpha[i] * s[i] + (1 - alpha[i]) * klmf[p]
    abs_slope = np.abs(np.diff(klmf, prepend=0.0))
    avg_slope = (
        pd.Series(abs_slope).ewm(span=200, adjust=False, min_periods=200).mean().to_numpy()
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        decline = (abs_slope - avg_slope) / avg_slope
    return np.nan_to_num(decline, nan=-1e9) >= threshold


def filter_adx(
    h: pd.Series, lo: pd.Series, c: pd.Series, threshold: float, length: int = 14
) -> np.ndarray:
    """Real ADX > threshold, always computed. Whether it actually gates entries
    is decided at the call site (`use_adx_filter` for the base gate; the
    feature_set "B" candidate check always applies it, per spec §2 Version B)."""
    return (_adx(h, lo, c, length) > threshold).fillna(False).to_numpy()


# =========================================================================
# Lorentzian k-NN classifier  (sliding window — see module docstring)
# =========================================================================
def lorentzian_prediction(
    feats: np.ndarray, labels: np.ndarray, max_bars_back: int, neighbors: int, eval_from: int
) -> np.ndarray:
    n = len(feats)
    pred = np.zeros(n, dtype=float)
    k34 = round(neighbors * 3 / 4)
    for t in range(max(eval_from, 1), n):
        lo = max(0, t - max_bars_back)
        fi = feats[lo:t]
        lbl = labels[lo:t]
        d = np.log1p(np.abs(feats[t] - fi)).sum(axis=1)
        last = -1.0
        dists: list[float] = []
        preds: list[int] = []
        for j in range(len(d)):
            if ((lo + j) % 4) == 0:            # ref `i%4` truthy => skip when ==0
                continue
            dj = d[j]
            if dj >= last:
                last = dj
                dists.append(dj)
                preds.append(int(lbl[j]))
                if len(preds) > neighbors:
                    last = dists[k34]
                    dists.pop(0)
                    preds.pop(0)
        pred[t] = float(sum(preds))
    return pred


# =========================================================================
# Bar loading / resampling
# =========================================================================
def _load_1min(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


# index_proxy = 3yr cash index (volume=0; the Loren signal uses no volume).
# shoonya = ~41 real-futures days, timestamps naive UTC. truedata_stitch =
# ~56 sparse pre-expiry days.
_FUT_SRC = {
    "index_proxy": "NIFTY_alice_index_1min.csv",
    "shoonya": "NIFTY_FUT_shoonya_1min.csv",
    "truedata_stitch": "NIFTY_FUT_1min.csv",
}


def _load_futures_1min(data_dir: Path, source: str) -> pd.DataFrame:
    df = _load_1min(data_dir / "underlyings" / _FUT_SRC[source])
    if source == "shoonya":
        # file is naive UTC -> IST (India has no DST); must precede _resample's
        # between_time("09:15","15:29"). first row 2026-07-01 03:45 -> 09:15.
        df["timestamp"] = df["timestamp"] + pd.Timedelta(hours=5, minutes=30)
    return df


def _monthly_expiry_dates(bars5: pd.DataFrame) -> set[date]:
    """The last trading day of each calendar month present in the frame. Used
    to skip new entries on that day (spec §0/§5: monthly-future roll /
    basis+liquidity distortion). Deliberately weekday-agnostic -- NIFTY's
    index-derivative expiry weekday has changed over the backtest span
    (Thursday historically, Tuesday in the 2025-26 data), and the index proxy
    has no contract boundary to key on; the month's last trading day is always
    at/near expiry and is unambiguous."""
    days = sorted({pd.Timestamp(t).date() for t in bars5["ts"].to_numpy()})
    by_month: dict[tuple[int, int], date] = {}
    for d in days:
        by_month[(d.year, d.month)] = d          # sorted -> last wins
    return set(by_month.values())


def _resample(df1: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    """1-min -> tf-min OHLC, RTH only, buckets right-labelled to close time,
    anchored to 09:15 IST. label(ts) = the completed bar whose close is at ts."""
    d = df1.set_index("timestamp")
    d = d.between_time("09:15", "15:29")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = d.resample(f"{tf_min}min", label="right", closed="right", origin="start_day").agg(agg)
    out = out.dropna(subset=["open", "close"])
    # keep only bars whose close falls inside a real session
    out = out[(out.index.time > SESSION_OPEN) & (out.index.time <= time(15, 30))]
    return out.reset_index().rename(columns={"timestamp": "ts"})


# =========================================================================
# Signal pipeline  (produces prediction / signal / kernel series on a 5m frame)
# =========================================================================
@dataclass
class Signals:
    ts: np.ndarray                 # bar close datetimes
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    prediction: np.ndarray
    signal: np.ndarray             # +1 / -1 / 0 with hysteresis
    kernel_bull: np.ndarray        # yhat1[t] > yhat1[t-1]
    kernel_bear: np.ndarray
    filt_vol: np.ndarray
    filt_regime: np.ndarray
    filt_adx: np.ndarray
    ema_up: np.ndarray
    ema_dn: np.ndarray
    rsi14: np.ndarray


def build_signals(bars5: pd.DataFrame, cfg: Config, eval_from_ts: datetime | None) -> Signals:
    h = bars5["high"].astype(float)
    lo = bars5["low"].astype(float)
    c = bars5["close"].astype(float)
    o = bars5["open"].astype(float)
    hlc3 = (h + lo + c) / 3.0

    f1 = n_rsi(c, *cfg.rsi1)
    f2 = n_wt(hlc3, *cfg.wt)
    f3 = n_cci(h, lo, c, *cfg.cci)
    f4 = n_adx(h, lo, c, cfg.adx1[0])
    f5 = n_rsi(c, *cfg.rsi2)
    feats = np.column_stack(
        [f1.to_numpy(), f2.to_numpy(), f3.to_numpy(), f4.to_numpy(), f5.to_numpy()]
    )
    feats = np.nan_to_num(feats, nan=0.0)

    # backward label — EXACT reference form (looks inverted; intentional)
    cc = c.to_numpy()
    labels = np.zeros(len(cc), dtype=int)
    prev4 = np.concatenate([np.full(4, np.nan), cc[:-4]])
    labels[prev4 < cc] = -1      # price rose over last 4 bars -> SHORT
    labels[prev4 > cc] = 1

    n = len(bars5)
    eval_from = 0
    if eval_from_ts is not None:
        after = np.where(bars5["ts"].to_numpy() >= np.datetime64(eval_from_ts))[0]
        eval_from = int(after[0]) if len(after) else n
    eval_from = max(eval_from - cfg.max_bars_back - cfg.ema_period - 50, 0)

    pred = lorentzian_prediction(feats, labels, cfg.max_bars_back, cfg.neighbors, eval_from)

    filt_vol = filter_volatility(h, lo, c, cfg.use_volatility_filter)
    filt_regime = regime_filter(
        (o + h + lo + c) / 4.0, h, lo, cfg.regime_threshold, cfg.use_regime_filter
    )
    filt_adx = filter_adx(h, lo, c, cfg.adx_threshold)
    filt_all = filt_vol & filt_regime & filt_adx

    sig = np.zeros(n, dtype=int)
    for i in range(n):
        if pred[i] > 0 and filt_all[i]:
            sig[i] = 1
        elif pred[i] < 0 and filt_all[i]:
            sig[i] = -1
        else:
            sig[i] = sig[i - 1] if i else 0

    src = c.to_numpy()
    yhat1 = rational_quadratic(src, cfg.kernel_h, cfg.kernel_r, cfg.kernel_x)
    yhat2 = gaussian(src, max(cfg.kernel_h - cfg.kernel_lag, 1), cfg.kernel_x)
    y1p = np.concatenate([[np.nan], yhat1[:-1]])
    if cfg.use_kernel_smoothing:
        kbull = yhat2 >= yhat1
        kbear = yhat2 <= yhat1
    else:
        kbull = y1p < yhat1
        kbear = y1p > yhat1
    kbull = np.nan_to_num(kbull, nan=0).astype(bool)
    kbear = np.nan_to_num(kbear, nan=0).astype(bool)

    # Real close-vs-EMA200 alignment, always computed. Only consumed by the
    # feature_set "B" (trend-confirmed, spec §2 Version B) candidate gate --
    # feature_set "A" never reads these, so this is inert for the spec baseline.
    ema_p = _ema(c, cfg.ema_period).to_numpy()
    ema_up = np.nan_to_num(c.to_numpy() > ema_p, nan=0).astype(bool)
    ema_dn = np.nan_to_num(c.to_numpy() < ema_p, nan=0).astype(bool)

    return Signals(
        ts=bars5["ts"].to_numpy(),
        open=o.to_numpy(), high=h.to_numpy(), low=lo.to_numpy(), close=cc,
        atr=_atr(h, lo, c, cfg.atr_period).to_numpy(),
        prediction=pred, signal=sig, kernel_bull=kbull, kernel_bear=kbear,
        filt_vol=filt_vol, filt_regime=filt_regime, filt_adx=filt_adx,
        ema_up=ema_up, ema_dn=ema_dn,
        rsi14=_rsi(c, 14).to_numpy(),
    )


# =========================================================================
# State machine + option execution
# =========================================================================
@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str
    atr_entry: float
    pnl: float | None = None
    # futures-mode extras (option path leaves these at their defaults):
    side: str = "BUY"                    # "BUY" long / "SELL" short
    lot_size: int = NIFTY_LOT_SIZE
    stop_price: float | None = None      # smuggled through the CSV pcr_* columns


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _yymmdd(d: date) -> str:
    return f"{d.year % 100:02d}{d.month:02d}{d.day:02d}"


def _option_symbol(expiry: date, strike: int, is_ce: bool) -> str:
    return f"NIFTY{_yymmdd(expiry)}{strike}{'CE' if is_ce else 'PE'}"


def _load_option(expiry_dir: Path, expiry: date, strike: int, is_ce: bool) -> pd.DataFrame | None:
    p = expiry_dir / f"{_option_symbol(expiry, strike, is_ce)}.csv"
    if not p.is_file():
        return None
    return _load_1min(p)


def _pick_strike(spot: float, is_ce: bool, rule: str, available: set[int]) -> int | None:
    atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
    want = atm
    if rule == "1_ITM":
        want = atm - STRIKE_STEP if is_ce else atm + STRIKE_STEP
    if want in available:
        return want
    if not available:
        return None
    return min(available, key=lambda s: abs(s - want))


def _first_at_or_after(df: pd.DataFrame, ts: datetime, max_gap_min: int = 6) -> pd.Series | None:
    sub = df[df["timestamp"] >= ts]
    if sub.empty:
        return None
    row = sub.iloc[0]
    if (row["timestamp"] - ts) > timedelta(minutes=max_gap_min):
        return None
    return row


def run_expiry(
    expiry: date,
    expiry_dir: Path,
    under5: pd.DataFrame,
    under1: pd.DataFrame,
    cfg: Config,
    near_expiry_days: int | None,
) -> list[Trade]:
    win_lo = date(2000, 1, 1)
    if near_expiry_days is not None:
        win_lo = expiry - timedelta(days=near_expiry_days)

    # History cap: the classifier only ever looks back `max_bars_back` 5m bars,
    # regime EMA needs 200, kernels need `kernel_x`. Keep a generous margin
    # before the eval window so every per-expiry signal build is cheap and
    # independent of how far back the full CSV goes.
    if near_expiry_days is not None:
        keep = cfg.max_bars_back + 600
        w = under5[under5["ts"] < pd.Timestamp(win_lo)]
        if len(w) > keep:
            under5 = pd.concat([w.iloc[-keep:], under5[under5["ts"] >= pd.Timestamp(win_lo)]])
            under5 = under5.reset_index(drop=True)

    eval_from_ts = datetime.combine(win_lo, time(0, 0)) if near_expiry_days else None
    sig = build_signals(under5, cfg, eval_from_ts)

    ss = _parse_hhmm(cfg.session_start)
    _parse_hhmm(cfg.session_end)
    ec = _parse_hhmm(cfg.entry_cutoff)

    avail_strikes: set[int] = set()
    for f in expiry_dir.glob(f"NIFTY{_yymmdd(expiry)}*CE.csv"):
        try:
            avail_strikes.add(int(f.stem[len(f"NIFTY{_yymmdd(expiry)}"):-2]))
        except ValueError:
            pass

    opt_cache: dict[tuple[int, bool], pd.DataFrame | None] = {}

    def opt(strike: int, is_ce: bool) -> pd.DataFrame | None:
        key = (strike, is_ce)
        if key not in opt_cache:
            opt_cache[key] = _load_option(expiry_dir, expiry, strike, is_ce)
        return opt_cache[key]

    if near_expiry_days is not None:
        under1 = under1[under1["timestamp"] >= pd.Timestamp(win_lo) - pd.Timedelta(days=2)]
    u1_reset = under1.sort_values("timestamp").reset_index(drop=True)
    u1_by_ts = u1_reset.set_index("timestamp")[["open", "high", "low"]]

    trades: list[Trade] = []
    state = "FLAT"
    p_dir = 0
    p_sig_hi = p_sig_lo = 0.0
    p_expiry_idx = -1
    per_day: dict[date, int] = {}
    stopped_dirs: dict[date, set[int]] = {}

    ts_arr = pd.to_datetime(sig.ts)
    for i in range(1, len(sig.ts)):
        bts: datetime = ts_arr[i].to_pydatetime()
        bday = bts.date()
        if not (win_lo <= bday <= expiry):
            continue
        if cfg.skip_expiry_day and bday == expiry:
            continue
        if bts.strftime("%A") in cfg.skip_weekdays:
            continue

        # new opposite signal cancels a pending
        new_long = sig.signal[i] == 1 and sig.signal[i - 1] != 1
        new_short = sig.signal[i] == -1 and sig.signal[i - 1] != -1

        if state == "PENDING":
            if (p_dir == 1 and new_short) or (p_dir == -1 and new_long):
                state = "FLAT"
            elif i > p_expiry_idx:
                state = "FLAT"
            else:
                buf = max(
                    cfg.breakout_buffer_min_pts,
                    cfg.breakout_buffer_atr_frac * _nz(sig.atr[i]),
                )
                px = sig.close[i] if cfg.breakout_confirmation == "close" else (
                    sig.high[i] if p_dir == 1 else sig.low[i]
                )
                broke = (p_dir == 1 and px > p_sig_hi + buf) or (
                    p_dir == -1 and px < p_sig_lo - buf
                )
                if broke:
                    t = _try_enter(
                        i, p_dir, p_sig_hi, p_sig_lo, sig, bts, expiry, expiry_dir,
                        avail_strikes, opt, u1_reset, u1_by_ts, cfg, per_day, stopped_dirs,
                    )
                    if t is not None:
                        trades.append(t)
                    state = "FLAT"
            continue

        # FLAT: look for a fresh candidate
        if bts.time() < ss or bts.time() > ec:
            continue
        if per_day.get(bday, 0) >= cfg.max_trades_per_day:
            continue

        kern_ok_long = (not cfg.trade_with_kernel) or sig.kernel_bull[i]
        kern_ok_short = (not cfg.trade_with_kernel) or sig.kernel_bear[i]
        base_ok = (
            sig.filt_vol[i] and sig.filt_regime[i]
            and (sig.filt_adx[i] or not cfg.use_adx_filter)
        )

        cand_long = new_long and kern_ok_long and base_ok
        cand_short = new_short and kern_ok_short and base_ok
        if cfg.feature_set == "B":
            cand_long = cand_long and sig.rsi14[i] > 50 and sig.filt_adx[i] and sig.ema_up[i]
            cand_short = cand_short and sig.rsi14[i] < 50 and sig.filt_adx[i] and sig.ema_dn[i]

        if cand_long or cand_short:
            p_dir = 1 if cand_long else -1
            p_sig_hi = sig.high[i]
            p_sig_lo = sig.low[i]
            p_expiry_idx = i + cfg.breakout_max_wait
            state = "PENDING"

    # attach post-hoc exits computed inside _try_enter already; pnl set there
    return trades


def _nz(x: float) -> float:
    return 0.0 if (x is None or (isinstance(x, float) and math.isnan(x))) else float(x)


def _try_enter(
    i: int, direction: int, sig_hi: float, sig_lo: float, sig: Signals, bar_ts: datetime,
    expiry: date, expiry_dir: Path, avail: set[int], opt,
    u1_reset: pd.DataFrame, u1_by_ts: pd.DataFrame, cfg: Config,
    per_day: dict[date, int], stopped_dirs: dict[date, set[int]],
) -> Trade | None:
    bday = bar_ts.date()
    if not cfg.reentry_same_dir_after_stop and direction in stopped_dirs.get(bday, set()):
        return None

    atr14 = _nz(sig.atr[i])
    is_ce = direction == 1
    # entry at NEXT 5m bar open == the 1-min bar at/after this bar's close ts
    entry_ts = bar_ts
    u_entry_row = _first_at_or_after(u1_reset, entry_ts)
    if u_entry_row is None:
        return None
    spot = float(u_entry_row["open"])

    strike = _pick_strike(spot, is_ce, cfg.strike_rule, avail)
    if strike is None:
        return None
    odf = opt(strike, is_ce)
    if odf is None or odf.empty:
        return None
    oe = _first_at_or_after(odf, entry_ts)
    if oe is None:
        return None
    entry_prem = float(oe["open"])
    if entry_prem <= 0:
        return None

    # underlying structure stop level + max-risk reject
    if is_ce:
        structure = sig_lo - cfg.stop_buffer_atr_frac * atr14
    else:
        structure = sig_hi + cfg.stop_buffer_atr_frac * atr14
    cap = cfg.max_risk_atr_frac * atr14
    if atr14 > 0 and abs(spot - structure) > cap:
        if cfg.risk_exceed_action == "cap":
            structure = spot - cap if is_ce else spot + cap
        else:
            return None

    per_day[bday] = per_day.get(bday, 0) + 1

    # ---- exit walk (vectorised alignment; single pass over option bars) ----
    prem_stop = entry_prem * (1.0 - cfg.premium_backstop_pct)
    prem_target = entry_prem * (1.0 + cfg.target_pct) if cfg.target_pct else None
    se = _parse_hhmm(cfg.session_end)

    ov = odf[odf["timestamp"] > entry_ts]
    exit_time = exit_price = None
    reason = "no_further_data"
    if not ov.empty:
        ots = ov["timestamp"].to_numpy()
        o_hi = ov["high"].to_numpy(float)
        o_lo = ov["low"].to_numpy(float)
        o_cl = ov["close"].to_numpy(float)
        ua = u1_by_ts.reindex(ov["timestamp"]).ffill()
        u_hi = ua["high"].to_numpy(float)
        u_lo = ua["low"].to_numpy(float)
        sig_ts = sig.ts                                   # sorted datetime64
        want_kernel = cfg.exit_mode in ("kernel_only", "combined")
        want_oppo = cfg.exit_mode in ("classifier_only", "combined")

        for j in range(len(ots)):
            ts = pd.Timestamp(ots[j]).to_pydatetime()
            # spec §4 priority, adverse-first: stop -> structure -> target ->
            # kernel/opposite -> 15:15. eod is checked last so a stop that hits
            # on the 15:15+ bar is still labelled a stop, not "eod".
            if o_lo[j] <= prem_stop:
                exit_time, exit_price, reason = ts, prem_stop, "premium_stop"
                stopped_dirs.setdefault(bday, set()).add(direction)
                break
            if (is_ce and u_lo[j] <= structure) or ((not is_ce) and u_hi[j] >= structure):
                exit_time, exit_price, reason = ts, o_cl[j], "structure_stop"
                stopped_dirs.setdefault(bday, set()).add(direction)
                break
            if prem_target is not None and o_hi[j] >= prem_target:
                exit_time, exit_price, reason = ts, prem_target, "target"
                break
            if ts.time() >= se:
                exit_time, exit_price, reason = ts, o_cl[j], "eod"
                break
            if want_kernel or want_oppo:
                pos = int(np.searchsorted(sig_ts, ots[j]))
                if pos < len(sig_ts) and sig_ts[pos] == ots[j] and pos >= 1:
                    krev = (is_ce and sig.kernel_bear[pos] and sig.kernel_bull[pos - 1]) or (
                        (not is_ce) and sig.kernel_bull[pos] and sig.kernel_bear[pos - 1]
                    )
                    oppo = (is_ce and sig.signal[pos] == -1 and sig.signal[pos - 1] != -1) or (
                        (not is_ce) and sig.signal[pos] == 1 and sig.signal[pos - 1] != 1
                    )
                    if want_kernel and krev:
                        exit_time, exit_price, reason = ts, o_cl[j], "kernel_reversal"
                        break
                    if want_oppo and oppo:
                        exit_time, exit_price, reason = ts, o_cl[j], "opposite_signal"
                        break

        if exit_price is None:
            exit_time = pd.Timestamp(ots[-1]).to_pydatetime()
            exit_price = float(o_cl[-1])

    t = Trade(
        symbol=_option_symbol(expiry, strike, is_ce),
        entry_time=entry_ts, entry_price=entry_prem,
        exit_time=exit_time, exit_price=exit_price, exit_reason=reason,
        atr_entry=atr14,
    )
    if exit_price is not None:
        t.pnl = (exit_price - entry_prem) * NIFTY_LOT_SIZE
    return t


# =========================================================================
# CSV
# =========================================================================
# Header is byte-identical to run_backtest.py's _TRADE_CSV_HEADER so the
# analysers and merge_backtest_shards.py accept it unchanged. In --futures
# mode the option-only `pcr_entry`/`pcr_exit` columns are repurposed to carry
# the underlying stop price and the entry->stop risk in points (harmless: an
# index-future instrument has no put/call ratio; analyze_loren_futures.py
# reads them, analyze_walkforward.py ignores them).
def write_csv(trades: list[Trade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_TRADE_CSV_HEADER)
        for t in sorted(trades, key=lambda x: x.entry_time):
            et = t.entry_time.replace(tzinfo=IST).isoformat()
            xt = t.exit_time.replace(tzinfo=IST).isoformat() if t.exit_time else ""
            stop_col = "" if t.stop_price is None else round(t.stop_price, 2)
            risk_col = (
                "" if t.stop_price is None
                else round(abs(t.entry_price - t.stop_price), 2)
            )
            w.writerow([
                t.symbol, t.side, "current", et, round(t.entry_price, 2),
                xt, "" if t.exit_price is None else round(t.exit_price, 2),
                t.exit_reason, 1, t.lot_size,
                "" if t.pnl is None else round(t.pnl, 2),
                "", "", round(t.atr_entry, 2), "", stop_col, risk_col, "", "",
            ])


def _print_splits(df: pd.DataFrame, title: str, value_col: str = "ret_pts",
                  split: tuple[float, float] = (0.6, 0.8)) -> None:
    """Shared TRAIN(60)/VALID(20)/TEST(20) calendar-split report + by-weekday /
    by-exit-reason / by-direction breakdowns. `df` needs columns `date`,
    `value_col`, `reason`, and optionally `dir`. Split fractions must match
    analyze_loren_futures.py."""
    if df.empty:
        print(f"{title}: no trades")
        return
    df = df.sort_values("date").reset_index(drop=True)
    d0, d1 = df["date"].min(), df["date"].max()
    span = (d1 - d0).days
    tr_end = d0 + timedelta(days=int(span * split[0]))
    va_end = d0 + timedelta(days=int(span * split[1]))

    def stat(x: pd.DataFrame, name: str) -> None:
        if x.empty:
            print(f"  {name:12s} n=0")
            return
        r = x[value_col].to_numpy()
        w = r[r > 0]
        pf = w.sum() / abs(r[r <= 0].sum()) if r[r <= 0].sum() != 0 else float("inf")
        print(f"  {name:12s} n={len(r):4d}  win%={100 * len(w) / len(r):5.1f}  "
              f"exp={r.mean():8.2f}  total={r.sum():10.1f}  PF={pf:.2f}")

    def _grp(frame: pd.DataFrame, col: str) -> dict:
        return frame.groupby(col)[value_col].agg(["count", "mean"]).round(2).to_dict("index")

    print(f"{title}  [{d0} .. {d1}]  ({len(df)} trades)")
    stat(df, "ALL")
    stat(df[df["date"] <= tr_end], "TRAIN(60)")
    stat(df[(df["date"] > tr_end) & (df["date"] <= va_end)], "VALID(20)")
    stat(df[df["date"] > va_end], "TEST(20)")
    print("  by weekday:", _grp(df.assign(d=pd.to_datetime(df["date"]).dt.day_name()), "d"))
    print("  by exit:   ", _grp(df, "reason"))
    if "dir" in df.columns:
        print("  by dir:    ", _grp(df.assign(s=df["dir"].map({1: "long", -1: "short"})), "s"))


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


# =========================================================================
# Self-check: non-repainting
# =========================================================================
def selfcheck(data_dir: Path) -> int:
    df1 = _load_1min(data_dir / "underlyings" / "NIFTY_alice_index_1min.csv")
    df1 = df1[df1["timestamp"] >= pd.Timestamp("2026-03-01")]
    b5 = _resample(df1, 5)
    cfg = Config()
    n = len(b5)
    cut = n - 400
    full = build_signals(b5, cfg, None)
    part = build_signals(b5.iloc[:cut].copy(), cfg, None)
    check_to = cut - (cfg.kernel_x + 6)
    a = full.prediction[cfg.max_bars_back:check_to]
    b = part.prediction[cfg.max_bars_back:check_to]
    np.nan_to_num(full.__dict__.get("_yhat1", np.zeros(0)))
    ok_pred = np.allclose(a, b, atol=1e-9)
    # kernels via direct recompute
    src_full = b5["close"].to_numpy()
    src_part = b5["close"].to_numpy()[:cut]
    kf = rational_quadratic(src_full, cfg.kernel_h, cfg.kernel_r, cfg.kernel_x)[:check_to]
    kp = rational_quadratic(src_part, cfg.kernel_h, cfg.kernel_r, cfg.kernel_x)[:check_to]
    ok_kern = np.allclose(np.nan_to_num(kf), np.nan_to_num(kp), atol=1e-9)
    print(f"non-repainting: prediction={'OK' if ok_pred else 'FAIL'} "
          f"kernel={'OK' if ok_kern else 'FAIL'}  (compared {len(a)} bars)")
    return 0 if (ok_pred and ok_kern) else 1


# =========================================================================
# Underlying-only signal validation (full ~3yr + 60/20/20 split)
# =========================================================================
def underlying_only(data_dir: Path, cfg: Config) -> None:
    df1 = _load_1min(data_dir / "underlyings" / "NIFTY_alice_index_1min.csv")
    b5 = _resample(df1, cfg.signal_tf_min)
    sig = build_signals(b5, cfg, None)
    ts = pd.to_datetime(sig.ts)
    ss = _parse_hhmm(cfg.session_start)
    ec = _parse_hhmm(cfg.entry_cutoff)
    n = len(sig.ts)

    def new_sig(i: int, direction: int) -> bool:
        return sig.signal[i] == direction and sig.signal[i - 1] != direction

    def walk_exit(i: int, d: int, day: date, stop: float) -> tuple[float, str]:
        for j in range(i + 1, n):
            if ts[j].to_pydatetime().date() != day:
                return float(sig.close[j - 1]), "eod"
            hit_stop = (d == 1 and sig.low[j] <= stop) or (d == -1 and sig.high[j] >= stop)
            if hit_stop:
                return stop, "stop"
            krev = (
                (d == 1 and sig.kernel_bear[j] and sig.kernel_bull[j - 1])
                or (d == -1 and sig.kernel_bull[j] and sig.kernel_bear[j - 1])
            )
            if krev:
                return float(sig.close[j]), "kernel"
            if new_sig(j, -d):
                return float(sig.close[j]), "oppo"
        return float(sig.close[-1]), "eod"

    rows: list[dict] = []
    state, p_dir, p_hi, p_lo, p_exp = "FLAT", 0, 0.0, 0.0, -1
    for i in range(1, n):
        bts = ts[i].to_pydatetime()
        nl, nshort = new_sig(i, 1), new_sig(i, -1)
        if state == "PENDING":
            if (p_dir == 1 and nshort) or (p_dir == -1 and nl) or i > p_exp:
                state = "FLAT"
                continue
            buf = max(
                cfg.breakout_buffer_min_pts,
                cfg.breakout_buffer_atr_frac * _nz(sig.atr[i]),
            )
            if cfg.breakout_confirmation == "close":
                px = sig.close[i]
            else:
                px = sig.high[i] if p_dir == 1 else sig.low[i]
            broke = (p_dir == 1 and px > p_hi + buf) or (p_dir == -1 and px < p_lo - buf)
            if broke:
                entry = float(sig.open[i] if i + 1 < n else sig.close[i])
                atr14 = _nz(sig.atr[i])
                stop = (
                    p_lo - cfg.stop_buffer_atr_frac * atr14 if p_dir == 1
                    else p_hi + cfg.stop_buffer_atr_frac * atr14
                )
                exit_px, rsn = walk_exit(i, p_dir, bts.date(), stop)
                rows.append({
                    "date": bts.date(), "dir": p_dir,
                    "ret_pts": (exit_px - entry) * p_dir, "reason": rsn,
                })
                state = "FLAT"
            continue
        if bts.time() < ss or bts.time() > ec:
            continue
        base_ok = (
            sig.filt_vol[i] and sig.filt_regime[i]
            and (sig.filt_adx[i] or not cfg.use_adx_filter)
        )
        kl = (not cfg.trade_with_kernel) or sig.kernel_bull[i]
        ks = (not cfg.trade_with_kernel) or sig.kernel_bear[i]
        cl = nl and kl and base_ok
        cs = nshort and ks and base_ok
        if cfg.feature_set == "B":
            cl = cl and sig.rsi14[i] > 50 and sig.filt_adx[i] and sig.ema_up[i]
            cs = cs and sig.rsi14[i] < 50 and sig.filt_adx[i] and sig.ema_dn[i]
        if cl or cs:
            p_dir = 1 if cl else -1
            p_hi, p_lo, p_exp = sig.high[i], sig.low[i], i + cfg.breakout_max_wait
            state = "PENDING"

    if not rows:
        print("underlying-only: no trades")
        return
    _print_splits(pd.DataFrame(rows), "underlying-only signal validation")


# =========================================================================
# Futures mode (--futures) -- one continuous instrument, spec §4 exit stack
# =========================================================================
def _futures_enter(
    i: int, direction: int, sig_hi: float, sig_lo: float, sig: Signals,
    u1_reset: pd.DataFrame, cfg: Config,
    per_day: dict[date, int], stopped_dirs: dict[date, set[int]],
) -> Trade | None:
    entry_ts = pd.Timestamp(sig.ts[i]).to_pydatetime()
    bday = entry_ts.date()
    if not cfg.reentry_same_dir_after_stop and direction in stopped_dirs.get(bday, set()):
        return None
    atr14 = _nz(sig.atr[i])
    is_long = direction == 1

    u_entry = _first_at_or_after(u1_reset, entry_ts)
    if u_entry is None:
        return None
    entry_px = float(u_entry["open"])

    # spec §4 stop on the futures price itself
    stop = (sig_lo - cfg.stop_buffer_atr_frac * atr14) if is_long else (
        sig_hi + cfg.stop_buffer_atr_frac * atr14
    )
    cap = cfg.max_risk_atr_frac * atr14
    if atr14 > 0 and abs(entry_px - stop) > cap:
        if cfg.risk_exceed_action == "cap":
            stop = entry_px - cap if is_long else entry_px + cap
        else:
            return None
    per_day[bday] = per_day.get(bday, 0) + 1

    se = _parse_hhmm(cfg.session_end)
    target = None
    if cfg.target_pct:
        target = entry_px * (1 + cfg.target_pct) if is_long else entry_px * (1 - cfg.target_pct)
    want_kernel = cfg.exit_mode in ("kernel_only", "combined")
    want_oppo = cfg.exit_mode in ("classifier_only", "combined")

    ov = u1_reset[u1_reset["timestamp"] > entry_ts]
    exit_time = exit_price = None
    reason = "no_further_data"
    if not ov.empty:
        ots = ov["timestamp"].to_numpy()
        o_hi = ov["high"].to_numpy(float)
        o_lo = ov["low"].to_numpy(float)
        o_cl = ov["close"].to_numpy(float)
        sig_ts = sig.ts
        for j in range(len(ots)):
            ts = pd.Timestamp(ots[j]).to_pydatetime()
            # 1. stop (adverse-first)
            if (is_long and o_lo[j] <= stop) or ((not is_long) and o_hi[j] >= stop):
                exit_time, exit_price, reason = ts, stop, "stop"
                stopped_dirs.setdefault(bday, set()).add(direction)
                break
            # 2. optional target
            if target is not None and (
                (is_long and o_hi[j] >= target) or ((not is_long) and o_lo[j] <= target)
            ):
                exit_time, exit_price, reason = ts, target, "target"
                break
            # 3. 15:15 forced exit
            if ts.time() >= se:
                exit_time, exit_price, reason = ts, o_cl[j], "eod"
                break
            # 4/5. kernel reversal / opposite signal at an aligned 5m close
            if want_kernel or want_oppo:
                pos = int(np.searchsorted(sig_ts, ots[j]))
                if pos < len(sig_ts) and sig_ts[pos] == ots[j] and pos >= 1:
                    krev = (is_long and sig.kernel_bear[pos] and sig.kernel_bull[pos - 1]) or (
                        (not is_long) and sig.kernel_bull[pos] and sig.kernel_bear[pos - 1]
                    )
                    oppo = (is_long and sig.signal[pos] == -1 and sig.signal[pos - 1] != -1) or (
                        (not is_long) and sig.signal[pos] == 1 and sig.signal[pos - 1] != 1
                    )
                    if want_kernel and krev:
                        exit_time, exit_price, reason = ts, o_cl[j], "kernel_reversal"
                        break
                    if want_oppo and oppo:
                        exit_time, exit_price, reason = ts, o_cl[j], "opposite_signal"
                        break
        if exit_price is None:
            exit_time = pd.Timestamp(ots[-1]).to_pydatetime()
            exit_price = float(o_cl[-1])

    t = Trade(
        symbol="NIFTYFUT", entry_time=entry_ts, entry_price=entry_px,
        exit_time=exit_time, exit_price=exit_price, exit_reason=reason,
        atr_entry=atr14, side="BUY" if is_long else "SELL",
        lot_size=cfg.fut_lot_size, stop_price=stop,
    )
    if exit_price is not None:
        t.pnl = (exit_price - entry_px) * direction * cfg.fut_lot_size   # raw; costs in analyzer
    return t


def run_futures(
    bars5: pd.DataFrame, bars1: pd.DataFrame, cfg: Config,
    eval_from_ts: datetime | None, eval_to_ts: datetime | None,
) -> list[Trade]:
    sig = build_signals(bars5, cfg, eval_from_ts)
    ts = pd.to_datetime(sig.ts)
    n = len(sig.ts)
    ss = _parse_hhmm(cfg.session_start)
    ec = _parse_hhmm(cfg.entry_cutoff)
    expiry_days = _monthly_expiry_dates(bars5) if cfg.skip_last_trading_day_of_month else set()
    u1_reset = bars1.sort_values("timestamp").reset_index(drop=True)

    def new_sig(i: int, d: int) -> bool:
        return sig.signal[i] == d and sig.signal[i - 1] != d

    trades: list[Trade] = []
    per_day: dict[date, int] = {}
    stopped_dirs: dict[date, set[int]] = {}
    state, p_dir, p_hi, p_lo, p_exp = "FLAT", 0, 0.0, 0.0, -1

    for i in range(1, n):
        bts = ts[i].to_pydatetime()
        if eval_from_ts is not None and bts < eval_from_ts:
            continue
        if eval_to_ts is not None and bts > eval_to_ts:
            break
        bday = bts.date()
        nl, nshort = new_sig(i, 1), new_sig(i, -1)

        if state == "PENDING":
            if (p_dir == 1 and nshort) or (p_dir == -1 and nl) or i > p_exp:
                state = "FLAT"
                continue
            buf = max(
                cfg.breakout_buffer_min_pts,
                cfg.breakout_buffer_atr_frac * _nz(sig.atr[i]),
            )
            if cfg.breakout_confirmation == "close":
                px = sig.close[i]
            else:
                px = sig.high[i] if p_dir == 1 else sig.low[i]
            broke = (p_dir == 1 and px > p_hi + buf) or (p_dir == -1 and px < p_lo - buf)
            if broke:
                t = _futures_enter(i, p_dir, p_hi, p_lo, sig, u1_reset, cfg,
                                   per_day, stopped_dirs)
                if t is not None:
                    trades.append(t)
                state = "FLAT"
            continue

        # FLAT: candidate detection
        if bts.time() < ss or bts.time() > ec:
            continue
        if bday in expiry_days:
            continue
        if bts.strftime("%A") in cfg.skip_weekdays:
            continue
        if per_day.get(bday, 0) >= cfg.max_trades_per_day:
            continue
        base_ok = (
            sig.filt_vol[i] and sig.filt_regime[i]
            and (sig.filt_adx[i] or not cfg.use_adx_filter)
        )
        kl = (not cfg.trade_with_kernel) or sig.kernel_bull[i]
        ks = (not cfg.trade_with_kernel) or sig.kernel_bear[i]
        cl = nl and kl and base_ok
        cs = nshort and ks and base_ok
        if cfg.feature_set == "B":
            cl = cl and sig.rsi14[i] > 50 and sig.filt_adx[i] and sig.ema_up[i]
            cs = cs and sig.rsi14[i] < 50 and sig.filt_adx[i] and sig.ema_dn[i]
        if cl or cs:
            p_dir = 1 if cl else -1
            p_hi, p_lo, p_exp = sig.high[i], sig.low[i], i + cfg.breakout_max_wait
            state = "PENDING"

    return trades


# =========================================================================
# main
# =========================================================================
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--options-subdir", default="options_1min_past")
    ap.add_argument("--underlying-source", default="alice_index",
                    choices=["alice_index", "spot", "futures_proxy"])
    ap.add_argument("--expiry", type=date.fromisoformat, default=None)
    ap.add_argument("--all-expiries", action="store_true")
    ap.add_argument("--near-expiry-days", type=int, default=6)
    ap.add_argument("--from", dest="from_date", type=date.fromisoformat, default=None)
    ap.add_argument("--to", dest="to_date", type=date.fromisoformat, default=None)
    ap.add_argument("--config", default=None, help="JSON overrides for Config")
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--out-csv", type=Path, default=None)
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--underlying-only", action="store_true")
    ap.add_argument("--futures", action="store_true",
                    help="run the futures/no-Greeks backtest (spec §4 exits, CSV out)")
    ap.add_argument("--futures-source", default="index_proxy",
                    choices=list(_FUT_SRC))
    args = ap.parse_args()

    if args.selfcheck:
        sys.exit(selfcheck(args.data_dir))

    cfg = Config.from_json(args.config)

    if args.underlying_only:
        underlying_only(args.data_dir, cfg)
        return

    if args.futures:
        b1 = _load_futures_1min(args.data_dir, args.futures_source)
        b5 = _resample(b1, cfg.signal_tf_min)
        ef = datetime.combine(args.from_date, time(0, 0)) if args.from_date else None
        et = datetime.combine(args.to_date, time(23, 59)) if args.to_date else None
        # date-shard: split [ef|data-min .. et|data-max] into shard_count
        # contiguous day-aligned ranges (safe -- every trade is intraday).
        if args.shard_count > 1:
            lo = ef or pd.Timestamp(b5["ts"].min()).to_pydatetime()
            hi = et or pd.Timestamp(b5["ts"].max()).to_pydatetime()
            total = (hi.date() - lo.date()).days + 1
            step = -(-total // args.shard_count)  # ceil
            s0 = lo.date() + timedelta(days=step * args.shard_index)
            s1 = min(hi.date(), lo.date() + timedelta(days=step * (args.shard_index + 1) - 1))
            if s0 > hi.date():
                print(f"shard {args.shard_index}: empty range, nothing to do")
                return
            ef = datetime.combine(s0, time(0, 0))
            et = datetime.combine(s1, time(23, 59))
        print(f"futures: source={args.futures_source} window="
              f"{ef.date() if ef else 'all'}..{et.date() if et else 'all'}")
        trades = run_futures(b5, b1, cfg, ef, et)
        print("\n" + summary(trades))
        rows = [{
            "date": t.entry_time.date(),
            "ret_pts": float(t.pnl) / cfg.fut_lot_size,
            "reason": t.exit_reason,
            "dir": 1 if t.side == "BUY" else -1,
        } for t in trades if t.pnl is not None]
        if rows:
            _print_splits(pd.DataFrame(rows), "futures signal validation (gross pts/lot)")
        if args.out_csv:
            write_csv(trades, args.out_csv)
            print(f"wrote {args.out_csv}")
        return

    src_file = {
        "alice_index": "NIFTY_alice_index_1min.csv",
        "spot": "NIFTY_1min.csv",
        "futures_proxy": "NIFTY_underlying_proxy_1min.csv",
    }[args.underlying_source]
    under1_full = _load_1min(args.data_dir / "underlyings" / src_file)
    under5_full = _resample(under1_full, cfg.signal_tf_min)

    opt_base = args.data_dir / args.options_subdir / "NIFTY"
    if args.expiry:
        expiries = [args.expiry]
    elif args.all_expiries:
        expiries = _discover_expiries(opt_base)
    else:
        raise SystemExit("pass --expiry or --all-expiries (or --selfcheck / --underlying-only)")

    if args.from_date:
        expiries = [e for e in expiries if e >= args.from_date]
    if args.to_date:
        expiries = [e for e in expiries if e <= args.to_date]
    if args.shard_count > 1:
        expiries = [e for k, e in enumerate(expiries) if k % args.shard_count == args.shard_index]

    all_trades: list[Trade] = []
    for e in expiries:
        edir = opt_base / e.isoformat()
        if not edir.is_dir():
            print(f"  [{e}] no dir, skip")
            continue
        # underlying context: everything up to the expiry (classifier needs history)
        u1 = under1_full[under1_full["timestamp"] <= pd.Timestamp(e) + pd.Timedelta(days=1)]
        u5 = under5_full[under5_full["ts"] <= pd.Timestamp(e) + pd.Timedelta(days=1)]
        if len(u5) < cfg.max_bars_back + cfg.ema_period + 100:
            print(f"  [{e}] not enough underlying history ({len(u5)} bars), skip")
            continue
        tr = run_expiry(e, edir, u5.reset_index(drop=True), u1.reset_index(drop=True),
                        cfg, args.near_expiry_days)
        print(f"  [{e}] {len(tr)} trades")
        all_trades.extend(tr)

    print("\n" + summary(all_trades))
    if args.out_csv:
        write_csv(all_trades, args.out_csv)
        print(f"wrote {args.out_csv}")


if __name__ == "__main__":
    main()
