# Loren track — Lorentzian Classification backtest (Framework 5)

> **Naming**: this is the **"Loren" track**, NOT ORB W7. ORB's Sweep #3 uses
> W6/W7/W8 (`run_refined_sweep_3w*.sh`). The Loren track has two passes:
> `loren-options` (`run_loren_options_sweep.sh`, done 2026-08-29) and
> `loren-futures` (`run_loren_futures_sweep.sh`, this file's futures section).
> It was originally "Framework 5 / Part C" of `SWEEP_3W6_STRUCTSTOP_PLAN.md`.

Status: **both passes RUN 2026-08-29 → PARK.** `loren-options`: all 17 configs
fail the robust bar (severe IS/OOS split). `loren-futures` Pass 0: the signal
has no tradeable edge on futures either — gross ~+6 pts/trade, erased by real
F&O cost; TRAIN/VALID both net-negative. Per `Loren_Futures.txt` §5 this means
**the fix is in the signal/filters, not the instrument.** Details in
`BACKTEST_LEARNINGS.md`. Everything below is the as-built record + the deferred
A1 `Strategy`-subclass integration plan.

---

## RUN-NOW EXECUTION (standalone, e4) — supersedes Stages 1–3 below for this pass

User decision 2026-08-29: run the Loren backtest on e4 **now**, before it dies,
rather than waiting for the A1 pipeline integration. Since Lorentzian is a
brand-new signal engine with zero representation in `strategy_engine`, and
integrating + QC-ing a real `Strategy` subclass in the ~2-day e4 window is not
realistic, the e4 pass uses a **self-contained script** instead:

- **`scripts/loren_backtest.py`** — pandas/numpy only, no app/DB/`run_backtest.py`
  imports. Full Lorentzian classifier + NW kernel + filters + spec §3 breakout
  state machine + real NIFTY option execution (11-strike chains,
  `options_1min_past`) + spec §4 exit stack. Ported from `advanced-ta` 0.1.8
  (Appendix A), two documented divergences (sliding-window neighbour pool;
  causal `normalize()`). `--selfcheck` proves non-repainting (PASSES).
  Per-trade CSV columns are byte-compatible with `analyze_walkforward.py` /
  `analyze_refined_batches.py`. `ruff`/`mypy` clean.
- **`scripts/run_loren_sweep_3w7.sh`** — 17-config sweep driver + a
  `--underlying-only` 60/20/20 signal-validation pass. No Postgres, no
  `--db-suffix`, **no reaper needed** (the standalone script touches no DB).
  Runs under `~/an_venv`. ETA on e4 with `SHARDS=12`: ~2–4 min.

**Local validation done before shipping (2026-08-29):**
- `--selfcheck`: non-repainting prediction=OK, kernel=OK.
- `--underlying-only` (3.2 yr NIFTY 5m, spec §8 overfitting check):
  **ALL n=833, 53.4% win, +22.5 pts/trade, PF 3.03**; TRAIN 51.4%/+22.8/2.97,
  VALID 54.3%/+19.5/3.12, **TEST 58.4%/+25.0/3.14**. Train→test deltas small and
  *positive* — no overfit at the signal level. `stop` exits lose (−28 pts);
  `kernel`/`eod`/`oppo` exits win. **Green light for the option-level sweep.**
- Option-level probes (subset): spec-literal `max_risk` reject → only ~7
  trades/yr (73% PF 2.4 but n far too small). `risk_exceed_action:"cap"` →
  ~180 trades/yr, ~25% win gross-+ve. `exit_mode:"combined"` (kernel-managed
  exits) is the clear improver → ~36% win, gross +141/lot, PF 1.19. **After the
  0.5%/side cost model, gross-+ve configs can go net-negative** (option premiums
  are large) — the sweep + `analyze_walkforward` decide which survive.

**To run on e4** (from `~/trading-bot/backend`, after `scp`-ing both new files):
```
setsid bash scripts/run_loren_sweep_3w7.sh </dev/null >/tmp/sweep3w7_nohup.log 2>&1 & disown
tail -f /tmp/sweep3w7_status.log
```
Env knobs: `SHARDS` (default 12), `NEAR_DAYS` (default 6), `PY` (default
`~/an_venv/bin/python`). Results land in
`data/historical/backtest_reports/sweep3w7_loren_<STAMP>/<name>_current.csv`.
Then:
```
~/an_venv/bin/python scripts/analyze_walkforward.py \
  --dir data/historical/backtest_reports/sweep3w7_loren_<STAMP> \
  --configs l_spec,l_cap,l_cap15,l_combined,l_kernel,l_tgt35_comb,l_featB,l_itm,l_best \
  --oos-from 2026-03-01
```

**The 17 configs** (`run_loren_sweep_3w7.sh` `CONFIGS`): fidelity baselines
(`l_spec` = pure spec, `l_cap`); risk-handling (`l_cap15`, `l_cap25`);
exit-mechanism / spec's 3 exit modes (`l_combined`, `l_kernel`, `l_oppo`,
`l_comb_cap075`); target overlays (`l_tgt35_comb`, `l_tgt50_comb`); and the
win-rate / PnL levers stacked on the `combined` base (`l_featB` trend-confirmed
features, `l_itm` 1-strike-ITM, `l_morning` 09:20–12:00 entries only, `l_k5` /
`l_k11` neighbour count, `l_wait3` tighter breakout wait, `l_best` = featB +
ITM + 0.40 target combined). All flagged in the driver by group.

**Everything below is the original full plan** — still the blueprint for the
proper `Strategy`-subclass integration on A1 after e4. The standalone run is a
first read, not a substitute for it.

---

## loren-futures pass — as built + RESULT (2026-08-29)

