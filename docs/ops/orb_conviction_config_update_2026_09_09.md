# ORB conviction live config update — 2026-09-09

**w70 + 2-leg all-capped exit.** `params`-only change to two `strategy_configs`
rows in the live OCI `trading_bot` DB (`144.24.137.112`). No code, no
migration, no restart.

Apply script: `backend/scripts/ops_update_orb_conviction_2leg_exit_2026_09_09.py`
(idempotent, validates before writing, touches `params` only).

## Why

Backtest arc **p17 → p18b3** (`backend/scripts/BACKTEST_LEARNINGS.md`,
2026-09-09 ~21:00 IST) closed two open questions for the ORB conviction family:

1. **OR-width, re-swept with `require_prior_day_trend` ON** (open since
   2026-09-01): w55 −33 / w65 +7 / **w70 +69.1** / w75 +64.2 / w80 −124 /
   w85 −113. 70–75 is a plateau between sharp cliffs at 65 and 80; w70 wins on
   E and sits one step further from the w80 cliff → **65 → 70**.
2. **Exit-shape grid at w70** (`--exit-mode current`, n≈40, OOS from
   2026-04-01): the lever is *staying in longer* (drop the +33% ceiling), not a
   tighter trail. Best blend **60 % X70-MID-T66 / 40 % X70-CAP-T50-L80**
   (E +272, PF 2.50, P(mean≤0) 0.014, IS +236 → OOS +326) vs current live CTRL
   (E +69, PF 1.51, P 0.173). **Both legs hard-capped** (+66 % / +50 %) → no
   uncapped runner, every position has a defined exit; that bounded exposure is
   the real-world-noise hedge. A 3rd leg added no robustness; making it
   uncapped re-introduced the tail risk it was there to remove.

## Targets

| | `ORB_Convic_Live` | `ORB_Convic_Paper` |
|---|---|---|
| id | `7329fdf0-aef6-4b11-bec2-385d1c3a5c81` | `76b61473-075f-4b59-bb31-ab985195f255` |
| strategy_type | `orb_conviction` | `orb_conviction` |
| is_enabled / runtime_mode / _source | true / `force_paper` / `manual` | true / `force_paper` / (null) |
| underlying | NIFTY | NIFTY |

`runtime_mode` / `runtime_mode_source` are **not** touched. Both rows are
`force_paper`, so they trade paper regardless of session mode — safe to apply
mid-session (session `fbdfeccd…` is `live_enabled` + active). 0 open positions
system-wide at apply time; all ORB_Convic StrategyRuns for the day are
`stopped`.

## The changes

- **C1** `max_or_range_nifty_points` **65 → 70**.
- **C2** `exit_legs` — replace the 3-leg 25/25/50 core/runner/target (two
  uncapped legs) with a 2-leg 60/40, both hard-capped:

  | leg | `kind` | `qty_fraction` | `stop_pct` | `target_pct` | `trail_activation_fraction` | `trail_lock_fraction` | `use_structure` |
  |---|---|---|---|---|---|---|---|
  | A | `core` | 0.60 | 0.20 | 0.66 | 0.18 | 0.70 | true |
  | B | `runner` | 0.40 | 0.22 | 0.50 | 0.24 | 0.80 | true |

  Both arm the trail at ≈ +12 % of entry (`target_pct × trail_activation_fraction`
  = 0.66×0.18 = 0.1188 ≈ 0.50×0.24 = 0.12). The different per-leg arm fractions
  are intentional — they equalise the arm point across the two targets.
  `use_structure: true` kept on both (parity with every sibling conviction
  config; adds an OR-structure-break exit on top of stop/target/trail — the p18
  grid modelled stop/target/trail only, so this makes live *more* conservative
  than the backtested E, not riskier).
- **C3** top-level fallback params **= Leg A**: `stop_pct` 0.22 → **0.20**,
  `target_pct` 0.33 → **0.66**, `trail_activation_fraction` 0.12 → **0.18**,
  `trail_lock_fraction` 0.6 → **0.70**. Governs the malformed-/no-`exit_legs`
  single-exit fallback and Risk pre-trade analytics. (The 1-lot collapse reads
  Leg A's *own* leg params via `pick_collapsed_exit_leg`, not top-level — same
  net effect since top-level is set = Leg A.)

**Unchanged, carried verbatim:** `qty_lots` (Live only — see note below),
`orb_entry_cutoff_time` `"10:15"`, `require_prior_day_trend` true,
`entry_rsi_block_ce_above` 75, `entry_rsi_block_pe_below` 25,
`entry_require_confirm_bar` true.

## Blast summary

No code path changes — data on 2 rows only; the exit-legs engine, strategy
classes, risk service, reconciliation and reporting are untouched. Blast is
confined to signals/positions these 2 configs produce.

- **C1** loosens the entry filter slightly (66–70-pt ORs now qualify).
  Per-day volume still ceilinged by `_fired_directions` (1 CE + 1 PE / run /
  day) and `max_concurrent_positions = 2`.
- **C2** both new legs = `stop_pct` + `target_pct` + `use_structure` + trail —
  the exact shape the old `target` leg already had, so `_check_leg` exercises
  every branch today (no new/untested path). Trail arms later (~+12 % vs the
  old ~+4 %) → fewer premature trail-outs, larger give-back on a small
  favourable reversal, bounded by the −20 %/−22 % premium stop + the still-live
  structure-break exit. Lot splits (`allocate_leg_lots_floored`, [0.6, 0.4]):
  n10 → `[6,4]`, n3 → `[2,1]`, n2 → `[1,1]`, n1 → collapse to Leg A.
