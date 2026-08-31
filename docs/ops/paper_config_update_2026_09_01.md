# Paper-trading conviction-config update — 2026-09-01

Plan + QC record for updating the three surviving conviction `strategy_configs`
on the live OCI box (`144.24.137.112`, DB `trading_bot`) for a multi-leg paper
run. Every change below has a recorded reason; every reason is either a
backtest number from the 2026-09-01 triage (626 configs, post-`--near-expiry-days 6`)
or a code path traced in this repo.

**Nothing here is a code change.** It is `strategy_configs.params` only, plus
two `is_enabled` flips. No migration, no deploy, no restart required — the
runner re-reads `params` when a run is started, and `resolve_qty_lots` is
re-resolved every cycle.

---

## 0. Preconditions checked before writing this plan

| check | result | why it matters |
|---|---|---|
| running `strategy_runs` (not stopped/completed) | **0** | a live runner holds an in-memory `Strategy` built from the *old* params; changing the row under it would leave DB and runtime disagreeing until restart |
| open `positions` | **0** | no position is currently being managed by a stop/target/trail derived from the old params |
| active `risk_limit_configs` | v3, `per_trade_lot_cap = 1`, `max_concurrent_positions = 2`, `max_trades_per_day = 25` | the lot cap is the binding constraint on the `qty_lots` decision below |

Both zeros make this a safe window. If either is non-zero when the change is
actually applied, stop the run first.

---

## 1. Changes, per config

### 1.1 `ORB_Conviction` — `76b61473-075f-4b59-bb31-ab985195f255`

| field | from | to | reason |
|---|---|---|---|
| `orb_entry_cutoff_time` | `"10:00"` | `"10:15"` | see §2.1 |
| `exit_legs` | *(absent)* | 3 legs, 0.4/0.3/0.3 | see §2.2 |
| everything else | — | unchanged | already the best backtested config (`w7_s18_a12_l06`: n=26, 76.9% win, +₹520/lot, PF 16.70, maxDD ₹395, P(mean≤0)=0.000, 8/8 gates) |

### 1.2 `OI_Volume_Conviction` — `c10bd223-5fd9-4f60-b985-b97ed4d0ff8c`

| field | from | to | reason |
|---|---|---|---|
| leg `trail_activation_fraction` | `0.5` | `0.30` | see §2.3 |
| leg `trail_lock_fraction` | `0.8` (all legs) | `0.6 / 0.8 / 0.85` | see §2.2 |
| leg 1 `stop_pct` | `0.17` | `0.11` | leg 1 must match leg 0 in everything but lock, or the lock A/B is confounded (§2.2) |
| leg 2 `stop_pct` | `0.09` | `0.17` | the wide-stop leg moves to slot 2, keeping a stop spread in the config |
| top-level `stop_pct` / `target_pct` / arm / lock | *(absent)* | `0.11 / 0.18 / 0.30 / 0.85` | see §2.4 — this is the LIVE/collapsed path |
| `qty_lots` | `10` | *(removed)* | see §2.5 |

### 1.3 `EMA_Micro_Conviction` — `239f70ab-c1d7-4f24-9c2e-dd19950f32fc`

| field | from | to | reason |
|---|---|---|---|
| leg `trail_activation_fraction` | `0.5` | `0.70` | see §2.3 |
| leg `trail_lock_fraction` | `0.8` (all legs) | `0.6 / 0.8 / 0.8` | see §2.2 |
| leg 1 `stop_pct` | `0.06` | `0.12` | matches leg 0 so the lock A/B is clean (§2.2) |
| leg 2 `stop_pct` | `0.08` | `0.06` | the tight-stop leg moves to slot 2 |
| top-level `stop_pct` / `target_pct` / arm / lock | *(absent)* | `0.12 / 0.12 / 0.70 / 0.8` | see §2.4 |
| `qty_lots` | `10` | *(removed)* | see §2.5 |

### 1.4 Disables

