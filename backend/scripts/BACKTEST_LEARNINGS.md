# Backtest learnings ledger — conviction-strategy refinement

Append-only. Every entry timestamped (IST). Newest first.
All PnL figures are **per 1 lot, net of realistic costs** (flat ₹40/lot +
0.04% turnover + 0.1% STT + **0.5%/side premium slippage**) unless stated
"gross". `run_backtest.py --exit-mode current` models zero costs itself —
costs are applied only in analysis.

---

## 2026-08-28 — Refined sweep #2 launched (47 configs, NIFTY)

**Run:** `refined_sweep_20260828T071332Z` on the e4 backtest VM
(`129.159.226.106`), full 52-expiry NIFTY 1-min options archive,
`--exit-mode current`, ALL days / full market hours, 10-way sharded.
Config list: `data/historical/backtest_reports/refined_sweep_20260828T071332Z/SWEEP_META.txt`.

**Harness fixes applied before this run (all backtest-side, no live deploy):**
- **VIX seeded** as real `INDIA VIX` `Instrument` + per-bar `QuoteTick`s
  from `INDIA_VIX_alice_index_1min.csv`, so `env_metrics.get_vix_as_of`
  (every strategy's VIX gate) resolves against real data. Verified: 3,250
  ticks/expiry-week, values in a realistic band. **In sweep #1 the VIX
  gate was completely inert** (`orbc_vix20` byte-identical to baseline).
- **PCR floored** — `PCR_MIN_SIDE_OI = 100_000`; below that on either side
  PCR reports `None` instead of garbage. Sweep #1 `pcr_entry` ranged
  **0.003 – 505** (degenerate ratio on a thin/early chain). Post-fix a
  real trade showed `pcr_entry = 0.92`.
- **`max_loss_per_lot` + `time_stop_minutes`** added as `TradeProposal`
  fields, consumed by `_reconstruct_exit_current` (new `ExitReason.MAX_LOSS`
  / `TIME_STOP`). `max_loss_per_lot` = hard ₹ loss cap per lot, checked
  before the premium stop (can only tighten). `time_stop_minutes` = exit
  if held past the window **and not in profit** ("let winners run").
  **Production PositionManager wiring + `stop_plans` columns are a
  deliberately-parked follow-up** — decide after the backtest shows value.
- **New `orb_conviction` gates:** `ce_only`, `skip_weekdays` (IST day
  names), `min_breakout_strength_atr` ((|close − OR boundary|) / ATR14 ≥
  x), `require_drift_alignment` (breakout dir must match the underlying's
  net move since 9:15 IST).

**What sweep #2 tests:** structure-break persistence A/B (30/60/120/600 s +
wider ATR buffer), each findings-driven gate alone, risk overlays alone
(₹2000/2500/3000 stop, 60/90/120-min time-stop), functional VIX gate
(≤16/18/22), HTF/ATR re-test, target/trail/stop/range tuning, and ~12
stacked hypotheses up to an "everything" config. Judge on **OOS**
(entries ≥ 2026-04-01) — every rule was picked from sweep-#1 data, so
overfitting is the primary risk.

---

## 2026-08-28 — Sweep #1 (13 configs) + diagnostic analysis — NIFTY

### Headline
**No config was net-positive after realistic costs.** Best was
conviction-gated ORB with the HTF-EMA-trend gate: **−₹266/lot ALL,
−₹132/lot OOS** (PF 0.69 / 0.86). ORB gross was near-breakeven
(`orbc_htf` gross E −₹12/lot, PF 0.98) — the entire loss is friction.

### Findings (ranked)
1. **`atr_breakout` (new Turtle-style strategy) failed hard** — 3 worst
   configs, PF 0.09–0.21, 12–16-trade losing streaks. **Abandoned.**
   `atr_delta` r = **−0.70** vs PnL: it needs volatility to keep expanding
   after entry, which it rarely does.
2. **PE (bearish) ORB breakouts are structurally broken** — **19% win
   rate** across the year (the year was a net up-drift). **CE-only ≈ 43%
   win.** `ce_only` is the single biggest clean lever.
3. **The fat left tail, not the hit rate, is the problem.** CE-only wins
   44% but a few −₹5k…−₹9k trades sink it. A hard **−₹2,500/lot stop** on
   the CE-only + ex-Tuesday slice flips it **−₹546 → +₹473/lot, PF 1.77**
   (n=30 — hypothesis, not proof).
4. **Trades that work, work fast.** `hold_min` r = **−0.40** vs PnL.
   Winners resolve ~65 min; losers grind 112–126 min. Hold-time buckets
   (CE-only): 45–90 min = **80% win / +₹2,542 / PF 8.37**; **>180 min =
   26% win / −₹1,733 / PF 0.17**.
5. **Entry after ~10:00 IST is 3× worse.** 09:30 breakout entries
   −₹270/lot; 10:00–10:29 −₹875; 10:30+ −₹1,005. CE-only + ex-Tuesday:
   **09:xx entries +₹292/lot (55% win)**, 10:xx entries −₹1,955.
