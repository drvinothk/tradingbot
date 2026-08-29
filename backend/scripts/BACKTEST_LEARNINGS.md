# Backtest learnings ledger — conviction-strategy refinement

Append-only. Every entry timestamped (IST). Newest first.
All PnL figures are **per 1 lot, net of realistic costs** (flat ₹40/lot +
0.04% turnover + 0.1% STT + **0.5%/side premium slippage**) unless stated
"gross". `run_backtest.py --exit-mode current` models zero costs itself —
costs are applied only in analysis.

---

## CANONICAL RELIABLE-BACKTEST SETUP (read this first — don't rediscover it)

**The one invocation that gives trustworthy numbers** (per-strategy sharded,
all 52 NIFTY weekly / 12 BANKNIFTY monthly expiries):

```
./.venv/bin/python scripts/run_backtest.py \
  --strategy <type> --underlying NIFTY \
  --all-expiries --options-subdir options_1min_past \
  --underlying-source alice_index \
  --exit-mode current --fast \
  --near-expiry-days 6 \
  --strategy-params '<json>' \
  --shard-count 18 --shard-index <i> \
  --db-suffix <unique> --out-csv <dir>/<name>_s<i>.csv
```
then `merge_backtest_shards.py --glob '<dir>/<name>_s*.csv' --out <dir>/<name>_current.csv`.

**Non-negotiables, each learned the hard way:**
- **`--near-expiry-days 6` is mandatory.** Without it, `--all-expiries` replays
  each weekly from its ~2-week listing day, so ORB trades land at 10–14 DTE
  (not the current week a live run trades) AND the same calendar day is traded
  2–3× across overlapping expiry directories. `6` = the NIFTY Wed→Tue week;
  also collapses the multi-directory overlap. Every sweep-#1/#2 "edge" was an
  artifact of omitting this.
- **`--exit-mode current`** = the faithful exit stack (see below). `legacy` /
  `target_mult` had a 26× PnL-scale bug (fixed) and model close-only fills.
- **`--underlying-source alice_index`** for everything EXCEPT `vwap_pullback`,
  which needs **`futures_proxy`** — the index feed reports volume=0 so VWAP
  never forms (matches the production `set_volume_proxy` gap). Note:
  futures_proxy also swaps the *price* series to the future (~index + basis;
  negligible for breakout logic, real for anything level-sensitive).
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
BANKNIFTY current-week ORB is ~9 trades/yr — too thin to backtest a strategy.

**Robustness bar for any candidate** (`analyze_walkforward.py`): positive in
IS *and* OOS *and* both 6-month halves; bootstrap P(mean≤0) ≤ ~0.15 with a
non-negative 5th-percentile; survives 1.0%/side slippage; expiry-week sign
test as supporting (not decisive — n is small). At n≈20–40, every pass is
"paper-trade to collect live data", never "deploy".

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
