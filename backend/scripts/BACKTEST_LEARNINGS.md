# Backtest learnings ledger — conviction-strategy refinement

Append-only. Every entry timestamped (IST). Newest first.
All PnL figures are **per 1 lot, net of realistic costs** (flat ₹40/lot +
0.04% turnover + 0.1% STT + **0.5%/side premium slippage**) unless stated
"gross". `run_backtest.py --exit-mode current` models zero costs itself —
costs are applied only in analysis.

---

## 2026-09-08 (~12:45 IST) — p18s VWAP `--volume-source` smoke: verdict + unsynced-work triage

### p18s — `run_backtest.py --volume-source {same,futures}` splice, VWAP smoke (2 configs, `s6_p18s`, run ~05:37 IST)

**What / why.** 2026-09-07 root-caused backtest VWAP diverging from live to a
price-series + volume mismatch: live's `vwap_pullback*` runs on real spot-index
PRICE with **spliced real futures VOLUME**
(`ShoonyaBrokerAdapter._splice_future_volume`); the backtest ran either on
`alice_index` (real spot price, **volume=0** → VWAP degenerate) or
`futures_proxy` (futures *price*, not spot). New `run_backtest.py
--volume-source futures` reproduces live: keep `alice_index`'s spot PRICE bars,
overwrite each bar's VOLUME with the same-minute value from
`NIFTY_underlying_proxy_1min.csv`. `--volume-source same` (default) = byte-identical
to today. Applied to the box bundle only via `patch_volume_source.py` (LF-native);
NOT committed anywhere.

**Smoke** — `vwap_pullback_conviction` + `require_momentum_alignment` /
`momentum_lookback_bars=1` (= live `VWAP_RSI_Paper_Convic`):

| config | series | trades | win% | E/lot | PF | IS E | OOS E | P(mean≤0) |
|---|---|---|---|---|---|---|---|---|
| VW-N1-FIX | alice_index price + spliced futures volume | 16 | 31.2% | −343.2 | 0.27 | −388.0 | −268.6 | 0.981 |
| VW-N1-OLD | futures_proxy price (control) | 16 | 25.0% | −352.1 | 0.26 | −435.8 | −212.6 | 0.986 |

- ts-alignment proxy→alice 99.7%; futures-volume coverage ~100% of near-expiry
  weeks. ~9/16 trades identical, ~7 differ by strike-within-same-expiry (real
  volume nudges the VWAP line, not whether the setup fires).
- **The fix works as designed** — the VWAP series is now the one live sees — **but
  both legs are deep losers on this sample**: E ~−340/lot, PF ~0.27,
  P(mean≤0) ~0.98. FIX is marginally less bad (win 31 vs 25, IS −388 vs −436),
  not a meaningful improvement.

**Verdict — data-coverage wall, NOT a strategy verdict.** Both legs = 16 trades,
100% DTE 5–6, near-expiry weeks only. `vwap_pullback` has no once-per-run cap;
the setup just occurs ~0–2×/week across the ~26 near-expiry weeks the archive
covers, vs live running every session on real ticks (~13/day). The smoke
**cannot adjudicate live VWAP either way.** Standing point (user, 2026-09-08):
no-backtest ≠ no-live — VWAP is validated *forward* (paper signal-count +
outcome ledger, then monitored 1-lot live), and the raw signal frequency/edge
can be checked on the full continuous `alice_index` series independent of any
option data.

**Decisions:**
- **Full Phase 18 VWAP sweep — NOT run** (fails the "only if the smoke improves"
  gate).
- **`--volume-source` patch — KEPT dormant on the box bundle** (`same` default =
  no-op). `patch_volume_source.py` preserved in the session scratchpad. Re-run a
  real VWAP sweep on it once the near-expiry option archive has grown enough to
  lift the 16-trade ceiling.
- **VWAP stays paper-only / directional**, unchanged.

### Unsynced-work triage (2026-09-08)

Cleared the piled-up not-synced backtest work.

**Kept (active / dormant):**
- `wip/strike-selection-dte-batch` (local, unpushed) —
  `analyze_strike_selection_dte_split.py` + `phase16/17_*.txt`. p17 runs 15:32
  IST tonight. Stays local per the backtest-scripts-local-only policy.
- **DTE-aware strike-selection wiring** on the box bundle
  (`strike_ranking/engine.py` `resolve_dte_ranking` + `dte_aware_strike_selection`
  / 5 window params on ORB/OI/EMA + `strategies.py` param allowlist). p16: only
  ORB's `non_expiry_day_window` (ATM±1) helps; OI negative, EMA worse; expiry-day
  levers untestable (no DTE 0–3 option data). ORB *conviction* re-test = p17.
  Box backup at `~/deploy-bak/strike-sel-batch-20260908/`; edited copies in the
  session scratchpad `bundle/`.
- `--volume-source` patch — above.

**Archived (removed from the working set):**
- `wip/entry-filters-and-momentum-plateau` (79e57c3) → tag
  `archive/entry-filters-and-momentum-plateau` (local branch deleted; origin
  branch left in place). +539/−30: RSI14-extreme entry block + confirm-bar
  filters across all 4 strategies, the momentum-plateau `entry_time`-floor
  bugfix, and tests. **Not promoted — why:**
  - **p10c** (momentum-plateau EXIT sweep, 2026-09-04): `require_momentum_plateau_exit`
    HURTS the two strategies that matter — ORB conv +122 → **−80** (P 0.19→0.83),
    OI conv +187 → +35. Feature stays dormant (deployed opt-in, no config sets
    it). The `entry_time`-floor bugfix rides with that feature and doesn't earn
    a merge alone.
  - **p15** (entry-avoidance filters, 2026-09-07): RSI-block-*alone* degrades base
    ORB (E +105→+59, OOS +33→+2); the live `ORB_Convic_*` (RSI + confirm-bar) is
    already the best config in the p15 set (E +209, P 0.053). No config change.
  Restore if a new entry-filter test is planned:
  `git checkout -b <name> archive/entry-filters-and-momentum-plateau`.

**Deleted:**
- 6 merged local branch refs (`feat/collapsed-exit-uses-dominant-leg`,
  `feat/invert-paper-live-model`, `feat/live-exit-path-redesign`,
  `feat/staged-exit-collapse-and-live-carrier-stop`,
  `fix/option-chain-plausibility-rails`, `fix/reconciliation-scope-to-app-symbols`)
  — all ancestors of `main`, all deployed; history intact on `main`.
- Box `sweep_configs/phase18_smoke.txt` — consumed; the 2 configs, for the
  record:
  ```
  VW-N1-FIX|vwap_pullback_conviction|alice_index|{"require_momentum_alignment": true, "momentum_lookback_bars": 1}
  VW-N1-OLD|vwap_pullback_conviction|futures_proxy|{"require_momentum_alignment": true, "momentum_lookback_bars": 1}
  ```

**Committed:** `backend/scripts/fetch_shoonya_near_expiry_options.py` (was
untracked on `main` — the near-expiry option-pull source, deployed to the box
2026-09-05).

### Ledger resync

This file on `main` was ~6 days / ~12 entries behind the `backtest_engine` box
bundle copy (the box copy is the working source of truth — phases 10c–16 and
Part 0 were only ever written there). Replaced `main`'s copy wholesale with the
box copy — verified a clean prepend-only superset: identical from the 2026-09-02
entry down, the sole other divergence being the CANONICAL section, where the box
copy is the newer one (documents `run_sweep_canonical.sh`). Entries
2026-09-03 "Phase 9 concluded" → 2026-09-08 "Part 0" plus this entry now land on
`main` together.


## 2026-09-08 (~08:50 IST) — Part 0: analyzed p15/p14-tails/p10c (all 3 unwritten) + Phase 17 (ORB conv strike-width + OR-width) scheduled

### p15 — entry-avoidance filters (RSI14-extreme block + confirm-bar), 26 configs, run 2026-09-07

Walk-forward (IS ≤ 2026-04-01 / OOS after; 10k bootstrap). Config names:
`{ORB,OI}` = base type, `{ORBC,OIC}` = conviction; `C0` none, `B` confirm-bar,
`R2575`/`R25` etc. = RSI block, `RB` = both.

| config | E/lot | PF | P(mean≤0) | OOS E | read |
|---|---|---|---|---|---|
| **OIC-R25** (= live `OI_Convic_*`) | **+224** | 3.38 | **0.013** | +180 | **best in the OI set** |
| OIC-C0 | +187 | 2.75 | 0.029 | +180 | RSI block PE<25 adds real value |
| OIC-R30 | +230 | 3.57 | 0.015 | +179 | ~tie with R25 |
| OIC-R2570 (adds CE>70) | +149 | 2.21 | 0.109 | +137 | CE-side block hurts (n 18→13) |
| OIC-B / OIC-RB (confirm bar) | — | — | — | — | **kills OI conv** (n→5 / n→1) |
| **ORBC-RB** (= live `ORB_Convic_*`, RSI+confirm) | **+209** | 1.97 | **0.053** | +300 | **best in the ORB set** |
| ORBC-B (confirm only) | +185 | 1.74 | 0.114 | +326 | strong; RB edges it on P/E |
| ORBC-C0 | +122 | 1.49 | 0.186 | +35 | |
| ORBC-R2575 (RSI only) | +59 | 1.21 | 0.338 | +2 | **RSI block alone hurts** |
| ORB-B (= live `Nifty_ORB_Base`) | +105 | 1.40 | 0.214 | +33 | confirm bar rescues base ORB (C0 = −177) |
| ORB-RB (base, RSI+confirm) | +68 | 1.23 | 0.309 | −41 | RSI block degrades the base confirm config |
| OI base (all) | −132 … −447 | <0.65 | >0.88 | neg | stays parked |

**Verdict: the two live conviction configs (`OI_Convic_*` RSI PE<25, `ORB_Convic_*`
RSI+confirm) are the best configs in their respective p15 sets. WS5 validated.
No config change.** (Caveat: p15's ORBC exit params were the pre-2026-09-07
`stop .18/target 1.0`, not the current `.22/.33` from phase14's R:R grid — the
*relative* filter ranking is the signal, not the absolute E.)

### p14 — OI/EMA/VWAP R:R grid tails (3 SL × 4 ratios each), 36 configs, run 2026-09-06

| grid | best cell | E/lot | PF | P(mean≤0) | vs live |
|---|---|---|---|---|---|
| OI | `g_oi_s11_r20` (SL .11 / ratio 2.0) | +200 | 2.87 | **0.025** | **= live `OI_Convic_*` top-level (.11/.22)** — at optimum |
| EMA | `g_ema_s08_r15` (SL .08 / ratio 1.5) | +94 | 1.72 | **0.069** | **= live `EMA_Convic_*` (.08/.12)** — at optimum |
| VWAP | every cell **fails** (E −260…−425, P 0.94–0.99) | | | | ran on `futures_proxy` = the broken series → **invalid, ignore** |

**Verdict: OI and EMA conviction exit params already sit at their grid optimum.
No change. VWAP grid must be rerun after the price-series fix (Phase 18).**

### p10c — momentum-plateau EXIT (`slope` 5-bar / `slopelb10` 10-bar vs `off`), 24 configs, run 2026-09-04

| strategy | off | slope | slopelb10 |
|---|---|---|---|
| ORB conv | +122 (P 0.186) | **−80** (P 0.83) | +36 (P 0.37) |
| OI conv | **+187** (P 0.029) | +35 (P 0.36) | +161 (P 0.05) |
| EMA base | −54 | +4 | −63 | (all ≈ 0, noise) |
| VWAP | all deeply negative (invalid series) | | |

**Verdict: `require_momentum_plateau_exit` HURTS the two strategies that matter
(ORB/OI conviction). Do NOT promote. Feature stays dormant (deployed opt-in,
no config sets it). Leave the parked entry-anchor bugfix on
`wip/entry-filters-and-momentum-plateau` parked — the feature doesn't earn it.**

### Phase 17 (RUN_TAG=p17) — scheduled 15:32 IST via `p17-delayed-launch.timer` (rule 7: outside market hours; reaper stopped per rule 13)