| config | id | reason |
|---|---|---|
| `EMA_Micro_Conviction_PCR` | `23e93cc0-…` | see §2.6 |
| `VWAP_Conviction` | `93dd1a03-…` | 0 of 113 configs clear the 8-gate bar. Best (`g1_s06_t50`) has E +250/lot but goes to **−₹91/lot** once its two best trades are removed — its entire P&L is 3 trades deep. |
| `Liquidity_Sweep_Conviction` | `1baf3381-…` | **already disabled — no action.** 0 of 92 pass, including Groups C/D/E; every config IS-negative. |

---

## 2. Reasons

### 2.1 ORB entry cutoff `10:00` → `10:15`

The original `10:00` traces to a near-week-rebaseline note that 10:xx entries
were worth −₹437/lot. That was measured on the **`w_25_65` entry set**, not the
gate actually deployed. Re-checked across every ORB entry set, the sign of the
10:xx bucket is not stable:

| entry set | 10:xx trades | their net |
|---|---|---|
| `d_pdt_w65` + deployed exit *(the live gate)* | 3 | **+1,839** |
| `d_pdt_w65`, bare exits | 3 | −443 |
| `d_pdt` (no width filter) | 6 | +1,808 |
| `w_25_65` (no PDT) — *source of the original claim* | 4 | −1,629 |
| `ref_orb_baseline` | 6 | −1,379 |

The same handful of trades flips sign with the exit stack applied, which is what
noise looks like at this n. On the deployed gate the cutoff ladder is:

| cutoff | n | win | E/lot | total | PF |
|---|---|---|---|---|---|
| 09:45 | 16 | 75.0% | +387 | 6,194 | 9.62 |
| 10:00 *(current)* | 23 | 78.3% | +508 | 11,693 | 16.81 |
| **10:15** | 25 | **80.0%** | **+546** | **13,655** | **19.46** |
| none | 26 | 76.9% | +520 | 13,532 | 16.70 |

**This is deliberately NOT justified by the +₹38/lot.** 10:15 drops exactly one
trade (10:16, −122), so that improvement is a single data point and must not be
treated as an edge. The actual argument is distributional: all 26 entries fall
between **09:32 and 10:16**, 23 of them (88%) before 10:00. A 10:15 cap is a tail
guard that is nearly inert on the observed distribution; 10:00 cuts into the live
body of it, and its benefit reverses across gates. `10:15` is also
`ORBStrategy`'s own constructor default, so this is a revert to default rather
than a tuned number.

### 2.2 Leg design — legs 0 and 1 identical except `trail_lock_fraction`

Requested split is 4/3/3 lots. `allocate_leg_lots(10, [0.4, 0.3, 0.3])` returns
`[4, 3, 3]` exactly (verified), so the fractions are 0.4/0.3/0.3.

The lock ladder is monotonic with **no turning point** and, critically, **win
rate and maxDD are byte-identical at every lock level** on all three strategies
— only the size of the winners moves:

| | lock 0.6 | lock 0.8 | lock 0.85 | lock 0.9 | win% / maxDD across the ladder |
|---|---|---|---|---|---|
| ORB `d_pdt_w65` arm .12 | +520 | +538 | — | +547 | 76.9% / ₹395 — unchanged |
| OI `o3_atr_pcrl` arm .30 | +257 | +271 | +274 | +278 | 71.4% / ₹539 — unchanged |
| EMA `e_pdt_atr` arm .70 | +294 | +304 | — | — | 58.3% / ₹977 — unchanged |

That flatness is exactly *why* a live test is needed: on 1-minute bars a tighter
lock can never turn a winner into a loser, because the sim cannot see the
sub-minute adverse wick that a 0.8 lock (20% give-back room) is half as protected
against as a 0.6 lock (40%). The backtest therefore says "lock higher is free",
and it says so for a reason that is an artifact of bar resolution.

So **legs 0 and 1 are identical in every field except `trail_lock_fraction`
(0.6 vs 0.8)** — a clean, attributable A/B inside a single position, on the one
exit question the data structurally cannot answer. Leg 2 is the "as required"
leg and varies exactly one *other* thing, so it cannot confound the lock test:

| strategy | leg 0 (4 lots) | leg 1 (3 lots) | leg 2 (3 lots) | leg 2 asks |
|---|---|---|---|---|
| ORB | stop .18, runner, lock **0.6** | stop .18, runner, lock **0.8** | stop .18, **target 0.40**, lock 0.6 | does a hard target beat pure trailing? (backtest: +717 vs +520) |
| OI | stop .11, lock **0.6** | stop .11, lock **0.8** | **stop .17**, lock 0.85 | does a wider stop help? |
| EMA | stop .12, lock **0.6** | stop .12, lock **0.8** | **stop .06**, lock 0.8 | does a tight stop help? |

`use_structure: true` on every leg — each leg inherits the signal's own
structure-break level, matching the backtest, where the structure-break exit was
always active.

### 2.3 Trail-arm: OI `0.5 → 0.30`, EMA `0.5 → 0.70`

`trail_activation_fraction` is the strongest exit lever measured anywhere in the
archive, and the two families run in **opposite directions** on it:

| arm | OI `o3_atr_pcrl` (lock .80) | EMA `e_pdt_atr` (lock .80) |
|---|---|---|
| 0.30 | **+271** — 8/8 gates | +133 — 5/8 |
| 0.40 | +204 — 7/8 | +155 — 7/8 |
| 0.50 *(deployed)* | *≈ +185 interpolated* | *≈ +205 interpolated* |
| 0.60 | +165 — 3/8 | +261 — 7/8 |
| 0.70 | +219 — 4/8 | **+304 — 8/8** |

OI's 0.30 is a measured peak (tested down to 0.10 and up to 0.70; falls away both
sides). EMA's 0.70 is the top of a monotonic rise and is corroborated by its
sibling gate `e3_pdt_atr_pcrl`, which prefers the same direction. Mechanism, from
the exit-reason breakdown: arming the trail later leaves the `stop` /
`structure_break` / `target` buckets byte-identical and only lengthens the `trail`
bucket — i.e. it stops the trail strangling winners in their first few minutes.

Deployed `0.5` sits on the wrong side of the peak for both. Gap ≈ **₹85/lot/trade
(OI)** and **₹100/lot/trade (EMA)**.

### 2.4 Top-level exit params added to OI and EMA — NOT redundant

`execution_engine/paper/exit_legs.build_position_exit_legs` returns `None`
(collapsing to the legacy single-exit path, with a one-time `exit_legs_collapsed`
alert) in two cases:

1. `is_live` — **multi-leg staged exit is not supported for LIVE positions at all**;
2. the position has too few lots to give every leg ≥ 1.

The collapsed path reads the **top-level** `stop_pct` / `target_pct` /
`trail_activation_fraction` / `trail_lock_fraction`. Today OI and EMA set none of
them, so a collapsed or live position silently falls back to the strategy class
constructor defaults:

| | class default | tuned value | gap |
|---|---|---|---|
| OI arm / lock | 0.5 / **0.5** | 0.30 / 0.85 | both wrong |
| EMA arm / lock | 0.5 / **0.5** | 0.70 / 0.80 | both wrong |
| OI stop / target | 0.11 / 0.18 | 0.11 / 0.18 | already correct |
| EMA stop / target | **0.08** / 0.12 | 0.12 / 0.12 | stop wrong |

So this is a **latent bug being closed, not a new setting**: any live OI/EMA trade
today would exit on untuned defaults. Targets are pinned to the class-default
values (OI 0.18, EMA 0.12) because those are the values the backtests themselves
ran under — writing them explicitly protects against a future default change
silently altering a validated config.

ORB already carries all four top-level values, and they are exactly
`w7_s18_a12_l06`. No change needed there.

### 2.5 `qty_lots: 10` removed from OI and EMA — and NOT added to ORB

Intent is 10 lots in paper, 1 lot live. **Omitting `qty_lots` delivers exactly
that**; setting it explicitly breaks the live half.