6. **Exit dead zone 11:00–13:00 IST.** Exits landing at 12:00 IST avg
   −₹2,728/lot; 13:00 −₹1,098. Nothing good happens to an ORB trade still
   open at noon.
7. **Tuesday (NIFTY weekly-expiry cadence day) = 43% of all trades**,
   worst day (23% win, −₹564). Skipping it lifts win 30→37% and halves
   per-trade loss — but the remaining days are **still net-negative**
   (−₹453/lot). Wednesday is the only positive-leaning day.
8. **`structure_break` is misfiring** — **53% of all exits**, **median
   hold 1 minute**, 77% close within ±3% of entry premium. It cuts trades
   flat before they can develop. By contrast `trail` exits = **100% win,
   +₹1,250, median 37 min**; `target` = 0.9% of exits. Root cause: at
   1-min bars the `structure_break_persistence_seconds` (6.0s default)
   timer collapses to "confirm on the 2nd breaching bar". Sweep #2 A/Bs
   the persistence value and ATR buffer.
9. **VIX entry level carries no edge** — r = −0.06 vs PnL; the high-VIX
   tercile actually won *more* (36% vs 27%). A "sit out high VIX" gate
   would remove winners. (Consistent with the prior live-trade finding.)
   `vix_delta` (rise mid-trade) r = −0.43 but that's a co-symptom of a
   losing trade, not a leading signal.
10. **Wider entry + fixed R-multiple targets both overfit / underperform.**
    `orbc_wide_nogates` −₹629 vs tight −₹381. `orbc_htf_atr_r2` gross IS
    PF 1.21 → OOS PF 0.14 (textbook curve-fit). Keep the tight ORB
    defaults and the strategy's own %-based target.

### Data-coverage / validity audit (what the strategies actually saw)
| Input | In source | Reached strategy | Evaluated | Note |
|---|---|---|---|---|
| Underlying 1-min OHLC (alice_index) | yes, 295k bars gap-free | yes | yes | volume = 0 (index) |
| EMA9 / EMA20 | computed live, ~3,240 rows/wk | yes | yes (9/46 entries changed by htf gate) | 60s bars — a 9/20-*minute* EMA, not a 15-min HTF |
| ATR14 | ~3,237 rows/wk, values 2.5–15 | yes | yes (32/46 entries changed by atr gate) | — |
| Option LTP / volume / OI | real | yes | yes | — |
| Option bid/ask / depth | **synthetic** (OI/vol proxy) | yes | yes | no historical source has real quotes |
| **VIX** | in CSV; `vix_entry` 100% populated as *diagnostic* | **NO in sweep #1** (harness didn't seed the QuoteTick) → **fixed for sweep #2** | inert in #1 | — |
| PCR | computed; **0.003–505 garbage in #1** → floored for #2 | yes | not gated | single early chain snapshot per run |
| **VWAP** | never computed (index volume = 0 → `VWAPCalculator` stays None → 0 rows) | n/a | n/a | no tested strategy uses VWAP |
| Underlying volume (volume-surge gate) | 0 on alice_index; futures proxy = **only 92 days / 37% of the year**, skewed to Jul–Aug'26 + pre-expiry weeks | partial | not a real test | Alice Blue NFO history = dead end; Shoonya can't pre-date a contract's ~2-mo listing |

### Permanent data ceilings (normalize for, can't fix)
- ~1 year of 1-min option premiums (52 NIFTY weekly expiries, Aug'25–Aug'26)
  — one macro sample, though it contains calm/low-VIX, a real VIX spike to
  ~28 (Mar–May'26), and a trend-up (Apr'26).
- 1-min bars, no ticks — structure-break persistence collapses; same-bar
  stop/target ties resolved loss-first.
- No real bid/ask anywhere — synthetic spread from OI/volume.
- No cross-trade P&L feedback in the sim — daily-loss-limit / consecutive-
  loss circuit breakers are analysis overlays only.
- `run_backtest.py` models zero brokerage/STT/slippage — applied in analysis.

### Parked for a live decision (NOT deployed)
- Structure-break persistence change in production `evaluate_open_position`
  (only if sweep #2 A/B shows prod is also trigger-happy).
- `max_loss_per_lot` / `time_stop_minutes` on the DB + PositionManager.
- Real 5-min HTF EMA in `IndicatorEngine`.
- Anchored-TWAP (volume-free VWAP proxy) as the primary VWAP for both
  backtest and live.

### Method notes
- IST everywhere — see `BACKTEST_TIME_CONVENTIONS.md`. `run_backtest.py`
  is already IST-correct; analysis scripts `tz_convert("Asia/Kolkata")`
  before any `.dt.hour`/`.dt.day_name()`.
- Analysis scripts: `analyze_conviction_sweep.py` (per-config KPIs, IS/OOS,
  cost haircut), `analyze_trade_diagnostics.py` (by-day, by-hour,
  per-diagnostic buckets, streak/extreme signatures). Run under
  `~/an_venv` on the VM (pandas/numpy).