- **C3** `stop_pct`/`target_pct` are price-builders only in ORB (no R:R gate);
  a 2 pp tighter base stop marginally *eases* Risk budget checks. No cap
  crossed.
- **In-flight positions:** `PositionExitLeg` rows are built once at fill time;
  changing `params` never rewrites existing legs. 0 open positions → nothing to
  half-migrate.

**Pre-existing landmine, NOT touched here:** `ORB_Convic_Live` carries
`qty_lots: 10`. Inert while `force_paper`, but active `risk_limit_configs` v18
has `per_trade_lot_cap = 3`, and an explicit `qty_lots` is **rejected, never
clamped**, above the cap. The moment `_Live` is armed `force_live` in a
`live_enabled` session every live ORB intent would fail
(`per_trade_lot_cap_exceeded`). Raise separately before arming `_Live`.

## Rollback — current `params`, verbatim (2026-09-09, pre-change)

### `ORB_Convic_Live` `7329fdf0-aef6-4b11-bec2-385d1c3a5c81`

```json
{"qty_lots": 10, "stop_pct": 0.22, "exit_legs": [{"kind": "core", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}, {"kind": "runner", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.8, "trail_activation_fraction": 0.12}, {"kind": "target", "stop_pct": 0.22, "target_pct": 0.33, "qty_fraction": 0.5, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}], "target_pct": 0.33, "trail_lock_fraction": 0.6, "orb_entry_cutoff_time": "10:15", "require_prior_day_trend": true, "entry_rsi_block_ce_above": 75, "entry_rsi_block_pe_below": 25, "entry_require_confirm_bar": true, "max_or_range_nifty_points": 65, "trail_activation_fraction": 0.12}
```

### `ORB_Convic_Paper` `76b61473-075f-4b59-bb31-ab985195f255`

```json
{"stop_pct": 0.22, "exit_legs": [{"kind": "core", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}, {"kind": "runner", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.8, "trail_activation_fraction": 0.12}, {"kind": "target", "stop_pct": 0.22, "target_pct": 0.33, "qty_fraction": 0.5, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}], "target_pct": 0.33, "trail_lock_fraction": 0.6, "orb_entry_cutoff_time": "10:15", "require_prior_day_trend": true, "entry_rsi_block_ce_above": 75, "entry_rsi_block_pe_below": 25, "entry_require_confirm_bar": true, "max_or_range_nifty_points": 65, "trail_activation_fraction": 0.12}
```

## Target `params` (what the script writes)

### `ORB_Convic_Live`

```json
{"qty_lots": 10, "stop_pct": 0.2, "target_pct": 0.66, "trail_activation_fraction": 0.18, "trail_lock_fraction": 0.7, "max_or_range_nifty_points": 70, "orb_entry_cutoff_time": "10:15", "require_prior_day_trend": true, "entry_rsi_block_ce_above": 75, "entry_rsi_block_pe_below": 25, "entry_require_confirm_bar": true, "exit_legs": [{"kind": "core", "qty_fraction": 0.6, "stop_pct": 0.2, "target_pct": 0.66, "trail_activation_fraction": 0.18, "trail_lock_fraction": 0.7, "use_structure": true}, {"kind": "runner", "qty_fraction": 0.4, "stop_pct": 0.22, "target_pct": 0.5, "trail_activation_fraction": 0.24, "trail_lock_fraction": 0.8, "use_structure": true}]}
```

### `ORB_Convic_Paper`

Identical to the above **without** the `qty_lots` key.

## Apply

```bash
# from a checkout of this branch
scp -i <oci_key> backend/scripts/ops_update_orb_conviction_2leg_exit_2026_09_09.py \
    ubuntu@144.24.137.112:/tmp/

# on the box (operator — Claude's classifier blocks the prod-DB write)
cd /home/ubuntu/trading-bot/backend && \
  .venv/bin/python /tmp/ops_update_orb_conviction_2leg_exit_2026_09_09.py
```

No restart — `auto_spawner` reads `is_enabled` + `params` per spawn and
`_apply_exit_leg_templates` reads `params["exit_legs"]` per signal.

## Verify

1. `SELECT name, params FROM strategy_configs WHERE id IN
   ('7329fdf0-aef6-4b11-bec2-385d1c3a5c81','76b61473-075f-4b59-bb31-ab985195f255')`
   — diff against "Target params" above; `runtime_mode` / `_source` unchanged.
2. `python scripts/qc_paper_configs_live.py rows.txt` (fresh enabled-config
   dump) → `ALL STRUCTURAL CHECKS PASSED`; both ORB_Convic rows show
   `[2] exit_legs OK 2 legs sum 1.0`, `[3] leg keys OK`,
   `[4] construct OK -> ORBConvictionStrategy`, `[6] n=10 [6,4] / n=2 [1,1] /
   n=1 dominant leg (0.6)`.
3. Next `auto_spawner` spawn (09:00 IST, or manual `POST /sessions/bootstrap-now`)
   → new `StrategyRun` + first `Signal.exit_legs` is the 2-leg shape.

## Applied

_(fill in on apply)_ — applied-at (IST): … · verify output: …