`strategy_engine/sizing.resolve_qty_lots`: an explicit `params["qty_lots"]`
*always* wins, in both modes. It is not mode-aware. The mode-aware default only
applies when the key is absent — `DEFAULT_QTY_LOTS_PAPER = 10`,
`DEFAULT_QTY_LOTS_LIVE = 1`.

`risk_engine/service.evaluate_trade_intent`:

```python
per_strategy_cap  = resolve_qty_lots(strategy_config, trading_session, strategy_run)
effective_lot_cap = min(risk_config.per_trade_lot_cap, per_strategy_cap)
if is_live and trade_intent.qty_lots > effective_lot_cap:
    reasons.append("per_trade_lot_cap_exceeded")
```

With the live config's `per_trade_lot_cap = 1`: `min(1, 10) = 1`, intent carries
10, `10 > 1` → **rejected**. It rejects; it does not clamp. That is the same
failure `sizing.py`'s own docstring records from 2026-08-28. Confirmed by
`tests/integration/test_risk_engine.py::test_per_trade_lot_cap_allows_a_strategys_own_configured_qty_lots`,
which has to raise the workspace cap to 5 before a `qty_lots: 5` strategy is
allowed through.

The 2026-08-28 fix that made the live default work keyed the *default* off
`is_strategy_routed_live` instead of the retired `StrategyStatus`. It did not
change the behaviour of an *explicit* `qty_lots`. So the live 1-lot path is
indeed fixed — but only for configs that leave the key unset.

Paper behaviour is unchanged by the removal: absent → `DEFAULT_QTY_LOTS_PAPER`
= 10 → `[4, 3, 3]`.

### 2.6 `EMA_Micro_Conviction_PCR` disabled — it is a strict subset

Trade-by-trade diff of the two EMA gates at the same arm/lock:

```
shared trades: 7    only-in-plain: 5    only-in-PCR: 0
PCR set is a strict subset of the plain set: True
```

It is not a second strategy; it is `EMA_Micro_Conviction` with a filter on top.
Over the year that filter removed 5 trades netting **+₹934**:

| trade | exit | net |
|---|---|---|
| 2025-12-31 26100CE | trail | +603 |
| 2026-02-04 25800PE | structure_break | −435 |
| 2026-03-11 23800PE | structure_break | −346 |
| 2026-04-22 24200PE | structure_break | −196 |
| 2026-05-27 23800CE | trail | +1,308 |

It cut 3 losers but also both of the biggest winners. Total drops 3,651 → 2,717.
E/trade *rises* (304 → 388) only because it deleted more trades than it should
have. Running both means the PCR config fires only on days the plain one also
fires — double size on one signal, no diversification, and the worse of the two
on sample size (n=7 vs 12) and gates (7/8 vs 8/8).

---

## 3. QC — side effects and regressions

Automated checks (`scratchpad/qc_configs.py`, run against this repo's live
imports — not a reimplementation):

| # | check | result |
|---|---|---|
| 1 | every top-level key in that `strategy_type`'s API allowlist | **PASS** — 7/9/6 keys, all forwarded, none silently inert |
| 2 | `exit_legs` through `deserialize_exit_leg_templates` + `validate_exit_leg_templates` | **PASS** — 3 legs each, `qty_fraction` sums to exactly 1.0 |
| 3 | no leg key silently dropped by `ExitLegTemplate._filtered()` | **PASS** |
| 4 | `_build_strategy` constructs with the forwarded params | **PASS** — no `TypeError`; `orb_entry_cutoff_time` parses to `time(10, 15)` |
| 5 | diff vs what is deployed | only the intended fields differ |
| 6 | `allocate_leg_lots` at real sizes | **PASS** — 10 lots → `[4, 3, 3]`; 1 lot → collapses (below) |

### Side effects considered

**S1 — a non-allowlisted param is stored but never forwarded.**
`api/v1/strategies._build_strategy` filters `params` through a per-type key set;
anything outside it is persisted inertly and silently does nothing. Checked
explicitly (check 1); all keys are inside their allowlist. This is why
`qty_lots` removal is safe to reason about and why a typo here would be silent
rather than loud.

