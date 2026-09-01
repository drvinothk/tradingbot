# Backtest learnings ledger — conviction-strategy refinement

Append-only. Every entry timestamped (IST). Newest first.
All PnL figures are **per 1 lot, net of realistic costs** (flat ₹40/lot +
0.04% turnover + 0.1% STT + **0.5%/side premium slippage**) unless stated
"gross". `run_backtest.py --exit-mode current` models zero costs itself —
costs are applied only in analysis.

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