6 `orb_conviction` configs (`sweep_configs/phase17_orb_conviction_strike_and_width.txt`),
base = `ORB_Convic_Paper` params (exit_legs omitted — `--exit-mode current`
doesn't read them), ~2.3 h at SHARD_COUNT 4.

- **Strike-width (Batch 2 of strike-selection)** — carry Phase 16 Batch 1's
  ORB-base finding (ATM±1 beat ATM±3: E +33 %, PF 1.40→1.52, P 0.214→0.160)
  onto the real conviction entry gate. `OC-C0` control / `OC-NW1`
  (`non_expiry_day_window` 1) / `OC-NW2` (2). Expiry-day levers still excluded
  (no DTE 0–3 option data).
- **OR-range width re-sweep** (open since 2026-09-01) —
  `max_or_range_nifty_points` ∈ {55, 65(=`OC-C0`), 75, 85} with
  `require_prior_day_trend` on, which the 2026-08-28 width-ridge sweep never
  tested. Gates any decision to size ORB above 1 lot.

### Still planned (not yet started)

- **Phase 18 — VWAP price-series fix**: new `run_backtest.py --volume-source
  {same,futures}` (splice real spot price from `alice_index` + volume joined
  from `NIFTY_underlying_proxy_1min.csv` on ts), SOURCE_MAP `vwap_pullback*` →
  `alice_index`. Then a 7-config VWAP sweep (base / p120 / conviction ± momentum
  N1–N3) on the corrected series + a before/after `futures_proxy` control. Smoke
  the ts-overlap % first (futures volume ≈ 1 wk/month = the near-expiry weeks
  `--near-expiry-days 6` already filters to).
- **EMA persistence=120s** — real `trade_outcomes` comparison, not a backtest
  (persistence 6/30/60/120 are indistinguishable from 0 on 1-min bars).


## 2026-09-08 (~06:00 IST) — Phase 16: DTE / expiry-day-aware STRIKE SELECTION wired + Batch 1 (base configs) launched

**What it is.** `strike_ranking/engine.py` has carried a full DTE/time-of-day
strike-window since Ops-Hardening Phase 1 (2026-08-14) —
`_dte_allowed_strikes` + `StrikeRankingConfig`'s `non_expiry_day_window` /
`expiry_morning_window` / `expiry_morning_premium_floor` /
`expiry_afternoon_deep_itm_offset` / `expiry_afternoon_window` — but no
strategy ever called `rank_from_latest_snapshot` with `dte=`/`current_time=`,
so it was 100% dormant. Own-data confirmation of the premise (2026-09-07
research): NIFTY 2026-08-18 expiry, ORB's 09:16→10:15 window — ITM (24100)
decayed −17.2 % vs ATM (24250) −46.0 % vs OTM (24400) −53.7 %.

**Wiring (bundle `app/` re-pin, 5 files — deliberate, per README rule 4).**
New `strike_ranking.engine.resolve_dte_ranking(base, expiry_date, now_ist, *,
enabled, <5 overrides>)` → `(config, dte, current_time)`; `enabled=False` →
`(base, None, None)` = byte-identical to the pre-existing plain-`atm_range`
path. Called from ORB / OI-Volume / EMA Micro-pullback's one
`rank_from_latest_snapshot` site each, `now_ist =
to_ist(latest_bar.bucket_start)` (bar-derived, never a wall-clock read —
same restart-safe convention ORB's own opening-range anchor uses). 6 new
opt-in `params` keys (`dte_aware_strike_selection` + the 5 window overrides)
added to `ORB/EMA/OI *_PARAM_KEYS` via a shared `_DTE_STRIKE_SELECTION_KEYS`
set, so the conviction variants inherit them for Batch 2. VWAP deferred —
its `futures_proxy` price-series fix (2026-09-07 root-cause entry) comes
first. `dataclasses.replace` preserves OI-Volume's `min_oi`/`min_volume`/
weight overrides while swapping only the window fields (QC-verified).

**QC done before any compute.** `resolve_and_qc.py` builds all 8 lines via
the real `_build_strategy` (rule 14) — pass. Explicit assertion that
`dte_aware_strike_selection` + overrides land on the constructed object —
pass. `resolve_dte_ranking` disabled→passthrough, dte=0/dte=5 branches, OI
`replace` preservation — pass. Synthetic-chain `rank_strikes` windowing:
flat = ATM±3; expiry-morning CE = {ATM, 1-ITM-lower}; expiry-morning PE =
{ATM, 1-ITM-higher} (directional, correct); non-expiry D1 = ATM±1;
non-expiry `non_expiry_day_window=3` ≡ flat (**D3's non-expiry path is
byte-identical to C0** — the batch's isolation claim); expiry-afternoon
offset2/band1 anchors 2-ITM ±1; `expiry_morning_premium_floor=20` drops
sub-₹20 strikes. Real-data smoke (ORB `--expiry 2026-09-01`) ran clean, no
errors/traceback.

**Batch 1 — 8 configs, base only (`sweep_configs/phase16_strike_selection_base.txt`, RUN_TAG=p16).**
Rows = each strategy's live-BASE top-level params (trading_bot DB
2026-09-08, `exit_legs` stripped — exit shape held constant, phase15 method)
+ overlay. `--exit-mode current --all-expiries --near-expiry-days 6`,
`alice_index` for all (SOURCE_MAP). Variants, uniform across strategies:

| tag | dte_aware | non-expiry window | expiry-morning | ₹ floor | expiry-afternoon (offset/band) |
|---|---|---|---|---|---|
| C0 | off | ATM±3 (today) | — | — | — |
| D1 | on | ATM±1 | ATM→1-ITM | 0 | 3 / 0 |
| D3 | on | ATM±3 (≡ C0) | ATM→1-ITM | 20 | 2 / 1 |

ORB C0/D1/D3, OI C0/D1/D3 (3-deep — `D3−C0` = pure expiry-day effect,
`D1−D3` = non-expiry-tightening effect), EMA C0/D1 (2-deep first screen).
All windows clamped ≤ ATM±3 (older archive expiries carry only ~ATM±5–6
strikes). NB: expiry-afternoon params inert for ORB (all entries ≤ 10:15 =
"morning").

**Known Batch-1 limitations (by design).** Exits at class defaults → the
ITM-vs-ATM absolute-stop-width confounder is not addressed (ITM-tuned exits
= Batch 2). `exit_legs` stripped ≠ the live multi-leg exit shape (same
entries, different P&L — measure within-batch deltas only). OI/EMA `dte==0`
subset is ~1/5 of a small base n → directional read; ORB has the n.

**Analysis plan.** `analyze_walkforward.py` for the standard read, **plus a
DTE split** — bucket each `_current.csv` trade by `entry_date ==
parse_expiry(symbol)` and compare C0 vs D1/D3 on the expiry-day subset and
the non-expiry subset separately (that's the whole point of the batch).

**Status:** launched RUN_TAG=p16, ~3.1 h (8 × ~23 min orb/oi/ema-family,
SHARD_COUNT=4). Results + verdict appended when done. `main`'s tracked

### RESULTS (2026-09-08 ~08:45 IST) — completed 08:32 IST, 209 min, 8/8 OK, zero shard failures

**🔴 The primary question — expiry-day theta avoidance — could NOT be tested.
Zero trades at DTE 0–3 in the entire batch.** DTE distribution across all 8
configs: ORB `{1:1, 4:7, 5:6, 6:30}`, OI/EMA `{5:3, 6:42}`. The
`options_1min_past` near-expiry archive is ~100 % DTE 5–6 for these expiries,
and ORB/OI/EMA each fire once per direction per expiry-week run — so the
entry lands on the Wednesday (DTE 6) and `_fired_directions` blocks any
later same-week entry. The `expiry_morning_window` / `expiry_afternoon_*` /
`expiry_morning_premium_floor` levers got **zero exercise**. This is the data
wall `[[project_shoonya_near_expiry_pull_2026_09_05]]` addresses going
forward; the historical archive doesn't have DTE 0–3 option data. **D3 ≡ C0
byte-for-byte** for ORB and OI (D3's `non_expiry_day_window=3` == C0, and no
expiry-day trades to differ on) — confirms the wiring is correct and
non-destructive.

**Secondary finding — the `non_expiry_day_window` lever (DTE 6, ATM±1 vs
ATM±3): a real, clean improvement for ORB only.** All comparisons are on the
identical 44/45/47-trade entry set (strike selection doesn't touch entry
logic) — 9 of ORB's 44 strikes changed under D1, every one toward ATM
(richer premium); 7/9 improved P&L.

`analyze_walkforward.py` (IS ≤ 2026-02-19 / OOS after; 10k bootstrap):

| config | E/lot | PF | win% | P(mean≤0) | IS E | OOS E | slip @1.0% |
|---|---|---|---|---|---|---|---|
| **ORB-C0** (ATM±3) | +104.6 | 1.40 | 45.5 % | 0.214 | +154 | +33 | −2 |
| **ORB-D1** (ATM±1) | **+139.2** | **1.52** | 47.7 % | **0.160** | +201 | +49 | +32 |
| ORB-D3 (≡ C0) | +104.6 | 1.40 | 45.5 % | 0.214 | — | — | — |
| OI-C0 | −132.3 | 0.63 | 42.2 % | 0.887 | −90 | −196 | −236 |
| OI-D1 | −124.9 | 0.66 | 42.2 % | 0.864 | −81 | −191 | −231 |
| EMA-C0 | −75.8 | 0.75 | 44.7 % | 0.794 | −83 | −65 | −173 |
| EMA-D1 | −86.5 | 0.72 | 44.7 % | 0.821 | −104 | −61 | −186 |

- **ORB**: D1 beats C0 on every metric — +33 % E, PF 1.40→1.52,
  P(mean≤0) 0.214→**0.160** (just outside the 0.15 bar), both halves
  positive, and materially **less slippage-fragile** (+32 vs −2 at 1.0 %/
  side). One 44-trade sample, doesn't clear the bar outright, but a
  consistent directional gain. **Carry ATM±1 into Batch 2 (conviction
  configs)** — ORB is the one strategy with a live split.
- **OI**: deeply negative either way (already a parked strategy per the
  2026-09-01 archive triage). D1 marginally less bad, still a clear fail.
- **EMA**: D1 is **worse** than C0 (E −87 vs −76). Tightening the window
  hurts EMA. Don't carry it.

**Batch-1 limitations that stand:** exits at class defaults (the ITM-vs-ATM
absolute-stop-width confounder was never reached — no ITM entries fired);
`exit_legs` stripped ≠ live multi-leg shape; base configs ≠ the live
conviction configs; single 44-trade sample.

**Next:**
1. Expiry-day strike selection stays **unbacktestable** until the near-expiry
   pull accumulates real DTE 0–3 option data (weeks–months) — or decide it
   on logic/paper alone. Historical backfill is out (brokers reject expired
   tokens).
2. **Batch 2**: ORB conviction (`ORB_Convic_*`) C0 vs `non_expiry_day_window`
   ∈ {1, 2} on the real live params, to confirm the ATM±1 gain survives the
   conviction entry gate + multi-leg exits. Skip OI/EMA (no signal / negative).
3. VWAP price-series fix, then a VWAP strike-selection run, per the standing
   plan.

`BACKTEST_LEARNINGS.md` is ~800 lines / 5 days behind this box copy
(phase10–14) — needs its own resync-to-main commit, out of scope here.
## 2026-09-07 (~03:30 IST) — phase14 partial analysis: ORB leg/R:R findings applied live; root-caused why VWAP/OI live trade frequency vastly exceeds backtest; DTE-aware strike selection found dormant

**phase14 status at time of writing**: 27/55 configs done, zero shard
failures, on pace (~26min/config for orb/oi/ema-family). Original ~17.8hr
estimate revised to ~14:00-14:30 IST 2026-09-07 (the run straddles today's
09:00 IST market-open CPU-quota throttle, 200%→100%, an existing safety
net — confirmed independent of the sweep's own SHARD_COUNT).

### ORB — Group C completion + R:R grid, fully analyzed, applied live

All 4 multi-leg exit legs (core/runner/tightlock/target — same 40-trade
entry set, confirmed via symbol+entry_time hash) compared:
`core`/`runner`/`tightlock` are **100% trade-agreement** — same
winners/losers, only trail-lock timing differs the magnitude. `runner`
strictly dominates both on net AND tail-check; `tightlock` is strictly
dominated by `core`. Zero diversification among the three. `target`
(bounded exit, not pure trail) is genuinely different — 85% agreement, and
has the best "sustained" profile (lowest max drawdown, best mean/std, fewest
negative months) despite lower raw net.

**Decision applied live** (`ORB_Conviction`,
`trading_bot.strategy_configs`): dropped `tightlock`, kept
core/runner/target at **qty_fraction 0.25/0.25/0.50**. Blending the three
100%-position backtests linearly (valid — all three share identical
entries, PnL scales with qty) confirmed 25/25/50 captures the full
drawdown improvement of heavier target-weighting without giving up as much
net. Target leg's own exit params also updated from the R:R grid
(`g_orb_s22_r15`: stop 0.22/target 0.33, same trail 0.12/0.6 as before) —
beat the live target leg's old params (stop 0.18/target 0.4) by +28% net /
+45% tail on the identical entry set.

**ORB's R:R grid itself (12 configs) all share the SAME 40-trade entry set
as everything above** — exit-overlay comparisons, not independent
evidence (same trap as ORB's earlier 79-exit-overlay archive finding).
Within that one sample: net PnL and tail both fall monotonically as target
ratio loosens past 1.5x SL; `g_orb_s22_r15` (widest SL tested, tightest
ratio) currently wins.

A live-vs-paper split was created (`ORB_Conviction_Live`, later renamed
`ORB_Convic_Live`/`ORB_Convic_Paper` by the user) — ORB is the only
strategy family with this split, since it's the only one with real
backtest validation behind it.

Full writeup: local Claude memory
`project_orb_3leg_reweight_live_paper_split_2026_09_07`.

### VWAP/OI — root-caused a 30-100x live-vs-backtest trade-frequency gap

User noticed VWAP fires far more often live than backtest implies (`Test 4`
had 221 closed trades in 17 real days; a full year of backtest for one
config is only 40-113 trades). Root-caused via background investigation,
ranked:

1. **Primary: underlying price-series mismatch.** `resolve_and_qc.py`'s
   `SOURCE_MAP` resolves `vwap_pullback*` to `futures_proxy` (NIFTY futures
   OHLC) — every other strategy uses `alice_index` (real spot). Live trades
   real spot ticks, only splicing in *volume* from futures
   (`ShoonyaBrokerAdapter._splice_future_volume`). Two genuinely different
   price series for a level-sensitive (VWAP-touch) strategy — this file
   already flagged this as "real for anything level-sensitive" when the
   `futures_proxy` substitution was first made.
2. **Contributing: bar-construction fidelity.** Live bars are built from
   ~350 real ticks/min (true running high/low); backtest replays one
   static pre-aggregated 1-min row from the (wrong) futures instrument.
   This is the corrected form of "tick vs 1-min bar" — not eval frequency,
   bar quality on the wrong series.
3. **Refuted: evaluation cadence.** Both sides fire at most once per
   completed 60s bar — `run_backtest.py` literally imports and calls
   live's own `run_cycle`, not a reimplementation. Byte-identical.
4. **Ruled out**: the 2026-09-02 trade-cap removal (VWAP wasn't in that
   commit; paper sessions exempt from caps since 08-12; no step-change in
   real daily trade counts around 09-02) and multi-instrument scope (both
   configs checked trade NIFTY only).

**A second, independent piece of evidence, found via an older E4-VM sweep**
(`refined_sweep_20260828T072024Z`, this file's own 2026-08-28 entry,
Batch 1): `structure_break_persistence_seconds` 30s/60s are **byte-identical
to persistence=0 on 1-min bars** (a 60s bar can't resolve <2 bars' worth of
time); 120s barely helps; 600s hurts. This means the 2026-09-04 "sb6-2leg"
redesign (`persistence_seconds:6`, applied uniformly to OI/EMA/VWAP base
configs) was **never backtested because it structurally can't be** at 1-min
resolution — it would look identical to persistence=0. Real live/paper data
is the *only* evidence that can exist on this question.

**Real data confirms it, twice over** — the old, disabled
`persistence_seconds:120` configs (an untuned 2026-08-24 placeholder,
disabled 2026-09-04 purely for an unrelated UI-naming-collision reason, not
performance) dramatically outperform their `sb6` (persistence=6)
replacements AND the respective conviction variants in real trading:

| strategy | config | n | net PnL | avg/trade | PF |
|---|---|---|---|---|---|
| VWAP | `Test 4` (persistence=120s) | 221 | +139,027.80 | +629.09 | 1.83 |
| VWAP | `VWAP_Base` (persistence=6s, "sb6") | 22 | -16,025.75 | -728.44 | 0.46 |
| VWAP | `VWAP_RSI_Paper_Convic` (conviction+N1, live) | 30 | +19,193.20 | +639.77 | 1.46 |
| OI | `Test ` (persistence=120s) | 56 | +29,733.50 | +530.96 | 1.54 |
| OI | `OI/Vol_Base` (persistence=6s, "sb6") | 4 | +7,527.00 | — | too thin |
| OI | `OI_Volume_Conviction` (statistically-confirmed peak) | 22 | +2,830.75 | +128.67 | 1.22 |

Even `OI_Volume_Conviction` — this project's cleanest statistical pass
(P(mean≤0)=0.004) — is beaten on real per-trade average by the untested,
disabled base config. Revived both persistence=120s configs as paper-only
A/B arms alongside their sb6 replacements. EMA's equivalent (`Test 1`) not
yet checked for the same pattern — flagged as an open next step.

**Recommended fix, not yet built**: teach the backtest engine to splice
real spot price + futures volume for VWAP (matching what live already
does) instead of fully substituting to `futures_proxy` — would close the
primary gap using 1-min data already on hand, without waiting for the
user's new tick-by-tick data collection effort to mature (which addresses
the secondary, bar-fidelity gap).

**Comparative ranking of VWAP's existing 113-config archive** (since the
fixed pass/fail bar doesn't fit a strategy whose real frequency this
outpaces its backtest sample): best raw net is `g1_s06_t50` (+250/lot) but
fails the tail-check hard (drop-2 -91, "3 trades deep"); most defensible is
the full-conviction + momentum-N1 variant (+84/lot, P=0.248) — exactly
what's live today. Phase 11's later quick-scalper redesign came back worse
(0/22, this file's own 2026-09-04 ~22:53 IST entry) — the live pick is
already the best of a genuinely weak field.

**Standing decision**: only ORB gets a live-vs-paper split for now.
OI/EMA/VWAP stay paper-only until the price-series fix + more tick data +
the still-running phase14 EMA/VWAP R:R grid tails give them a real verdict.

Full writeup: local Claude memory
`project_vwap_oi_persistence_root_cause_and_revivals_2026_09_07`.

### Strike selection — DTE-aware window found dormant, real ITM/OTM decay data pulled

Current ORB (and every strategy) uses a plain symmetric ATM±3 window,
identical every day. Found a full DTE/time-of-day-aware window already
built into `strike_ranking/engine.py` since Ops-Hardening Phase 1
(2026-08-14) — expiry-morning narrows to ATM-to-1-ITM only, expiry-afternoon
jumps to a deep-ITM anchor, explicitly built "to completely avoid theta
decay traps" — but **never wired into any strategy's call site**
(`orb.py:239` calls it positionally, no `dte=`/`current_time=`). Fully
dormant since creation.

Pulled real 1-min NIFTY option data for the 2026-08-18 expiry, ORB's own
entry window (09:16→10:15 IST): ITM (24100) decayed -17.2%, ATM (24250)
-46.0%, OTM (24400) -53.7% — confirms the theory directly, not just
external research. Design wrinkle flagged: %-based stop/target thresholds
tuned on near-ATM premiums likely need separate tuning for an ITM strike's
very different premium scale — not a straight carry-over.

**Not backtested yet** — user is planning a dedicated ORB expiry-day
backtest (DTE-aware strike selection on vs off) targeting a decision before
Tuesday's weekly expiry.

Full writeup: local Claude memory
`project_strike_selection_dte_awareness_research_2026_09_07`.

---

## 2026-09-05 (~15:30 IST) — recurring near-expiry-week option data pull built, deployed, first real pull done (NIFTY)

Follow-up to the open `project_backtest_data_coverage_monthly_append_todo`
gap (~2-3wk missing since the last TrueData load). TrueData's trial expired
2026-08-24 (credentials already pulled off the box) — confirmed dead, not
just stale. Tested both remaining brokers' option-history depth live:

- **Shoonya `TPSeries`**: real 1-min data for a *currently-listed* option's
  full life (BANKNIFTY monthly returned 45+ real days back — no artificial
  cap, matching what was already known for futures). **But the instant a
  contract expires, its token is rejected outright**
  (`"invalid input[Token not prsent]"`), confirmed directly against a token
  already cached in our own `option_contracts` table from 4 days after its
  own expiry — there is no retroactive backfill path, at all, ever. A
  farthest-dated NIFTY/BANKNIFTY "LEAP" (listed years out) returned zero
  rows on every window — listed, but never once traded, so it can't
  answer a depth question either way.
- **Alice Blue NFO history**: contrary to the 2026-08-26 audit's "confirmed
  dead end" (which used a broken, illiquid-strike query), a near-ATM query
  now returns real data — but **every row is an exact duplicate** (770
  candles / 385 unique timestamps for one real trading day, confirmed by
  hand), and it has the same >45-day cliff and zero >1yr data as Shoonya.
  No depth advantage; would need a dedup pass to ever be usable. Not
  pursued further given Shoonya already covers the same window cleanly.

**Conclusion: there is no way to backfill already-expired weeks from
either broker — the only usable design is capturing each week's
near-expiry data *before* its own expiry.** Matches exactly what this
system actually needs (`run_backtest.py --near-expiry-days 6` already only
ever replays the Wed→Tue week before expiry).

**Built**: `backend/scripts/fetch_shoonya_near_expiry_options.py` (new,
uncommitted) — reuses the live app's own cached Shoonya session (no
separate login) and its own already-synced `option_contracts` rows (real
ATM-centered strikes come straight from production's daily sync; no
SearchScrip lookup needed). Narrows to the nearest `--strikes-each-side`
strikes to the latest known spot (from this app's own `price_bars`) before
fetching — production's `is_active` band for a single NIFTY weekly turned
out to be 454 rows (mostly stale never-cleaned structural rows from
routine chain syncs, most strikes never near the money), which would have
burned ~450 near-useless TPSeries calls per run without this filter.
Pulls `TPSeries` for the Wed→Tue window, converts to the archive's exact
`{underlying}{expiry:%y%m%d}{strike}{CE|PE}.csv` filename convention
(matches `run_backtest.py`'s `_parse_option_symbol` byte for byte),
applies the existing `MAX_PLAUSIBLE_OPTION_PREMIUM` ceiling, and
merges into any existing file by timestamp (idempotent — safe to re-run
mid-week or after a partial failure). Skips an underlying gracefully (not
an error storm) when its nearest expiry's own near-expiry week hasn't
started yet — BANKNIFTY's monthly expiry (2026-09-29) correctly no-op'd
with "starts in 18d" instead of firing 60 doomed API calls.

**Deployed**: `fetch-shoonya-near-expiry.service`/`.timer` on A1
(`/etc/systemd/system/`), two `OnCalendar=Tue` runs — 09:50 UTC (15:20 IST,
safety run before market close, in case the token dies exactly at close)
and 10:05 UTC (15:35 IST, the requested time) — both hit the same
idempotent script, so a token-death-at-close scenario still gets
everything but the last ~10 minutes from the first run. Confirmed enabled,
next fire correctly shows Tue 2026-09-08 09:50 UTC (this week's real NIFTY
expiry).

**First real pull (manual, this session)**: NIFTY 2026-09-08 expiry, 60
contracts (ATM±15, spot ~23,950-24,300 through the week), 66,432 rows
written to `options_1min_past/NIFTY/2026-09-08/` — 1125 rows/contract for
the 3 full trading days so far this week (Sep 2-4; Sep 5 is a Saturday, so
correctly nothing beyond Sep 4), a couple of far strikes showing fewer
rows (807/375) from only recently coming into range — expected, not a
bug. Cross-validated `intv` (per-bar volume) against an independent
source: diffed consecutive `option_chain_snapshots` cumulative-volume
reads for the same contract/window and got the same order of magnitude
(~1-2M/min) as the fetched bars — confirms the large numbers are real
NIFTY option volume, not a unit-mislabeling bug. BANKNIFTY correctly
no-op'd (its own near-expiry week doesn't start until ~Sep 23).

**Not yet done**: git commit (script lives in `backend/scripts/`, not
`backend/app/`, so the local-only-uncommitted-backtest-code policy doesn't
strictly apply, but it hasn't been asked for yet); a second real week's
data to confirm the systemd timer fires and merges correctly unattended
(next real test is this coming Tuesday, 2026-09-08); BANKNIFTY's own first
real pull (~2026-09-23 onward).

---

## 2026-09-05 (~12:30 IST) — Phase 12 CONCLUDED: 0/42 pass, one genuine near-miss (`ema_t10_sl150`), OI/Volume's smoke-test edge did not generalize

Sweep completed 06:37 IST (281min, all 42 configs OK, 16 trades each, zero
errors — confirmed via `journalctl`, disk stable, `trading-bot.service`
unaffected throughout). Analyzed via `analyze_walkforward.py` against the
standing bar (positive IS+OOS+both halves; bootstrap P(mean≤0)≤0.15 with
non-negative 5th-pctile; survives 1.0%/side slippage).

**0/42 pass, but `ema_t10_sl150` is the closest anything has come across
every phase of this research line.** target=10/stop=15: PF 1.82 overall,
positive in ALL of IS (+566, PF 1.55) / OOS (+856, PF 2.23) / H1 (+310, PF
1.35) / H2 (+1111, PF 2.32) — the first config anywhere in this ledger to
clear that specific combination. Bootstrap P(mean≤0)=0.144, just under the
0.15 bar. **Fails on the two tail-risk checks only**: 5th-percentile
bootstrap is −₹51 (bar wants ≥0), and at 1.0%/side slippage it flips to
−₹5/lot (bar wants net-positive). Both misses are thin, not decisive, but
a miss under the bar's own rule. `ema_t10_sl200` is byte-identical (same
16 trades, same exits) — the stop never actually gets hit at either 15 or
20 points once the target is this wide; same plateau shows up at
`ema_t8_sl150`==`sl200` and `ema_t12_sl150`==`sl200`. Confirms wider SL
stops mattering past ~150% of a target this size — a real, useful boundary
found, not a bug.

**OI/Volume's smoke-test signal did not generalize across the full grid.**
Every one of the 21 scalp configs still has a negative IS half — the
OOS/H2-only strength that looked promising in `p12_smoke`
(`oi_scalp_t5_sl100`/`t7_sl150`) shows up again here (e.g. `oi_t10_sl200`
OOS +565 vs IS −2235) but never once pairs with a positive IS, across 21
independent configs. Reads as a temporal-regime effect (recent months
trade differently from older ones in this archive) rather than a real,
stable edge — consistent enough across the full grid that it's no longer
plausible as sampling noise the way Phase 11's single-point spikes were.

**Action**: neither strategy's quick-scalper variant clears the bar.
`ema_t10_sl150`/`sl200` is worth remembering if this line is ever revisited
(closest near-miss on record), but per the standing rule ("at n≈20-40,
every pass is paper-trade to collect live data, never deploy") it doesn't
even qualify for that — it isn't a pass. `backtest-reaper.timer`
re-enabled on A1. Code (`stop_points`/`target_points` on both strategies,
`oi_false_breakout_grace_bars`) stays LOCAL/UNCOMMITTED — no reason to
commit for a non-passing result; left as-is pending explicit user
direction on whether to keep, discard, or fold the near-miss into a future
targeted follow-up (e.g. a tighter grid around target 9-11 / SL 125-175%
for EMA specifically).

**Housekeeping note**: this session lost SSH/HTTPS/ICMP reachability to
the A1 box for a period while the sweep was running (user-confirmed
network issue on their end, resolved itself) — the sweep, being a detached
`systemd` unit, was completely unaffected and ran to completion regardless,
exactly as designed.

---

## 2026-09-05 (~02:26 IST) — Phase 12 launched: EMA Micro-pullback + OI/Volume Confirmed quick-scalpers, full 42-config grid

Follows the same points-based target/SL redesign Phase 11 applied to VWAP,
now extended to EMA Micro-pullback and OI/Volume Confirmed per explicit
user request ("in the same pattern... EMA and OI/volume strategy, remove
the rate limiter in entry and exit, keep similar profit range and SL
combo, keep the base as control").

**Rate-limiter evaluation** (both strategies have several candidate
mechanisms, unlike VWAP's single obvious knob — see
`project_vwap_quick_scalper_2026_09_04` memory for the full writeup):
`structure_break_persistence_seconds` (exit, both strategies) left UNSET
everywhere — already defaults to 6.0s (not VWAP's 120.0 control override),
already proven "quick enough" by Phase 11's own smoke test. EMA's Bone
Zone 2-bar entry confirmation left UNCHANGED — structural to the strategy's
own definition, same category as VWAP's 2-bar touch+confirm which stayed.
OI/Volume's false-breakout 3-bar re-entry grace (`FALSE_BREAKOUT_GRACE_
BARS`) was the one genuine entry-side rate limiter found — made into a new
opt-in `oi_false_breakout_grace_bars` param (default 3, zero behavior
change for every existing config) and set to `0` on every OI scalp config.

**Code**: `stop_points`/`target_points` added to both `EMAMicroPullback
Strategy` and `OIVolumeConfirmedStrategy` (identical opt-in pattern to
VWAP's, reusing the existing `compute_stop_target_points` helper — no
changes needed there). `oi_false_breakout_grace_bars` added to
`OIVolumeConfirmedStrategy`. Both strategies' `api/v1/strategies.py`
param-forwarding allowlists updated in both copies (real repo +
`backtest_engine`'s pin) up front this time — the exact 2-copy gotcha
Phase 11 hit once already. 6 new tests (fixed-point vs pct-based
regression guards per strategy + a grace-bars=0 behavior test), 86/86
relevant tests pass, ruff/mypy clean. **LOCAL/UNCOMMITTED in the main
repo**, same standing status as every other backtest-only change this
session — see `project_backtest_local_only_standing_policy_2026_09_04`.

**Smoke test first** (`p12_smoke`, 6 configs: control + 2 scalp variants
per strategy) — genuinely different result from Phase 11's clean 0/22:
`oi_control` PF 0.27 (dismal) vs `oi_scalp_t5_sl100`/`t7_sl150` PF
0.94-0.96, win 63-69%, strong OOS/H2 halves (+₹355-637). `ema_scalp_
t7_sl150` was IS-positive (PF 1.11). **Diffed the actual trades**:
`oi_control` vs `oi_scalp_t5_sl100` share 15/16 identical entries — the PF
jump is overwhelmingly from the points-based exit, not from
`grace_bars=0` (which changed exactly 1 trade). Kept `grace_bars=0` on
every OI scalp config anyway (cheap, that 1 trade was net-positive) but
didn't build a separate isolation dimension for it.

**Grid redesign from the smoke test's own evidence, not just copied from
Phase 11**: plotted VWAP's Phase 11 PF-by-target at fixed SL ratio (e.g.
SL150: t4=0.48 t5=0.81 t6=0.30 t7=0.31) — a single-point spike at t5 with
flat neighbors on both sides, noise at n=16 trades/config, not a
resolvable curve. Sparsified target grid from Phase 11's dense {4..10} to
{4,6,8,10,12} — same number of samples, extended range instead of denser
resolution in a region that wasn't resolving anything reliable. SL ratio
grid extended from {50,100,150%} to {50,100,150,200%} — wider SL has won
in every smoke/sweep result so far across all three strategies (VWAP/EMA/
OI), 150% hasn't been shown to be the ceiling.

**Final grid**: target ∈ {4,6,8,10,12} pts × SL ratio ∈ {50,100,150,200%}
= 20 scalp configs + control, per strategy × 2 strategies = **42 configs
total**. `sweep_configs/phase12_ema_oi_scalper.txt`, checksum verified
local == A1. Launched `backtest-20260904-202555.service`, 02:25:55 IST
2026-09-05 (Saturday, well off-hours, whole run fits inside the weekend —
`SHARD_COUNT=4` applies throughout). Reaper timer stopped for the
duration. **42 configs confirmed in the launch log.**

**ETA**: smoke test measured ~405-407s/config for BOTH strategies (not
the README's ~22-25min/config conviction-variant rate — that table was
for the conviction subclasses; these plain base classes run at VWAP's own
pace). 42 × ~6.8min ≈ **~4.75 hours → complete by ~07:15 IST**. Analyze
`s6_p12` with `analyze_walkforward.py` once done, same robustness bar as
every prior sweep. Re-enable `backtest-reaper.timer` once done.

---

## 2026-09-04 (~22:53 IST) — Phase 11 CONCLUDED: VWAP quick-scalper fails the robustness bar outright, PARK — none of 22 configs pass, worse than %-based control

Sweep `s6_p11` completed 16:02:34 UTC (21:32 IST, 144 min, all 22 configs OK,
16 trades each). Analyzed with `analyze_walkforward.py` (default cost model,
`--oos-from 2026-04-01`) against all 22 configs, same robustness bar as
every prior sweep (positive IS *and* OOS *and* both 6-month halves; bootstrap
P(mean≤0) ≤ ~0.15 with non-negative 5th-percentile; survives 1.0%/side
slippage).

**Verdict: 0/22 pass — a clean, decisive negative result, not a marginal
one.** Every single config, `p11_control` included, has a negative overall
expectancy and a negative IS expectancy; bootstrap `P(mean≤0)` ranges
0.599–0.998, nowhere close to the ≤0.15 bar. A handful of the wider-SL
configs (`t5_sl150`, `t6_sl150`, `t7_sl150`, `t8_sl150`, `t9_sl50`,
`t10_sl50`) show a positive OOS half, but every one of those still fails IS
hard (IS tot from −643 to −4079) — the bar requires both, not either. Least
bad by total/PF: `p11_scalp_t5_sl150` (tot −325, E −20.3, PF 0.81) — still a
clear fail (bootstrap P(mean≤0)=0.599).

**Reading**: the fixed-point target/SL reparameterization (this session's
whole premise — swap %-of-entry for absolute premium points, see the entry
below) did *not* fix the underlying problem. `p11_control` itself (the
%-based baseline, re-run fresh this batch) is comparably bad to the
already-parked `VWAP_Pullback` (0/113 gates, see the 2026-09-01 archive
triage below) — this reads as further confirmation that VWAP_Pullback's
issue is upstream of exit parameterization (entry/signal quality), not
something either the % or the points-based exit design can fix by itself.
Entry logic (2-bar VWAP touch+confirm) was deliberately left untouched this
sweep per the original plan — that's the next thing to question if VWAP
pullback is ever revisited, not another exit-grid sweep.

**Action**: VWAP quick-scalper joins `VWAP_Pullback`/`Liquidity_Sweep` on
the PARKED list. `backtest-reaper.timer` re-enabled on A1 (sweep + analysis
both complete, results already on the box, nothing pulled to Windows per
restrictive rule 11 — the full analysis output above is the only artifact
needed). The `stop_points`/`target_points` code additions
(`vwap_pullback.py`, `common_rules.py`) remain LOCAL/UNCOMMITTED in the main
repo per standing policy — no reason to commit code for a parked approach;
left as-is pending explicit user direction on whether to keep, discard, or
repurpose for a different strategy.

---

## 2026-09-04 (~19:08 IST) — Phase 11 launched: VWAP quick-scalper, fixed premium-point target/SL instead of %-of-entry

**Done.** New opt-in `stop_points`/`target_points` params on
`VWAPPullbackStrategy` (`app/modules/strategy_engine/strategies/
vwap_pullback.py`) + a new shared `compute_stop_target_points` helper
(`common_rules.py`, mirrors `compute_stop_target`'s shape — absolute
points instead of percent, floored at one tick above zero since a fixed
stop can exceed a cheap deep-OTM entry premium, unlike the pct-based
formula). Both params default `None` — zero behavior change for every
existing config; entry logic (2-bar VWAP touch+confirm) is untouched, kept
deliberately per explicit user decision. **This code is LOCAL/UNCOMMITTED
in the main repo (`backend/app/...`), by explicit user instruction — same
standing status as the momentum-plateau entry-anchor fix two entries
below.** Not pushed, not deployed. 40/40 relevant tests pass, ruff/mypy
clean.

**Two real bugs the plan's own pre-sweep smoke test caught, before any
compute was wasted**:
1. `api/v1/strategies.py`'s param-forwarding allowlist exists in two
   copies (real repo + `backtest_engine`'s own pin) — only the real repo's
   copy got the new keys on the first pass, so the smoke test's
   `--strategy-params` silently fell back to the old %-based path with zero
   error (`_build_strategy` drops unknown keys before the constructor ever
   sees them). Fixed with a **surgical 2-line patch** on both copies
   (local + OCI), not a full-file sync — `backtest_engine`'s copy of this
   file has drifted too far from unrelated same-day production work
   (archive/rename/circuit-breaker) to safely overwrite wholesale.
2. Original design set `structure_break_persistence_seconds: 0` on every
   scalper config to "remove exit delay." Smoke test showed this produces
   a real artifact: **~25% of trades exit at the exact entry minute with
   exactly zero pnl** — a same-bar-wick false breach with no grace to
   reclaim, sourced from the very confirmation bar that fired entry.
   Re-tested with the param left **unset** (class default 6.0s) — same
   trades still exit via `structure_break` within ~1 minute, but with real
   price movement, not a flat freeze. 6.0s is already effectively instant
   for a scalp that plays out over several minutes; final sweep leaves it
   unset on all 21 scalper configs (control keeps 120.0, matching Phase
   10c's `p10c_vwap_off` exactly).

**Sweep**: `sweep_configs/phase11_vwap_scalper.txt`, 22 configs —
`p11_control` (VWAP_base unchanged, run fresh in this batch rather than
reused from Phase 10c, for a byte-identical comparison environment) + 21
scalper configs = target ∈ {4..10 points} × SL ratio ∈ {50%,100%,150% of
that config's own target}. Trail left at class defaults (0.5/0.5) — self-
scales lighter automatically against the smaller target, no separate sweep
dimension. Launched `backtest-20260904-133808.service` (first attempt
failed instantly — wrapped `run_bt.sh` in a redundant outer `systemd-run`
when it already does that internally; fixed by calling it directly per its
own usage comment). 22 configs confirmed in the launch log, reaper timer
stopped for the duration.

**ETA ~21:40-22:10 IST tonight** (VWAP-family configs ran ~410s/config in
Phase 10c, `futures_proxy` source). **Analyze `s6_p11`** with
`analyze_walkforward.py` against `p11_control`, same robustness bar as
every prior sweep. Re-enable `backtest-reaper.timer` once done.

**PENDING — backtest data coverage gap, not started, not part of this
sweep**: current NIFTY/options data covers ~52 weeks (~1yr); ~2-3 weeks of
real data collected live since the last load hasn't been appended. Two
closure options flagged by the user, neither evaluated: (a) append what's
already been collected live, or (b) backfill the gap directly from Alice
Blue/Shoonya historical data. Intended to become a **recurring monthly
append** once done once. Needs its own dedicated session — IST handling,
tick-vs-1-min-bar granularity, the near-expiry-week convention, and the
same data-quality skepticism behind the 2026-09-03 option-tick-
plausibility fix all apply. Full detail in the
`project_backtest_data_coverage_monthly_append_todo_2026_09_04` Claude
memory entry — do not attempt inline with an unrelated sweep.

---

## 2026-09-04 (~03:14 IST) — Phase 10c launched: rescoped to the 8 currently-enabled strategies, RSI/bothor/bothand dropped, lookback_bars=10 added

Continues the entry below (same session, same feature). `p10b` (the first
relaunch after the entry-anchor bug fix, see the entry two below) was
stopped after 2/44 configs (`p10_orb_slope`, `p10_orb_rsi` both OK) once
its own fresh, bug-fixed data gave a real answer to the open RSI-vs-slope
question — decisively no, RSI is worse than slope on every metric on
apples-to-apples fixed data (win 38.6% vs 34.1%, E −210 vs −183, PF 0.31 vs
0.43, P(mean≤0) 0.991 vs 0.949). `p10b`'s partial output backed up to
`/tmp/s6_p10b_partial_2configs_backup` on the box (2 configs only, not
useful beyond what's already recorded here).

**Separately, three fast targeted smoke tests** (`run_backtest.py --pairs
'YYYY-MM-DD:YYYY-MM-DD,...'`, replaying only the exact 3 (entry, expiry)
day-pairs the fixed `orb_slope` run's own `momentum_plateau` exits came
from — not a full near-expiry-week scan) tested whether the default
5-bar lookback window is simply too tight, independent of the RSI
question: `plateau_lookback_bars` 5 -> 10 -> 15 on those same 3 trades
gave a monotonic, large improvement (total PnL −676 -> −43 -> +67). Two of
the three trades already resolve (stop/trail) before bar 10, so 10->15
changed nothing further for them; the third trade's loss kept shrinking
(−588 -> −371 -> −260) as it got more room. n=3, hand-picked (these were
exactly the trades the tight window damaged) — not a base rate, but a real,
concrete, monotonic signal on top of the RSI finding.

**Rescoped and relaunched as `backtest-20260903-214405.service`
(`RUN_TAG=p10c`, output `s6_p10c`)** at 21:44:05 UTC / ~03:14 IST:
- **8 baselines** instead of 11 — the 8 currently-**enabled**
  `strategy_configs` rows only (dropped the 4 disabled ones this session's
  earlier 11-baseline design had included). `VWAP_RSI_modified`'s real
  live params (`momentum_lookback_bars:1, require_momentum_alignment:true`)
  now used as the `vwap_pullback_conviction` slot, replacing disabled
  `VWAP_Conviction` — the earlier exclusion of `VWAP_RSI_modified` was
  reversed once the user asked to use all 8 enabled configs.
- **3 variants per baseline** (not 5): `off` (new — no plateau at all,
  the control this design was missing before), `slope` (default 5-bar),
  `slopelb10` (10-bar). RSI, bothor, bothand all dropped per the findings
  above. 8 x 3 = 24 configs — same count as the original trimmed plan,
  swapped for more informative data per explicit user instruction.
- Ordered by explicit user priority: VWAP, EMA, ORB, OI/Vol (each
  base+conviction pair together) — `sweep_configs/phase10c_trimmed.txt`.
- QC'd against real `_build_strategy` before launch (all 24 clean, 0
  build failures) — same pre-launch check every prior batch has used.
  Checksum-verified box copy matches the local QC'd source exactly
  (`12f7449ae9d61b43ba9ab084d1b77515`) before launching.
- Pre-launch safety checks all clean: reaper timer confirmed still
  stopped, no other backtest process running, credentials dir clean
  (no real `.env`/session-cache files), 85GB free.
- One cosmetic-only anomaly checked and ruled out: `run_sweep.sh`'s own
  config-count log line says "25 configs" (its `grep -cv '^#'` counts a
  blank separator line between the file's comment header and the first
  config as if it were one) — confirmed via live log inspection that the
  actual while-loop correctly skips it (no phantom `--- ` log entry, no
  error, first real log entry is `p10c_vwap_off`) — display-only, not a
  functional bug, and not something this session introduced (same file
  shape every prior sweep here has used).

**ETA ~10.4hrs from launch** (24 configs at the ~26min/config observed
rate) — expect completion **~13:30-14:00 IST 2026-09-04**. **Analyze
`s6_p10c`** (not `s6_p10` or `s6_p10b`, both superseded/backed up, not
deleted). Re-enable `backtest-reaper.timer` once done and results are
pulled, per standing rule 13.

---

## 2026-09-04 (~02:04 IST) — Phase 10 real bug found from the sweep's own first results, fixed, relaunched

The first Phase 10 run (`backtest-20260903-154300.service`) was stopped
after 10/44 configs — every single one failed catastrophically (PF
0.24-0.62, P(mean≤0) 0.83-1.00, win rates crashed to 20-38% vs. these
strategies' normal 40-50%+ baselines). Traced to a real, structural bug,
not noise — killed and its output moved to
`/tmp/s6_p10_buggy_pre_entry_anchor_fix_backup` on the box (not deleted,
kept for reference).

**Root cause, confirmed against real underlying data, not inferred**:
`momentum_plateau` accounted for ~35-40% of all exits, every one firing
within 0-17 minutes of entry (two at the *exact* entry minute). The
lookback window was "last N bars **as of now**", not "last N bars **since
entry**" — on the very first poll after entry, that window is still
dominated by *pre-entry* bars. For a breakout strategy (ORB/OI), the
pre-entry window is by definition the calm consolidation the entry itself
fired on — not genuine post-breakout decay. Verified on one real case: a
2025-10-01 09:35 ORB entry's window (09:30-09:35, all pre-entry-or-at-entry)
moved only 0.8 points against a ~3.6-point (0.3×ATR) threshold, firing
`MOMENTUM_PLATEAU` at the *same minute* as entry.

**Fix**: both the production wrapper (`_momentum_plateau_detected`, now
takes `entry_time` and passes `since=entry_time` to both
`get_recent_completed_bars`/`get_recent_indicator_values`) and the
backtest's `_lookup_last_n_at_or_before` (new `since` floor param, applied
at the Step 3b call site with `since=entry_time`) now floor the window at
the position's real entry — `position.opened_at` in production,
`trade_intent.created_at` in the backtest (already the function's own
`entry_time` reference). The shared pure function `momentum_plateau_signals`
itself needed no change — the bug was entirely in the two *data-gathering*
wrappers, confirming that "shared pure function, different data-gathering"
split was the right original design (one isolated fix per side, not two).

New regression test
(`test_evaluate_open_position_momentum_plateau_ignores_pre_entry_flat_bars`)
directly reproduces the real bug (flat bars seeded before entry only) and
asserts the position stays open. 1582/1582 backend tests pass, ruff/mypy
clean. Live-confirmed via a re-run smoke test against the exact strategy
that showed 0-minute exits: momentum_plateau exits now hold 8-14 minutes,
zero 0-minute exits observed.

Relaunched as `backtest-20260903-203447.service` (`RUN_TAG=p10b`, output
`s6_p10b`) at 20:34:47 UTC / ~02:04 IST — same 44 configs, same
methodology as the original Phase 10 design. Superseded by the rescoped
Phase 10c launch above before it got past 2 configs.

---

## 2026-09-03 (~15:43 IST) — Phase 10 launched: new exit-side "momentum plateau" condition built + tested, 44-config sweep running

**New production feature** (not yet deployed live — backtest-only for now,
per standing "backtest first, judge against real data" discipline):
`ExitReason.MOMENTUM_PLATEAU`, a new stateless exit check in
`evaluate_open_position` (between structure_break and spread_blowout,
skipped once TRAIL is already ACTIVE) — exits when the underlying's own
breakout drive has flattened (price-slope and/or RSI14 barely moved over
the last N bars), independent of `structure_level` proximity. Params
(`require_momentum_plateau_exit`, `plateau_use_slope`, `plateau_use_rsi`,
`plateau_combine_mode` any/all, `plateau_lookback_bars`,
`plateau_slope_atr_fraction`, `plateau_rsi_flatten_delta`) resolved live
off the owning `StrategyConfig.params` row (not frozen at signal time,
strategy-type agnostic — applies to base and `*_conviction` alike with
zero constructor changes). Zero new DB columns/migration. Threshold logic
lives in one shared pure function, `common_rules.momentum_plateau_signals`
— production wraps it with DB reads, the backtest wraps it with
`underlying_series`/`rsi_series`/`atr_series` in-memory slicing (same
performance reasoning `atr_series` itself exists for), so the two can't
silently diverge. 1553/1553 backend tests pass (up from 1550), ruff/mypy
clean.

**Two real bugs caught before/during this build, both fixed same session**:
1. First draft unioned the new params into the same `*_PARAM_KEYS` sets
   `_build_strategy` uses to forward kwargs into each strategy's
   constructor — crashed immediately (`TypeError: unexpected keyword
   argument 'require_momentum_plateau_exit'`) since none of the 10
   strategy constructors accept them, by design (params are read live off
   `strategy_config.params`, never through a constructed object). Fixed by
   *not* unioning them in at all — same "inert config the constructor
   doesn't need" pattern `ORB_PARAM_KEYS`'s own comment already documents.
2. The backtest engine's `HistoricalBrokerAdapter` never implemented
   `BrokerPort.get_recent_trades` (added to the interface 2026-09-02 for
   production's reconciliation auto-repair path) — the smoke test's first
   run crashed on `TypeError: Can't instantiate abstract class` the moment
   the re-synced `app/` pin picked up the new abstract method. Fixed with
   an unconditional `[]` (mirrors `MockBrokerAdapter`'s own choice) —
   genuinely never called in a backtest replay, but costs nothing to
   implement correctly rather than leave crashing.

**Re-sync near-miss, twice, both caught and fixed before anything left this
machine**: a plain `robocopy /MIR` of `backend/app` -> `backtest_engine
/backend/app` (no exclude) copied real, live `.env` credentials (shoonya,
alice_blue, angel_one, telegram, truedata) and the Alice Blue session-cache
JSON into the backtest copy — the exact filesystem-copy-leaks-gitignored-
secrets trap this ledger already knew about from an earlier session, just
rediscovered under `/MIR` specifically. Caught and deleted both times
before any `scp`/commit. A subsequent `/XD` exclude attempt to fix it
*also* failed to actually exclude the directory (robocopy's multi-segment
`/XD` path matching didn't behave as expected) and leaked the same files a
second time — caught and cleaned immediately again. **Lesson for next
resync**: don't use `robocopy /MIR` (or any recursive mirror) against
`backend/app` at all — copy only the specific files that actually changed,
or if a full mirror is ever truly needed, exclude `config/credentials`
by full absolute source AND dest path on both sides of the command, then
verify with an explicit `find -iname '*.env' -not -iname '*.example'`
before any `scp`/git operation, every time, not just once.

**Sweep**: 11 baselines (every current `strategy_configs` row, deduped,
`VWAP_RSI_modified` excluded per explicit instruction — it's the
`require_momentum_alignment` config, not `require_rsi_alignment`, matching
naming intent) × 4 exit-side variants (`plateau_slope` / `plateau_rsi` /
`plateau_both_or` / `plateau_both_and`) = 44 configs,
`sweep_configs/phase10_plateau_exit.txt`. Every line QC'd against the real
`_build_strategy` before launch (all clean, matching the established
pre-launch check). Launched detached
(`backtest-20260903-154300.service`, `RUN_TAG=p10`, reaper stopped first)
at 15:43 IST — smoke-tested first (1 config, 4-shard) before the full
launch, confirmed real `momentum_plateau` exits firing correctly (2 of 4
sample trades) alongside an unaffected `structure_break` exit in the same
small sample. ETA ~18-19hrs (44 configs at Batch 4's observed ~25min/config
rate) — analyze with `analyze_walkforward.py` once done, same methodology
as every prior batch, same trade-level noise-spike skepticism the ORB
momentum-entry N=5 result already demonstrated this session (check
neighboring param values, check whether a "pass" traces to 1-2 trades).
Re-enable `backtest-reaper.timer` once the sweep completes and results are
pulled, per standing rule 13.

---

## 2026-09-03 (~10:00 IST) — Phase 9 concluded: Batch 4 (full conviction battery) analyzed — momentum gate NOT promoted for either strategy

Continues the entry below (same session pin `83ee6ad`). `backtest-20260902-180928.service`
completed cleanly at 00:27 UTC / 05:57 IST — within the ~05:30-06:00 IST
ETA, all 15/15 configs `OK`, zero failures, 377min total. `backtest-reaper.timer`
re-enabled per standing rule 13 once results were pulled. Analyzed with the
same `analyze_walkforward.py` methodology (`--oos-from 2026-04-01`,
0.5%/side slippage, bar = `P(mean≤0)≤0.15`).

### ORB_Conviction (full baseline) — off/N1..N10

| N | n | win% | E/lot | PF | P(mean≤0) | bar (≤0.15)? |
|---|---|---|---|---|---|---|
| off / N1 (identical) | 40 | 50.0% | 122 | 1.49 | 0.186 | fail (marginal) |
| N2 | 40 | 50.0% | 52 | 1.16 | 0.374 | fail |
| N3 | 40 | 52.5% | 28 | 1.08 | 0.429 | fail |
| N4 | 38 | 55.3% | 47 | 1.14 | 0.380 | fail |
| N5 | 35 | 57.1% | **226** | 2.08 | **0.062** | **pass** |
| N6 | 27 | 48.1% | 99 | 1.43 | 0.250 | fail |
| N8 ⚠️ | 9 | 44.4% | 353 | 5.61 | 0.040 | pass, fragile (n=9) |
| N10 ⚠️ | 5 | 60.0% | 371 | 7.17 | 0.077 | pass, fragile (n=5) |

**N5's numbers are byte-identical to Batch 1's already-known N5 result** —
same deterministic entry set, not independent confirmation (per this
ledger's own 2026-09-01 methodological point: pass-counts on a shared
entry set aren't new evidence). What's genuinely new here is the
**neighbourhood**: N4 fails (P=0.380) and N6 fails (P=0.250) on both
sides of N5's pass, on respectable sample sizes (38, 27) — not thin-n
noise. A single point surrounded by clean failures on adequately-sized
neighbours is the **same "lone spike = noise" shape already flagged for
the ORB width sweep** (see [[reference_orb_width_resweep_pending]]) and
for Batch 3's N=8. **Read: ORB_Conviction's momentum "edge" at N=5 does
not clear the bar for promotion** — it looks like a fluke in this specific
N, not a real monotonic momentum effect. N8/N10 passes were already
expected-fragile (n=9, n=5) and don't change this.

### OI_Volume_Conviction (full baseline) — off/N1..N5

| N | n | win% | E/lot | PF | P(mean≤0) | bar (≤0.15)? |
|---|---|---|---|---|---|---|
| off / N1 (identical) | 14 | 71.4% | 274 | 5.07 | **0.004** | **pass** |
| N2 | 10 | 70.0% | 299 | 8.75 | 0.002 | pass |
| N3 | 8 | 75.0% | 342 | 10.18 | 0.002 | pass |
| N4 ⚠️ | 5 | 60.0% | 166 | 3.78 | 0.100 | pass, thin |
| N5 ⚠️ | 4 | 75.0% | 267 | 19.32 | 0.004 | pass, thin (n=4) |

**Important correction to how Batch 1's OI result was framed**:
`momentum_lookback_bars=1` requires the last **1** close to be
"monotonic" — trivially always true for a single point — so `off` and
`N1` are identical by construction (same convention already noted for
every other config family in Batch 2/3). That means **the OI baseline
already clears the bar with zero momentum filtering at all**
(n=14, win 71.4% in *both* IS and OOS halves, P=0.004) — momentum was
never the thing making OI pass. N2/N3 stay passing as N tightens, but
sample shrinks fast (14→10→8) with no clear improvement in P, and N4/N5
are too thin (5, 4) to trust either way. **Read: momentum adds no
demonstrated incremental value for OI_Volume_Conviction — the strategy
was already robust without it.** Do not add the gate to the live
`OI_Volume_Conviction` paper config on this evidence; it would only
shrink the sample for no proven gain.

### Verdict — momentum gate (`require_momentum_alignment`) not promoted anywhere this series

Across all 4 batches (Batch 1-4, VWAP/EMA/ORB/OI, base and full
baselines): **no config shows a clean, non-fragile, monotonic momentum
benefit.** EMA_Micro_Conviction's earlier pass (Batch 1/2) predates
momentum entirely — it's the pre-existing `full` conviction gate set,
already live. Every apparent momentum "win" elsewhere (ORB N5, ORB
N8/N10, OI N2-N5) is either a lone spike between failing neighbours or
too thin a sample to trust — the identical failure mode already
catalogued in the 2026-09-01 full-archive triage's own methodological
notes. `VWAP_RSI_modified` (momentum N=1 only, already live in paper
per the entry below) stays as-is — no code or config change follows from
this analysis; it was already a low-risk, no-other-gates test. Phase 9
is concluded: the new gate is built, tested, documented, and evidently
doesn't move the needle for ORB/OI/VWAP at any N — no further sweeping
planned unless a new hypothesis motivates it.

### Housekeeping

- `backtest-reaper.timer` re-enabled (`sudo systemctl start
  backtest-reaper.timer`), confirmed active, daily 16:00 IST cadence.
- `backend/scripts/BACKTEST_LEARNINGS.md` re-synced box↔local
  (checksum-verified) before and after this entry.
- Disk: 86G free on the A1 box post-sweep, no cleanup needed.

---

## 2026-09-02 (~23:45 IST) — Phase 9 continued: Batch 2 (EMA) + Batch 3 fully analyzed, Batch 4 (full conviction battery) launched detached, reaper cadence changed to daily

Continues the entry below (same session, same pin `83ee6ad`). Methodology
unchanged: `analyze_walkforward.py`, `FLAT_COST_PER_LOT=10.0`, 0.5%/side
slippage, 10k-resample bootstrap for `P(mean≤0)`, bar = `P(mean≤0)≤0.15`
(fast single-metric screen, not the full 8-gate archive-triage bar from the
2026-09-01 full-archive re-analysis entry).

### QC on Batch 2 + Batch 3 raw data, before trusting any number below

- All 26 result files load cleanly, correct columns, no missing/corrupted
  shards, no zero-byte files.
- **Sanity check passed**: trade count falls monotonically (or holds flat)
  as `N` increases within every config family — ORB base-entry 44→44→43→39→
  33→13→7→0 (N1..N15), OI base-entry 45→42→36 (N1..N3) — exactly what a
  stricter monotonic-close filter should do. No config shows trade count
  *rising* with `N`, which would indicate a filter bug.
- **Expected duplication, not a bug**: `*_off` and `*_n1` are byte-identical
  in every Batch 2 pair (VWAP base/full/nopcr, EMA base/full).
  `momentum_lookback_bars=1` has nothing to compare against, so the
  "strictly monotonic" check is vacuously true and the gate is a no-op at
  N=1 — confirms the pipeline reproduces correctly, not corruption. Table
  below merges these pairs into one row rather than showing duplicates.
- **One fragile result flagged, not hidden**: ORB base-entry N=8 (n=13)
  passes the bar strongly (P=0.014), but sits between N=6 (n=33, fails
  badly) and N=10 (n=7, fails badly) with no smooth trend — classic
  small-n noise spike, not a real signal. Treat as unreliable until
  independently re-confirmed (it *is* re-tested, on the full conviction
  baseline this time, by Batch 4 below — N=8 is in that grid).

### Batch 2 (`s6_mom2`) — COMPLETE, 15 configs, off/N1/N2 matrix, ANALYZED

| Strategy | Config | n | win% | E/lot | PF | P(mean≤0) | bar (≤0.15)? |
|---|---|---|---|---|---|---|---|
| VWAP | base, off/N1 (identical) | 16 | 25.0% | −352 | 0.26 | 0.986 | fail |
| VWAP | base, N2 | 16 | 18.8% | −406 | 0.20 | 0.995 | fail |
| VWAP | full, off/N1 (identical) | 15 | 53.3% | +84 | 1.50 | 0.248 | fail |
| VWAP | full, N2 | 15 | 46.7% | +15 | 1.08 | 0.451 | fail |
| VWAP | nopcr, off/N1 (identical) | 16 | 50.0% | −44 | 0.83 | 0.623 | fail |
| VWAP | nopcr, N2 | 16 | 43.8% | −130 | 0.63 | 0.796 | fail |
| EMA | base, off/N1 (identical) | 47 | 40.4% | −56 | 0.78 | 0.752 | fail |
| EMA | base, N2 | 47 | 42.6% | −95 | 0.65 | 0.878 | fail |
| EMA | full, off/N1 (identical) | 12 | 58.3% | **+304** | 3.05 | **0.039** | **pass** |
| EMA | full, N2 | 11 | 54.5% | +264 | 2.63 | **0.071** | **pass** |

`m2_ema_base_off` (n=47) also serves as the clean rerun of the reaper-
corrupted Batch-1 `m1_base_ema` — this closes "Not yet done" item 5 from
the entry below; that corrupted number can now be fully discarded.

**Read**: VWAP and OI never benefit from momentum at any N tested (0/1/2/3)
— consistent failure across both batches, no reversal. EMA's `full`
variant passing is not new information — it's the already-known
`EMA_Micro_Conviction`/`_PCR` result reproduced, not a new finding from
this batch.

### Batch 3 (`s6_mom3`) — COMPLETE, 11 configs, base-entry-only wide-N grid, ANALYZED

| Strategy (base entry) | N | n | win% | E/lot | PF | P(mean≤0) | bar (≤0.15)? |
|---|---|---|---|---|---|---|---|
| ORB | 1 | 44 | 40.9% | −177 | 0.60 | 0.869 | fail |
| ORB | 2 | 44 | 43.2% | −152 | 0.65 | 0.817 | fail |
| ORB | 4 | 43 | 46.5% | −205 | 0.60 | 0.878 | fail |
| ORB | 5 | 39 | 38.5% | −139 | 0.66 | 0.825 | fail |
| ORB | 6 | 33 | 45.5% | −135 | 0.65 | 0.799 | fail |
| ORB | 8 ⚠️ | 13 | 53.8% | +330 | 5.02 | **0.014** | pass, **fragile — see QC note** |
| ORB | 10 | 7 | 42.9% | −114 | 0.63 | 0.674 | fail |
| ORB | 15 | 0 | — | — | — | n/a | too strict, zero trades |
| OI | 1 | 45 | 40.0% | −147 | 0.55 | 0.941 | fail |
| OI | 2 | 42 | 40.5% | −73 | 0.76 | 0.745 | fail |
| OI | 3 | 36 | 36.1% | −233 | 0.50 | 0.921 | fail |

**Read — this is scope divergence, not a reproducibility failure.** Batch
1's "ORB benefits from momentum" finding (N=1 fail → N=5 pass, E climbing
122→226) was on the *full* `ORB_Conviction` (`require_prior_day_trend` +
`max_or_range_nifty_points=65` + tuned exits). This batch's *base*-entry
ORB (no other conviction gates, momentum only) shows no clean trend at
all — N1-N6 all fail, N8 spikes (flagged fragile above), N10 fails again,
N15 has zero trades. The two config families are genuinely different
strategies sharing a name prefix; Batch 3 was never a rerun of Batch 1's
ORB config, so this isn't evidence the earlier result doesn't reproduce —
it's evidence the effect is specific to the full conviction baseline, not
base entry. Batch 4 below tests that directly.

### Batch 4 (`s6_mom4`) — LAUNCHED 2026-09-02 23:39 IST, RUNNING (resume here)

**Why**: per explicit user instruction, given Batch 3's base-entry ORB
didn't reproduce Batch 1's full-conviction ORB finding, run a *full
battery* on the two conviction baselines that mattered — including a
fresh `off` reference and fresh N=1/N=5 — rather than trust Batch 1's N=1/
N=5 numbers stitched against new N values from a different date. Every N
in this grid is now directly comparable within one run/pin.

15 configs, exact same param blobs as Batch 1's `m1_orb_n1`/`m1_oi_n1`
(the real live top-level params — `d_pdt_w65`/`w7_s18_a12_l06` for ORB,
`o3_atr_pcrl` arm.30/lock.85 for OI), momentum toggled off/N1..N-max, same
`off`-drops-the-momentum-keys convention as Batch 2:

- `ORB_Conviction` (full): off, N∈{1,2,3,4,5,6,8,10} — 9 configs. Wide
  grid because Batch 1 showed only a mild trade-count drop N1→N5 (40→35),
  so the entry set stays large enough to be worth exploring further.
- `OI_Volume_Conviction` (full): off, N∈{1,2,3,4,5} — 6 configs. Capped at
  5 because Batch 1 already showed a steep drop N1→N5 (14→4) — going
  further would be sample-starved before it starts.

**QC'd before any compute spent**: every config line verified against the
real production `_build_strategy` (imports `app.api.v1.strategies
._build_strategy` directly, constructs each `StrategyConfig` stub, checks
every intended param actually landed on the resulting `Strategy` object) —
all 15 clean, no gate silently missing, no unintended gate on. One
false-positive in the QC check itself (comparing raw `"10:15"` string
against the parsed `datetime.time(10, 15)` object) — fixed in the checker,
not a real config problem.

**Launch mechanism**: `sudo /opt/backtest/run_bt.sh env RUN_TAG=mom4
./run_sweep.sh sweep_configs/phase9_momentum_batch4.txt` — a detached
`systemd-run` transient unit (`backtest-20260902-180928.service`) inside
`backtest.slice`, survives laptop/session disconnect by design (same
mechanism `/opt/backtest/run_bt.sh` already provided; Batch 3 additionally
used a wait-loop to sequence after Batch 2 — not needed here, nothing else
queued).

`backtest-reaper.timer` stopped before launch (confirmed `inactive`), to
be re-enabled only once Batch 4 fully completes and results are pulled —
per standing rule 13. Reaper cadence itself changed today from hourly to
**daily at 16:00 IST**, `Persistent=true` — disk headroom made hourly
unnecessary, and `Persistent=true` means a resume after a paused sweep
that missed that day's 16:00 slot fires an immediate catch-up run, which
is safe by construction since resume only ever happens once nothing is
active. Both the box's and local `backtest_engine/README.md` updated and
checksum-verified in sync.

**ETA**: ~23-25 min/config at `SHARD_COUNT=4` (ORB/OI family rate),
running at the full 200% off-hours CPU quota (launched at night) —
expect completion **~05:30-06:00 IST**, then re-enable the reaper and
analyze with the same `analyze_walkforward.py` methodology as above.

### Not yet done (resume here)

1. **Analyze Batch 4** once it completes — this is the real test of
   whether momentum genuinely helps the full `ORB_Conviction`/
   `OI_Volume_Conviction` baselines, on a clean single-session dataset
   covering off/N1 through the practical N ceiling for each.
2. **Real RSI14-oscillator gate** (`require_rsi_alignment`, band-vs-50 —
   distinct from the momentum/"micro-trend" gate) still fully deferred:
   base VWAP + RSI, full VWAP conviction + RSI (regression check, already
   known net-harmful from Sweep #4 Phase 7), base EMA + RSI, full EMA
   conviction + RSI (both EMA combos genuinely untested).
3. If Batch 4 finds a real N that clears the bar cleanly (not a thin/
   fragile n like Batch 3's N=8), that becomes a real production-decision
   candidate for `ORB_Conviction`/`OI_Volume_Conviction` — judge against
   real paper data first, per standing rule, not deploy straight off a
   backtest pass.
4. `backend/scripts/BACKTEST_LEARNINGS.md` synced to the box copy (was
   stale there, missing this entire Phase 9 series) — done as part of this
   update, checksum-verified.

---

## 2026-09-02 (~03:34 IST, session ongoing) — Phase 9: new `require_momentum_alignment` gate built, tested, one live paper deploy; Batches 1–2 done/running, Batch 3 queued+auto-chained

New gate, new sweep series, not a continuation of Sweep #4's own gate set.
Cost model throughout: `analyze_walkforward.py`'s `FLAT_COST_PER_LOT=10.0`
(the corrected value — see this ledger's own top-of-file caveat about the
₹40 vs ₹10 split). All NIFTY, `--all-expiries --near-expiry-days 6
--exit-mode current`.

### What was built (commit `83ee6ad`, already on `main`/deployed live)

1. **RSI14 test coverage** — `RSICalculator`/`IndicatorEngine`/`warm_start`
   wiring and the `require_rsi_alignment` gate (both built 2026-08-31, zero
   tests until now) got real coverage: hand-computed Wilder-smoothing tests,
   engine warmup tests, and gate-behavior tests via ORB's own harness.
2. **`require_momentum_alignment`/`momentum_lookback_bars`** — new,
   independent `ConvictionGateMixin` gate. For lookback `N`, requires the
   last `N` closes **strictly monotonic** (all consecutive increasing for
   CE, all decreasing for PE) — not a single current-vs-N-bars-ago
   comparison. Default `False`/`1`, zero behavior change until opted in.
   Wired through all 5 `*_conviction` subclasses.

### Real bugs found and fixed before/during the sweep, now in `backtest_engine/README.md`'s own rules

- **Hourly `backtest-reaper.timer` force-drops a currently-running sweep's
  own shard databases** — no liveness check, matches on name pattern alone.
  Killed 3 of 4 shards of `m1_base_ema` mid-run at an exact hour boundary
  (`AdminShutdown` in the shard logs, confirmed via the systemd journal:
  the reaper's own `DROP DATABASE ... WITH (FORCE)` at the top of the
  hour). Fixed by stopping the timer for the duration of any multi-hour
  sweep, re-enabling only once it fully completes — `run_sweep.sh`'s own
  per-config reaping is unaffected and is what actually keeps disk in
  check during a run.
- **Re-syncing `app/` via plain `tar`/`scp` copies live broker credentials**
  — `app/config/credentials/*.env` and `.alice_blue_session_cache.json` are
  gitignored (skipped by git-based tooling) but not excluded by a
  filesystem-level copy. Caught within minutes on the first re-pin, deleted
  from both the box and the local scratchpad copy before any further use —
  never left this box (already trusted, already running live trading with
  the same credentials).
- **A stale sweep-config file silently sat on the box** after a local
  revision — checksums didn't match, would have silently run an older,
  smaller config set. Caught by verifying `md5sum` before every launch,
  now standing practice.
- **Silent-param-drop risk from an out-of-date `app/` pin** — `run_backtest.py`
  imports the real production `_build_strategy`; unrecognized
  `strategy_configs.params` keys are silently ignored, not rejected. A
  sweep testing a param that didn't exist at the pinned commit would
  produce a normal-looking, wrongly-gate-off result instead of an error.
  Fixed by a deliberate re-pin to `83ee6ad` + a QC script
  (`_build_strategy` against every config line, verifying the intended
  params actually land) run against both the local venv and the box's own
  venv before any compute was spent — caught the stale-file bug above via
  this exact check.

### `VWAP_RSI_modified` — new, real, live paper config

`d510ca3f-a043-4778-98ae-bf756b1f7e55`, `vwap_pullback_conviction`,
`params={"require_momentum_alignment": true, "momentum_lookback_bars": 1}`
only (no ATR expansion, no PCR band — `ConvictionGateMixin`'s own
guarantee makes this byte-identical to base VWAP Pullback entry logic plus
the momentum filter alone). `is_enabled=true`, `runtime_mode=force_paper`
(deliberately — user wants control over if/when this ever goes live,
independent of the master switch; `NULL`/follow-session was considered and
rejected, see the session transcript for the exact reasoning). No
`max_trades_per_day`, no `qty_lots` override. Verified via the live app's
own `_build_strategy`, not just the DB row. Will auto-spawn at the next
daily bootstrap.

### Batch 1 (`s6_mom1b` + `s6_mom1liq`) — COMPLETE, 16 configs

Baselines are each strategy's exact already-confirmed-best params (ORB:
`d_pdt_w65`/`w7_s18_a12_l06` — the real live top-level params from
`docs/ops/paper_config_update_2026_09_01.md`; OI: `o3_atr_pcrl` arm
.30/lock .85; EMA: `e_pdt_atr` arm .70/lock .80; EMA-PCR: `e3_pdt_atr_pcrl`;
VWAP: `v_atr_pcrl` arm .5/lock .8; Liquidity: Group C's lock=.85 +
`--structure-stop-mode pivot_s1r1`, run as a separate invocation since
that flag would otherwise silently apply to every other strategy in the
same run).

| config | n | win% | E/lot | PF | P(mean≤0) | bar (≤0.15)? |
|---|---|---|---|---|---|---|
| VWAP base (no gates) | 16 | 25.0% | −352 | 0.26 | 0.986 | fail |
| VWAP_Conviction N=1 | 15 | 53.3% | +84 | 1.50 | 0.248 | fail |
| VWAP_Conviction N=5 | 10 | 20.0% | −85 | 0.16 | 0.994 | fail |
| EMA base (⚠️ reaper-corrupted, discard) | 43 | — | — | — | — | — |
| EMA_Micro_Conviction N=1 | 12 | 58.3% | **+304** | 3.05 | **0.039** | **pass** |
| EMA_Micro_Conviction N=5 | 1 | — | +662 | — | n/a | n=1, meaningless |
| EMA_Micro_Conviction_PCR N=1 | 7 | 71.4% | **+388** | 4.38 | **0.021** | **pass** |
| EMA_Micro_Conviction_PCR N=5 | 1 | — | +662 | — | n/a | n=1, meaningless |
| ORB base (no gates) | 44 | 40.9% | −177 | 0.60 | 0.869 | fail |
| OI base (no gates) | 45 | 40.0% | −147 | 0.55 | 0.941 | fail |
| ORB_Conviction N=1 | 40 | 50.0% | +122 | 1.49 | 0.186 | fail (marginal) |
| ORB_Conviction N=5 | 35 | 57.1% | +226 | 2.08 | **0.062** | **pass** |
| OI_Volume_Conviction N=1 | 14 | 71.4% | **+274** | 5.07 | **0.004** | **pass, strong** |
| OI_Volume_Conviction N=5 | 4 | 75.0% | +267 | 19.32 | 0.004 | pass, but n=4 — likely a statistical mirage (same trap this ledger already flags elsewhere) |
| Liquidity_Sweep_Conviction N=1 | 22 | 54.5% | +96 | 1.54 | 0.245 | fail |
| Liquidity_Sweep_Conviction N=5 | 0 | — | — | — | n/a | too strict, zero trades |

**The one real correction mid-session**: momentum was first read as hurting
ORB, based on N=1 alone (E=122, fails the bar). With N=5 in, **ORB is the
opposite of every other strategy tested** — momentum strictness *helps*
ORB (E climbs to 226, P(mean≤0) drops to 0.062, clears the bar) while it
hurts or is a wash everywhere else. This is *why* Batch 3 below runs a
wide N grid on ORB specifically, not just N=1/N=5.

**Clean summary**: `EMA_Micro_Conviction`, `EMA_Micro_Conviction_PCR`,
`ORB_Conviction` (N=5 specifically, not N=1), and `OI_Volume_Conviction`
(N=1; N=5's pass is thin, n=4) clear the bar. VWAP and Liquidity don't
clear it at any N tested so far.

### Batch 2 (`s6_mom2`) — IN PROGRESS, 15 configs, off/N1/N2 matrix

VWAP and EMA, each at 3 entry-gate variants (full ATR+PCR / PCR-removed /
base-no-gates) × momentum off/N1/N2 — isolating whether the PCR gate
specifically interacts with momentum, and extending the off/N1/N2
calibration to base entry logic too (not just each strategy's tuned
conviction baseline), per explicit user request. All 9 VWAP-family configs
done, zero shard failures, tight consistent timing (392–405s each,
matching Batch 1's VWAP-family rate). Currently on the 6 EMA-family
configs (`m2_ema_full_off` done: 12 trades, matches `EMA_Micro_Conviction`'s
own Batch-1 N=1 baseline sample size closely; `m2_ema_full_n1` running).
Not yet analyzed — do that before touching these configs further.

### Batch 3 (`s6_mom3`) — QUEUED, auto-chained, not yet started

**Scope note**: user asked for "ORB conviction" / "OI_volume conviction",
meaning the full-gate conviction baselines — what actually got built is
**base entry only** (`orb_conviction`/`oi_volume_confirmed_conviction`
with *only* `require_momentum_alignment` set, no other gate) at a wide N
grid, per a mid-conversation mix-up caught and left as-is by explicit user
call ("no issues we will do it now, and conviction ones tomorrow" — see
"Not yet done" below).

11 configs: `orb_conviction` base entry, N∈{1,2,4,5,6,8,10,15} (coarse
grid, not exhaustive — motivated directly by the ORB N=1-vs-N=5 reversal
above); `oi_volume_confirmed_conviction` base entry, N∈{1,2,3}. The
momentum-off ("N=0") reference for each already exists from Batch 1
(`m1_base_orb` E=−177/44 trades, `m1_base_oi` E=−147/45 trades) — not
rerun. QC'd twice (local + box venv) including an explicit check that no
*other* gate landed on for any of the 11 lines.

**Launch mechanism**: a detached wait-loop unit
(`backtest-20260901-215329.service`) polls Batch 2's unit every 5 min and
only invokes `run_sweep.sh` once it reports inactive — chosen over a
Claude-side scheduler specifically because it survives the session/laptop
ending entirely, running solely on the A1 box. Confirmed genuinely
waiting (near-zero CPU) before the session ended.

### Not yet done (resume here)

1. **Analyze Batch 2** (VWAP off/N1/N2 × full/nopcr/base already
   complete; EMA still finishing) once it's done.
2. **Analyze Batch 3** (ORB/OI base-entry wide-N sweep) once the
   auto-chain fires and completes.
3. **The actual conviction-gate versions of Batch 3** — `ORB_Conviction`
   (full: `require_prior_day_trend` + `max_or_range_nifty_points=65`) and
   `OI_Volume_Conviction` (full: `require_atr_expansion` +
   `pcr_oi_min`/`max`) at the same N grids, momentum layered *on top of*
   each strategy's already-tuned entry gates rather than replacing them —
   this is what "conviction ones tomorrow" refers to.
4. **Real RSI14-oscillator gate** (`require_rsi_alignment`, band-vs-50 —
   distinct from the momentum/"micro-trend" gate above) still fully
   deferred, not started this session: base VWAP + RSI, full VWAP
   conviction + RSI (already known net-harmful from Sweep #4 Phase 7,
   would be a regression check not new information), base EMA + RSI, full
   EMA conviction + RSI (both EMA combos genuinely untested).
5. **`m1_base_ema` clean rerun** — superseded by Batch 2's
   `m2_ema_base_off`, which serves the same purpose; confirm once Batch 2
   is analyzed that this is covered and the corrupted Batch-1 number can
   be fully discarded.
6. Re-enable `backtest-reaper.timer` once Batch 3 (the last queued item)
   fully completes — currently off, correctly, must not be forgotten.

Also this session: 3 stale local+remote git branches deleted
(`feat/multi-leg-exit-engine`, `fix/shoonya-option-chain-expiry-anchor`,
`fix/structure-break-and-orb-window-fixes` — all confirmed strict subsets
of `main` before deletion, zero commits lost); `backtest_engine/README.md`
gained 3 new restrictive rules (reaper-vs-active-sweep, credential
exclusion on re-pin, config-construction QC before launch, checksum
verification after sync) and a real-data Efficiency rule 4 (was an
untested placeholder, now: VWAP-family ~6.3–6.7 min/config,
EMA/ORB/OI/Liquidity-family ~22–25 min/config, at `SHARD_COUNT=4` on A1).

---

## 2026-09-01 (~01:30–02:45 IST) — FULL-ARCHIVE RE-ANALYSIS (626 configs, fresh from the CSVs) + paper configs updated on OCI

Not a new sweep. A from-scratch re-read of **every post-DTE-fix run** — 626
configs / 38 run directories — scored on one bar, then acted on. Artifact:
[Expiry-Week Config Triage](https://claude.ai/code/artifact/1f1a7c2a-0b39-476a-bb0d-2f2e443a0747).
Tooling: `scripts/qc_paper_configs.py` (pre-apply) and
`scripts/qc_paper_configs_live.py` (post-apply, validates what is actually in
the DB). Full plan + reasons + applied record:
`docs/ops/paper_config_update_2026_09_01.md`.

**Scope.** Only `--near-expiry-days 6` runs count: `sweep3*` (from
`20260828T152220Z`), all of Sweep #4 (`s4p1`–`s4p5`, `s4p15`, `s4p16`), all of
Phase 6/7/8 (`s6_g1/g2/g3/g7ab/g7c/g7d/g8e`). Excluded: every pre-fix run
(`conviction_sweep/`, `refined_sweep_20260828T072024Z`, `orb_NIFTY_targeted2x`,
`2026-08-26_current_validation`, `gamma_blast`), three `*_PARTIAL` dirs, and six
non-timestamped twins each verified byte-identical before dropping.

**Pipeline is trustworthy.** Rebuilt from raw trade CSVs, not from this ledger,
and it reproduces three independent prior entries exactly — W7b arm .12/lock .9,
Phase 5 `o3_atr_pcrl` arm .30/lock .85, and Phase 5 Group B `e3_pdt_atr_pcrl`
arm .7/lock .8 (n=7, 71.4% win, IS +397.9 / OOS +380.8). Only delta is the
flat-cost constant (₹10 as in `analyze_walkforward.py`; this ledger's prose says
₹40 — worth ~₹30/trade, **the two are not interchangeable, state which one a
number came from**).

### The bar: 7 standing gates + 1 new one

n≥8 · E>0 · IS>0 **and** OOS>0 · both 6-mo halves>0 · P(mean≤0)≤0.15 ·
5th-pctile≥0 · survives 1.0%/side slip · **E>0 after deleting the 2 best trades**.
The last is new and is the cheapest tail-dependence check available at n≤26 — it
is what separates ORB/OI/EMA from VWAP/Liquidity, which both go negative.

### Result

| strategy | pass 8/8 | configs | distinct **entry sets** | verdict |
|---|---|---|---|---|
| ORB_Conviction | 80 | 123 | 27 | deploy to paper |
| OI_Volume_Confirmed | 33 | 190 | 24 | deploy to paper |
| EMA_Micro_Pullback | 3 | 91 | 23 | paper, minimum size |
| VWAP_Pullback | **0** | 113 | 26 | PARK |
| Liquidity_Sweep | **0** | 92 | 21 | PARK |
| Loren | 0 | 17 | 13 | PARK |

**Read pass-counts as "how many exit variants survive", never as independent
evidence.** 626 configs collapse to **145 distinct entry sets** — most of the
search space was exit tuning on a handful of gates. ORB's 80 passers are
**79 exit overlays of ONE 26-trade entry set** (`d_pdt_w65`). That cuts the right
way, though: *all 79 are net positive* (+₹133 to +₹717), the strongest available
evidence that ORB's edge lives in the entry, not the exit. Hash the
(symbol, entry_ts) tuple set to detect this — it is invisible otherwise.

### Per-strategy leads (₹/lot, net, 0.5%/side)

- **ORB `d_pdt_w65`** (`orb_conviction` + `require_prior_day_trend` +
  `max_or_range_nifty_points:65`), n=26: bare exits +304; best exits
  `w7_s18_a12_l06` +520 (76.9% win, PF 16.70, maxDD ₹395, drop-2 +391, P=0.000),
  `w7_s18_tgt40` +717 (61.5% win, maxDD ₹1,222), `x_stop15_arm06` +274
  (84.6% win, maxDD ₹334, L-streak 1).
  Marginal contribution is clean: bare ORB −13 (P=0.54); PDT alone +132
  (IS +11 / OOS +372); width≤65 alone +124 (IS +162 / OOS +46); **together +304
  with IS +297 / OOS +318** — the two filters' opposite regime weaknesses cancel.
  9–10 of 10 traded months positive; top-3 trades only 42–56% of total.
- **OI `o_pcrl` / `o3_atr_pcrl`**: `x4_o_pcrl_a30_l80` n=23, 65.2%, +221,
  drop-2 +160, PF 3.03, 8/8 — the largest passing sample outside ORB;
  `x5_o3_atr_pcrl_a30_l85` n=14, 71.4%, +274, 8/8. IS +322 / OOS +89 on the n=23
  one — **edge is decaying, not growing; watch OOS in paper.**
- **EMA `e_pdt_atr`**: `x4_e_pdt_atr_a70_l80` n=12, 58.3%, +304, drop-2 +155,
  8/8. Top-3 trades are **78%** of total — real but fragile.
- **VWAP best** `g1_s06_t50` +250 but **drop-2 = −91**; **Liquidity best**
  `g7c_l_pcrt_lock90` +98 but **drop-2 = −21**, IS −58. Both are 3 trades deep.

### Findings worth keeping

1. **EMA_Micro_Conviction_PCR is a strict subset of EMA_Micro_Conviction.**
   Trade-by-trade: shared 7, only-in-plain 5, **only-in-PCR 0**. The PCR gate
   removed 5 trades netting **+₹934** (cut 3 `structure_break` losers, but also
   both biggest winners: +603 and +1,308). Total 3,651 → 2,717. E/trade *rises*
   304 → 388 only because it deleted more than it should have. **Two configs on
   the same gate, one a subset of the other, is double size on one signal — not
   diversification.** Always diff the entry sets before treating two variants as
   independent strategies.
2. **The 10:xx ORB cutoff claim was measured on the wrong entry set.** The
   "10:xx = −437" note came from `w_25_65`; on the deployed `d_pdt_w65` gate the
   same bucket is **+1,839**. Sign flips across gates AND across exit stacks
   (`d_pdt_w65` with bare exits: −443). All 26 entries fall **09:32–10:16**, 88%
   before 10:00. Moved 10:00 → **10:15** (also `ORBStrategy`'s own constructor
   default) — justified distributionally, **not** by the +₹38/lot, which is one
   trade.
3. **`trail_activation_fraction` is ~3× the lever `trail_lock_fraction` is, and
   OI and EMA run OPPOSITE directions on it.** OI peaks at 0.30 (8/8; 0.40 → 7/8,
   0.60 → 3/8); EMA peaks at 0.70 (8/8; 0.60 → 7/8, 0.30 → 5/8). Both were
   deployed at 0.5 — the wrong side of both peaks, ~₹85 (OI) and ~₹100 (EMA) per
   lot per trade.
4. **The lock ladder is monotonic with no turning point, and win% + maxDD are
   byte-identical at every lock level** on all three strategies — only winner
   size moves. That flatness is an artifact of 1-min bars: the sim cannot see the
   sub-minute adverse wick a low lock exists to survive. **Lock cannot be settled
   by this harness at all** — it needs live fills. Hence the paper A/B below.

### Configs updated on OCI the same night (paper only; DB-only, no deploy)

Applied in two transactions with `running_runs = 0`, `open_positions = 0`,
`pending_orders = 0` verified immediately before each. Rollback snapshot:
`docs/ops/paper_config_rollback_2026_09_01.txt`.

- **ORB_Conviction** — cutoff 10:00 → 10:15; gained a **4-leg** staged exit
  `[3,3,2,2]` at 10 lots: `core` lock .6 (anchor) / `runner` lock .8 /
  `tightlock` lock .4 / `target` target .40 lock .6.
- **OI_Volume_Conviction** — arm .5 → **.30**, leg locks .6/.8/.8, plus top-level
  exit params (see below).
- **EMA_Micro_Conviction** — arm .5 → **.70**, leg locks .6/.8/.8, plus top-level
  exit params.
- **EMA_Micro_Conviction_PCR** and **VWAP_Conviction** disabled.
  Liquidity_Sweep_Conviction was already disabled.

**Leg-design rule adopted:** every leg differs from a designated anchor leg in
**exactly one** field, or the comparison is unattributable. This is why a lock
value repeats across legs — ORB's `target` leg shares lock .6 with `core` so the
target test is single-variable. The first draft got OI wrong (wide leg at lock
.85 against legs at .6/.8, so *neither* comparison was clean); fixed to .80.

### Two latent bugs found while doing this — both pre-existing, both now closed

1. **`qty_lots: 10` would have blocked every live trade.** In
   `resolve_qty_lots`, an explicit `params["qty_lots"]` wins in **both** modes —
   it is not mode-aware; only the *absent* case is (paper 10 / live 1). Risk then
   computes `effective_lot_cap = min(per_trade_lot_cap, resolved)` = `min(1, 10)`
   = 1 and **rejects** an intent of 10 (`per_trade_lot_cap_exceeded`) — it does
   not clamp. Confirmed by
   `test_per_trade_lot_cap_allows_a_strategys_own_configured_qty_lots`, which has
   to raise the workspace cap to 5 before a 5-lot config passes. The 2026-08-28
   fix keyed the *default* off `is_strategy_routed_live`; it did not change
   explicit behaviour. **Removed `qty_lots` from OI and EMA; never added it to
   ORB.** Paper unchanged at 10; live now works at 1.
2. **OI and EMA had no top-level exit params, so their LIVE exit used class
   defaults.** `build_position_exit_legs` returns `None` — collapsing to the
   single-exit path — for **any LIVE position** and for a position too small to
   give every leg ≥1 lot; that path reads the **top-level** values. OI/EMA set
   none, so a live trade would have exited on arm .5 / lock **.5** (and EMA stop
   .08 instead of .12). Added top-level `stop_pct`/`target_pct`/arm/lock to both,
   pinned to each strategy's best backtested single-leg config. Targets pinned to
   the class-default values the backtests themselves ran under (OI .18, EMA .12)
   so a future default change cannot silently alter a validated config.
   **Consequence worth remembering: multi-leg exits are already paper-only in
   code, so `exit_legs` can never affect live — the top-level params ARE the live
   config.**

### Needs attention next time

- 🔴 **ORB's `max_or_range_nifty_points: 65` has still never been re-swept with
  `require_prior_day_trend` on.** `d_pdt_w55` / `w75` / `w85` do not exist
  anywhere in the archive. 65 came from the 21-band width-ridge sweep where it
  was explicitly judged "a lone spike surrounded by negatives = noise". The whole
  ORB result rests on 65 still being right once PDT is on. **~20 min, 4 configs,
  the highest value per minute of anything outstanding.** Gates any decision to
  size up; does not block paper.
- **Lock 0.4 is unbacktested for OI and EMA** (their grids were .6/.7/.8 only).
  It *is* backtested for ORB (8/8 gates, −₹18 vs .6), which is why only ORB got
  the 4th leg. If the live lock A/B shows tighter is better, run the 0.4 cell for
  OI/EMA before configuring it.
- **The multi-leg exit engine itself has zero backtest coverage.** Every per-leg
  number is from a separately-backtested *single-leg* run; a real staged position
  shares one entry and its structure-break exit fires on all legs at once, so the
  blended arithmetic will not reproduce. Measuring that is the point of the run.
- **OI's edge is decaying** (IS +322 / OOS +89 on the n=23 config). If paper
  confirms the OOS half, re-derive rather than re-tune.
- **Liquidity's untouched pathology**: `structure_break` is 8/22 trades, 12.5%
  win, 1-min median hold, ~−900 total, and is inert to every entry and exit lever
  swept across Groups C/D/E. Any revival attacks that exit first; nothing else.
- Four stub configs (`Bank nifty`, `Test `, `Test 1`, `Test 4`) remain
  `is_enabled` and will auto-spawn alongside the conviction set, competing for
  `max_concurrent_positions = 2`. **`Test 1` has `runtime_mode = NULL`** — it
  follows the session rather than being pinned to paper, so it would route real
  money if the master switch is ever flipped live. Left as-is on explicit
  instruction; clear before any live session.
- `ruff check .` currently reports 16 errors, all in pre-existing untracked
  analysis scripts (`analyze_phase*.py`, `fetch_today_replay_data.py`,
  `alice_blue_ws_quality_diagnostic.py`). CI runs `mypy app tests` only, so
  `scripts/` is out of mypy scope — consistent with every existing script there.

---

## 2026-09-01 (~01:05 IST) — e4 backtest VM recovered into a single portable `backtest_engine/` folder, ready to move to A1

The paid e4 VM (`129.159.226.106`) hits its trial-credit cutoff ~05:30 IST today.
Everything on it was recovered and consolidated locally **before** that, into one
self-contained, movable folder: **`backtest_engine/`** at the repo root
(gitignored, 437 MB). See its own `README.md` and `VERSION.txt`.

**What was actually at risk (only on the VM, not local):** 51 report directories —
i.e. *every sweep result from Phase 3 through Phase 8*, including all of `s6_g1`/
`g2`/`g3`/`g7ab`/`g7c`/`g7d`/`g8e` and the `s4p1`-`s4p5`/`sweep3w*` families. Local
had 39 report dirs the VM lacked, so neither side was a superset — the union was
taken. **Verified: 0 files present on the VM and missing locally** (18,367 report
files now, 749 dirs).

**Engine parity checked, not assumed** (content-diffed ignoring CRLF):
`run_backtest.py`, `merge_backtest_shards.py`, `backtest_pivots.py` were
**byte-identical** VM vs local, as were all strategy modules — the engine was never
at risk. Two analysis scripts differed, and **the VM's copy was the newer one for
one of them**: `analyze_conviction_sweep.py` had `FLAT_COST_PER_LOT = 10.0`
(corrected 2026-08-31) while local still had a stale **40.0**. Local has been
corrected. *Note: this ledger's own header still says "flat ₹40/lot" — that line is
stale; the real cost model is ₹10/lot flat (Rs5/order × 2 legs).* Also recovered:
12 VM-only scripts/config lists (incl. `run_phase6_generic.sh`, which carries the
DB reaper) and 33 sweep runners.

**Disk hygiene — the thing that saved the e4, now generalised.** Root cause of the
VM filling up: `run_backtest.py` creates **one Postgres DB per shard** and never
drops it; a 28-shard × 5-config sweep leaves 140 behind. The e4 was found holding
**202 orphan databases / 4.0 GB**. The sweep runners reaped between configs, but
anything dying mid-run (SSH drop, OOM, Ctrl-C) leaked permanently. Now three
layers in `backtest_engine/setup/`: reap-between-configs + `trap` on exit
(`run_sweep.sh`), `disk_guard.sh` (refuses to start below a free-GB floor, reaps,
aborts if still short), and an hourly `backtest-reaper.timer` systemd safety net.

**A1-specific safety, because A1 is the LIVE trading box** (unlike the e4, which
ran nothing else): the bundle uses its own **`DB_NAME=btengine`**, so no backtest
database can ever share a namespace with production `trading_bot`. The reaper
matches with POSIX regex `^<DB_NAME>_backtest_` — **not SQL `LIKE`**, where `_` is
a single-char wildcard — and hard-refuses `postgres`/`template0`/`template1`/
`<DB_NAME>`/`<DB_NAME>_test`. `provision_a1.sh` reuses the already-installed
Postgres *server* but creates only its own role/DB, builds its own venv, and never
touches anything named `trading-bot*`.

**A1 is a much smaller box**: 2 OCPU / 12 GB vs the e4's 16 OCPU / 128 GB, and its
100 GB disk also carries the live service. `run_sweep.sh` therefore defaults to
**`SHARD_COUNT=4`, not 28** — a 5-config group should be budgeted at roughly
**2-3 h**, not the e4's ~18 min. The 33 historical runners in `runners/` are kept
**unmodified as provenance** and must not be run on A1 (their paths and
`SHARD_COUNT=28` assume the old VM).

**Verified end-to-end from inside the bundle**, not just assembled:
`run_backtest.py --help` exits 0 with all 11 strategy types, `DEFAULT_DATA_DIR`
resolves inside the bundle, and `analyze_walkforward.py` reproduced the Phase 8
`g7d_l_pcrt_arm70` and `g8e_width25` numbers exactly.

**Two cleanups**: real broker credentials (`shoonya.env`, `alice_blue.env`,
`telegram.env`, `angel_one.env`, `truedata.env`, plus a live Alice Blue session
cache) were pulled in by the `app/` copy and have been **removed from the bundle**
— the backtest replays a CSV archive and needs none (repo/live copies untouched).
And `_paidvm_data_snapshot_2026-08-27/` (369 MB) was deleted after verifying it was
a **strict subset** — all 1,957 of its files already present in the archive.
`backend/data` is now a Windows junction into the bundle, so there is exactly **one
physical copy** of the 424 MB archive and both paths still work.

---

## 2026-09-01 (~00:50 IST) — Phase 8 COMPLETE (Groups E + D): entry-gate loosening REFUTED; `arm` found to be the strongest exit lever yet — but nothing clears the bar. Liquidity_Sweep_Conviction stays PARKED.

Chain finished unattended on the VM exactly as queued (`[master8] Phase 8 chain
ALL COMPLETE`, 13:36:34Z / 19:06 IST 2026-08-31). All 10 configs `OK`, **zero
shard failures**, full 20-25 trade samples each (the db-name-truncation bug that
invalidated the original Group C stays fixed). CSVs pulled to
`data/historical/backtest_reports/s6_g8e_liq_entry/` and `s6_g7d_liquidity_arm/`;
analyzed with `analyze_walkforward.py` (default cost model, OOS ≥ 2026-04-01).

### Cross-run reproducibility check — PASSED
Group D's `arm50` cell (`arm=0.5, lock=0.80`) is the *same configuration* as
Group C's `lock80`, run in a separate process on a separate DB: n=22, win 54.5%,
E=92.7, PF 1.52, P(mean≤0)=0.250 — **identical to 3 significant figures**. The
harness is deterministic across runs; Group C/D/E results are directly comparable.

### Group E — entry-gate loosening: REFUTED, and it doesn't even add trades

Motivated by 2026-08-31's live gate-block logs (real observed sweep distances of
4.75/0.65/2.95 vs the 5.0 floor, widths 23.5-28.75 vs the [30,120] band). Held at
`lock=0.85`, `arm=0.5`. Baseline for comparison = Group C `lock85` (n=22, E=95.5,
PF 1.54, P=0.245).

| config | loosened | n | win% | E/trade | PF | IS E | OOS E | P(mean≤0) |
|---|---|---|---|---|---|---|---|---|
| *(g7c lock85 baseline)* | — | 22 | 54.5% | **95.5** | 1.54 | -58.7 | 318.2 | **0.245** |
| g8e_width25 | width 30→25 | 25 | 52.0% | 65.1 | 1.39 | -65.6 | 261.1 | 0.303 |
| g8e_width20 | width 30→20 | 24 | 45.8% | 6.9 | 1.04 | -92.0 | 171.7 | 0.468 |
| g8e_dist40 | dist 5.0→4.0 | 20 | 45.0% | -79.8 | 0.73 | -304.2 | 194.4 | 0.688 |
| g8e_both | dist 4.0 + width 25 | 22 | 45.5% | -77.3 | 0.73 | -264.8 | 250.7 | 0.693 |
| g8e_dist35 | dist 5.0→3.5 | 20 | 35.0% | -210.7 | 0.50 | -461.9 | 96.3 | 0.881 |

**Every single Group E config is worse than the untouched baseline on E/trade, PF,
and P(mean≤0).** Two findings worth keeping:

1. **The distance floor is the wrong thing to blame.** Loosening it is monotonically
   destructive (5.0 → 4.0 → 3.5 = E 95.5 → -79.8 → -210.7). A shallower sweep really
   is a worse setup — the live near-misses were the gate working, not the gate
   being miscalibrated.
2. **Loosening `min_sweep_distance` *reduced* trade count (22 → 20).** Counter-
   intuitive and worth remembering: a looser floor lets an earlier, weaker sweep
   qualify first and consume the setup, displacing a later, better one that would
   otherwise have fired. Same next-bar-re-qualification displacement already seen
   in sweep #2's `g_skip_tue`. **Trade count is not a monotonic function of gate
   looseness — never assume "looser gate = more trades" in this harness.**
3. The width band is the milder of the two (25 costs ~30 E/trade, 20 costs ~89) but
   still net-harmful. Widening the *sample* by 3 trades bought nothing.

**Verdict: do not loosen either entry floor. One day of live near-misses was
correctly read as "no real setup formed", not "the floor is too tight."**

### Group D — the arm sweep (deprioritized, and it turned out to be the strongest lever tested)

Held at `lock=0.80`, `stop_pct=0.16`, `pivot_s1r1`. Swept `trail_activation_fraction`
0.3 → 0.7:

| arm | n | win% | E/trade | PF | IS E | OOS E | P(mean≤0) | 5th-pctile |
|---|---|---|---|---|---|---|---|---|
| 0.30 | 22 | 54.5% | -3.0 | 0.98 | -119.4 | 165.1 | 0.504 | -204.8 |
| 0.40 | 22 | 54.5% | 46.0 | 1.26 | -77.2 | 224.0 | 0.354 | -171.7 |
| 0.50 | 22 | 54.5% | 92.7 | 1.52 | -59.8 | 313.0 | 0.250 | -142.5 |
| 0.60 | 22 | 54.5% | 132.0 | 1.75 | -19.4 | 350.7 | 0.189 | -116.8 |
| 0.70 | 22 | 50.0% | **164.1** | **1.89** | **-9.8** | 415.4 | **0.158** | -105.7 |

**`arm` is roughly 3× the lever `lock` is.** Group C's full lock sweep (0.60→0.90)
moved E/trade only 81.6 → 98.3 (+17); this arm sweep moves it -3.0 → 164.1 (+167)
over a comparable range. **Group C's conclusion that "exit-side tuning is
exhausted" was premature — it swept the weaker of the two exit axes.** The
deprioritized run turned out to be the more informative one.

**Mechanism is clean and fully isolated** (exit-reason breakdown): between arm 0.3
and 0.5 the `stop`, `structure_break`, and `target` buckets are *byte-identical*
(same n, same totals). **Only the `trail` bucket changes** — median hold 5.5 →
12.5 → 20 min, E/trail-trade 444 → 708 → 1060. Arming the trail later does exactly
one thing: it stops the trail from strangling winners in their first few minutes.
This is the same "trades that work, work fast, but a trail armed too early cuts
them before they get there" shape sweep #1 finding #4 first hinted at.

**The trend is already turning, though.** At arm=0.70 one trade converts from
`trail` to `stop` (trail 8→7, stop 3→4, stop total -2943.8 → -3134.1). Net E still
improved because the surviving winners gained more, but this is the first evidence
of the cost side of "arm later". **Do not extrapolate past 0.7 without testing it.**

### Verdict against the standing bar — still NO

- **No config clears `P(mean≤0) ≤ 0.15`.** `arm70` at **0.158** is the closest any
  Liquidity_Sweep_Conviction config has ever come, but it misses.
- **Every config in both groups is still IS-negative.** `arm70`'s IS E=-9.8 is
  essentially breakeven — the best IS number this strategy has produced — but the
  "IS-negative / OOS-flattered" shape this ledger distrusts elsewhere (VWAP
  wide-target, OI at high arm) is fully intact: all the profit lives in OOS.
- Expiry-week sign test is non-significant for every config (p = 0.83-1.00).
- n=22 throughout. Two IS/OOS halves of 13 and 9 trades.

**Liquidity_Sweep_Conviction stays PARKED / not deployed** — same disposition as
VWAP. It is now the *best-understood* parked strategy, not a shippable one.

### The pathology nothing has touched yet
Across every Group C/D/E config, `structure_break` is **8/22 trades (36%), 12.5%
win rate, median hold 1 minute**, contributing ~-840 to -1000 total. It is
completely inert to `arm`, `lock`, and every entry gate tested. This is the same
misfire documented in sweep #1 finding #8 (1-min bars collapse
`structure_break_persistence_seconds` to "confirm on the 2nd breaching bar"), and
it is the single largest untouched drag on this strategy. **Every lever swept so
far tunes the 8 trail trades; none of them touches the 8 structure_break trades.**

### What would actually be next (NOT run — VM terminates ~05:30 IST 2026-09-01)
Two cheap, mechanism-motivated cells remain, ~18 min for a 5-config group:
1. `arm` 0.75 / 0.80 / 0.90 at `lock=0.85` — find the turning point of a trend that
   is monotonic and has not plateaued. Cannot be extrapolated; must be measured.
2. `--structure-stop-mode pivot_s2r2` (a *wider* structure level, already
   implemented, never tested on this strategy — Groups C/D/E all used `pivot_s1r1`)
   — the only no-code-change lever that attacks the 36%/12.5%-win/1-min pathology.
   Fully disabling the structure-break exit would need a code change (`--structure-
   stop-mode` has no `off` choice), so it is not a same-night test.

If the VM is gone, both are re-runnable anywhere the archive is — neither needs
16 OCPU (each config is ~215s on 28 shards).

---

## 2026-08-31 (~18:34 IST) — Group E→D queued unattended on the VM itself, survives laptop/session shutdown

Per explicit instruction to make the remaining backlog run without needing
this session (or the local machine) to stay alive: wrote `~/run_phase8_
master.sh` directly on the VM (129.159.226.106) — waits for the
already-running Group E process to exit, then launches Group D
(`phase8_groupd_liquidity_arm.txt`, the deprioritized arm sweep, held for
exactly this "queue everything planned" case). Launched via `setsid nohup
... < /dev/null > ~/s8_master_stdout.log 2>&1 &` — confirmed detached
(`PPID=1`, own session id, no controlling tty), so it is immune to SSH
disconnect, this Claude session ending, or the laptop sleeping/shutting
down. **If this session doesn't survive to see it finish**: resume by SSHing
in and checking `~/s8_master.log` for `[master8] Phase 8 chain ALL
COMPLETE`, then pull + analyze both groups' CSVs the same way as every
other phase in this file (`s6_g8e_liq_entry/` and `s6_g7d_liquidity_arm/`
under `data/historical/backtest_reports/`). No further backlog beyond
these two exists as of this note — once both finish, the Liquidity_Sweep_
Conviction question is either answered (an entry-gate variant helps) or
closed (re-park, matching VWAP).

## 2026-08-31 (~18:31 IST) — Group C RERUN (fixed, valid): no lock clears the bar, still IS-negative/OOS-flattered; today's live paper-trade forensics motivates a real entry-gate test (Group E, launched)

### Group C rerun — fix confirmed, results now trustworthy but still don't clear the bar

All 5 configs completed with a full **22-trade sample each** (vs. 0-3 before
the db-name fix), zero shard failures. `analyze_walkforward.py`:

| lock | n | win% | E/trade | PF | IS E | OOS E | P(mean≤0) |
|---|---|---|---|---|---|---|---|
| 0.60 | 22 | 54.5% | 81.6 | 1.46 | -64.3 | 292.3 | 0.273 |
| 0.70 | 22 | 54.5% | 87.1 | 1.49 | -62.1 | 302.7 | 0.261 |
| 0.80 | 22 | 54.5% | 92.7 | 1.52 | -59.8 | 313.0 | 0.250 |
| 0.85 | 22 | 54.5% | 95.5 | 1.54 | -58.7 | 318.2 | 0.245 |
| 0.90 | 22 | 54.5% | 98.3 | 1.56 | -57.6 | 323.4 | 0.241 |

**Sanity check passed**: lock=0.80's P(mean≤0)=0.250 matches Phase 6's
original (buggy-VM-era but apparently still-valid-by-luck) finding almost
exactly — the fix didn't overturn prior conclusions, it just made Group C's
*new* 0.6/0.7/0.85/0.9 cells trustworthy for the first time. **Monotonic but
flattening improvement with lock** (0.6→0.7: +5.5 E; 0.85→0.90: +2.8 E) —
diminishing returns, a plateau, not a lever with more headroom. **None of
the 5 clears the P(mean≤0)≤0.15 bar**, and every one shows the same
"IS-negative / OOS-flattered" shape this ledger already distrusts elsewhere
(VWAP wide-target, OI at high arm) — IS is negative at *every* lock tested,
all the profit lives in OOS. **Verdict: exit-side tuning (stop-mode + lock)
for Liquidity_Sweep_Conviction is exhausted, not under-tuned** — matches the
"no exit tweak fixes a weak entry" pattern already proven 3× for VWAP. Do
not chase this further on the exit side (the drafted Group D arm-sweep,
`phase8_groupd_liquidity_arm.txt`, is deprioritized for this reason — held,
not deleted, in case the entry-side test below changes the picture).

### Today's live paper-trade forensics (2026-08-31, excl. confirmed glitch windows) — real evidence, not assumption

Two parallel background agents ran against the live OCI box
(144.24.137.112), read-only. Full detail in
[[project_sweep4_conviction_exit_tuning_2026_08_30]] memory; key points:

**Glitch window excluded**: 08:26-09:46 IST (option-chain "Feed: Dead"
incident, confirmed via a real 3965s `option_chain_snapshots` refresh gap —
4 trade_intents excluded). The 10:08-14:52 IST Shoonya→Alice Blue failover
window was **checked and found clean** (zero `price_bars` gaps, option
chain kept refreshing throughout) — not excluded, unlike the feed-dead
window.

**Per-strategy, n=37 clean trades, NIFTY down-drifting low-VIX chop day
(24175.65→24066.50, VIX~11.2)**: `ema_micro_pullback` best (+16,718, 5/6
win), `oi_volume_confirmed` flat (+2,399), `vwap_pullback` base −4,173
(chasing pattern confirmed again — 2 same-strike re-entry-at-higher-premium
clusters, both losers), `VWAP_Conviction` −13,510 (0/3, but its conviction
gate correctly did NOT show the chase pattern that hit its non-conviction
sibling), `OI_Volume_Conviction` −9,864 (0/2, but only traded post-restart —
see cap bug below).

**Real, already-known bug concretely reproduced today**:
`ema_max_trades_per_session`-style per-session caps are **not mode-scoped**
and **not reset except by a process restart** — `EMA_Micro_Conviction`,
`EMA_Micro_Conviction_PCR`, and `OI_Volume_Conviction` each hit their cap
within minutes of every process start today with **zero real trades fired
yet**, then only traded in the ~2min windows after later restarts reset the
counter. **Their "0-2 trade" results today say nothing about their edge** —
treat as unmeasured, not weak, until this is fixed.

**Liquidity_Sweep_Conviction: zero trades all day**, but NOT silent —
real gate-block log lines fired repeatedly: `sweep distance 4.75/0.65/2.95
below min 5.00` and `window width 23.5-28.75 outside [30,120]` (defaults:
`min_sweep_distance_nifty_points=5.0`,
`sweep_min_range_width_nifty_points∈[30,120]` —
`liquidity_sweep_reversal.py`). Real observed values sat **just under**
both floors, repeatedly, on a real low-volatility day. **One day of
near-misses isn't proof the floor is wrong** — could just as easily mean
"correctly no real setup formed today" — but it's a concrete,
live-evidence-backed hypothesis worth testing against the full historical
sample rather than assuming either way.

### Group E — LAUNCHED 13:00 UTC / 18:30 IST: entry-gate loosening, motivated by the finding above

5 configs (`phase8_groupe_liquidity_entry.txt`), held at
`trail_lock_fraction=0.85` (Group C's near-best point, deliberately not the
single-best 0.90 — avoids overfitting the exit choice to n=22 while testing
a different axis) and `arm=0.5` unchanged (isolating one lever, same
discipline as Group A's separate entry/exit tightening for VWAP):
`min_sweep_distance_nifty_points` 4.0/3.5 (2 configs), `sweep_min_range_
width_nifty_points` 25.0/20.0 (2 configs), and one combined loosening.
Running on the VM (129.159.226.106) now, ETA ~18:48 IST (same ~18min shape
as Group C's 5 configs). **This is the actual next-most-informative test**,
not the arm-sweep — an untested entry lever with real live motivation beats
further-diminishing exit-lever tuning.

### Config audit — confirms nothing has been applied to production yet

A separate audit agent confirmed: all 5 conviction strategies are still
running at the pre-this-session `arm=0.5/lock=0.8` on every leg — none of
the OI (`arm:0.3,lock:0.85`) or EMA (`arm:0.7`) retunes from earlier today
have been deployed. `Liquidity_Sweep_Conviction` was already disabled
(`is_enabled=false`) at 15:46 IST today — consistent with holding it for
this sweep work, not an accident. Confirmed via code read: a `params` DB
edit only takes effect at the next `start_strategy` call (construction
time), never on an already-running instance — any future production update
needs a stop/restart of the affected strategy, not just a DB edit.

**Nothing applied to production this session** — per explicit instruction,
backtesting continues before any production decision is made.

---

## 2026-08-31 (~16:50 IST) — Phase 7 ANALYZED: RSI gate hurts (not helps) on VWAP Pullback; Group C results INVALID (new bug: Postgres DB-name truncation collision)

Sweep finished on its own at 10:44 UTC / 16:14 IST (`[master7] Phase 7 ALL
COMPLETE`), 0 shard failures reported for Groups A/B, all 5 Group C configs
flagged `HAD SHARD FAILURES`. Results pulled to
`data/historical/backtest_reports/s6_g7ab_vwap/` and
`s6_g7c_liquidity_pivot/`, analyzed via `analyze_walkforward.py` (numpy/pandas
installed into the local `.venv` to run it locally instead of the VM's
`an_venv` — no other change).

### Group A/B verdict: the RSI 60/40 gate is net-harmful for VWAP Pullback, not net-positive as hypothesized

Full table (n=12-15/config, IS=Nov-Mar, OOS=Apr-Aug 2026, costed):

| config | RSI | entry/exit tightening | n | win% | E/trade | PF | P(mean≤0) |
|---|---|---|---|---|---|---|---|
| g7a_base_rsioff | off | none | 15 | 53.3% | **84.0** | 1.50 | **0.248** |
| g7a_base_rsi60 | 60/40 | none | 14 | 35.7% | -111.7 | 0.63 | 0.773 |
| g7a_base_rsi65 | 65/35 | none | 14 | 50.0% | -4.0 | 0.98 | 0.534 |
| g7a_entrytight_rsioff | off | entry | 15 | 46.7% | 69.0 | 1.42 | 0.287 |
| g7a_entrytight_rsi60 | 60/40 | entry | 13 | 46.2% | -148.0 | 0.48 | 0.840 |
| g7a_entrytight_rsi65 | 65/35 | entry | 12 | 58.3% | -39.4 | 0.78 | 0.624 |
| g7a_exittight_rsioff | off | exit | 15 | 53.3% | 23.0 | 1.10 | 0.436 |
| g7a_exittight_rsi60 | 60/40 | exit | 14 | 28.6% | -165.0 | 0.53 | 0.848 |
| g7a_exittight_rsi65 | 65/35 | exit | 14 | 42.9% | -95.9 | 0.66 | 0.748 |
| g7a_bothtight_rsioff | off | both | 15 | 53.3% | 40.6 | 1.21 | 0.377 |
| g7a_bothtight_rsi60 | 60/40 | both | 13 | 23.1% | -176.3 | 0.46 | 0.869 |
| g7a_bothtight_rsi65 | 65/35 | both | 12 | 41.7% | -69.5 | 0.67 | 0.710 |

Pattern is consistent across all 4 entry/exit variants: **adding the RSI
gate always makes E/trade and P(mean≤0) worse**, and 60/40 is worse than
65/35 in every case. Entry/exit tightening alone (RSI off) also underperforms
the untouched baseline. **The single best Group A/B config remains
`v_atr_pcrl` with everything off** — E=84.0, PF=1.50, P(mean≤0)=0.248,
unchanged from before this session's RSI work. This directly contradicts the
Phase 6 diagnostic's own prediction (CE/PE RSI-vs-win-rate split) — plausible
explanation: that diagnostic was run on the raw win/loss population without
conditioning on VWAP Pullback's own entry gates, and the two don't compose
the way assumed. **Conclusion: do not ship the RSI gate on VWAP Pullback.**

### Group B verdict: RSI gate does NOT fix the wide-target robustness problem — it's now catastrophic, not just weak

| config | target | RSI | n | win% | E/trade | P(mean≤0) |
|---|---|---|---|---|---|---|
| g7b_t50_rsi60 | 0.50 | 60/40 | 14 | 7.1% | -232.7 | 0.916 |
| g7b_t50_rsi65 | 0.50 | 65/35 | 14 | 7.1% | -333.3 | 1.000 |
| g7b_tnone_rsi60 | 1.0 (trail-only) | 60/40 | 14 | 0.0% | -391.5 | 1.000 |
| g7b_tnone_rsi65 | 1.0 (trail-only) | 65/35 | 14 | 7.1% | -333.3 | 1.000 |

Phase 6's original wide-target configs (no RSI) were mediocre — P(mean≤0)
0.18-0.43. Adding RSI made every one of these **worse**, not better — 3 of 4
show `P(mean≤0)=1.000` (mean net loss in all 10k bootstrap resamples). Note
`g7b_t50_rsi65` and `g7b_tnone_rsi65` produced byte-identical trade-level
stats — the wide/no target never actually mattered because trail
(activation 0.5/lock 0.8) always closed the trade first; effectively both
rows tested the same "RSI-gated trail-only exit," not two different target
widths. **Conclusion: the wide-target idea is dead — do not revisit without
a fundamentally different exit design, RSI does not rescue it.**

### Group C: results are INVALID — new bug found, not a strategy finding

All 5 configs logged `HAD SHARD FAILURES` with far too few trades to mean
anything (lock60: **0** trades, lock70: 2, lock80: 3, lock85: 3, lock90: 2 —
vs. the expected ~15-30 for a full 28-shard run). Root cause confirmed via
`/tmp/s6_logs/g7c_liquidity_pivot/*_s*.log` on the VM:
`sqlalchemy.exc.IntegrityError: ... duplicate key ... "trading_bot_backtest_
s6_g7c_liquidity_pivot_g7c_l_pcrt_lock60_s" already exists` — the
per-shard Postgres test-database name (`trading_bot_backtest_<db-suffix>`)
is **64-65 characters**, over Postgres's 63-byte `NAMEDATALEN` identifier
limit, so **every shard's name (`..._s0` through `..._s27`) silently
truncates to the identical string**, and all 28 shards race to
create/use the same one database — collisions, not real failures, and the
1-3 trades that did land per config came from whichever single shard won
the race, not a representative sample. Group A/B were unaffected only
because their shorter names (`s6_g7ab_vwap` + shorter config names) stay
under 63 chars. **This is a latent bug in the sweep driver's `--db-suffix`
construction (long group-dir + long config names), not new to Phase 7 —
any future group with a long dir name + long config name will hit it
silently again** (log says "HAD SHARD FAILURES" but still reports a
misleadingly small nonzero trade count instead of erroring loudly).

**Not fixed yet, not rerun.** Fix should shorten/hash the db-suffix (e.g.
truncate the *group+config* combination or hash it to a fixed short id)
rather than trying to fit full names under 63 chars by convention. Group C's
underlying question (which `trail_lock_fraction` is best at the liquidity
pivot-S1/R1 stop) is **still open** — Phase 6 already found lock=0.8 good
(P(mean≤0) 0.457→0.250, E ₹14.6→₹92.7); Phase 7 was meant to check
0.6/0.7/0.85/0.9 around it and cannot answer that yet. Rerun after the fix,
before the backtest VM (129.159.226.106, paid, terminates night of
2026-08-31 per `project_backtest_vm_e4_16ocpu_128gb_2026_08_26` memory) goes
away, or move the rerun to the durable A1 box if this VM is gone first.

### Net effect on the 4 still-open production decisions (unchanged from the entry below)

None of Phase 7's results change any of the 4 open production decisions —
Group A/B's answer is "don't adopt RSI," a negative result, and Group C is
inconclusive due to the bug above. See the entry below for the decisions
themselves.

---

## 2026-08-31 (~15:56 IST) — SESSION HANDOFF: RSI conviction gate built, 2 real bugs fixed, Phase 7 sweep RUNNING (resume here)

**Session paused here at the user's request** — everything below is the
state to resume from in a fresh session. The VM (129.159.226.106) is left
running deliberately; only this session's own automatic check-in was
stopped.

### What's new since the Phase 6 entry (below): a real RSI indicator, built into production

Motivated by real-data diagnostics (see the Phase 6 entry below for the
methodology) showing CE winners carry materially higher RSI14 than CE
losers, and PE winners materially lower RSI14 than PE losers — consistent
on both option types, unlike every other tested discriminator. Built as
real production code, not backtest-only:

- **`app/modules/market_data/indicators/rsi.py`** — new `RSICalculator`,
  Wilder-smoothed, identical shape to `ATRCalculator`.
- **`app/modules/market_data/indicators/engine.py`** — wired into
  `IndicatorEngine.on_tick`/`on_completed_bar`/`warm_start`, persists as
  `RSI14` via the exact same generic `updated.items()` → `IndicatorSnapshot`
  loop every other indicator already uses (zero `ingestion.py` changes
  needed).
- **`app/modules/strategy_engine/conviction_gates.py`** — new
  `require_rsi_alignment` + `rsi_neutral_band` (default 10.0) gate.
  **Not a flat RSI>50/<50 split** — the real diagnostic data showed losers
  cluster *near* 50 on both sides (CE-loss median 53.0, PE-loss median
  46.4), not on the wrong side of a plain line, so a symmetric dead-zone
  around 50 was built instead: CE requires RSI > 50+band, PE requires RSI <
  50-band. Default band 10 (→60/40) was chosen, not 15 (→65/35), because
  65/35 would exclude the real median *winner* on both sides too — the
  wider band overcorrects. `rsi_neutral_band` is a real, sweepable float
  param, not hardcoded, specifically to let both be tested rather than
  guessed at.
- All 5 `*_conviction` subclasses (`orb`, `vwap_pullback`,
  `ema_micro_pullback`, `oi_volume_confirmed`, `liquidity_sweep_reversal`)
  updated to accept and forward both new params — see the bug below for why
  this step is easy to silently miss in this codebase's pattern.
- Lint/mypy/pytest all clean (75 passed, 0 failed, no regressions) before
  any of this was synced anywhere.

### Two real bugs found and fixed while building this (not pre-existing, both self-inflicted, both caught by the smoke test before any real sweep ran)

1. **Every `*_conviction` subclass explicitly re-lists each gate param in
   its own `__init__`** (not auto-forwarded from `ConvictionGateMixin`,
   despite what that module's docstring implies about `PARAM_KEYS`
   unioning — that union only covers API validation, not construction).
   Adding `require_rsi_alignment` to the shared mixin alone, without
   updating all 5 concrete subclasses, produced a `TypeError` at strategy
   construction — which manifested as **every single candidate silently
   producing 0 trades across 28 shards**, easy to misread as "the gate
   rejects everything" rather than "the gate crashes everything." Caught
   by re-adding a temporary debug print and finding the traceback, not by
   trusting the 0-trades number at face value.
2. **The backtest VM's `app/` tree hadn't been synced since 2026-08-27** —
   4 days of real production changes never made it there (only
   `scripts/run_backtest.py` gets updated per-sweep; `app/` itself is
   apparently never routinely re-synced). This included the entire
   "Market Terminal signal panel" feature (`SignalStatus`/
   `last_signal_status`, added 2026-08-30) that every `*_conviction`
   subclass's own rejection-handling code already assumes exists — every
   conviction-gate rejection for ANY strategy (not just the new RSI gate)
   would have crashed on this same stale VM, it simply happened to never
   get exercised hard enough to surface before now. **Fixed via a full,
   clean `tar`-based re-sync of `backend/app/`** (not file-by-file
   patching) — 20 files differed. Old tree kept briefly as
   `app.bak-20260831-presync` on the VM, since removed after verifying the
   fix. **Anyone syncing new strategy/gate code to this VM in the future
   should re-sync the whole `app/` tree, not just `scripts/run_backtest.py`
   — this gap will silently recur otherwise.**

Both bugs fully verified fixed via a smoke test (14/15 trades pass the
60/40 gate on real data, 0 crashes across 28 shards) before Phase 7 below
was launched.

### Phase 7 — RUNNING, launched 2026-08-31 ~15:47 IST, NOT YET ANALYZED

21 configs, 3 groups, on the VM right now. Master log: `~/s7_master.log`,
per-config status: `~/s6_status.log` (yes, `s6_status.log` — the generic
driver's log filename, reused across phases 6 and 7, doesn't reset per
phase — don't be confused by the name). As of the last check (10:26 UTC /
15:56 IST): **6 of 21 done, 0 failures**, ~75-215s/config depending on
group, estimated completion ~16:30-16:45 IST.

- **Group A** (12 configs, dir `s6_g7ab_vwap/g7a_*`): entry-tightened
  (`min_trend_side_fraction` 0.70→0.85, `max_vwap_crosses_in_lookback`
  3→1) / exit-tightened (`structure_break_atr_multiplier` 0.15→0.35,
  `structure_break_persistence_seconds` 6→120) / both-tightened, each ×
  RSI {off, 60/40 band, 65/35 band}. Base = `v_atr_pcrl` (ATR expansion +
  PCR-loose, stop=0.10, arm=0.5/lock=0.8) — deliberately no
  `require_htf_ema_trend` stacked on any of these, per explicit
  instruction.
- **Group B** (4 configs, dir `s6_g7ab_vwap/g7b_*`): the wide-target idea
  from Phase 6 Group 1 (`stop:0.06, target:0.50` and
  `stop:0.06, target:1.0` i.e. trail-only) combined with RSI {60/40,
  65/35} — tests whether the RSI filter fixes Group 1's robustness failure
  (all 8 original wide-target configs had P(mean≤0) 0.18-0.43) by cutting
  bad-direction losers while the wide target still lets correct-direction
  winners run.
- **Group C** (5 configs, dir `s6_g7c_liquidity_pivot/g7c_*`): lock ∈
  {0.6, 0.7, 0.8, 0.85, 0.9} at liquidity's already-confirmed-good
  pivot-S1/R1 structure stop (Phase 6 found P(mean≤0) 0.457→0.250,
  E ₹14.6→₹92.7 at lock=0.8 — this checks whether a different lock
  improves on that further, same pattern that worked for OI).

### Exact steps to resume in a new session

1. **Check the VM is still alive and see if Phase 7 finished**:
   ```
   ssh -i "D:\Documents\Trading Bot_Oracle\ssh-key-2026-08-03_Pvt Key.key" ubuntu@129.159.226.106 "tail -20 ~/s6_status.log; cat ~/s7_master.log"
   ```
   Look for `[master7] Phase 7 ALL COMPLETE` in `s7_master.log`. If the VM
   has been terminated (the user owns termination, no automated
   backstop — see `project_backtest_vm_e4_16ocpu_128gb_2026_08_26` memory),
   the sweep is lost and would need relaunching from the config files
   below, which are safe locally regardless.
2. **If complete**, pull results:
   ```
   scp -i "...key.key" 'ubuntu@129.159.226.106:~/trading-bot/backend/data/historical/backtest_reports/s6_g7ab_vwap/*_current.csv' "C:\Users\drvin\Trading Bot\data\historical\backtest_reports\s6_g7ab_vwap\"
   scp -i "...key.key" 'ubuntu@129.159.226.106:~/trading-bot/backend/data/historical/backtest_reports/s6_g7c_liquidity_pivot/*_current.csv' "C:\Users\drvin\Trading Bot\data\historical\backtest_reports\s6_g7c_liquidity_pivot\"
   ```
3. **Run the walk-forward analysis** (same as every prior phase):
   `backend/scripts/analyze_walkforward.py` (`FLAT_COST_PER_LOT` already
   fixed to 10.0 locally) against each group's config names (strip
   `_current.csv`, comma-join, pass via `--configs`).
4. **What to look for**, per group — see the full Group A/B/C description
   above for the exact question each answers. Also compute maxDD/worst-
   losing-streak for Group A the same way it was done for Phase 6 Group 1
   (`analyze_walkforward._load` + running-cumsum drawdown), since this is
   exactly the kind of config (tightened entries, fewer trades) where a
   good E/PF can still hide a bad tail.
5. **Local config source files** (durable, VM-independent — copied out of
   the session scratchpad into the repo itself for exactly this handoff):
   `backend/scripts/sweep_configs/phase7_configs.txt` and
   `phase7_groupc_liquidity.txt`. Also present on the VM itself as
   `~/phase7_configs.txt`/`~/phase7_groupc_liquidity.txt` if needed there
   directly (e.g. to relaunch).

### Still-open production decisions (unchanged from before this session — nothing has been applied to production yet)

1. OI_Volume_Conviction → `trail_activation_fraction:0.3,
   trail_lock_fraction:0.85` (all 3 legs) — fully confirmed twice.
2. EMA_Micro_Conviction / EMA_Micro_Conviction_PCR →
   `trail_activation_fraction:0.7` — confirmed (PCR variant directly,
   base variant by strong corroboration).
3. VWAP_Conviction — still under active research (this session's whole
   Phase 7). Do not re-park yet; Group A/B may change the picture.
4. Liquidity_Sweep_Conviction — candidate structure-stop improvement
   found (pivot_s1r1) but not yet clearing the robustness bar; Group C may
   settle whether a different lock closes the gap.

Full prior detail: this file's own 2026-08-31 ~10:50 IST (QC + overnight
read), ~12:10 IST (Phase 5), and ~14:00 IST (Phase 6) entries below, plus
memory `project_sweep4_conviction_exit_tuning_2026_08_30`.

---

## 2026-08-31 (~12:10 IST) — Sweep #4 Phase 5: follow-up sweep confirms OI arm=0.3/lock=0.85 and settles EMA-PCR

24 new configs, 28-way sharded on the still-alive backtest VM, 86 min,
**0 shard failures**, launched to answer the two open questions the
Phase 4 grid + walk-forward re-read (above) raised, plus fill the
`e3_pdt_atr_pcrl` coverage gap. Config list + driver:
`~/phase5_configs.txt` / `~/trading-bot/backend/scripts/run_phase5_followup.sh`
on the VM (copied from `run_phase4_armlock.sh`, `s4p4`→`s4p5`). **Launch
bug caught immediately**: the driver derives its working directory from
its own script path (`cd "$(dirname "$0")/.."`), so a copy placed in
`~/` instead of `~/trading-bot/backend/scripts/` silently `cd`s to the
wrong directory and every shard fails instantly with "0 trades" — caught
within seconds (all 24 configs showed `HAD SHARD FAILURES (0s, 0 trades)`),
fixed by moving the script to the sibling directory `run_phase4_armlock.sh`
already lived in, relaunched clean.

**Group A — does OI's arm advantage continue below 0.3, and does lock
beat 0.8?** Tested arm ∈ {0.10, 0.15, 0.20, 0.25} at lock 0.8, and lock ∈
{0.85, 0.90} at arm 0.3, on the 3 strongest OI variants
(`o3_atr_pcrl`, `o3_atr_pcrt`, `o3_pdt_atr_pcrt`).

- **Arm 0.30 is a genuine peak, not a grid-edge artifact.** All 3 variants
  show a clean monotonic rise from arm 0.10 (worst, P(mean≤0) 0.024-0.121)
  up through 0.30 — combined with the original grid's fall-off above 0.30,
  this settles the shape as a real peak, not one lucky endpoint.
  `o3_atr_pcrl`: a10 P=0.121 (marginal) → a15 P=0.025 → a20 P=0.014 →
  a25 P=0.007 → a30 P=0.047(l80)/0.004(l85).
- **Lock 0.85 clears real extra headroom over 0.80; 0.90 adds almost
  nothing past 0.85.** `o3_atr_pcrl` at arm 0.30: P(mean≤0) 0.047 (l80) →
  **0.004** (l85) → 0.004 (l90, flat). Same "no turning point, but the
  real gain is at one specific step" shape, not identical to ORB's own
  ladder (which kept improving smoothly all the way to 0.9) — here the
  step from 0.80→0.85 is what matters.
- `o3_atr_pcrt`/`o3_pdt_atr_pcrt` were already excellent at arm 0.30/lock
  0.80 (P=0.001/0.004) and stay flat-to-marginally-better through 0.85/0.90
  — confirms these don't regress, doesn't add new information.

**Group B — e3_pdt_atr_pcrl (deployed EMA_Micro_Conviction_PCR gate),
never arm-tuned before this.** Tested arm ∈ {0.3, 0.6, 0.7} × lock ∈
{0.6, 0.8}. **Graduates from "thin but marginal" to a clean pass**:
deployed arm=0.5 sat at P(mean≤0)=0.052 with a *negative* 5th-percentile
(-1.4, from the earlier walk-forward re-read) — passing one criterion,
failing the other. At **arm 0.7 / lock 0.8**: n=7, win 71.4%,
IS E=+397.9 / OOS E=+380.8 (both strongly positive and balanced),
P(mean≤0)=**0.021**, 5th-pctile=**+59.9** — clears the full bar cleanly.
Confirms the exact same directional preference (later activation, not
earlier) already found for its sibling `e_pdt_atr` in the Phase 4 grid
(P=0.039 at arm 0.7 vs 0.100 at deployed arm 0.5) — the two EMA-family
configs now corroborate each other, not just themselves. Arm 0.3
(matching OI's direction) was actually *worse* here (P=0.107-0.115) than
the deployed arm 0.5 — confirms EMA and OI genuinely run opposite
directions on this lever, not just "0.3 always wins."

**Revised "what to change" list** (still not actioned — reported for a
production decision per standing rule to judge against real paper data
first):
1. OI_Volume_Conviction → `trail_activation_fraction:0.3,
   trail_lock_fraction:0.85` (all 3 legs) — now confirmed, not inferred.
2. EMA_Micro_Conviction_PCR → `trail_activation_fraction:0.7` (lock 0.8
   unchanged) — now confirmed directly on the deployed gate itself.
3. EMA_Micro_Conviction → `trail_activation_fraction:0.7` — Phase 4's
   original finding, now corroborated by its sibling's identical result.
4. VWAP_Conviction — re-park (deliberately not re-tested in Phase 5; no
   exit tweak fixes a weak entry, confirmed 3× already).
5. Liquidity_Sweep_Conviction — re-park (P(mean≤0)=0.457 at every cell
   tested anywhere).

Full write-up + tables:
[Sweep 4 Ledger artifact](https://claude.ai/code/artifact/d7d38361-75aa-4412-8fbe-7cedb60706bc)
(updated same session). Raw data: `wf_p5_results.txt` +
`s4p5_refinement_20260831T051015Z/*_current.csv`, both pulled to local
disk (VM data no longer at risk from termination for any of Phase 2-5).

---

## 2026-08-31 (~14:00 IST) — Sweep #4 Phase 6: real-data-motivated follow-ups (VWAP asymmetric exit, OI noise-floor confirmed, structure-stop for vwap/liquidity)

34 configs, 3 independent groups, all launched via a new generic driver
(`run_phase6_generic.sh`, appends a per-run `EXTRA_BT_ARGS` env var to every
shard invocation — reused for all 3 groups instead of one-off scripts), 0
shard failures, ~90min total wall time.

**Real-data correction first (glitch exclusion).** Per explicit instruction,
excluded two documented incidents before evaluating real paper trades: the
frozen-VWAP bug (Aug 25-27, VWAP pinned at a stale 24182.8 since TrueData's
archival broke volume-weighted VWAP — see `project_vwap_frozen_index_no_volume
_2026_08_27`) and today's option-chain-feed-dead incident (before ~10:10 IST —
`project_option_chain_feed_dead_fix_2026_08_31`). This **overturned an earlier
same-day finding**: 15 of the 17 "real" VWAP stop-losses reported earlier were
inside the glitch window. Clean data: base VWAP net **+₹21,006** over 26 real
trades (65% win rate), not the murkier picture reported before exclusion. It
also killed a same-session hypothesis (EMA9-vs-EMA20 trend misalignment
discriminating CE losers) — on clean data CE losses and CE wins have nearly
identical trend context (+3.37 vs +3.23 avg spread); the earlier "-7.68"
signal was entirely a glitch artifact. Dropped that config idea rather than
launch a test built on contaminated evidence.

**New harness code**: `--min-minutes-before-trail-arm` (default 0.0, smoke-
tested byte-identical to prior results) — even once the trail's price
condition is met, require at least N minutes since entry before it can arm.
A crude noise-filter proxy for the fact this 1-min-bar backtest has no
intrabar ticks, motivated by the user's own realism concern about arm=0.3.

### Group 1 — VWAP tight-stop/wide-target (8 configs)
Real-evidence motivated: both of today's real spike trades (11:54/11:56 IST,
the sharp NIFTY move the user's own chart showed) closed at exactly
15.00%/15.01% — capped precisely at `target_pct=0.15`, which was **never once
varied across all of Sweep 4's ~180 VWAP configs** (only `stop_pct` was ever
swept). Tested `stop_pct` ∈ {0.06, 0.08} × `target_pct` ∈ {0.25, 0.35, 0.50,
1.0 (trail-only)}, arm/lock held at the confirmed-best 0.5/0.8.

Win rate drops as hypothesized (26.7-40% vs ~50-53% baseline), point
estimates look attractive (PF 1.20-2.12, all positive E/lot) — **but all 8
configs fail the walk-forward robustness bar** (P(mean≤0) 0.183-0.426, every
one above 0.15), with a stark H1-negative/H2-positive regime split (H1 down to
-432 E/lot, H2 up to +812) — the same "profit concentrated in the back half"
trap this ledger has flagged since sweep #2. MaxDD is proportionally large at
n=15 (e.g. `s08_t35`: DD ₹2,920 vs total net only ₹808 — drawdown exceeds
total profit). Best point estimate: `stop 0.06/target 0.50`, E=250.3, PF=2.12,
still P=0.183. **Verdict: directionally plausible (win-rate mechanism behaves
exactly as predicted), not yet proven — n=15 too thin, needs more real data or
a larger backtest sample before any production change.**

### Group 2 — OI arm-noise-floor (12 configs: 3 strongest OI variants × 1/2/3/5 min)
Directly answers the realism concern from the arm=0.3 recommendation. Results
are **essentially unchanged across all 4 thresholds** — P(mean≤0) stays
0.001-0.005 throughout for every config, E/PF move by <1%. The noise-floor
gate never binds: real winning trades behind the arm=0.3 edge take longer
than 5 minutes to reach 30%-of-target, so same-bar/near-instant activation
isn't actually driving this edge. **Confirms the OI retune isn't exposed to
the intrabar-whipsaw risk that motivated this test.**

### Group 3 — Structure/S-R stop on vwap_pullback_conviction + liquidity_sweep_reversal_conviction (14 configs: swing {5,10,15,20,30} + pivot_s1r1 + pivot_s2r2, both strategies)
- **liquidity_sweep_reversal_conviction: floor pivot S1/R1 is a real
  improvement** over its own default structure level — P(mean≤0)
  0.457→**0.250**, E ₹14.6→**₹92.7**, PF 1.07→**1.52**. Still short of the
  0.15 bar but the best result this strategy has produced anywhere in the
  project. Pivot S2/R2 also helps (P→0.331) but less than S1/R1. Swing
  lookback 5/10/15 ≈ no change from baseline; 20/30 make it worse.
- **vwap_pullback_conviction: no structure-stop mode helps at all** — every
  variant sits within noise of its own or_boundary-equivalent default
  (P=0.277-0.444 vs baseline 0.270). Confirms "no exit tweak fixes a weak
  entry" once more, this time for VWAP specifically.

### Files
`run_phase6_generic.sh` (VM, backend/scripts/), config lists
`phase6_g{1,2,3}_*.txt`, results pulled locally to
`data/historical/backtest_reports/s6_g{1,2,3}_*/`, walk-forward output at
`s4_walkforward_results/wf_s6_*.txt`. `analyze_walkforward.py`'s
`FLAT_COST_PER_LOT` fix (40→10) from earlier today still in effect.

---

## 2026-08-31 (~10:50 IST) — Sweep #4 QC + overnight results READ (walk-forward pass + Phase 4 arm×lock grid pulled off the VM before termination)

**Data recovery.** VM (129.159.226.106) was still reachable this morning
(04:54 UTC / ~10:24 IST). Pulled both `wf_p2_results.txt`/`wf_p3_results.txt`
and all 144 Phase-4 merged `*_current.csv` files to local disk
(`data/historical/backtest_reports/s4_walkforward_results/`,
`.../s4p4_refinement_20260830T201059Z/`) before anything could be lost to the
VM's termination. `analyze_walkforward.py`'s `FLAT_COST_PER_LOT` fixed
locally 40→10 (was already corrected on the VM for wf_p2/wf_p3, confirmed
from their own header line) before re-running it locally against the Phase 4
CSVs to produce `wf_p4_results.txt` (144 configs, same IS/OOS/bootstrap/
slippage methodology).

**QC verdict on the 2026-08-30/31 shortlist and deploy decision.** The
sweep's *methodology* through Phase 3 was sound and genuinely self-
correcting — the robustness reality-check that overturned 3 of 4 raw
"winners" once real costs were applied, and the stop-pinning transcription
catch before Phase 3 launched, are real QC, not rubber-stamping. But **the
2026-08-31 ~01:24 IST deploy decision was made one filter short of the
sweep's own stated robustness bar**: `analyze_walkforward.py` (IS/OOS,
both halves, bootstrap P(mean≤0)≤0.15, non-negative 5th-percentile) had
been *launched* against all 138 Phase 2+3 configs but its results sat
unread on the VM at deploy time. Re-checking the 5 deployed entry gates
against that bar, now that it's been read:

| deployed config | n | P(mean≤0) | 5th-pctile | IS / OOS | verdict |
|---|---|---|---|---|---|
| `o3_atr_pcrl` (OI, lock 0.8) | 14 | **0.047** | **+5.4** | +142.6 / +395.6 | **clears the bar** |
| `e_pdt_atr` (EMA, lock 0.8) | 12 | 0.100 | -52.7 | +47.9 / +310.7 | borderline (IS thin, win 50%) |
| `e3_pdt_atr_pcrl` (EMA-PCR) | 7 | 0.052 | -1.4 | +242.2 / +314.3 | thin but clean, not a mirage |
| `v_atr_pcrl` (VWAP, all 3 legs) | 15 each | **0.21–0.39** | -102 to -189 | H1-neg/H2-pos on every leg | **fails** |
| `l_pcrt` (Liquidity, lock 0.8) | 22 | **0.457** | -203.9 | +10.5 / +20.5 | **fails — coin-flip** |

VWAP_Conviction and Liquidity_Sweep_Conviction — 2 of the 5 deployed
strategies — do not clear the bar this project holds every other strategy
to (same bar `d_pdt_w65` had to clear before being called more than
"paper-test worthy"). Blast radius was correctly bounded (`force_paper`,
paper-only), but the walk-forward pass exists specifically to catch a
VWAP/Liquidity-shaped false positive before it reaches even paper trading,
and reading it a day late meant it didn't.

**Real, actionable finding from the Phase 4 arm×lock grid (144 configs,
locally re-scored).** `trail_lock_fraction 0.8 > 0.7 > 0.6` replicates
cleanly at every `trail_activation_fraction` tested, across all 10 covered
configs — the wick-cushion worry that motivated this grid isn't a real
cost in this data. But **arm timing matters far more than lock, and OI's
deployed arm is not the best one**: all 5 tested `oi_volume_confirmed_
conviction` variants (`o3_atr_pcrl`, `o3_atr_pcrt`, `o3_pdt_atr_pcrt`,
`o_pcrl`, `o_pcrt`) show the identical shape — activating the trail at
`trail_activation_fraction:0.3` (30% of target) is dramatically more
robust than the deployed 0.5:

| arm | win% | E/lot | IS E | OOS E | P(mean≤0) |
|---|---|---|---|---|---|
| **0.30** (untested at deploy) | 71.4% | +270.9 | +204.4 | +337.3 | **0.004** |
| 0.40 | 64.3% | +203.8 | +78.1 | +329.6 | 0.076 |
| 0.50 (deployed) | 64.3% | +269.1 | +142.6 | +395.6 | 0.047 |
| 0.60 | 57.1% | +165.0 | -127.2 | +457.2 | 0.203 |
| 0.70 | 57.1% | +218.9 | -80.7 | +518.5 | 0.157 |

(all at lock 0.8). Mechanism: past arm≈0.5, IS expectancy flips sharply
negative while OOS keeps climbing — the same "OOS-flattered" shape this
ledger's sweep #2 write-up already distrusts. Arm 0.3 is the one point
where both halves are solidly positive — and it repeats independently
across all 5 oi_volume_confirmed variants in the grid, not one lucky
config. **`ema_pdt_atr` runs the opposite direction** — only arm
0.7/lock 0.8 clears the bar cleanly (P(mean≤0)=0.039); arm 0.3/0.4 stay
marginal, consistent with `e_pdt_atr`'s own borderline verdict above.
**`vwap_pullback` (`v_atr`/`v_pdt`) and `liquidity_sweep_reversal`
(`l_pcrt`) fail at every one of their combined 36 grid cells** — no
arm/lock combination rescues either; the exit-tuning lever is exhausted
for both, not just under-tuned.

**Coverage gap found while cross-checking.** `e3_pdt_atr_pcrl` and
`v_atr_pcrl` — the actual deployed gates for EMA_Micro_Conviction_PCR and
VWAP_Conviction — were never among the 12 configs carried into the Phase 4
arm×lock grid (that used `e3_atr_pcrl` and `v_atr`/`v_pdt` instead, related
but not identical configs). Both do have Phase-3 walk-forward coverage
(the table above), but neither has ever been tested at any
`trail_activation_fraction` besides the deployed default — the arm-tuning
finding above cannot be applied to them without a dedicated run.

**What to change, in order** (not yet actioned — reported here for a
production decision, per the standing rule that changes are judged against
real paper data first, not backtest alone):
1. Re-tune OI_Volume_Conviction's `trail_activation_fraction` 0.5→0.3
   (all 3 legs, keep lock 0.8) — the strongest, most-repeated finding in
   the dataset, upside on an already-good strategy, not a rescue.
2. Consider re-tuning EMA_Micro_Conviction's arm 0.5→0.7 — same direction
   its own borderline verdict already hinted at; run a dedicated
   confirmation first (inferred from a related config family, not a
   config-identical grid cell).
3. Re-park VWAP_Conviction formally — every variant tested anywhere in
   Phase 2/3/4 fails; the conviction-gate layer did not change this
   strategy's outcome the way it did for the other three.
4. Re-park Liquidity_Sweep_Conviction formally, not just flag as weakest —
   P(mean≤0)=0.457 at every grid cell is statistically indistinguishable
   from no edge.
5. Run the missing Phase-4-equivalent grid for `e3_pdt_atr_pcrl` and
   `v_atr_pcrl` if either strategy is kept running.

Full write-up, per-strategy verdict cards, and the complete arm×lock table:
[Sweep 4 Ledger artifact](https://claude.ai/code/artifact/d7d38361-75aa-4412-8fbe-7cedb60706bc)
(updated same session). Raw data now safe locally regardless of the VM's
fate: `wf_p2_results.txt`, `wf_p3_results.txt`, `wf_p4_results.txt` (new),
144 Phase-4 trade CSVs.

---

## CANONICAL RELIABLE-BACKTEST SETUP (read this first — don't rediscover it)

**2026-09-05 update: use `run_sweep_canonical.sh` for any new sweep, don't
hand-build the invocation below anymore.** Built the same day a sweep
(`phase13_exit_staging`) launched with `--underlying-source futures_proxy`
on every config — wrong for everything except `vwap_pullback*`, caught only
because `a_pdt_w65` came back with 9 trades instead of the established 26.
The source-per-strategy-type rule below had been sitting in this exact
section in prose the whole time; it just wasn't enforced anywhere in code,
so a rebuilt config file could silently get it wrong again.

`run_sweep_canonical.sh` + `resolve_and_qc.py` (`backend/scripts/`, synced
to A1) wrap `run_phase6_generic.sh` (shard/merge/reap logic untouched,
proven) with two things that used to be manual: (1) auto-resolves
`--underlying-source` from `resolve_and_qc.py`'s `SOURCE_MAP` — config
files are now 3-field (`name|strategy_type|params_json`), no source column
to mistype; a 4th field still works as a deliberate override, printed
loudly as `OVERRIDE`, never silent; (2) runs the real `_build_strategy` QC
gate automatically before anything launches — a bad config aborts with a
clear error, zero shards started. Also auto-injects
`oi_use_futures_volume_confirmation:false`/`oi_use_atm_oi_buildup:false`
for any `oi_volume_confirmed*` config that doesn't already set them
(matches the existing class defaults — belt-and-suspenders, not a
behavior change).

Usage: `RUN_TAG=<tag> ./run_sweep_canonical.sh sweep_configs/<file>.txt`
(from `backend/scripts/`), then launch that through `run_bt.sh` exactly as
before. Old 4-field config files (phase7-12) still work unchanged — the
resolver accepts either 3 or 4 fields.

**QC'd 2026-09-05** (syntax check, resolver correctness on 4 hand-built
cases, failure path, and a full end-to-end abort-path test through the real
wrapper — see `project_canonical_sweep_harness_2026_09_05` Claude memory for
the complete record). **Pristine baseline saved** at
`backend/scripts/_baseline_2026_09_05/` (both local and A1, checksum-
verified) before any future edit. **Standing rule: start every new sweep
from this harness — update its config file / `SOURCE_MAP`, don't rebuild —
unless a genuinely significant change is needed**, in which case copy from
the baseline first and record a new one the same way.

**The underlying invocation this generates** (per-strategy sharded, all 52
NIFTY weekly / 12 BANKNIFTY monthly expiries) — kept here for reference,
not for hand-building:

```
./.venv/bin/python scripts/run_backtest.py \
  --strategy <type> --underlying NIFTY \
  --all-expiries --options-subdir options_1min_past \
  --underlying-source alice_index \
  --exit-mode current --fast \
  --near-expiry-days 6 \
  --strategy-params '<json>' \
  --shard-count 28 --shard-index <i> \
  --db-suffix <unique> --out-csv <dir>/<name>_s<i>.csv
```
then `merge_backtest_shards.py --glob '<dir>/<name>_s*.csv' --out <dir>/<name>_current.csv`.
(Shard count corrected 18→28 in this doc — every real sweep since Phase 6
has actually used 28, the 18 here was stale.)

**Non-negotiables, each learned the hard way:**
- **`--near-expiry-days 6` is mandatory.** Without it, `--all-expiries` replays
  each weekly from its ~2-week listing day, so ORB trades land at 10–14 DTE
  (not the current week a live run trades) AND the same calendar day is traded
  2–3× across overlapping expiry directories. `6` = the NIFTY Wed→Tue week;
  also collapses the multi-directory overlap. Every sweep-#1/#2 "edge" was an
  artifact of omitting this.
- **`--exit-mode current`** = the faithful exit stack (see below). `legacy` /
  `target_mult` had a 26× PnL-scale bug (fixed) and model close-only fills.
- **`--underlying-source alice_index`** for everything EXCEPT `vwap_pullback*`,
  which the `SOURCE_MAP` routes to **`futures_proxy`** — the index feed reports
  volume=0 so VWAP never forms. **Caveat, established p18s / Part 0
  (2026-09-08): `futures_proxy` is a volume stopgap that also substitutes the
  *price* series to the future (~index + basis). For a breakout signal that is
  negligible; for VWAP — a price-line mean-reversion signal — it is not. The
  faithful series is `alice_index` spot price + spliced real futures volume,
  which is what live does (`ShoonyaBrokerAdapter._splice_future_volume`).** That
  path is built as `run_backtest.py --volume-source futures` (idempotent applier
  `patch_volume_source.py`), dormant on the box, not promoted — the p18s smoke
  showed it corrects the series but the near-expiry archive still caps
  `vwap_pullback` at ~16 trades (see the uncapped-strategy note under the
  robustness bar). Until it is promoted, **every `vwap_pullback*` backtest KPI
  is directional only, not a live proxy** — the p14 VWAP R:R grid was voided
  for exactly this.
- **VIX + PCR are seeded/floored automatically** inside `run_backtest.py` now
  (VIX as real `INDIA VIX` QuoteTicks; PCR returns `None` below
  `PCR_MIN_SIDE_OI = 100_000` instead of garbage). Sweep #1's VIX gate was
  completely inert; its PCR ranged 0.003–505.
- **`oi_volume_confirmed`**: always run with
  `oi_use_futures_volume_confirmation:false, oi_use_atm_oi_buildup:false` —
  the single-snapshot-per-run backtest can't support the temporal
  confirmation modes; only the chain-participation-weighted ranking works.
- Costs are **never** in the backtest — apply the cost model in
  `analyze_conviction_sweep.py` / `analyze_refined_batches.py` /
  `analyze_walkforward.py` (flat ₹40 + 0.04% turnover + 0.1% STT +
  **0.5%/side slippage**, sweep 0.3–1.0% for sensitivity).
- **IST everywhere.** `run_backtest.py` is IST-correct; analysis scripts
  `tz_convert("Asia/Kolkata")` before any `.dt.hour`/`.dt.day_name()`. Full
  rules: `BACKTEST_TIME_CONVENTIONS.md`.

**`--exit-mode current` models, bar-by-bar on the 1-min option premium
(intrabar high/low):** fixed %-stop (`stop_pct`), fixed %-target
(`target_pct`), trailing stop (arms at `trail_activation_fraction` of target,
locks `trail_lock_fraction`), structure-break (underlying re-crosses the
opening-range boundary), spread-blowout (synthetic bid/ask), EOD square-off
15:09 IST, and — if the strategy sets them — `max_loss_per_lot` /
`time_stop_minutes`. Same-bar stop+target ties resolve as the loss.

**Permanent data ceilings (normalise for, cannot fix):** ~1 year of 1-min
option premiums (52 NIFTY weeklies / 12 BANKNIFTY monthlies, Aug'25–Aug'26);
1-min bars, no ticks (structure-break persistence collapses); no real bid/ask
anywhere (synthetic spread from OI/volume); no cross-trade P&L feedback in the
sim (daily-loss / consecutive-loss breakers are analysis overlays only);
BANKNIFTY current-week ORB is ~9 trades/yr — too thin to backtest a strategy;
**the near-expiry archive is ~100% DTE 5–6 (established p16, 2026-09-08) — and
ORB/OI/EMA fire once per direction per `--all-expiries` expiry-week run, the
entry landing on the first eligible day (DTE ~6) with `_fired_directions`
blocking the rest — so ZERO DTE 0–3 trades are producible: any expiry-day /
theta-decay / expiry-morning-vs-afternoon lever is untestable on this archive,
and max n for these strategies = (near-expiry weeks with data) × 2 regardless
of intra-week data volume.**

**Robustness bar for any candidate** (`analyze_walkforward.py`): positive in
IS *and* OOS *and* both 6-month halves; bootstrap P(mean≤0) ≤ ~0.15 with a
non-negative 5th-percentile; survives 1.0%/side slippage; expiry-week sign
test as supporting (not decisive — n is small). At n≈20–40, every pass is
"paper-trade to collect live data", never "deploy".

**Uncapped strategies (`vwap_pullback`) do not even reach n≈20–40 here (p18s,
2026-09-08).** With no once-per-run cap the setup fires only when it occurs
(~0–2×/week), and `--near-expiry-days 6` restricts to ~26 weeks → ~16 trades
total across the whole archive, vs live running every session (~13/day). Their
backtest KPIs measure a tiny unrepresentative near-expiry slice and are not a
live proxy. Validate these **forward** — paper signal-count + outcome ledger,
then monitored 1-lot live — and characterise the raw signal (frequency, points
edge, MFE/MAE) on the full continuous `alice_index` series independent of any
option data.

---

## 2026-08-30 (11:26 IST → ongoing, ~23:22 IST as of this entry) — Sweep #4: conviction-gates + exit-tuning for the 4 non-ORB strategies (vwap_pullback, ema_micro_pullback, oi_volume_confirmed, liquidity_sweep_reversal)

**Context.** Sweep #3 (2026-08-28/29) already re-tested all 4 non-ORB
strategies on the canonical setup and found them still net losers after
tightening each strategy's own *native* params — verdict was "permanent
park, all four." Never tried on these 4: the cross-cutting conviction-gate
layer (`ConvictionGateMixin`, day-trend/VIX/ATR-expansion/volume-surge/
HTF-EMA/PCR-band/day-of-week) that already rescued ORB. This sweep ports
that layer, adds 4 new strategy-specific "native" params, then runs
entry-conviction → exit-tuning → lock-fraction refinement, same
entries-first-then-exits shape ORB's own sweep #3 used. **VM
(129.159.226.106) terminates the night of 2026-08-31 — this sweep is
scoped to that deadline.**

### Code built (all merged, tested, `ruff`/`mypy` clean before syncing)
- `strategy_engine/conviction_gates.py` — `ConvictionGateMixin` extracted
  from `orb_conviction.py`, generalized to take an explicit `option_type`
  (the other 4 strategies already know direction before finalizing a
  proposal, unlike ORB). `orb_conviction.py` itself refactored onto the
  mixin (byte-identical, its own test suite re-run unchanged).
- 4 new subclasses, same shape as `orb_conviction.py`:
  `vwap_pullback_conviction.py`, `ema_micro_pullback_conviction.py`,
  `oi_volume_confirmed_conviction.py`, `liquidity_sweep_reversal_conviction.py`.
  `oi_volume_confirmed` needs the same `_fired_directions.discard()` undo
  ORB needs (it latches direction before the gate sees the proposal); the
  other 3 don't latch, so a gate rejection is just `return None`.
- **Real bug fixed while wiring the direction lookup**: `OptionContract
  .option_type` is `Mapped[OptionType]` but backed by plain `String(2)`
  (no SQLAlchemy Enum type) — a freshly-queried row returns a raw `str`,
  silently breaking every `is OptionType.CE/PE` identity check. Fixed via
  `OptionType(contract.option_type)` normalization in all 4 subclasses
  (idempotent either way).
- 4 new strategy-specific "native" params, one per strategy, each `0`/
  `False` by default = byte-identical to the base strategy:
  `min_bars_since_open` (vwap — VWAP-band session-open warm-up),
  `min_ema_spread_atr_ratio` (ema — ADX-substitute trend-strength filter,
  `|EMA9-EMA20|/ATR14`), `require_oi_price_alignment` +
  `oi_alignment_lookback_bars` (oi — PCR-slope proxy for real temporal OI
  buildup, since per-contract OI history isn't tracked anywhere today),
  `min_displacement_atr` (liquidity — ICT-style confirmation-bar body/ATR
  displacement filter).
- Registered in `api/v1/strategies.py` (4 new `*_PARAM_KEYS` + 4
  `_build_strategy` branches + `KNOWN_STRATEGY_TYPES`) and
  `run_backtest.py`'s `STRATEGY_TYPES` CLI choices.

### Phase 1 — single-gate entry sweep (~10 configs/strategy, base exit config)
Confirms **ATR expansion is the standout single lever**: `o_atr` PF 1.66,
`o_pdt_atr` PF 2.41, `e_pdt_atr` PF 2.06 (n=12) — vs `o_pdt`/`e_pdt`/`l_pdt`
alone all net losers (PF 0.74–0.77). Non-obvious interaction found here and
confirmed in every later round: **`prior_day_trend` is a loser alone for
oi/ema but a real winner stacked with ATR expansion.** `l_atr` alone is a
clear loser for liquidity specifically (PF 0.63, n=38) — the one strategy
where ATR does NOT help, unlike the other 3. `v_atr`/`v_pdt` were the only
two live signals for vwap (PF ~1.03–1.18); `v_pdt_atr` combined was worse
(PF 0.93) — stacking hurts vwap, unlike oi/ema.

### Phase 1.5 (mix-and-match) + Round 3 (untested double/triple combos)
16 configs stacking each strategy's best Phase-1 gate with its new native
param; 8 configs testing untested PDT/ATR/PCR-tight/PCR-loose combos for
oi + ema specifically. **The 4 new native params were mostly a bust in
isolation** — `min_bars_since_open`, `min_ema_spread_atr_ratio`,
`min_displacement_atr` all net negative, worse than the shared gates
alone. `require_oi_price_alignment` (oi) showed a positive signal only
combined with other gates (`o15_align5`), not decisively on its own.

### Robustness reality-check (real costs) — overturned 3 of 4 raw "winners"
Running the raw-PnL Phase 1/1.5 "champions" through the real cost model
(flat ₹40/lot + 0.04% turnover + 0.1% STT, **no slippage yet**) flipped
**3 of 4 to net losers** — only `oi_volume_confirmed`'s best config
survived as a borderline (not clean-pass) candidate. This is why exit
tuning ran on a much broader win_rate≥50% pool (below), not just the
raw-PnL leaderboard — raw PnL alone was actively misleading at this n.

### Phase 2 — exit-tuning (102 configs: 34 qualifying entry configs ×
3-point stop grid, `trail_lock_fraction` fixed 0.6, `trail_activation_fraction`
fixed 0.5). Entry pool = every config from Phase 1/1.5/Round 3 with
win_rate≥50% (content-hash deduped — 2 exact duplicates removed). Result
dir: `s4p2_exittuning_20260830T104615Z`, 310min, 0 shard failures.

**Cost-adjusted results by strategy** (net of flat ₹40/lot + 0.04% + 0.1%
STT, no slippage — see the ₹5/order Shoonya-brokerage correction below,
which means real costs are actually *lower* than what these numbers
assume, i.e. every figure here understates real net PnL):

| strategy | best cohorts (n, PF range, all 3 stops) | verdict |
|---|---|---|
| oi_volume_confirmed | `o3_atr_pcrl` (n=14, PF 1.9–3.9), `o3_atr_pcrt` (n=8, PF 2.5–4.0), `o3_pdt_atr_pcrt` (n=7, PF 2.2–3.4), `o_pdt_atr` (n=16, PF 1.3–2.4), `o_pcrl` (n=23, PF 1.3–1.9), `o_pcrt` (n=11, PF 1.8–2.4) | **strongest — 6 cohorts, PF>1.8 consistently across ALL 3 stops (real robustness signal)** |
| ema_micro_pullback | `e_pdt_atr` (n=12, PF 2.1–3.1), `e3_atr_pcrl` (n=10, PF 2.4–2.9, stop-insensitive) | real, smaller sample |
| vwap_pullback | `v_pdt` (n=16, PF 1.20 @ 0.10), `v_atr` (n=16, PF 1.17 @ 0.08) | marginal at best |
| liquidity_sweep_reversal | `l_pcrt` (n=22, PF 1.20 @ max stop only) | **no robust winner — re-confirms sweep #3's "permanent park"** |

`e3_atr_pcrt`/`e3_pdt_atr_pcrt` showed PF 6.6/8.1 but n=4–5 — flagged as
statistical mirages, not chased.

**Cross-strategy insight**: ATR expansion appears in every genuine winner
across all 4 strategies — the single most consistently valuable lever,
matching ORB's own `d_pdt_w65` precedent (volatility-regime gating > any
strategy's own setup logic).

### Cost-model correction (2026-08-30, ~23:00 IST)
**Shoonya's real brokerage is ₹5/order flat = ₹10/round-trip (entry+exit),
not the ₹40/lot `FLAT_COST_PER_LOT` constant `analyze_walkforward.py` /
`analyze_conviction_sweep.py` currently assume.** Every net-PnL figure
computed so far (Phase 2 table above, all of sweep #3) is understating
real profitability — apply the corrected ₹10 flat cost (keep the
proportional-turnover + STT components, which model real exchange
charges, unchanged) in the final walk-forward robustness pass on whatever
shortlist survives Phase 3, and correct the shared constant itself before
any future sweep reuses it.

### Phase 3 — refinement (36 configs, IN PROGRESS as of this entry, ~69%
done, 0 failures, disk stable ~91G free). Two parts, launched
`s4p3_refinement_20260830T164757Z`:
1. **Trail-lock bracket** (0.4, 0.8 — reusing existing 0.6 data) on the 12
   strongest candidates across all 4 strategies (6 oi + 2 ema + 2 vwap + 2
   liquidity — the last 2 included per explicit instruction to keep testing
   the weaker strategies too, despite liquidity having no real winner yet).
2. **4 new untested triple/pair combos**, full 3-stop bracket at lock=0.6:
   `e3_pdt_atr_pcrl`, `o3_pdt_atr_pcrl` (pdt+atr+PCR-loose — motivated by
   the Phase-1 finding that pdt only helps ema/oi when stacked with ATR),
   `v_atr_pcrl`, `v_atr_pcrt` (vwap's only working single gate, atr, never
   stacked with either PCR band before).

**QC catch before/during launch (real, worth recording as a process
lesson)**: a cross-check script comparing the Phase 3 config file against
the actual authoritative Phase 2 CSV data (not my own condensed recap
tables) found **4 real stop-pinning errors** — `o3_atr_pcrt`,
`o3_pdt_atr_pcrt`, `o_pcrt` were pinned to the wrong (worse) stop, `l_pcrt`
likewise. Root cause: I'd condensed 3-row per-cohort tables into a single
range string in chat and silently assumed stop-ascending order matched
PnL-ascending order, which was false for these 4. **Always cross-check a
condensed summary table against the raw per-variant data before turning it
into a new sweep's input** — a launched-then-caught version of this wasted
~14 min of VM time (2 wrong configs already run); caught before the
remaining 6 wrong configs launched. Killed, reaped orphaned DBs, fixed,
re-verified via the same script (0 mismatches), relaunched clean.

### Phase 3 results (completed 2026-08-30 18:31:37Z / 00:01 IST, 36 configs,
103min, 0 failures) — a real QC catch first: a cross-check against the
authoritative Phase 2 CSV data (not my own condensed recap tables) found 4
stop-pinning transcription errors (`o3_atr_pcrt`, `o3_pdt_atr_pcrt`,
`o_pcrt`, `l_pcrt` pinned to the wrong stop) baked into the first launch —
caught after only 2 wrong configs had run (~14min wasted), killed, fixed,
re-verified, relaunched clean. **Lesson: always cross-check a condensed
chat summary against raw per-variant data before turning it into a new
sweep's input.**

**Universal finding: `trail_lock_fraction 0.8 > 0.6 > 0.4`, zero exceptions
across all 12 directly-tested lock-refinement configs**, drawdown flat or
slightly better at 0.8. Same wick-cushion caution as ORB's own `d_pdt_w65`
precedent (backtest's synthetic spread can't see real fill slippage on a
tighter trail) — deployed at 0.8 per the backtest signal, but paper-tested
for a few days before fully trusting it; see the "Deployed to production"
section below.

Top picks (best PnL / best-optimized / highest win-rate / lowest-drawdown,
full table in that day's chat, not reproduced here): `o3_atr_pcrl` (oi,
n=14, PF 3.95 @ lock 0.8) is the single best candidate; `o3_pdt_atr_pcrl`
(new triple-combo, n=13, PF 3.54) and `v_atr_pcrl`/`v_atr_pcrt` (new vwap
combos, n=15, roughly tripled vwap's prior best PnL) were genuine new
discoveries this round; `l_pcrt` (liquidity) improved to PF 1.25 but
remains the weakest of the 4 strategies by a wide margin.

### Deployed to production for paper testing (2026-08-31 ~01:24 IST)
**5 new `strategy_configs` rows live on OCI** (`144.24.137.112`), all
`runtime_mode=force_paper`, `NIFTY`, `qty_lots=10`, auto-spawned via the
09:00 IST `DailyBootstrapScheduler` and confirmed `scanning` as of
2026-08-31 08:26 IST:

| Name | strategy_type | Entry gate | Exit structure |
|---|---|---|---|
| `OI_Volume_Conviction` | oi_volume_confirmed_conviction | `o3_atr_pcrl` (ATR expansion + PCR-loose 0.4–2.5) | 3-leg multi-leg exit (0.4/0.3/0.3 lots): stop 0.11/0.17/0.09, all lock 0.8 |
| `EMA_Micro_Conviction` | ema_micro_pullback_conviction | `e_pdt_atr` (prior-day-trend + ATR expansion) | 3-leg: stop 0.12/0.06/0.08, lock 0.8 |
| `EMA_Micro_Conviction_PCR` | ema_micro_pullback_conviction | `e3_pdt_atr_pcrl` (pdt+atr+PCR-loose, PF 4.75 but n=7) | 3-leg: stop 0.06/0.08/0.12, lock 0.8 |
| `VWAP_Conviction` | vwap_pullback_conviction | `v_atr_pcrl` (ATR expansion + PCR-loose) | 3-leg: stop 0.08/0.10/0.15, lock 0.8 |
| `Liquidity_Sweep_Conviction` | liquidity_sweep_reversal_conviction | `l_pcrt` (PCR-tight 0.7–1.3) | single-leg, stop 0.16, lock 0.8 (only 1 of 3 tested stops was profitable — no forced multi-leg split) |

Every multi-leg leg carries `use_structure:true` (preserves the
structure-break exit path — a real, frequent exit reason in every backtest
— which the multi-leg engine otherwise silently drops per-leg) and
`trail_activation_fraction:0.5`. Where a specific stop×lock pairing wasn't
directly Phase-3-tested, lock 0.8 was extrapolated from the universal
finding above (flagged per-leg in the original chat table, not reproduced
here). Frontend: 4 new strategy types exposed (`StrategyType`,
`friendlyLabel`, `PRIMARY_STRATEGY_TYPES` positions 7–10, Create-form
dropdown); `synthetic` archived from all 3 lists (its one DB row was
already disabled — zero live impact). Deploy record:
`docs/ops/oci_deploy_authorization.md`'s 2026-08-31 ~01:24 IST entry
(commits `51f8d77` code + `d4cf6c4` docs). QC before deploy: all 5 rows
dry-run constructed via the real `_build_strategy` + `validate_exit_leg_templates`
— 0 errors.

### Cost-model constant corrected (2026-08-31, before the overnight run)
`FLAT_COST_PER_LOT` changed **40.0 → 10.0** in both `analyze_walkforward.py`
and `analyze_conviction_sweep.py` on the VM (matches Shoonya's real ₹5/order
flat × 2 legs) — every net-PnL figure computed before this point across
sweep #3 and sweep #4 understated real profitability; anything computed
after this point uses the correct figure.

### Walk-forward robustness pass (2026-08-31, existing-data analysis, no
VM compute) — run against **all 138 configs** across both Phase 2
(`s4p2_exittuning_20260830T104615Z`, 102 configs) and Phase 3
(`s4p3_refinement_20260830T164757Z`, 36 configs) result directories, with
the corrected ₹10 cost. Output saved, **not yet analyzed**:
`backend/scripts/BACKTEST_LEARNINGS.md`-adjacent
`data/historical/backtest_reports/s4_walkforward_results/{wf_p2_results.txt,wf_p3_results.txt}`
on the VM (129.159.226.106) — pull these back before the VM terminates
tonight (2026-08-31, ~05:30 IST Sep 1).

### Phase 4 — trail_activation_fraction × trail_lock_fraction grid
(completed 2026-08-31 03:50:45Z / 09:20 IST, 144 configs, 459min ≈ 7.65hr,
**0 failures**), the deferred arm-sweep now finally run since this is the
VM's last night. On the same 12 strongest candidates from Phase 3 (pinned
at each one's established best stop): `trail_activation_fraction ∈
{0.3, 0.4, 0.6, 0.7}` × `trail_lock_fraction ∈ {0.6, 0.7, 0.8}` = 12 new
combos/config (arm=0.5 already covered by Phase 1-3; lock=0.4 dropped from
this grid since Phase 3 already showed it's consistently worst). Directly
answers the "is 0.6 or 0.8 the right lock, given the wick-cushion risk"
question the deployed strategies are running at 0.8 without full
confirmation on. Results at
`data/historical/backtest_reports/s4p4_refinement_20260830T201059Z/` on
the VM — **not yet analyzed**.

### Not yet done / planned next
1. **Pull and analyze both the walk-forward robustness results and the
   Phase 4 arm×lock grid** — the actual remaining gate before anything
   here can be called more than "paper-test worthy." Do this before the
   VM terminates tonight (2026-08-31 ~05:30 IST Sep 1) since the raw CSVs
   disappear with it — the two `.txt` result files and the Phase 4 CSVs
   are the priority to retrieve first.
2. Based on the arm×lock grid: decide whether the 5 live production
   configs' `trail_lock_fraction=0.8` should move to 0.6/0.7, or whether
   0.8 holds up — cross-reference against a few days of real paper
   performance (chart-level eyeball), not backtest alone, per explicit
   user instruction 2026-08-31.
3. `liquidity_sweep_reversal` stays the weakest of the 4 — reconsider
   formally re-parking it if the paper run does't improve on Phase 3's
   marginal PF 1.25.
4. VM (129.159.226.106) terminates tonight — this is the last chance to
   pull any raw CSV/result data off it. After that, only the already-saved
   summary tables/analysis in this file and the project memory survive.

---

## 2026-08-30 (~09:30 IST) — GAMMA BLAST v2.1 (expiry-day NIFTY options) — PARK, no config clears the tail-dependence guard

User-supplied spec (`D:\Documents\Trading Bot_Oracle\gamma_blast_config_v2_1.json`):
buy-only ATM NIFTY option, **expiry day only** (13:45-15:15 IST), gated by a
range-compression precondition + a Black-Scholes gamma/premium-band "arm"
condition + a spot breakout trigger, exited via momentum-stall (premium ROC
decay) or fixed %. New standalone harness (`gamma_blast_backtest.py` +
`analyze_gamma_blast.py` + `run_gamma_blast_sweep.py`, pandas/numpy only, same
shape as `loren_backtest.py`) — trades ONLY the expiry day itself, so unlike
ORB/Loren there is no multi-day warmup at all, making this the cheapest
backtest in the ledger (~1-2s/config across all 47 expiries).

**Data-availability finding (why this needed a from-scratch Greeks module)**:
neither Shoonya nor Alice Blue returns IV/delta/gamma anywhere — confirmed by
reading both brokers' tick/quote normalizer code directly, not assumed.
`black_scholes_iv_greeks()` (Newton-Raphson + bisection fallback, T =
minutes-to-15:30-close / (252x375), floored 30min, IV clipped [1%,150%]) is
the actual live-viable answer — same function a real implementation would
call each cycle, no vendor shortcut exists on this broker set.

**System-cutoff correction**: JSON's `force_exit_time` (15:15/20/25) and
`entry_window.latest` (15:15) sit past this system's real EOD cutoff
(15:09 IST). Headlined config always caps at 15:09; JSON-literal 15:20 kept
only as a labelled reference point (`..._NOTDEPLOYABLE` suffix), never
recommended.

**Phase 1 (entry conviction, single-axis off baseline)**: every axis is either
structurally inert or an OOS trap. `max_distance_points`/`arm_mode=off`/every
`gamma_threshold` value all produced **byte-identical** results to baseline —
`atm_heuristic` strike selection always lands exactly on ATM (distance 0), and
near-expiry ATM gamma is essentially always above the 0.001-0.004 threshold
band, so neither lever ever actually rejects a candidate on real data.
`precondition_threshold_pct:0.4` and `precondition_measure:day_range` both
looked promising IS (+83, +101) but flipped deeply negative OOS (-463, -601)
— the same "positive IS, dead OOS" shape this ledger already learned to
distrust from sweep #2's stacked-combo trap. `ema_cross` trigger and both
tighter/looser `volume_spike_mult` settings were all worse than baseline.
Baseline itself (net of the JSON's own precise cost model — brokerage +
date-switched STT + exchange txn + GST + stamp duty, slippage already baked
into the fill model, not double-counted): **n=29, win 17.2%, E -215/lot,
PF 0.48** — real, not thin-sample noise, 12 `hard_stop` losses averaging
-673/lot against only 16 `momentum_stall` wins averaging +117/lot.

**Phase 2 (exit optimization) + Phase 3 (confirmatory grid, ~70 configs
total across both e4-VM runs and a local refinement pass)**: the exit-mix
asymmetry from Phase 1 (loose 50% stop vs a fast 30%-stall exit) is real and
tunable — `exit_mode:fixed` with `target_pct:100, stop_pct:15` gets to
**E +90.5/lot, PF 1.37, ALL positive**. But **every single positive-headline
config found anywhere in the search — the fixed-exit grid, entry-window
narrowing (13:45-14:30/45), and stacking the "promising" Phase-1 precondition
variants on top — fails the tail-dependence guard**: removing just the top-2
trades flips every one of them negative (e.g. t100s15: E_all=+90.5 ->
E_excl_top2=-83.6). The pattern is structural, not a fluke of one config:
3-5 large `hard_target` wins (+1600 to +2200/lot, an option genuinely
exploding into the close) carry an otherwise net-losing book of many more
`hard_stop` losses (-295 to -330/lot each). Bootstrap P(mean<=0) for every
positive config sits at 21-48% — nowhere near this ledger's own robustness
bar (<=~0.15). `ema_cross` combined with the tuned exit is unambiguously
worse (E -276 to -337/lot, ~6% win rate) — cleanly ruled out, not just
untested.

**Verdict: PARK.** No axis, and no combination of axes, produces a config
that is both net-positive after realistic costs AND survives removing its
best 2 trades. This looks structurally like "expiry-day ATM options
occasionally explode 15-20x and everything else bleeds slowly to a stop" —
real, but not a systematic edge this spec's own filters (precondition/arm/
trigger as specified) manage to isolate in advance. Full config grid + raw
trade CSVs: `data/historical/backtest_reports/gamma_blast/{p0,p1,p2,p3}/`.
Revival path, if ever revisited: the arm/gamma filter needs a real
discriminating signal (it currently never binds) or a fundamentally
different entry-quality lever, not more exit tuning — exit tuning already
found its ceiling here and that ceiling is a tail-dependence artifact.

---

## 2026-08-30 (~00:30 IST) — LOREN TRACK (Lorentzian Classification) — options + futures both PARK

**Separate from the ORB Sweep #3 W-series.** jdehorty's ML Lorentzian
Classification + Nadaraya-Watson kernel, ported standalone
(`loren_backtest.py`, pandas/numpy only — no app/DB/`run_backtest.py`).
Files: `LOREN_BACKTEST_PLAN.md`, `run_loren_options_sweep.sh`,
`run_loren_futures_sweep.sh`, `analyze_loren_futures.py`. `--selfcheck` proves
the kernel + k-NN prediction are non-repainting.

**loren-options** (`sweep3w7_loren_20260829T132354Z`, 17 configs, real 1-yr NIFTY
weekly option chains, `--exit-mode current`-equivalent, per-1-lot net of the
0.5%/side option cost model):
- Signal-only sanity (`--underlying-only`, 3.2 yr index): 53% win, +24 pts/trade,
  PF ~3, flat across TRAIN/VALID/TEST — looked genuinely good.
- Every one of 17 option configs **fails the robust bar**: severe IS/OOS split
  (all profit in the last ~6 mo), bootstrap P(mean≤0) 0.27–0.39, negative
  5th-pctile, 16–19 of ~43 expiry weeks positive, most die at 1.0%/side.
  Spec-literal `reject if risk > 0.75·ATR14` trades only ~7×/yr. Best config
  `l_comb_cap075` (tight structure stop + kernel-managed combined exit): +₹138/lot
  ALL, PF 1.21, but P(mean≤0) 0.27. **PARK.**

**loren-futures** (`Loren_Futures.txt` v3 — same signal on NIFTY futures, no
Greeks, to isolate signal edge from option decay). Data reality: no multi-yr
1-min futures on disk (Shoonya ~41 days; TrueData stitch ~56 sparse pre-expiry
days; only the cash index has 3 yr, volume=0). The Loren signal is 100%
price-only, so cash index = sound no-Greeks proxy (basis ~0.3%, mean-reverting,
immaterial intraday). Real Indian-F&O futures cost model (STT sell-side
dominates, ≈₹460/lot ≈ 7 pts).
- **Pass 0 (edge gate) → PARK. Pass 1 (spec §6 matrix) NOT run.**
- Spec-literal `reject` mode: **3–6 trades in 3 years** (unusable n). Only
  `f_cap` (`risk_exceed_action:"cap"`) gives a real sample: **n=566, 26.9% win,
  gross +6.2 pts/trade, net −0.9 pts/trade (−₹60/lot), PF 0.95, maxDD ₹127k,
  worst streak 16.** TRAIN −129 / VALID −223 / TEST +325 Rs/lot; bootstrap
  **P(mean≤0) = 0.68**; worse at higher slippage. Same OOS-only shape as
  loren-options — no in-sample foundation.
- **Verdict (matches `Loren_Futures.txt` §5):** the loren-options IS/OOS
  collapse is NOT an option-execution artifact. On a clean instrument with tiny
  costs the gross edge (~6 pts/trade) is too thin to clear real cost and has no
  stable in-sample base. **The problem is in the signal/filters.** Any revival
  fixes the signal first — do not re-test the instrument, do not run the §6 sweep.
- Real-futures cross-check (Shoonya ~41 days) is near-useless: the file is too
  short to warm the 2000-bar classifier (~11 tradeable days after warmup);
  direction bias agreed (more shorts), magnitude didn't.

---

## 2026-08-29 (~22:45 IST) — Sweep #3 W7b: trail_lock_fraction ladder (0.7 / 0.8 / 0.9)

W4+W7 only ever tested `trail_lock_fraction` 0.4 and 0.6 (0.4->0.6 was a
consistent free gain in all 4 sweeps). W7b fills 0.7 / 0.8 / 0.9 at the W7
lead configs — all stop 18%, no fixed target — plus `stop18+target30`. Same
26 `d_pdt_w65` entries, pure exit-overlay re-slice. Dir
`sweep3w7tsl2_20260829T162046Z` (12 cfg, 18-sharded). Every config 26 trades.

### Lock ladder at trail-arm +12% (the "balanced" lead), stop 18, no target

| lock | win% | E/lot | PF | IS E | OOS E | P(mean<=0) | 1% slip |
|---|---|---|---|---|---|---|---|
| 0.6 (W7) | 73.1 | +491 | 12.95 | +473 | +529 | 0.000 | ~+430 |
| **0.7** | 73.1 | +499 | 13.17 | +483 | +537 | 0.000 | +435 |
| **0.8** | 73.1 | +508 | 13.39 | +492 | +545 | 0.000 | +444 |
| **0.9** | 73.1 | +517 | 13.61 | +502 | +552 | 0.000 | +453 |

Same monotonic, no-turning-point pattern at every other arm too:
arm +6% -> l07 +285 / l08 +294 (win 80.8% flat); arm +10% -> l07 +396 /
l08 +401 (win 73.1% flat); arm +14% -> l07 +527 / l08 +539 / l09 +550
(win 69.2% flat, but OOS weak: +432/+441/+451 vs IS +570/+582/+594);
`stop18+target30` -> l06 +586 / l08 +600 (win 65.4% flat, IS +578/+590,
OOS +602/+622).

### Findings

1. **Locking harder keeps helping, all the way to 0.9 — no turning point.**
   ~+9/lot per 0.1 step at arm 12. **Win rate is byte-identical** (73.1%) at
   every lock level; IS and OOS both rise proportionally; PF creeps
   12.95 -> 13.61.
2. **Drawdown does not move.** The losing trades are identical across all
   lock levels (exit mix `stop: 1/0%/-425`, `eod: 1/0%/-139`,
   `structure_break: 7/29%/+204` — unchanged l06->l09). Only the 17 winning
   `trail` exits shift (avg net +713 -> +740 at arm12). **maxDD stays ₹425**
   at arm 12 regardless of lock.
3. **Why it's free here:** the trailing stop is never what turns a winner
   into a loser in this sample — `trail` exits are 100% win at every lock
   level. Banking a bigger fraction of the run-up just adds a few ₹ on those
   17 trades at zero cost. The 0.4->0.6 "free gain" simply continues.
4. **The effect is tiny** — ~₹26/lot total from lock 0.6->0.9, driven by 17
   trail exits on n=26. Real in-sample and it replicates IS+OOS, but it is
   not a reason to re-pick the config. Use **0.8** (conventional ceiling;
   0.9 leaves almost no trail room) and move on.

### Revised balanced tier

`stop_pct:0.18, target_pct:1.0, trail_activation_fraction:0.12,
trail_lock_fraction:0.8` — **73.1% win, +₹508/lot, PF 13.39, maxDD ₹425,
IS +492 / OOS +545, P(mean<=0)=0.000, +₹444 at 1% slip.** Best *backtest*
number; ~+₹17/lot over the `:0.6` variant, same on every other axis. Still
n=26 -> paper-trade to collect data, not deploy. Nothing committed / deployed.

**BUT for the actual paper test, use `trail_lock_fraction:0.6`, not 0.8.**
The give-back room at 0.8 is only 20% of gain-beyond-arm (vs 40% at 0.6) —
half the cushion against a sub-minute adverse wick, which the 1-min sim
cannot see. The ₹17/lot edge rests on 17 in-sample trail exits with zero
sub-minute visibility. Run 0.6 live; tighten to 0.8 only once real fills
show trailing exits are clean (not chopped-then-price-ran-on). Full
paper-test config + spike-robustness + skip-Friday reversal + day-wise
table are in memory `project_orb_directional_filter_sweep3_2026_08_29`
(2026-08-30 ~01:00 IST entry).

---

## 2026-08-29 (~21:30 IST) — Sweep #3 W6 (chart structure stop) + W7 (stop x TSL grid) RESULTS

Both on the e4 VM, `--near-expiry-days 6`, `--exit-mode current`, 18-sharded,
pure exit-overlay re-slices of the SAME 26 `d_pdt_w65` entries (`orb_conviction`
+ `{require_prior_day_trend:true, max_or_range_nifty_points:65}`). Dirs:
`sweep3w6_structstop_20260829T135839Z` (13 cfg), `sweep3w7tsl_20260829T135042Z`
(24 cfg). Isolation anchors both byte-identical: W6 `s_or_baseline` ==
`x_baseline_stop15`; W7 `w7_s15_a08_l04` == `x_notgt_arm08_stop15`;
`w7_s15_a06_l06` == `x_notgt_stop15_arm06_lock06`.

### W6 -- chart-based structure stop -- NEGATIVE, doesn't beat the OR boundary

New backtest-only harness flag `--structure-stop-mode {or_boundary,swing,
pivot_s1r1,pivot_s2r2}` + `--swing-lookback` in `_reconstruct_exit_current`
(~55 lines, no strategy code; `or_boundary` smoke byte-identical to
`x_baseline_s0.csv`). Anchors the structure-break exit level to a recent swing
candle low/high or a classic floor pivot instead of `orb_conviction`'s
opening-range boundary. All configs carry `stop_pct:0.15` as a backstop.

| structure stop | win% | E/lot | PF | P(mean<=0) | vs OR-boundary |
|---|---|---|---|---|---|
| **or_boundary** (= `x_baseline_stop15`) | 73.1 | **+400** | 5.11 | 0.002 | -- |
| pivot S1/R1 | 73.1 | +400 | 5.11 | 0.002 | **byte-identical** |
| pivot S2/R2 | 73.1 | +400 | 5.11 | 0.002 | **byte-identical** |
| swing low, lookback 30 | 73.1 | +400 | 5.11 | 0.002 | **byte-identical** |
| swing low, lookback 10/15/20 | 69.2 | +320 | 3.23 | 0.016 | **-80/lot, worse** |
| swing low, lookback 5 | 69.2 | +362 | 4.55 | 0.002 | -38/lot, worse |
| or_boundary + no premium stop (`stop_pct:0.9`) | 73.1 | +400 | 5.15 | 0.004 | ~identical |

- **Floor pivots are pure noise.** S1/R1 sit ~100-170 NIFTY pts from spot; the
  underlying never retraces that far inside the expiry week, so the pivot
  structure-break never fires -> pivot stop == no structure stop == baseline
  (byte-identical, both `pivot_s1r1` and `pivot_s2r2`).
- **A tighter swing low (lookback 5-20) whipsaws.** Exit-mix: it converts ~1
  winning `trail` exit/yr into a losing `structure_break`, and makes the
  `structure_break` losses bigger (avg -22 -> -190/-239/-305). Cuts trades the
  wider OR boundary would have let run.
- **lookback >=30 collapses to the OR boundary** (the day's low stabilises
  after ~10 bars) -> inert.
- `s_swing10` / `_swing15` / `_swing20` are byte-identical to each other;
  `s_swing10_buf0` (ATR buffer 0) == `s_swing10` == `s_swing10` with default
  buffer -- the ATR buffer doesn't bind at this granularity either.

**W6 conclusion: the opening-range boundary IS already the right chart-structure
stop for this ORB-derived strategy. No swing or pivot level improves on
`d_pdt_w65 + stop_pct:0.15`.** The `--structure-stop-mode` flag is kept
(committed) for reuse, but there's no config to carry forward.

### W7 -- stop {0.15,0.18} x trail-arm {6,8,10,12,14}% x lock {0.4,0.6} + 4 target anchors

No fixed target on the grid (`target_pct:1.0`), so `trail_activation_fraction`
= arm point as a fraction of entry (0.06 -> arms +6%). Completes the W4 ladder,
which only had arm +6/+8 at stop 15/18 and no stop-18 ladder at all.

- **Stop -18% beats -15% at every trail-arm >= +8%** -- a consistent
  **+~56/lot**, and PF roughly doubles (s15_a12 PF 5.3 -> s18_a12 PF 12.5).
  `P(mean<=0) = 0.000` across the *entire* stop-18 column. At arm +6% the stop
  barely matters (the early trail exits everything first): s15 ~= s18 ~+277.
- **Trail-arm is a clean monotonic win% <-> E ladder** (the "+10% dead zone"
  from the stop-12 grid is GONE at loose stops -- it was a stop-12 artifact):

  | trail arm | win% | E/lot (stop 18, lock 0.6) | PF | maxDD |
  |---|---|---|---|---|
  | +6% | 80.8 | +277 | 9.2 | 425 |
  | +8% | 76.9 | +332 | 9.5 | -- |
  | +10% | 73.1 | +390 | 10.5 | -- |
  | **+12%** | 73.1 | **+491** | **12.95** | **425** |
  | +14% | 69.2 | +516 | 8.2 | 942 |

- **`trail_lock_fraction` 0.6 > 0.4 everywhere** (+15..25/lot, PF up,
  P(mean<=0) down) -- 4th sweep confirming it. Free.
- **A fixed target on the loose stop = the raw-rupee maximum, at a win-rate
  cost:**
  - `stop18 + target30%` -> **65.4% win, +571/lot**, PF 6.99, maxDD 942,
    IS +567 / OOS +581 (the most regime-balanced high-E config found in any
    sweep), +506 at 1.0% slip.
  - `stop18 + target40%` -> **57.7% win, +687/lot**, PF 5.60, maxDD 1252,
    IS +606 / OOS +870 (OOS-flattered), win% now < 60.
  - `stop18 + target30 + trail arm +14%` -> +453/lot -- the late trail *clips*
    the runners the target would have caught (571 -> 453). Don't stack an
    aggressive target with a late trail.

### Final tiers (all = `d_pdt_w65` gate + `--exit-mode current`, n=26)

| objective | params on top of the gate | win% | E/lot | total (26) | PF | maxDD | P(mean<=0) |
|---|---|---|---|---|---|---|---|
| **Max rupee** | `stop_pct:0.18, target_pct:0.40` | 57.7 | **+687** | +17868 | 5.6 | 1252 | 0.001 |
| **Max rupee, win >= 60%** | `stop_pct:0.18, target_pct:0.30` | 65.4 | **+571** | +14851 | 7.0 | 942 | 0.000 |
| **Balanced (best PF + DD)** | `stop_pct:0.18, target_pct:1.0, trail_activation_fraction:0.12, trail_lock_fraction:0.6` | 73.1 | +491 | +12753 | **12.95** | **425** | 0.000 |
| **Max win rate** | `...trail_activation_fraction:0.06, trail_lock_fraction:0.6` | 80.8 | +277 | +7208 | 9.2 | 425 | 0.000 |

**Bottom line (W4+W6+W7): the exit lever for `d_pdt_w65` is LOOSEN the premium
stop to -18%.** Then pick a spot on the win% <-> rupee curve via trail-arm
timing (+6% -> 81% win / +277; +12% -> 73% win / +491) OR add a fixed 30%
target (65% win / +571, best regime balance). `trail_lock_fraction:0.6`
always. Fixed target above the trail-arm point is otherwise inert; chart-based
structure stops (W6) add nothing. Still n=26 -- every tier is "paper-trade to
collect live data", not "deploy" or "size up". Nothing production-wired;
nothing deployed. `run_backtest.py`'s `--structure-stop-mode` machinery is the
only code change (backtest-only).

### Infra notes this session

- Same `run_backtest.py` DB leak as the W4/W5 entry -- every `run_refined_
  sweep_3w6/3w7*.sh` has the `reap_dbs()` + `trap ... EXIT` + per-config reap;
  disk held flat at ~90-91 GB free throughout.
- **scp-vs-running-sweep race**: `scp`'ing the updated `run_backtest.py` while
  W7 was mid-flight let 18 shards of one config (`w7_s15_a06_l06`) import a
  half-written file -> `SyntaxError` -> instant 0-trade fail. Only that one
  config; re-run cleanly afterward (matches its sibling `w7_s18_a06_l06`
  exactly). Lesson: deploy code changes to the VM *before* launching anything
  that imports them, or to a staged path.
- A separate **"W7 Lorentzian" sweep** (user's, `sweep3w7_loren_*`,
  `loren_backtest.py`) was running in parallel and shared the `s3w7_` DB
  prefix + `/tmp/sweep3w7_status.log`. This session's stop x TSL grid was
  re-isolated to `s3w7t_` / `sweep3w7tsl_*` / `/tmp/sweep3w7tsl_status.log`.
  Keep concurrent sweeps on distinct prefixes.

---

## 2026-08-29 (~17:15 IST) — Sweep #3 W4/W5 RESULTS: d_pdt_w65 exit-overlay grid + 4-framework deep entry

All on the e4 VM, `--near-expiry-days 6`, `--exit-mode current`, 18-sharded.
W4 = pure **exit-overlay** re-slices of the SAME 26 `d_pdt_w65` entries
(`orb_conviction` + `{require_prior_day_trend:true, max_or_range_nifty_points:65}`)
— entries never change, only stop/target/trail params. W5 = entry-tightening on
the 4 framework strategies. ~55 configs total across `sweep3w4_exitgrid_*`,
`sweep3w4_exitgrid_rerun_*`, `sweep3w4_exitgrid_extra_*`, `sweep3w4_sl_*`,
`sweep3w5_frameworks_deep_*` (+ `_rerun_*`). Robust bar =
`analyze_walkforward.py` (IS/OOS split at 2026-04-01 == the 6-mo halves at
this n; bootstrap P(mean<=0); 0.3-1.0%/side cost sweep).

### W4 headline — two dominant levers on d_pdt_w65's exit stack

**1. The fixed TARGET is inert; loosening the premium STOP is the big win.**

| variant (only the stop changed) | stop% | win% | E/lot | PF | maxDD | boot P(mean<=0) | slip@1.0% |
|---|---|---|---|---|---|---|---|
| x_baseline_stop10 | -10 | 57.7 | +229 | 2.35 | 1214 | 0.049 | +166 |
| **x_baseline** (current d_pdt_w65) | -12 | 65.4 | +274 | 2.77 | 1517 | 0.028 | +210 |
| x_baseline_stop15 | -15 | 73.1 | +400 | 5.11 | 1660 | 0.002 | +336 |
| **x_stop18** | **-18** | **73.1** | **+456** | **12.11** | **425** | **0.000** | **+392** |

Monotonic: -10 -> -12 -> -15 -> -18 improves E, PF, and P(mean<=0) every step,
IS and OOS both. Mechanism (exit-mix): at -12% the premium stop fired ~4/yr @
0% win, -877 avg, cutting trades that then would have recovered and trailed out
green. At -18% only **1** trade/yr stops out (-425) — `structure_break` (6/yr,
~neutral) + `trail` (14/yr, 100% win, +759) + occasional `target` (4/yr, +479)
carry it; maxDD collapses to ₹425. Effectively "barely stop, let structure/
trail/EOD handle it."

- **Raise target 20% -> 30/40/50% with trail arm held at +12%: byte-identical
  to baseline.** The trail catches everything before +20% is reached, so the
  fixed target is dead weight.
- **BUT the target re-activates once the stop is loose:** `x_stop15_tgt30`
  (stop -15, target +30%) = **+₹515/lot** (65.4% win, PF 4.40, OOS +589,
  +450 @ 1.0% slip, maxDD ₹2282) — the raw-₹ winner. More trades now survive
  long enough to actually hit +30%. Stop and target interact; can't tune one
  without the other.
- **Removing the target entirely (`target_pct:1.0`) ≈ -₹8/lot vs keeping it** at
  any given trail-arm. Genuinely inert, not harmful. Confirmed both directions.

**2. Trail-arm timing is a win-rate <-> expectancy dial (at a fixed stop).**
`trail_activation_fraction` expressed as +% of entry premium where the trail
arms (baseline d_pdt_w65 = +12%):

| trail arms at | win% | E/lot | notes |
|---|---|---|---|
| +4% | 77 | +103 | over-trimmed, barely clears 1% slip |
| +6% | 77-81 | +180-260 | PF 4-10, P(mean<=0) 0.000-0.004 — tightest, most reliable |
| +8% | 73-77 | +190-260 | |
| **+10%** | 65 | +180-190 | **DEAD ZONE** — 65% win *and* low E, P(mean<=0) ~0.07 |
| +12% | 65 | +274-284 | |
| +14% | 65 | +350-373 | late trail, winners run further |

- Once arm >= +10%, win rate reverts to the entry's own ~65% (trail no longer
  intercepts before ~+20%); +10% specifically is worst-of-both.
- **`trail_lock_fraction` 0.4 -> 0.6 is a free gain everywhere** (+₹11-23/lot,
  PF up, P(mean<=0) down) — every batch, every arm point.

### W4 lead candidates (all d_pdt_w65 gate + `--exit-mode current`, n=26)

| goal | params on top of the gate | win% | E/lot | PF | maxDD | P(mean<=0) |
|---|---|---|---|---|---|---|
| **max ₹** | `stop_pct:0.15, target_pct:0.30` | 65 | **+515** | 4.40 | 2282 | 0.003 |
| max ₹, low DD | `stop_pct:0.18` | 73 | +456 | 12.1 | **425** | 0.000 |
| **max win% + robustness** | `stop_pct:0.15, trail_activation_fraction:0.30, trail_lock_fraction:0.6` | **81** | +258 | 9.2 | 364 | **0.000** |
| ditto, no target | + `target_pct:1.0` | 81 | +280 | 9.9 | 364 | 0.000 |

Every W4 cell in the stop15/18 neighbourhood has P(mean<=0) <= 0.013 — the whole
region is solid, unlike the -12% baseline (0.028). Still n=26 → paper-trade to
collect live data, do not size on the backtest. Not production-wired; not
deployed.

### W5 — the other 4 framework strategies, DEEP entry-tightening — ALL DEAD

32 configs (existing constructor knobs only: trend lookback / side-fraction /
crosses / body-ratio / expansion / lookback_bars / sweep-distance / range-width
/ morning-window). Verdict, decisive:

| strategy | best deep-entry variant | verdict |
|---|---|---|
| vwap_pullback | `lb40_side80` ALL +32 but **OOS -57**, dies @0.7% slip, P(mean<=0)=0.41 | negative |
| ema_micro_pullback | `body50` ALL -90 / OOS +17 only; IS win 21-29% everywhere | negative |
| oi_volume_confirmed | `body55` ALL -40 / OOS -52; everything else -96..-258 | negative |
| liquidity_sweep_reversal | `dist10` -104; base OOS **PF 0.05**; whole family PF 0.05-0.34 | negative (worst family) |

Confirms sweep #2/W2. **No framework strategy has a robust edge on current-week
NIFTY weeklies — entry-tightening included. Permanent park, all four.** Same
root cause the ledger already recorded for ORB: "no exit/width tweak fixes a
weak entry" — and here the entries are wrong on 70-80% of trades in-sample.
TSL/exit optimisation was NOT run on the frameworks and won't help: W4 proved
exit tuning *reduces* PnL even on a genuine 65% edge; it redistributes
outcomes, it can't manufacture winners.

### Method / infra note — run_backtest.py leaks its per-run databases

`run_backtest.py` calls `_ensure_backtest_database_exists(suffix)` (CREATE
`trading_bot_backtest_<suffix>`) and **never drops it** — no `DROP DATABASE`
anywhere in the script. Every unique `--db-suffix` leaves a 20-70 MB DB
forever. This session's sweeps + every prior sweep on the e4 VM had
accumulated **2,396 orphaned DBs = 92 GB**, filling the 96 GB disk mid-run
(23/48 configs completed before it hit 100%, the rest failed instantly with
"0 trades / merge FAILED"). Fixed: dropped all `trading_bot_backtest_*` (no
persistent base DB exists — only `postgres`/`template0/1`; the harness
recreates per run from the CSVs in `data/historical/`), then added a
`reap_dbs()` + `trap reap_dbs EXIT` + per-config reap to every sweep driver
(`run_refined_sweep_3w4_*.sh`, `3w5_*`). Disk then held flat at ~91 GB free
across the reruns. **Durable fix still owed: a `try/finally` DROP (or
`--keep-db` opt-out) in `run_backtest.py:main()` itself** — the driver-level
reaper doesn't help ad-hoc invocations or `kill -9`'d runs. One self-inflicted
mistake mid-session: launched two W4 batches sharing the `s3w4_` DB prefix
concurrently, so one batch's start-reap dropped the other's in-flight shard
DBs and corrupted one config (redone). Fix: each concurrent batch now uses a
distinct prefix (`s3w4_`, `s3w4x_`, `s3w4s_`, `s3w6_`).

### Next: sweep #3 W6 — chart-based structure stop (planned, not yet run)

`docs`/`scripts/SWEEP_3W6_STRUCTSTOP_PLAN.md` — anchor the structure-break exit
level to a **swing candle low/high** or **classic floor pivot S1/R1/S2/R2**
(reusing `backtest_pivots.py`) instead of `orb_conviction`'s hard-wired opening-
range boundary. Backtest-only harness change (`--structure-stop-mode` in
`_reconstruct_exit_current`, ~55 lines, no strategy code). Plus one more
framework strategy the user will supply for a W5-style run. Both to run before
the e4 VM terminates (night of 2026-08-31).

---

## 2026-08-29 (~01:00 IST) — Sweep #3 W1/W2/W3 RESULTS (near-week NIFTY + BANKNIFTY probe)

All three run `--near-expiry-days 6` (current expiry week only), `--exit-mode
current`, ALL days/hours, VIX seeded + PCR floored. Robust bar =
`analyze_walkforward.py` (expiry-week sign test, IS/OOS, 6-mo halves, bootstrap
P(mean<=0), 0.3-1.0%/side cost sweep). Every verdict is "paper-trade / park",
never "deploy" — n is 7-47/config.

### W1 — directional-regime filter on ORB — **the one real result of sweep #3**

`sweep3w1_directional_20260828T185752Z`, 13 configs. New gate
`require_prior_day_trend` (CE only above prior-day close + buffer, PE only
below; framework Strategy A's daily trend filter). Baseline `ref_orb_baseline`
= 41 trades, ALL E **-43**/lot PF 0.87.

| config | ALL E | ALL PF | IS E | OOS E | H1/H2 | boot P(mean<=0) | slip@1.0% | n |
|---|---|---|---|---|---|---|---|---|
| baseline | -43 | 0.87 | -129 | +92 | -54/-27 | 0.63 | -118 | 41 |
| **d_pdt_w65** (pdt + maxOR 65) | **+274** | **2.77** | **+267** | **+288** | **+267/+288** | **0.028** | **+210** | 26 |
| d_pdt_skipfri | +108 | 1.53 | +29 | +264 | +29/+264 | 0.18 | +47 | 33 |
| d_pdt_htf | +114 | 1.45 | -19 | +378 | -4/+323 | 0.20 | +40 | 36 |
| d_pdt | +102 | 1.39 | -19 | +342 | -4/+289 | 0.23 | +28 | 36 |
| d_pdt_buf30 | +101 | 1.40 | -2 | +346 | +13/+283 | 0.24 | +25 | 34 |
| d_htf / d_htf_slope10 | +6 | 1.02 | -46 | +88 | — | — | — | 41 |
| d_ce_only (blanket) | **-127** | 0.75 | -181 | -22 | — | — | — | 32 |
| d_drift | -43 | 0.87 | = baseline exactly | | | | | 41 |

- **`require_prior_day_trend` alone** lifts ORB -43 -> ~+100/lot, but the lift
  is **OOS-concentrated** (IS stays ~breakeven). Cuts stops 12->9,
  structure_break 16->12 and its win 31%->17-25%.
- **`d_pdt_w65`** — ORB + `require_prior_day_trend` + `max_or_range_nifty_points:65`
  — is the **first config across all of sweep #3 (1+2+3) to clear the full
  robust bar**: positive in IS *and* OOS *and* both 6-mo halves (+267/+288),
  win 62-67% both halves, bootstrap **P(mean<=0)=0.028** with a *positive*
  5th-percentile (+38), survives to 1.0%/side slippage (+210), maxDD ₹1,517,
  worst streak 2, **every weekday positive** (even Friday +463). The trend gate
  + the wide-OR reject together trim to 26 trades and nearly neutralise the
  structure_break drag (6 exits, 17% win, avg -22).
  - Caveats: **n=26** (18 IS / 8 OOS); expiry-week sign test 17/26, **p=0.17**
    (week-to-week it is noisier than the aggregate); it is a 2-filter stack
    (though each filter is independently motivated — pdt from W1's CE/PE
    finding, maxOR-65 from 3a). max-OR-65 *alone* was noise in 3a; it only
    becomes real *with* the trend gate.
- **`d_ce_only` (blanket "always CE") is negative** (-127) — decisively worse
  than the *conditional* pdt gate. This directly answers the "was ORB
  directionally undisciplined?" question: **yes, and a proper day-over-day
  trend gate fixes it; a blanket long-only does not.**
- **`d_drift` == baseline exactly** — `require_drift_alignment` rejects nothing
  (a breakout is always aligned with the since-9:15 move). Dead lever, drop it.
- `d_htf` alone: marginal (-43 -> +6). `htf_ema_slope_lookback` 5 vs 10: no
  difference.

### W2 — the other 4 framework strategies — **all fail on near-week NIFTY**

`sweep3w2_frameworks_20260828T174254Z`, 24 configs (6/strategy). Smoke gate:
all 4 fire; `vwap_pullback` only on `--underlying-source futures_proxy` (index
feed volume=0 -> VWAP never forms; matches the production `set_volume_proxy`
fix).

| strategy | base ALL E | base PF | best variant | verdict |
|---|---|---|---|---|
| vwap_pullback | **-382** | 0.23 | `trendlb40` +86 (n=16, boot 0.26, dies@1% slip) | 1 lucky param of 6 = noise |
| ema_micro_pullback | -86 | 0.68 | `body05` ALL -90 / OOS +17; sign test 17/47 **p=0.08 (net-negative weeks)** | negative |
| oi_volume_confirmed | -177 | 0.49 | none positive | negative |
| liquidity_sweep_reversal | -243 | 0.21 | `dist10` -104 (n=18); base OOS PF **0.05** | negative |

`w2_*_morning` (morning-only entry window) == base for ema/oi/liq — the
afternoon window was already not contributing. **No framework strategy has a
robust edge on current-week NIFTY weeklies.**

### W3 — BANKNIFTY ORB dip-test — **parked, data too thin**

`sweep3w3_banknifty_20260828T194751Z`, 11 configs. DTE fix confirmed (median 6,
100% monthly-Tue, no cross-dir dup). BANKNIFTY = 12 monthly expiries; current
expiry week => **~9 trades/config/year**. Every "positive" number
(`bn_baseline` ALL +189, `bn_ce_only` +437) rests on 2-4 OOS trades — not
walk-forward-able, not bootstrap-able. Only weak echo of NIFTY: `bn_w280`
(wide OR) is the sole clearly-negative config (-507). **Revisit only with more
expiry history** (or if a weekly BANKNIFTY product returns).

### Paper-trade shortlist (from 3 sweeps)

**1 candidate, noise-grade confidence but the best of everything tested:**
`orb_conviction` +
`{"require_prior_day_trend": true, "max_or_range_nifty_points": 65}`,
**current expiry week only** (NIFTY Wed->Tue), enter 09:15-10:00 IST.
Expected (backtest, net of costs): ~60-65% win, ~+₹200-280/lot, maxDD ~₹1.5k,
~0.5 trades/day. Optional sibling to also paper: add `skip_weekdays:["Friday"]`
(`d_pdt_skipfri`: IS +29 / OOS +264, more trades, slightly lower peak).
**Collect live data — do not size up on the backtest alone (n=26).**

### Park list (re-backtest when more data exists)

- All 4 framework strategies on near-week NIFTY (vwap/ema/oi/liquidity-sweep).
- BANKNIFTY ORB — needs more expiry history.
- Blanket `ce_only`; `require_drift_alignment` (inert); `require_htf_ema_trend`
  alone (marginal); max-OR-width alone (noise without the trend gate — see 3a).
- Everything from sweep #2's "explicitly NOT re-tested" list stays parked.

---

## 2026-08-28 (~21:00 IST) — Part-0 gate for sweep #3: the sweep-#2 "edge" is a DTE artifact

`diagnose_expiry_resolution.py` on the sweep-#2 trade CSVs:

- **100% weekly (Tuesday) expiries** — not monthlies. Good.
- **But DTE median = 13, min 0, max 14; 83% of trades at 10-14 DTE, ~nothing in
  the last 3 days.** `run_backtest.py --all-expiries` iterates one expiry
  DIRECTORY at a time and replays its *entire* multi-day data window; NIFTY
  weeklies are listed ~2 weeks early, ORB fires ~once per directory, and it fires
  early in the window -> every trade is on a ~2-week-out option.
- **Count inflation:** 13 of ~40 trading days produced 2-3 trades, one per
  overlapping expiry directory (the exact overlap `run_backtest.py`'s own
  docstring lines 181-197 describe for `--dates`, present in `--all-expiries` too
  because each directory is its own StrategyRun with its own `_fired_directions`
  latch).

Re-cut of `f_range_tight` (the sweep-#2 "one honest edge"):

| slice | n | win% | net E/lot | PF |
|---|---|---|---|---|
| as-run (sweep-#2 headline) | 38 | 39.5% | +314 | 1.78 |
| dedup 1/day, nearest expiry | 30 | 36.7% | +190 | 1.51 |
| dedup 1/day, farthest expiry | 30 | 36.7% | +418 | 2.13 |
| **DTE <= 7 only** | 8 | 50.0% | **-77** | 0.85 |
| DTE <= 3 only | 1 | 0% | -533 | - |

Farther from expiry = better -> a **time-value / low-gamma effect, not an ORB
edge**. On the current expiry week it is negative. Every other config shows the
same: DTE<=7 slices all negative, dedup-nearest worse than as-run.

**=> Sweeps #1-#2's "f_range_tight is the one honest edge" conclusion does NOT
survive.** It was measured on ~13-DTE options + double-counted days.

**User decision:** backtest **only the current expiry week** — NIFTY: Wed -> next
Tue (0-6 DTE) per weekly; BANKNIFTY: last week before each monthly. Same rule
both.

**Harness fix (`run_backtest.py`, backtest-only):** `--near-expiry-days N` clamps
each `--all-expiries` expiry's replay window to `[expiry_date - N, expiry_date]`.
`N=6` = the NIFTY Wed->Tue week; also collapses the multi-directory overlap
(1 expiry per calendar day). ruff-clean, mypy unchanged, smoke-tested (trades
now at DTE 4-6). Every sweep-#3 run uses `--near-expiry-days 6`.

**Method note going forward:** current-week trade count will be very low
(old DTE<=7 slice was ~8/yr; a fresh near-week run gets a clean latch each
Wednesday so likely somewhat more, but still thin). Formal walk-forward folds are
not viable at that n — use a per-expiry-week sign test + IS/OOS + 6-month halves
(directional only) + bootstrap instead.

### Near-week re-baseline result (`--near-expiry-days 6`, all 52 expiries)

DTE now median 5 / max 6 / min 0, 100% weekly, **zero cross-directory
duplication** — the fix works. Trade count barely dropped (latch resets each Wed):

| current-week, net of costs | n | win% | E/lot | PF | maxDD | Lstrk |
|---|---|---|---|---|---|---|
| baseline (no width filter) ALL | 41 | 43.9% | -43 | 0.87 | 3429 | 4 |
| **w_25_65 ALL** | 34 | 55.9% | **+94** | 1.39 | 2787 | 3 |
| w_25_65 IS | 23 | 56.5% | +132 | 1.72 | 1371 | 3 |
| w_25_65 OOS | 11 | 54.5% | +15 | 1.04 | 2787 | 3 |

Current-week ORB is ~breakeven; the width filter tips it positive with a genuine
55%+ win rate and tiny drawdown. Exit mix: `stop` 6/34 (0% win, -947),
`structure_break` 13/34 (31% win, -136), `trail` 14/34 (100% win, +744),
`target` 1. Wed/Thu best; Friday negative; 10:xx entries negative (-437).
**Gate PASS -> Batch 3a width-ridge sweep launched with `--near-expiry-days 6`.**

### Batch 3a RESULT (21 width bands, current expiry week) -- NO RIDGE, edge is noise

`sweep3a_widthridge_20260828T153202Z`, 21 configs x 18 shards, 77 min.

- **`min_or_range_nifty_points` is completely inert** -- every `w_XX_65` is
  byte-identical for min 15/20/25/30 (NIFTY opening ranges are ~never below
  35 pts, so the floor never binds). The whole "width band" concept collapses
  to a single "reject if OR > X points" filter.
- **The `max` axis is a lone spike at 65, not a ridge:**

  | max OR | n | ALL E | ALL PF | IS E | OOS E | P(mean<=0) | slip@1.0% |
  |---|---|---|---|---|---|---|---|
  | 55 | 17 | -134 | 0.56 | -223 | +27 | 0.83 | -186 |
  | **65** | 34 | **+94** | 1.39 | +132 | +15 | **0.24** | +23 |
  | 75 | 39 | +10 | 1.04 | +11 | +8 | 0.47 | -65 |
  | 85 | 42 | -85 | 0.77 | -184 | +59 | 0.75 | -165 |
  | none (baseline) | 41 | -43 | 0.87 | -129 | +92 | 0.63 | -118 |

- max=65 sits between a strongly-negative neighbour (max=55, -134) and a
  barely-positive one (max=75, +10). Plan's own gate: "a lone spike surrounded
  by negatives = noise." **Fails the ridge test.**
- max=65 on its own: expiry-week sign test **19/34 weeks positive, p=0.61**
  (coin flip); **bootstrap P(mean <= 0) = 0.24**. Not a statistically supported
  edge -- it is the best of 21 noisy point estimates, exactly what chance
  produces when you test 21 configs.
- max=75 shows a clean regime flip: H1 (Sep-Feb) +95/lot PF 1.47, H2
  (Feb-Aug) -112/lot PF 0.72 -- the tell that these are regime artifacts.
- Exit mix unchanged across all 21: `stop` 0% win ~-950, `trail` 100% win
  ~+750, `structure_break` ~30% win slightly negative. Whether the sum is
  +94 or -134 depends on how many trails happened to land = noise at n<=39.

**Conclusion: sweep #1/#2's "f_range_tight is the one honest edge" does NOT
survive.** Once the DTE artifact is fixed (trade the current week, as live
would) and neighbouring width bands are tested, the edge evaporates. Current-week
NIFTY weekly ORB is ~breakeven-to-slightly-negative after costs with no width
parameter that reliably tips it positive. **The problem is ORB entry quality;
no exit/width tweak fixes a weak entry.**

**Batch 3b (stop/target/trail/overlay grid anchored on w_25_65) NOT launched** --
would be optimizing hyperparameters around a noise spike. Decision on the
remaining VM time surfaced to the user (BANKNIFTY dip-test vs. re-testing the
other 4 framework strategies on the near-week-corrected harness vs. a
directional-regime entry filter).

---

## 2026-08-28 (evening, ~19:30 IST) — Refined sweep #2 RESULTS (47 configs, NIFTY)

**Run:** `refined_sweep_20260828T072024Z` on the e4 VM. 47/47 configs, 0
failures, 379 min. Analysed in 5 batches (`analyze_refined_batches.py` +
`analyze_conviction_sweep.py`), OOS = entries >= 2026-04-01.

### Headline
**Still no honestly-profitable config.** Baseline `orb_conviction` (no
gates) = **−₹381/lot ALL, −₹109/lot OOS, PF 0.60 / 0.88**, 46 trades in
the whole year. Only ONE config is positive in BOTH in-sample and
out-of-sample with a single parameter and a real mechanism:
**`f_range_tight`** (accept only opening-range width 25–65 NIFTY pts):
**IS E +₹304 (PF 2.11), OOS E +₹332 (PF 1.51), ALL E +₹314 (PF 1.78)**,
n=38, maxDD ₹6.5k vs ₹21k baseline, worst losing streak 5. Everything
else that looks positive is regime-fit to the Apr–Aug up-drift (negative
or breakeven in-sample).

### The stacked-combo trap (batch 5) — do NOT trust these
`s_A … s_J` all report a big positive ALL expectancy (E +₹250…+₹640) but:
- **IS expectancy is breakeven-to-negative** for every one of them
  (s_A IS −62, s_B −10, s_C +49, s_H −144, s_J −93). The positive ALL
  number is entirely the 11 OOS trades in the up-drift.
- `s_C_B_tstop`, `s_D_C_sb120`, `s_F_C_htf`, `s_G_C_vix18` have
  **byte-identical OOS metrics** (n=11, E 1081.9, PF 5.96) — adding
  sb120 / htf / vix18 on top of s_C changed nothing OOS. The OOS slice
  collapsed to the same 11 trades. Classic overfit signature.
- Every `s_` config funnels 100% of entries into the 09:00 hour
  (10:00 cutoff + the re-qualification-next-bar behaviour), CE-only,
  ex-Tuesday → a very thin, very specific slice. n(total) 31–34.
`s_E_C_strength` is the only stack positive in both halves (IS +478 /
OOS +929) but it stacks 6 filters on 31 trades — maximal overfit surface,
not evidence.

### Per-batch findings

**Batch 1 — exit-timing fidelity (structure-break persistence/buffer,
trail/target/stop tuning).** Entries unchanged (n=46).
- `structure_break_persistence_seconds` 30 & 60 = **byte-identical to
  baseline** (bars are 60 s, so <2-bar persistence never binds). 120
  (3 bars) barely helps (sb-exit win 17%→30%, ALL E −381→−364). 600
  hurts (−446). **Persistence timing is not the lever.** ATR-buffer
  0.5 = identical, 1.0 = slightly worse.
- Exit mix is the core problem: `structure_break` = 23/46 exits, 17%
  win, avg −₹627; `stop` = 11/46, 0% win, avg −₹2460 (the fat tail);
  `trail` = 10/46, **100% win, +₹1595**; `target` = 1/46, +₹9255.
  Winners come only from trail + the single target.
- `f_trail_030` (activate trail at 30% of target): best in batch, still
  ALL −₹368 / OOS +₹96. Converts 5 stops→trails but shrinks trail avg
  ₹1595→₹625.
- `f_target_15` and `f_trail_045` are **catastrophic** (E −₹601): a
  tight 15% target / late 45% trail-activation kills the one big target
  winner. **Never compress the target.**

**Batch 2 — single entry filters.**
- **`f_range_tight` — the one keeper** (see headline). `f_range_wide`
  (max OR 120 pts) is its mirror: E −₹554, PF 0.39, **17-trade losing
  streak** — strong corroboration that wide opening ranges are pure
  noise.
- `g_ce_only`: **worse**, not better (ALL E −403, OOS −249). Sweep #1's
  "CE-only is the biggest clean lever" does **NOT** replicate once VIX
  is seeded and PCR floored. Reversal — de-prioritise.
- `g_cutoff_1000` (no entries after 10:00): ALL −199 (better than −381),
  OOS +38. Mechanism-sound, cheap, mild.
- `g_strength_06` (breakout ≥ 0.6·ATR beyond OR boundary): ALL −200,
  OOS +160 PF 1.21. Mild.
- `g_drift_align`: **byte-identical to baseline** — gate rejected
  nothing (a breakout is essentially always aligned with the day's
  move so far). Dead lever, drop it.
- `g_skip_tue`: ~breakeven ALL (−10). Removes 22 Tuesday trades but
  ADDS ~15 on Wed/Thu/Fri via next-bar re-qualification (net n 46→45),
  concentrating into Wednesday (the one good day). OOS +595 is up-drift.

**Batch 3 — risk overlays (don't touch entries, n=46).** Every one beats
baseline by cutting the left tail; none makes it profitable.
- `r_maxloss_2000` (hard ₹2000/lot cap): ALL E −381→−145, PF 0.60→0.80,
  OOS +129. Tightest cap = best; cuts `structure_break` avg loss
  −627→−166 by capping intrabar excursion. Still ALL-negative.
- `r_tstop_60` / `r_tstop_90` (exit after 60/90 min if not in profit):
  ALL ≈ breakeven (−5 / −38), OOS +489 / +496 PF ~2.5, OOS maxDD → ₹3k.
  But 10-trade losing streak (small chops). IS still −323 / −380.
- `r_maxloss2500_tstop90` combined: lowest maxDD in the whole sweep
  (₹10k), ALL −54, OOS +230 PF 1.40.

**Batch 4 — regime / indicator gates. All net-negative; two are
destructive.**
- **VIX cap is now functional and actively harmful.** `v_vix_max_16`
  OOS E −₹1011 PF 0.21; `v_vix_max_18` OOS −206; `v_vix_max_22` never
  binds (= baseline). Capping VIX removes *winners* — confirms sweep
  #1 finding #8 definitively now that the gate actually works. **A
  "sit out high VIX" rule destroys this strategy.**
- `e_atr_exp` (require ATR expanding at entry): n=19 (rejects 27/46),
  win 16%, PF 0.10, 10-trade losing streak. Same failure mode as the
  abandoned `atr_breakout`. **Kill permanently.**
- `e_htf` (EMA9/EMA20 stacked + slope): ALL −266 (mild help), OOS
  −132. Best regime gate — still net-negative.

**Batch 5 — stacked combos.** See "the stacked-combo trap" above.

### Structural observations (not config-specific)
- **~46 trades / year** at baseline (≈1/week), 38 for `f_range_tight`.
  The framework target of 1–2 trades/day is unreachable with this
  strategy family on 1-min close-only replay — consistent with the
  prior `ema_micro_pullback` under-firing note. Any "expectancy" here
  rests on 11–28 trades per IS/OOS half.
- `ref_orbc_none` == `ref_orb_baseline` exactly → the `orb_conviction`
  subclass wrapper is confirmed neutral when all gates are off. ✅
- **DTE skew**: 41/46 baseline trades are ≥7 calendar days to the
  symbol's embedded expiry — the `--all-expiries` iteration resolves
  mostly monthly-dated option symbols, not the weekly scalp the
  strategy is designed around. Same in sweep #1. Flag for the
  BANKNIFTY run and for any "is this even testing the right
  instrument" question.
- Wednesday is the only positive day-of-week across nearly every
  config (baseline Wed E +₹1888, n=8). Tuesday (expiry) is the worst
  (E −₹769, n=22). Thin, but stable across the sweep.

### What to carry into the next run
1. **Anchor on `f_range_tight`.** Next sweep: vary ONLY the OR-width
   band (e.g. 20–50 / 25–65 / 30–75 / 30–90 / 35–100) to check 25–65
   is a plateau, not a lucky point. Add a walk-forward (rolling 3-mo
   train / 1-mo test) since the IS/OOS single split is too coarse at
   this n.
2. Layer `r_maxloss_2000` and/or `r_tstop_90` on `f_range_tight` — pure
   tail/chop control, mechanism-clean, helps any variant.
3. **Drop for good:** VIX caps, ATR-expansion gate, target compression
   (`f_target_15`), `g_drift_align`, CE-only.
4. Treat every `s_` stack result as non-evidence. If a combo matters,
   it must show a positive IS expectancy too.
5. BANKNIFTY: only after the OR-width follow-up. Expect even thinner n
   (monthly expiries only in the archive).

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