**S2 — live positions.** Multi-leg exits are **already paper-only in code**
(`is_live` → collapse + `exit_legs_collapsed` alert). Adding `exit_legs` to these
configs therefore cannot change any live behaviour at all. The live path is
governed entirely by the top-level params — which is precisely why §2.4 matters.

**S3 — small paper positions.** At < 3 lots a leg rounds to 0
(`allocate_leg_lots(2, [.4,.3,.3]) = [1,0,1]`) and the guard collapses to the
single-exit path with an alert. Expected and handled; paper runs at 10 lots so
this only fires if `qty_lots` is later lowered.

**S4 — partial fills.** `build_position_exit_legs` also collapses when
`total_lots * lot_size != filled_qty`. Unchanged by this plan; noted so an
`exit_legs_collapsed` alert on a partial fill is not mistaken for a config error.

**S5 — reconciliation / stop plans.** Legs create `position_exit_legs` rows and
the leg-aware branch in `evaluate_open_position`/`close_position`. ORB is the
only config gaining legs where none existed, so it is the largest behavioural
change in this set and the one to watch first in the paper log.

**S6 — a running strategy would not pick this up.** The `Strategy` object is
built at `start_strategy` from `params`. Verified 0 running runs and 0 open
positions, so there is nothing to desynchronise. `resolve_qty_lots` is the
exception — it is re-resolved every cycle (`runner.run_cycle`), so the `qty_lots`
removal would take effect mid-run even if one were live.

**S7 — Telegram noise.** `exit_legs_collapsed` is a new alert category that will
fire once per collapsed position. Expected on any live trade (S2). Not a fault.

**S8 — `max_trades_per_day = 25` / `max_concurrent_positions = 2`.** Unchanged.
Three enabled conviction strategies at ~1 trade each per day sit well inside
both. Note `max_concurrent_positions = 2` can still block a third simultaneous
entry; that is existing, intended behaviour, not a regression from this change.

### Regressions explicitly ruled out

- Paper lot count is unchanged at 10 (§2.5).
- Live sizing is unchanged at 1 lot, and now actually passes the risk cap.
- ORB's entry gate, stop, target and arm are untouched; only the cutoff moves,
  by 15 minutes, to the code default.
- No schema, migration, dependency, or code change. Rollback is restoring the
  previous `params` JSON for three rows and re-enabling two.

### Known, accepted limitations

- **The multi-leg engine itself has no backtest coverage.** All per-leg numbers
  quoted here come from three separately-backtested *single-leg* runs. A real
  3-leg position shares one entry and its structure-break exit fires on all legs
  at once, so the live blend will not reproduce the arithmetic blend. Measuring
  that is the point of the run.
- Every candidate rests on n = 12–26 trades from one year of 1-minute option
  premiums. This is a data-collection run, not a sizing decision.
- ORB's `max_or_range_nifty_points: 65` has still never been re-swept with
  `require_prior_day_trend` on (`d_pdt_w55` / `w75` / `w85` do not exist). That
  remains the single open risk to the whole ORB result — a ~20-minute, 4-config
  sweep. Unrelated to this config change, but it gates any decision to size up.

---

## 4. Apply

Pre-flight: re-confirm 0 running runs and 0 open positions (see §0), then:

```sql
BEGIN;

UPDATE strategy_configs SET params = '{"require_prior_day_trend":true,"max_or_range_nifty_points":65,"orb_entry_cutoff_time":"10:15","stop_pct":0.18,"target_pct":1.0,"trail_activation_fraction":0.12,"trail_lock_fraction":0.6,"exit_legs":[{"kind":"core","qty_fraction":0.4,"stop_pct":0.18,"use_structure":true,"trail_activation_fraction":0.12,"trail_lock_fraction":0.6,"no_target":true},{"kind":"runner","qty_fraction":0.3,"stop_pct":0.18,"use_structure":true,"trail_activation_fraction":0.12,"trail_lock_fraction":0.8,"no_target":true},{"kind":"target","qty_fraction":0.3,"stop_pct":0.18,"use_structure":true,"trail_activation_fraction":0.12,"trail_lock_fraction":0.6,"target_pct":0.4}]}'::jsonb, updated_at = now()
  WHERE id = '76b61473-075f-4b59-bb31-ab985195f255';

UPDATE strategy_configs SET params = '{"oi_use_futures_volume_confirmation":false,"oi_use_atm_oi_buildup":false,"require_atr_expansion":true,"pcr_oi_min":0.4,"pcr_oi_max":2.5,"stop_pct":0.11,"target_pct":0.18,"trail_activation_fraction":0.3,"trail_lock_fraction":0.85,"exit_legs":[{"kind":"core","qty_fraction":0.4,"stop_pct":0.11,"use_structure":true,"trail_activation_fraction":0.3,"trail_lock_fraction":0.6},{"kind":"runner","qty_fraction":0.3,"stop_pct":0.11,"use_structure":true,"trail_activation_fraction":0.3,"trail_lock_fraction":0.8},{"kind":"wide","qty_fraction":0.3,"stop_pct":0.17,"use_structure":true,"trail_activation_fraction":0.3,"trail_lock_fraction":0.85}]}'::jsonb, updated_at = now()
  WHERE id = 'c10bd223-5fd9-4f60-b985-b97ed4d0ff8c';

UPDATE strategy_configs SET params = '{"require_prior_day_trend":true,"require_atr_expansion":true,"stop_pct":0.12,"target_pct":0.12,"trail_activation_fraction":0.7,"trail_lock_fraction":0.8,"exit_legs":[{"kind":"core","qty_fraction":0.4,"stop_pct":0.12,"use_structure":true,"trail_activation_fraction":0.7,"trail_lock_fraction":0.6},{"kind":"runner","qty_fraction":0.3,"stop_pct":0.12,"use_structure":true,"trail_activation_fraction":0.7,"trail_lock_fraction":0.8},{"kind":"tight","qty_fraction":0.3,"stop_pct":0.06,"use_structure":true,"trail_activation_fraction":0.7,"trail_lock_fraction":0.8}]}'::jsonb, updated_at = now()
  WHERE id = '239f70ab-c1d7-4f24-9c2e-dd19950f32fc';

UPDATE strategy_configs SET is_enabled = false, updated_at = now()
  WHERE id IN ('23e93cc0-efa2-4dc3-9b35-c04894fa812c',
               '93dd1a03-0d3b-4bc8-9600-9745375693b2');

COMMIT;
```

Take a `params` snapshot of the three rows before running, for rollback.

Applying through the UI/API instead is equally valid and marginally safer — the
create/update endpoint runs `validate_exit_leg_templates` server-side and 422s on
a bad payload. The same validator was run offline here (check 2), so both routes
land identically.

## 5. What to watch in the first paper sessions

1. `position_exit_legs` rows actually created — 3 per position, `[4, 3, 3]` lots.
2. Any `exit_legs_collapsed` alert — expect none in paper at 10 lots; one per
   live trade is normal (S2).
3. Per-leg realised P&L for legs 0 vs 1 on ORB — that is the lock 0.6 vs 0.8
   answer, and the only reason the split is shaped this way.
4. Whether ORB's `target` leg (leg 2) fills at +40% more often than the runners
   trail out, which is the backtest's +717-vs-+520 claim meeting reality.

---

## 6. APPLIED — 2026-09-01, ~02:25 IST

Applied directly to the live OCI database (`144.24.137.112`, `trading_bot`) in a
single transaction. **No code change, no migration, no deploy, no restart.**

**Pre-flight, re-checked immediately before the write:**
`running_runs = 0`, `open_positions = 0`, `pending_orders = 0`.

**Result:** `UPDATE 1 / UPDATE 1 / UPDATE 1 / UPDATE 2`, `COMMIT`, exit 0 — 5 rows.