Spec: `D:\Documents\Trading Bot_Oracle\Loren_Futures.txt` (v3, futures-first):
same signal/breakout/stop/exit logic on NIFTY **near-month futures**, no option
Greeks — isolate whether the signal itself has edge. Data reality: no usable
multi-year 1-min futures series exists (Shoonya ~41 continuous days 2026-07/08;
TrueData stitch ~56 sparse pre-expiry days; only `NIFTY_alice_index_1min.csv`
has 3 yr, cash index, volume=0). The Loren signal is 100% price-only, so the
cash index is a sound no-Greeks proxy (basis ~0.3%, mean-reverting, immaterial
intraday) and the only series with the 3 yr the 60/20/20 split needs.

Built (all `ruff`/`mypy` clean, `--selfcheck` passes):
- `loren_backtest.py --futures --futures-source {index_proxy,shoonya,truedata_stitch}`
  — new `run_futures` / `_futures_enter` reusing `build_signals` + the breakout
  state machine, minus option code. Real spec §4 exit stack honoring
  `exit_mode` (stop → target → 15:15 EOD → kernel_reversal → opposite_signal).
  Real shorts (`side` BUY/SELL). Stop on the futures price itself.
  `_monthly_expiry_dates` skips the last trading day of each month (weekday-
  agnostic — NIFTY expiry weekday changed over the span). CSV byte-identical to
  `_TRADE_CSV_HEADER`; `pcr_entry`/`pcr_exit` repurposed to carry stop price /
  risk-points.
- `analyze_loren_futures.py` — real Indian-F&O futures cost model (brokerage +
  STT 0.02% sell-side + txn + stamp + GST + fixed-tick slippage ≈ ₹460/lot ≈
  7 pts), 60/20/20 split w/ TRAIN→VALID→TEST deltas, IS/OOS, 6-mo halves, 10k
  bootstrap, slippage sensitivity, spec-§8 breakdowns, risk-% overlay.
- `run_loren_futures_sweep.sh` — `MODE=pass0` edge gate (7 configs +
  real-futures cross-check) then `MODE=pass1` spec-§6 matrix. Shards by config.

**PASS 0 RESULT → PARK. Pass 1 NOT run** (per the gate).
- Spec-literal `reject if risk > 0.75·ATR14` trades **3–6 times in 3 years**
  (n far too small — same as loren-options). Only `f_cap`
  (`risk_exceed_action:"cap"`) produces a real sample: **n=566, 26.9% win,
  gross +6.2 pts/trade, net −0.9 pts/trade (−₹60/lot), PF 0.95, maxDD ₹127k,
  worst streak 16.**
- TRAIN −129 / VALID −223 / TEST +325 Rs/lot; IS −126 / OOS +38; H1 −188 /
  H2 +54 — the same OOS-only, no-in-sample-foundation shape as loren-options.
  Bootstrap **P(mean≤0) = 0.68**. Gets worse at higher slippage.
- Conclusion (matches `Loren_Futures.txt` §5): the loren-options IS/OOS
  collapse is **not** an option-execution artifact. On a clean instrument with
  tiny costs, the gross edge (~6 pts/trade) is too thin to clear real
  transaction cost and has no stable in-sample base. **The problem is in the
  signal/filters.** Any revival must fix the signal first — not re-test the
  instrument, not run the §6 sweep.

---

**Scope boundary (explicit user decision 2026-08-29):** this framework is a
**standalone new strategy**, kept *separate* from the ORB / `orb_conviction`
sweep #3 W1–W6 findings. Do **not** fold ORB's `require_prior_day_trend`,
`max_or_range_nifty_points`, `stop_pct:0.15/0.18` etc. into Loren configs. ORB
learnings are used here for **method only** (canonical harness setup,
`--near-expiry-days 6`, `--exit-mode current`, the `analyze_walkforward` robust
bar, the DB-reaper / distinct-prefix / launch gotchas). Extra Loren configs
informed by those learnings are allowed where the *mechanism* transfers (e.g.
a loose-premium-stop backstop, an OR-width-style volatility gate) — flagged as
such, never as a shared parameter.