**Rollback snapshot** of the prior `params` / `is_enabled` for all 5 touched rows:
`docs/ops/paper_config_rollback_2026_09_01.txt` (also kept on the session
scratchpad as `rollback_20260901T022204.txt`).

### Post-apply verification

Configs were **read back out of the database** and re-validated against the live
code — not assumed from the write. `scripts/qc_paper_configs_live.py`:

| check | ORB | OI | EMA |
|---|---|---|---|
| runtime_mode = `force_paper` | OK | OK | OK |
| API allowlist (nothing inert) | 7/7 | 9/9 | 6/6 |
| `validate_exit_leg_templates` | OK, sum 1.0 | OK, sum 1.0 | OK, sum 1.0 |
| no leg key dropped | OK | OK | OK |
| `_build_strategy` constructs | OK | OK | OK |
| sizing | paper 10 / live 1 | paper 10 / live 1 | paper 10 / live 1 |
| `allocate_leg_lots(10)` | `[4, 3, 3]` | `[4, 3, 3]` | `[4, 3, 3]` |
| matches this plan | OK | OK | OK |
| lock A/B uncofounded (legs 0,1 differ only in lock) | OK | OK | OK |

`EMA_Micro_Conviction_PCR`, `VWAP_Conviction`, `Liquidity_Sweep_Conviction`
confirmed `is_enabled = false`.

**`LIVE QC RESULT: ALL CHECKS PASSED`**

### Lot sizing — no code fix was needed

Requirement was live 1 lot / paper 10 lots. Removing the explicit
`qty_lots: 10` achieves exactly that through the existing mode-aware default
(`DEFAULT_QTY_LOTS_PAPER = 10`, `DEFAULT_QTY_LOTS_LIVE = 1`), and it is now
verified against the live rows. With live sizing at 1 lot the risk check is
`min(per_trade_lot_cap=1, resolved=1) = 1` and the intent carries 1, so
`1 > 1` is false — it passes rather than being rejected. No change to
`sizing.py` or `risk_engine` was required or made.

### Other verification

- **No restart needed.** `auto_spawner.spawn_enabled_strategies` queries
  `StrategyConfig.is_enabled.is_(True)` from the DB on every run and
  `_build_strategy` reads `params` at spawn time; there is no in-process cache of
  strategy configs. Tomorrow's 09:00 IST bootstrap picks these up as-is.
- **Session state:** newest `trading_sessions` row is `paper_only` / `active`.
- `pytest -k "exit_leg or qty_lots or lot_cap or sizing"` → **53 passed**.
- `ruff check` clean on both new scripts. The 16 repo-wide `ruff` findings are
  all in pre-existing untracked backtest/analysis scripts
  (`analyze_phase*.py`, `fetch_today_replay_data.py`, …), none in this work.
- `mypy` is not run over `scripts/` in CI (`mypy app tests`), consistent with
  every existing script in that directory.

### Known, accepted — not changed on request

Four unrelated stub configs remain enabled and will auto-spawn alongside the
conviction set (`Bank nifty`, `Test `, `Test 1`, `Test 4`). `Test 1` has a NULL
`runtime_mode`, so it follows the session rather than being pinned to paper —
harmless while the session is `paper_only`, but it would route real money if the
master switch is ever flipped to live. They also compete for
`max_concurrent_positions = 2`. Left as-is per explicit instruction.

### Not done — stopped before save and sync

Working tree contains, uncommitted and undeployed:
`docs/ops/paper_config_update_2026_09_01.md`,
`docs/ops/paper_config_rollback_2026_09_01.txt`,
`backend/scripts/qc_paper_configs.py`,
`backend/scripts/qc_paper_configs_live.py`.

---

## 7. REVISION 1 — 2026-09-01, ~02:45 IST

Prompted by a review question ("two exits at 0.6 — should one test 0.4?"). The
duplicated lock values in ORB and EMA turned out to be correct and load-bearing,
but the same check found a real design error in OI.