**Source spec:** `D:\Documents\Trading Bot_Oracle\Loren setup.txt` ("Loren-Style
Backtest Spec — v2"). Config there is the single source of truth for
parameters; where it diverges from the reference implementation this doc's
Appendix A calls it out.

**Where it runs:** **NOT e4.** The e4 backtest VM (`129.159.226.106`)
terminates night of 2026-08-31 and its remaining window is committed to W6
(structure-stop sweep, ORB). W7 is a multi-day build (new strategy class +
harness hooks), so it targets the **A1 co-located backtest box**
(`144.24.137.112`, Phase 3+ per `docs/ops/a1_expansion_backtest_colocation.md`)
once e4 is gone. Stage 0 (below) needs no VM at all and can start immediately
on the local machine.

Read `BACKTEST_LEARNINGS.md`'s "CANONICAL RELIABLE-BACKTEST SETUP" section
first — every non-negotiable there applies to Stage 2/3.

---

## 0. What the strategy is (one paragraph)

jdehorty's "Machine Learning: Lorentzian Classification" (TradingView, PineScript
"Most Valuable" 2023) is an approximate k-NN classifier over a 5-feature vector
(RSI, WaveTrend, CCI, ADX, RSI), using **Lorentzian distance**
(`Σ log(1+|Δfᵢ|)`) instead of Euclidean, with a chronological-spacing trick
(only every 4th historical bar is a candidate neighbour). The label is the sign
of price change 4 bars ahead. Its raw prediction (`Σ` of the 8 nearest
neighbours' labels, range −8..+8) is gated by a volatility filter, a
Kalman-slope regime filter, and a **non-repainting Nadaraya-Watson kernel**
(rational-quadratic). The v2 spec wraps that signal in a **breakout-confirmation
state machine** (enter only on a close beyond the signal bar's high/low within 5
bars), an **ATR-based stop**, and **NIFTY option execution** (nearest weekly,
ATM or 1-ITM). Full ported algorithm + exact constants: **Appendix A**.

---

## Stage 0 — signal-only prototype (local, no repo changes, START HERE)

**Goal:** decide whether there is any underlying-level edge *before* spending
option-data or harness-wiring effort. Settle non-repainting, feature params,
and all filter tuning here, on the **train slice only**.

**Data:** `backend/data/historical/underlyings/NIFTY_alice_index_1min.csv`
(real NIFTY index, **2023-06-13 → 2026-08-20**, 1-min OHLC, `volume=0` — fine,
Lorentzian uses none of RSI/WT/CCI/ADX needs volume; WT uses hlc3). ~3.1 yr,
the long history the classifier's 2000-bar lookback wants.

**Script:** `backend/scripts/loren_signal_prototype.py` (new, standalone —
pandas/numpy only, NOT wired into the app). Steps:

1. **Load & resample** 1-min → **5-min** bars, RTH only (09:15–15:29 IST),
   `label = last`, drop partial sessions. Session-of-interest window per the
   spec is 09:20–15:15 for *entries*; compute indicators on the full RTH
   series, gate entries to the window.
2. **Features** (Appendix A §1) — port `n_rsi`/`n_wt`/`n_cci`/`n_adx` +
   `normalize`/`rescale`. Spec params: `RSI(9,1)`, `WT(10,11)`, `CCI(20,1)`,
   `ADX(20,2)`, `RSI(9,1)`. (Reference default f1 is `RSI(14,1)`; spec says 9
   — spec wins, note the divergence in the run log.)
3. **k-NN classifier** (Appendix A §2) — the ANN loop with `neighborsCount=8`,
   `maxBarsBack=2000`, `i%4` gate, `lastDistance` lower-25% reset. Output
   `prediction[t] ∈ [−8, 8]`.
4. **Filters** (Appendix A §4) — volatility (ATR(1) > ATR(10)), regime (KLMF
   slope-decline ≥ `−0.1`), ADX off by default.
5. **Kernel** (Appendix A §3) — `rationalQuadratic(src, h=8, r=8, x=25)`;
   `gaussian(src, h−lag=6, x=25)` for the smoothing variant. **Non-repainting
   check:** freeze `yhat1[t]` and `prediction[t]` at the moment bar `t` is
   processed; re-run the whole pipeline on `series[:t+k]` for several `k` and
   assert `yhat1[t]` / `prediction[t]` are unchanged (bit-for-bit). This is the
   single most important check in Stage 0 — the spec calls it "the most common
   source of inflated backtest performance." The reference `rationalQuadratic`
   is already backward-only (`src.shift(i)`, i≥0) so it *should* pass; the test
   is what proves it for our resampling.
6. **Signals** — `new_buy = prediction>0 and not prediction[t-1]>0` (and the
   bearish mirror); `kernel_bullish = yhat1[t] > yhat1[t-1]`;
   `kernel_green_transition[t] = kernel_bullish[t] and kernel_bearish[t-1]`
   (spec §1's single unified definition).
7. **State machine** (spec §3, NOT the reference's fixed 4-bar exit):
   `Flat → candidate → Pending(dir, signal_bar, signal_hi, signal_lo,
   expiry=t+5) → close beyond signal_hi/lo within 5 bars → Enter next bar
   open → cancel on opposite confirmed signal or timeout`.
   `candidate = new_signal and kernel_aligned and volatility_filter and
   regime_filter` (spec §2 Version A). Version B adds
   `rsi>50 / adx≥20 / close vs ema200`.
8. **Exit** (underlying-only P&L for Stage 0): `stop = signal_lo − 0.10·ATR14`
   (long) / `signal_hi + 0.10·ATR14` (short), reject if
   `risk > 0.75·ATR14`; then priority `stop → kernel reversal transition →
   opposite new signal → 15:15`.
9. **Split** (spec §0): 60/20/20 by calendar on the 3-yr series ≈
   train `2023-06-13 → 2025-01`, validation `2025-01 → 2025-11`,
   test `2025-11 → 2026-08`. **Tune only on train.** Report:
   trades, win%, avg fwd-return per trade, expectancy in index points,
   profit factor, max consecutive losses, results by weekday / hour /
   vol-regime, and the **train vs validation delta** (overfitting check).
10. **Sweeps on train** (spec §6, adapted — signal level, cheap here):
    wait ∈ {4,5,6}; exit ∈ {kernel_only, classifier_only, combined};
    ADX ∈ {off,20,25}; EMA200 ∈ {off,on}; stop buffer ∈ {0, 0.10, 0.25}·ATR;
    breakout confirm ∈ {wick, close}; kernel smoothing ∈ {off,on};
    `neighborsCount` ∈ {5,8,11}; feature-set A vs B.

**Kill gate:** if train-slice expectancy isn't clearly positive in index points
(after a nominal 1-tick slippage) and the validation delta isn't small, **stop
here** — do not proceed to Stage 1. Record the negative result in
`BACKTEST_LEARNINGS.md` and the memory, same as the 4 parked ORB frameworks.

**Deliverable:** `loren_signal_prototype.py` + a short results table appended to
`BACKTEST_LEARNINGS.md` (newest-first, IST) + a go/no-go recommendation.

---

## Stage 1 — real `Strategy` subclass (A1 or local, gated on Stage 0 = GO)

New file `backend/app/modules/strategy_engine/strategies/lorentzian.py`.

**Class shape:** subclass `Strategy` **directly** (not
`ConfirmationFilterStrategy` — the breakout state machine and the
"Pending" holding state don't fit that template's single-bar
`check_setup`). Implement `evaluate(db, strategy_run, latest_bar) ->
TradeProposal | None`.

**Constructor params** (all with defaults from the spec §0 config):

```
instrument_id, expiry_date,
signal_timeframe_minutes: int = 5,
neighbors_count: int = 8,
max_bars_back: int = 2000,
feature_set: str = "A",              # "A" = spec 5-feature; "B" adds trend confirm
rsi1_a=9, rsi1_b=1, wt_a=10, wt_b=11, cci_a=20, cci_b=1, adx_a=20, adx_b=2, rsi2_a=9, rsi2_b=1,
use_volatility_filter: bool = True,
use_regime_filter: bool = True,
regime_threshold: float = -0.1,
use_adx_filter: bool = False,
adx_threshold: float = 20.0,
use_ema_filter: bool = False, ema_period: int = 200,
kernel_h: int = 8, kernel_r: float = 8.0, kernel_x: int = 25,
kernel_lag: int = 2, use_kernel_smoothing: bool = False,
trade_with_kernel: bool = True,
breakout_max_wait_candles: int = 5,
breakout_confirmation: str = "close_beyond_signal_level",   # or "wick"
breakout_buffer_atr_frac: float = 0.05, breakout_buffer_min_points: float = 1.0,
stop_buffer_atr_frac: float = 0.10, max_risk_atr_frac: float = 0.75,
session_start_ist: str = "09:20", session_end_ist: str = "15:15",
skip_weekly_expiry_day: bool = True,
max_trades_per_day: int = 3, reentry_same_dir_after_stop: bool = False,
qty_lots: int = 1,
ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
strike_rule: str = "ATM",           # "ATM" | "1_ITM"
```

**5-min aggregation:** the pipeline feeds 1-min completed bars. Cache them
in-memory on the instance (same durability class as ORB's `_fired_directions`)
and roll into 5-min buckets aligned to `09:15`; run the classifier only when a
5-min bucket *completes*. Warm-up: needs `max_bars_back` 5-min bars ≈ 10 000
1-min bars ≈ 27 trading days before the first live signal — see Stage 2 harness
change.

**In-memory rolling state** (lost on restart, acceptable — matches every other
strategy): `list[Bar5m]`, plus the state-machine enum
(`FLAT | PENDING`) with `pending_dir / signal_bar_ts / signal_hi / signal_lo /
pending_expiry_idx`, plus `_trades_today` and `_stopped_dirs_today` keyed by
IST date.

**On a completed 5-min bar:**
1. If IST date changed → reset `_trades_today`, `_stopped_dirs_today`; if
   `skip_weekly_expiry_day and bar.date() == self.expiry_date` → do nothing all
   day.
2. Append bar; if `< max_bars_back + kernel_x + 20` bars buffered → return None
   (warming up).
3. Recompute features / prediction / filters / kernel **over the trailing
   window** (see Appendix A; recompute fresh each call, consume only `[t]` and
   `[t-1]` — never persist a bar's kernel/prediction and reuse it: that is the
   non-repainting guarantee).
4. `FLAT`: if `candidate` (spec §2, `feature_set` selects A/B) and
   `len(_trades_today) < max_trades_per_day` and entry-window open →
   `PENDING(dir, this_bar)`.
5. `PENDING`: opposite confirmed signal → back to `FLAT`. Else if
   `close` beyond `signal_hi`(long)/`signal_lo`(short) by `buffer` (spec §0
   `breakout.buffer`) → **emit** `TradeProposal`. Else if
   `bars_since_signal > breakout_max_wait_candles` → `FLAT`.
6. On emit: `_trades_today.append(...)`; `state → FLAT` (position now owned by
   execution). If `reentry_same_dir_after_stop is False` and this dir is in
   `_stopped_dirs_today` → suppress (return None).

**Strike + prices on emit:**
- `ranked = rank_from_latest_snapshot(db, instrument_id, expiry_date,
  ranking_config)`; pick CE for a long / PE for a short; `strike_rule` "1_ITM"
  = one step toward ITM from ATM (reuse `strike_ranking` offset, don't
  hand-roll).
- `entry_price = top.ltp` (option premium).
- `stop_price` (premium) and `structure_level` (underlying) — see next.

**Stop mapping (spec §4 is on the *underlying*; execution stop is on the
*premium*):**
- `structure_level` = `signal_lo − stop_buffer_atr_frac·ATR14` (long) /
  `signal_hi + stop_buffer_atr_frac·ATR14` (short), on the underlying index.
  Feed as `TradeProposal.structure_level` so `--exit-mode current`'s
  structure-break step (underlying re-crosses the level, ±ATR buffer,
  bar-close confirm) is the primary risk exit — the faithful translation of
  the spec's chart-based stop.
- Reject the whole signal if `abs(entry_underlying − structure_level) >
  max_risk_atr_frac·ATR14` (spec's `risk_points > 0.75·ATR14` guard).
- `stop_price` (premium): set a *loose* premium backstop so the underlying
  structure stop is what actually fires (mechanism borrowed from ORB W4's
  "looser is better" finding — **not** the ORB value). Start `stop_pct = 0.40`;
  W7 sweeps it. `target_price`: none by default (spec has no fixed target) —
  set `target_pct` very high or use the harness "no target" path.
- `structure_break_buffer` = `resolve_structure_break_buffer(...)`,
  `structure_break_persistence_seconds` = default (collapses to next-bar at
  1-min replay — documented ceiling).

**Wiring (3 places — Part C checklist):**

| File | Change |
|---|---|
| `strategy_engine/strategies/lorentzian.py` | new `LorentzianStrategy` |
| `strategy_engine/strategies/__init__.py` | export it |
| `api/v1/strategies.py` | `LORENTZIAN_PARAM_KEYS = {…}` (every ctor kwarg above except `instrument_id`/`expiry_date`/`ranking_config`); `_build_strategy` branch `if strategy_type == "lorentzian": return LorentzianStrategy(instrument_id, expiry_date, **{k:v for k,v in params.items() if k in LORENTZIAN_PARAM_KEYS})`; add `"lorentzian"` to `KNOWN_STRATEGY_TYPES` |
| `scripts/run_backtest.py` | add `"lorentzian"` to `STRATEGY_TYPES` (line ~368) |

**Indicators:** `n_rsi`/`n_wt`/`n_cci`/`n_adx`/`normalize`/`rescale`/
`regime_filter`/`filter_volatility`/`rationalQuadratic`/`gaussian` go in a new
`strategy_engine/lorentzian_kernel.py` (pure functions, no app imports) so the
Stage 0 prototype and the real strategy share **one** implementation. Unit-test
each against a hand-computed fixture.

**Tests:** `tests/unit/test_lorentzian_strategy.py` — non-repainting assertion
(freeze/replay), state-machine transitions (candidate→pending→enter, timeout,
opposite-signal cancel), the `0.75·ATR` risk reject, expiry-day skip,
`max_trades_per_day` cap. Plus add `"lorentzian"` to the existing
multi-strategy concurrency e2e. `ruff check .` + `mypy app tests` clean.

---

## Stage 2 — options backtest harness changes (`run_backtest.py`)

### A. `--warmup-bars` flag (required — the 1000-bar cap is fatal for this strategy)

`run_backtest.py:2318` hardcodes `warmup_bars = […][-1000:]` (~2.7 trading
days). Lorentzian needs ≈ 27 trading days of 5-min history before its first
signal. Add:

```python
parser.add_argument(
    "--warmup-bars", type=int, default=1000,
    help="1-min underlying bars of pre-window warm-up primed through the "
    "indicator/strategy pipeline before strategy evaluation begins. Default "
    "1000 (~2.7 trading days) suits EMA/ATR-only strategies; Lorentzian "
    "needs ~15000 (max_bars_back 5-min bars). Higher = slower replay, "
    "linearly.",
)
```
and thread `args.warmup_bars` into `_run_single_backtest` → the
`warmup_bars = [b for b … ][-args.warmup_bars:]` line. Every other strategy's
runs stay byte-identical at the default. For W7 pass `--warmup-bars 15000`.

**Smoke / non-regression:** run `orb` with and without the flag at default —
must be byte-identical to an existing `sweep3w4` baseline CSV.

### B. `--loren-exit-mode` hook (for the spec's 3 exit-mode variants)

`--exit-mode current` reconstructs exits *outside* the strategy
(`_reconstruct_exit_current` walks premium bars: stop/target/structure-break/
spread-blowout/trail/EOD). The spec's **kernel-reversal exit** and
**opposite-Lorentzian-signal exit** are underlying-signal events after entry,
which that loop doesn't currently model. Add them the same way W6 added
`--structure-stop-mode` — a harness-computed early-exit, not strategy code:

```python
parser.add_argument(
    "--loren-exit-mode", default="classifier_structure",
    choices=["classifier_structure", "kernel_only", "classifier_only", "combined"],
    help="`--strategy lorentzian --exit-mode current` only. Adds an early "
    "exit when, at a 5-min bar close at/after entry: 'kernel_only' = yhat1 "
    "makes a reversal transition against the position; 'classifier_only' = a "
    "new opposite Lorentzian signal prints; 'combined' = first of the two. "
    "'classifier_structure' (default) = neither extra trigger, i.e. today's "
    "structure-break + premium-stop + EOD stack only. Ignored for every "
    "other strategy / exit mode.",
)
```

Implementation (in the `mode == "current"` branch, near W6's swing/pivot
block): the harness already has `all_underlying_bars`. Resample to 5-min,
recompute `yhat1` (Appendix A §3, params from `--strategy-params` or the
strategy's defaults via `_build_strategy`) and the `prediction`/`signal` series
(Appendix A §2/§4 — reuse `strategy_engine/lorentzian_kernel.py`), once per
expiry (cache on `(expiry, param-hash)`). Then a helper
`_loren_early_exit_ts(entry_ts, direction) -> datetime | None` returns the
first 5-min bar close strictly after `entry_ts` where the selected condition
holds; pass it into `_reconstruct_exit_current` as an extra early-exit
timestamp compared alongside the structure-break hit (earliest wins; exit at
that bar's option close, `exit_reason = "kernel_reversal"` /
`"opposite_signal"`). `None` → unchanged behaviour.

`ruff`/`mypy` clean; byte-identical check for `classifier_structure` vs a
pre-change `--strategy lorentzian --exit-mode current` smoke CSV.

### C. Canonical invocation (Stage 2, A1 box, from `~/trading-bot/backend`)

```
./.venv/bin/python scripts/run_backtest.py \
  --strategy lorentzian --underlying NIFTY \
  --all-expiries --options-subdir options_1min_past \
  --underlying-source alice_index \
  --exit-mode current --fast \
  --near-expiry-days 6 \
  --warmup-bars 15000 \
  --loren-exit-mode classifier_structure \
  --strategy-params '<json>' \
  --shard-count 18 --shard-index <i> \
  --db-suffix s3w7_<name>_s<i> \
  --out-csv data/historical/backtest_reports/sweep3w7_loren_<name>/<name>_s<i>.csv
```
then `merge_backtest_shards.py --glob '…/<name>_s*.csv' --out …/<name>_current.csv`.

Data reality: **~1 yr of 1-min option premiums** (53 NIFTY weeklies,
Aug'25–Aug'26). `--near-expiry-days 6` → each weekly replayed only for its
current expiry week. A 5-min strategy trading ≤ 3×/day ≈ **60–150 option-level
trades/yr** — thin, so every W7 verdict is "paper-trade to collect live data,"
never "deploy." The 3-yr underlying series is only fully usable at Stage 0
(signal level).

---

## Stage 3 — W7 sweep matrix + driver

Driver `backend/scripts/run_refined_sweep_3w7_lorentzian.sh` — clone
`run_refined_sweep_3w5_frameworks_deep.sh` exactly (it carries `reap_dbs()` +
`trap reap_dbs EXIT` + per-config reap). Change:
- `DB_PREFIX="trading_bot_backtest_s3w7_"`, `--db-suffix "s3w7_${name}_s${i}"`
  — **distinct prefix**, so a concurrent W6 run's reaper can't kill W7's DBs.
- `RESULTS_DIR=…/sweep3w7_lorentzian_${STAMP}`, status/log files `sweep3w7_*`.
- CONFIGS rows are **`name|extraflags|params`** (3 fields, like W6): the loop
  does `--strategy-params "$params" $extraflags` and every row also carries
  `--warmup-bars 15000`.

### Matrix (spec §6, in order; run the winner of each step forward)

Baseline `l_base`: feature-set A, `--loren-exit-mode classifier_structure`,
ADX off, EMA off, `breakout_max_wait_candles:5`, `stop_buffer_atr_frac:0.10`,
`breakout_confirmation:close_beyond_signal_level`, `stop_pct:0.40`.

| step | configs |
|---|---|
| S1 wait candles | `w4` / `w5`(=base) / `w6` → `breakout_max_wait_candles` 4/5/6 |
| S2 exit mode | `x_cls_struct`(base) / `x_kernel` / `x_cls` / `x_combined` → `--loren-exit-mode` |
| S3 ADX filter | `adx_off`(base) / `adx20` / `adx25` → `use_adx_filter:true,adx_threshold:20\|25` |
| S4 EMA200 filter | `ema_off`(base) / `ema200` → `use_ema_filter:true` |
| S5 stop buffer | `sb0` / `sb10`(base) / `sb25` → `stop_buffer_atr_frac` 0.0/0.10/0.25 |
| S6 breakout confirm | `bo_close`(base) / `bo_wick` → `breakout_confirmation` |
| S7 kernel smoothing | `ks_off`(base) / `ks_on` → `use_kernel_smoothing:true` |
| S8 neighbours | `k5` / `k8`(base) / `k11` → `neighbors_count` |
| S9 feature set | `feat_A`(base) / `feat_B` → `feature_set:"B"` (trend-confirmed) |

### Extra configs from ORB-sweep learnings (mechanism transfer only — labelled)

- `l_premstop_loose` — `stop_pct:0.60` (ORB W4: looser premium stop
  monotonically improved survival; test whether the same holds when the real
  risk control is the underlying structure level).
- `l_premstop_tight` — `stop_pct:0.25` (the other end, to confirm direction).
- `l_regime_only` — `use_volatility_filter:false` (isolate the regime filter's
  contribution, mirroring ORB's "which gate actually carries the signal" pass).
- `l_no_struct` — `structure stop effectively off` via a very wide
  `stop_buffer_atr_frac:2.0` + `--loren-exit-mode kernel_only` (spec's pure
  "kernel + EOD" exit; ORB W6's `*_nostop` reference config shape).
- `l_skip_fri` — drop Friday entries (ORB W1 `d_pdt_skipfri` showed weekday
  concentration; check if Loren has one). Needs a `skip_weekdays` ctor param
  (add to `LORENTZIAN_PARAM_KEYS`).

Entries are **identical** across S2 and the exit-only extras → those are pure
exit-overlay re-slices of one replay pass (cheap). S1/S3–S9 change entries →
full replays.

### Launch (A1, from `~/trading-bot/backend`)

```
setsid bash scripts/run_refined_sweep_3w7_lorentzian.sh </dev/null >/tmp/sweep3w7_nohup.log 2>&1 & disown
```
Watch `/tmp/sweep3w7_status.log`. ETA depends on shard/core count on A1 — the
`--warmup-bars 15000` replay is ~3–5× slower per expiry than an ORB run;
budget accordingly and shard wide.

---

## Analysis + acceptance bar

```
~/an_venv/bin/python scripts/analyze_walkforward.py --dir <sweep dir> --configs <csv list>
~/an_venv/bin/python scripts/analyze_refined_batches.py --dir <sweep dir> --configs <csv list> --label loren
```
Costs applied **in analysis only** (flat ₹40/lot + 0.04% turnover + 0.1% STT +
0.5%/side premium slippage; sweep 0.3–1.0% for sensitivity). Risk-% position
sizing (spec §5) also an analysis overlay, never in the sim.

**Acceptance = the Part C loose gate:** ≥ 55% win **AND** positive OOS **AND**
positive both 6-month halves **AND** bootstrap `P(mean ≤ 0) ≤ ~0.20` with a
non-negative 5th percentile **AND** survives 1.0%/side slippage. Plus the
spec's own overfitting check: **train vs validation vs test delta** must be
small (Stage 0 already gates this at signal level; re-check at option level).
Anything short of all of that = **permanent park**, recorded in the ledger +
memory, same as the 4 ORB frameworks.

At the expected n (60–150 option trades/yr, fewer per config) every pass is
"paper-trade to collect live data," never "deploy."

---

## Data ceilings specific to Loren (state in the write-up)

- **5-min signal on a 1-min-replay harness.** Breakout confirmation and kernel
  transitions evaluate on 5-min closes (matches spec intent). Structure-break
  persistence collapses to "survives to next bar" (documented harness ceiling).
- **Non-repainting kernel** must be verified, not assumed (Stage 0 step 5). The
  reference `rationalQuadratic` is backward-only so it should hold, but our
  5-min resampling + rolling recompute is new code.
- **~1 yr option premiums** → val/test slices are small; the 60/20/20 split is
  only truly meaningful on the 3-yr *underlying* series at Stage 0.
- **No real bid/ask** — synthetic spread from OI/volume; the spec's
  `min_liquidity_filter` (reject if spread > X% of premium) can't be honoured
  faithfully. Approximate with the harness's existing `spread_pct` proxy at
  entry-eval time; note it.
- **Regime filter uses EMA(200) of `absCurveSlope`** — needs 200 5-min bars
  itself; folded into the 15000-bar warm-up.
- **`ta` library dependency** — the reference impl uses `ta.momentum.rsi` etc.
  Our port re-implements these from `pandas` to avoid adding `ta` /
  `scikit-learn` to `pyproject.toml` (the `MinMaxScaler` in `normalize` is a
  one-liner: `(x - x.min())/(x.max() - x.min())`). Keep `lorentzian_kernel.py`
  dependency-free.

---

## Gotchas (carried from the W6 plan — all still apply)

- `run_backtest.py` never drops its per-`--db-suffix` DB. Every driver reaps
  its own `trading_bot_backtest_<prefix>*` on start / per-config / EXIT.
  **Distinct prefix per concurrent batch** (`s3w7_`) — a shared prefix means
  one batch's reap kills the other's live DBs.
- Durable fix still owed: `try/finally` DROP (or `--keep-db`) in
  `run_backtest.py:main()`.
- Analysis `--dir`: point at the exact timestamped dir, not `…_*` (a
  killed/retried run leaves an empty second dir the glob picks up).
- Launch remote sweeps as `setsid bash <script> </dev/null >log 2>&1 &
  disown` with `cd` as its **own** statement (`cd DIR; setsid …`), never
  `cd DIR && setsid … &`.
- Kill a runaway sweep by PID/PGID (`kill -- -<pgid>`), never
  `pkill -f run_refined_sweep_…` from an ssh one-liner (matches the ssh
  wrapper's own command line, kills your session).

---

## Pull artifacts (A1 has git; still keep CSVs local)

```
scp -r -i "<key>" ubuntu@144.24.137.112:~/trading-bot/backend/data/historical/backtest_reports/sweep3w7_lorentzian_* <local>
```
Then append results to `BACKTEST_LEARNINGS.md` (newest-first, IST) + update
`memory/project_orb_directional_filter_sweep3_2026_08_29.md` (add a W7 section)
+ `MEMORY.md` index line.

---

# Appendix A — ported algorithm (from jdehorty ref impl, verbatim formulas)

Source: `advanced-ta` 0.1.8 (`LorentzianClassification/{Classifier,MLExtensions,
KernelFunctions,Types}.py`), which is the maintained Python port of jdehorty's
PineScript `MLExtensions/2` + `KernelFunctions/2`. Constants cross-checked
against the TradingView indicator defaults and the v2 spec. All series are
**5-min** bars for W7.

## §1. Features (normalized to [0,1] for the distance metric)

```
rescale(x, old_min, old_max, new_min=0, new_max=1):
    new_min + (new_max-new_min) * (x - old_min) / max(old_max-old_min, 1e-10)

normalize(x, a=0, b=1):          # unbounded → bounded, running min/max
    a + (b-a) * (x - x.min()) / (x.max() - x.min())
    # NOTE for causal use: min/max must be running (expanding), NOT whole-series.
    #   whole-series min/max is lookahead — use x.expanding().min()/.max()
    #   or a rolling window. This is a real repainting trap; the ref impl's
    #   MinMaxScaler over the whole array is fine for their batch plot, NOT
    #   for our bar-by-bar strategy. Verify in Stage 0 step 5.

n_rsi(close, n1, n2):   rescale( EMA(RSI(close, n1), n2), 0, 100 )
n_cci(h,l,c, n1, n2):   normalize( EMA(CCI(h,l,c, n1), n2) )
n_adx(h,l,c, n1):       rescale( ADX(h,l,c, n1), 0, 100 )
n_wt(hlc3, n1=10, n2=11):
    e1 = EMA(hlc3, n1)
    e2 = EMA(abs(hlc3 - e1), n1)
    ci = (hlc3 - e1) / (0.015 * e2)
    wt1 = EMA(ci, n2)
    wt2 = SMA(wt1, 4)
    normalize(wt1 - wt2)
```
W7 feature vector (spec §0): `f1=n_rsi(close,9,1)`, `f2=n_wt(hlc3,10,11)`,
`f3=n_cci(h,l,c,20,1)`, `f4=n_adx(h,l,c,20,2)`, `f5=n_rsi(close,9,1)`.
(Reference default f1 = `n_rsi(close,14,1)`, f3 = `n_cci(...,20,1)`, f5 =
`n_rsi(close,9,1)` — spec only really differs on f1's length. Keep spec; log it.)

## §2. Lorentzian k-NN (Approximate Nearest Neighbors)

```
label[t]  = SHORT(-1) if close[t+4] < close[t]
            LONG(+1)  if close[t+4] > close[t]
            NEUTRAL(0) otherwise
# at bar t only label[t-4] and earlier are known — no lookahead in the neighbour pool.

distance(cur, hist_i) = Σ_f  log(1 + |feature_f[cur] - feature_f[hist_i]|)   # 5 features

# per current bar t (for t >= maxBarsBackIndex):
lastDistance = -1.0
predictions, distances = [], []
for i in 0 .. min(maxBarsBack, t+1)-1:          # i indexes historical bars, chronological
    d = distance(t, i)
    if d >= lastDistance and (i % 4) != 0:      # every-4th-bar spacing
        lastDistance = d
        distances.append(d); predictions.append(round(label[i]))
        if len(predictions) > neighborsCount:   # = 8
            lastDistance = distances[ round(neighborsCount * 3/4) ]   # lower-25% reset
            distances.pop(0); predictions.pop(0)
prediction[t] = sum(predictions)               # integer in [-8, 8]
```
`maxBarsBack = 2000`, `neighborsCount = 8`. (The ref impl's `Distances`
batching is a perf detail — a plain loop is fine for W7's ≤ ~20k 5-min bars.)

## §3. Nadaraya-Watson kernel (non-repainting, rational quadratic)

```
rationalQuadratic(src, lookback=8, relativeWeight=8.0, startAtBar=25):
    num = 0.0 ; den = 0.0
    for i in 0 .. startAtBar+1:                 # backward only → causal
        w = (1 + i^2 / (lookback^2 * 2 * relativeWeight)) ^ (-relativeWeight)
        num += src[t-i] * w
        den += w
    yhat1[t] = num / den

gaussian(src, lookback=6, startAtBar=25):       # lookback = h - lag = 8 - 2
    w = exp( -i^2 / (2*lookback^2) ) ; same num/den loop → yhat2[t]

kernel_bullish[t] = yhat1[t] > yhat1[t-1]
kernel_bearish[t] = yhat1[t] < yhat1[t-1]
kernel_green_transition[t] = kernel_bullish[t]  and kernel_bearish[t-1]     # spec §1
kernel_red_transition[t]   = kernel_bearish[t]  and kernel_bullish[t-1]
# smoothing variant (use_kernel_smoothing=true): use crossover(yhat2, yhat1)
# / crossunder(yhat2, yhat1) instead of the rate transitions.
```
`trade_with_kernel=true` → `candidate` also requires `kernel_bullish[t]` for a
long / `kernel_bearish[t]` for a short (spec §2 "kernel_aligned").

## §4. Filters

```
filter_volatility(h,l,c):   ATR(h,l,c, 1)  >  ATR(h,l,c, 10)          # recentATR > historicalATR
                            # (useVolatilityFilter; minLength=1, maxLength=10)

regime_filter(ohlc4, h, l, threshold=-0.1):
    # KLMF (Kalman-like) recursive filter:
    value1[i] = 0.2*(src[i]-src[i-1]) + 0.8*value1[i-1]
    value2[i] = 0.1*(h[i]-l[i])       + 0.8*value2[i-1]
    omega     = |value1[i] / value2[i]|
    alpha     = (-omega^2 + sqrt(omega^4 + 16*omega^2)) / 8
    klmf[i]   = alpha*src[i] + (1-alpha)*klmf[i-1]
    absSlope  = |klmf[i] - klmf[i-1]|
    avgSlope  = EMA(absSlope, 200)
    normSlopeDecline = (absSlope - avgSlope) / avgSlope
    pass = normSlopeDecline >= threshold        # -0.1 default → "not sharply ranging"

filter_adx(src, h, l, threshold=20, length=14):  ADX(h,l,src, length) > threshold   # OFF by default
```

## §5. Signal + spec state machine (NOT the ref impl's fixed 4-bar exit)

```
# ref impl signal (kept, for prediction→direction with hysteresis):
signal[t] = LONG  if prediction[t] > 0 and filter_all
            SHORT if prediction[t] < 0 and filter_all
            else signal[t-1]

new_buy_signal[t]  = signal[t]==LONG  and signal[t-1]!=LONG
new_sell_signal[t] = signal[t]==SHORT and signal[t-1]!=SHORT

# spec §2:
candidate_long  = new_buy_signal[t]  and kernel_bullish[t] and filter_volatility and filter_regime
candidate_short = new_sell_signal[t] and kernel_bearish[t] and filter_volatility and filter_regime
#   Version B also: RSI(14)>50 (long)/<50 (short) AND ADX(14)>=20 AND close vs EMA(200)

# spec §3 state machine:
FLAT   --candidate--> PENDING(dir, signal_bar=t, hi=high[t], lo=low[t], expiry=t+5)
PENDING --close beyond hi(long)/lo(short) by buffer, within 5 bars--> ENTER at next bar open
PENDING --opposite candidate--> FLAT (cancel)
PENDING --t > expiry, no breakout--> FLAT (cancel)

buffer      = max(1.0 point, 0.05 * ATR(14))          # spec §0 breakout.buffer
long_stop   = signal_lo - 0.10 * ATR(14)              # underlying; spec §4
short_stop  = signal_hi + 0.10 * ATR(14)
reject if |entry_underlying - stop| > 0.75 * ATR(14)  # spec §4 max risk

# spec §4 exit priority per bar (adverse-first on ties):
1. stop hit   2. kernel reversal transition (§3)   3. opposite new signal   4. 15:15 EOD
```

## §6. Reference-impl defaults (for cross-checking `LORENTZIAN_PARAM_KEYS`)

`neighborsCount 8`, `maxBarsBack 2000`, `useDynamicExits false`,
`useEmaFilter false / emaPeriod 200`, `useSmaFilter false / smaPeriod 200`,
`useVolatilityFilter true`, `useRegimeFilter true`, `regimeThreshold -0.1`,
`useAdxFilter false`, `adxThreshold 20`,
kernel `lookbackWindow 8`, `relativeWeight 8.0`, `regressionLevel 25`,
`crossoverLag 2`, `useKernelSmoothing false`.