### Why the duplicate lock values are deliberate

Each leg must differ from a reference leg in **exactly one** field, or the
comparison cannot be attributed. Pairwise audit of the 3-leg design as applied
in §6:

| pair | varies | verdict |
|---|---|---|
| ORB core vs runner | lock 0.6 → 0.8 | clean |
| ORB core vs target | target only | **clean *because* both are lock 0.6** |
| EMA core vs runner | lock 0.6 → 0.8 | clean |
| EMA runner vs tight | stop only | **clean *because* both are lock 0.8** |
| OI core vs wide | stop **and** lock | **CONFOUNDED** |
| OI runner vs wide | stop **and** lock | **CONFOUNDED** |

Setting ORB's third leg to lock 0.4 would have made its target test vary two
fields at once, destroying the second comparison. The repeat is what buys it.

### R1.1 OI leg 2 lock `0.85 → 0.80` — fixes a confound

OI's wide-stop leg sat at lock 0.85 while legs 0/1 were 0.6/0.8, so **neither**
comparison against it was single-variable. Moving it to 0.80 makes
`runner vs wide` a clean stop test (0.11 → 0.17), matching the shape ORB and EMA
already had. Cost is ~₹3/lot of backtested E (lock .80 = +271 vs .85 = +274) —
paid to make the leg readable.

The **top-level** `trail_lock_fraction` stays **0.85**: that is the LIVE/collapsed
single-exit path (§2.4), where the best backtested value applies and there is no
A/B to keep clean. Leg-level and top-level differing here is intentional.

### R1.2 ORB gains a 4th leg at lock 0.40 — a 3-point lock ladder

Adding 0.4 is worth it **on ORB only**:

| | lock 0.4 backtested? | trades/yr |
|---|---|---|
| ORB `d_pdt_w65` arm .12 | **yes** — n=26, 76.9% win, E +502.6, drop-2 +379.6, PF 16.16, maxDD ₹395, **8/8 gates** | 26 |
| OI `o3_atr_pcrl` arm .30 | no — grid was 0.6/0.7/0.8 only | 14 |
| EMA `e_pdt_atr` arm .70 | no — same | 12 |

ORB has twice the trade flow of either other strategy and 0.4 already clears the
full 8-gate bar there, costing only ₹18/lot vs 0.6. On OI/EMA a fourth leg would
split 12–14 trades/yr four ways *and* run an unbacktested lock value — so they
stay at three legs.

Two lock points give a direction; three give a shape. Since the whole reason for
testing lock live is a mechanism the 1-min sim cannot see (sub-minute adverse
wicks), and 0.4 is where that protection is largest (60% give-back cushion vs
40% at 0.6, 20% at 0.8), the extreme is the informative point.

New ORB leg set — `qty_fraction` 0.3/0.3/0.2/0.2 →
`allocate_leg_lots(10, ...) = [3, 3, 2, 2]` (verified):

| leg | lots | stop | target | lock | vs `core` |
|---|---|---|---|---|---|
| `core` *(anchor)* | 3 | .18 | none | **0.6** | — |
| `runner` | 3 | .18 | none | **0.8** | lock only |
| `tightlock` | 2 | .18 | none | **0.4** | lock only |
| `target` | 2 | .18 | **0.40** | 0.6 | target only |

Every leg differs from the anchor in exactly one field — three independent
single-variable comparisons from one position.

### Applied + verified

`UPDATE 1 / UPDATE 1`, `COMMIT`, exit 0 (ORB and OI; EMA untouched). Configs read
back from the database and re-run through `scripts/qc_paper_configs_live.py`:
**`LIVE QC RESULT: ALL CHECKS PASSED`** — ORB now 4 legs summing to 1.0 splitting
`[3, 3, 2, 2]`, OI 3 legs `[4, 3, 3]`, both still `force_paper`, sizing still
paper 10 / live 1, all `matches plan` checks green against the updated
expectations.

`ruff` clean on both QC scripts. Rollback snapshot in §6 still applies — it
predates both revisions.
