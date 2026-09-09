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
| is_enabled / runtime_mode / _source (at apply) | true / `force_live` / `manual` | true / `force_paper` / (null) |
| underlying | NIFTY | NIFTY |

> **Concurrent change caught at apply time.** When this note was first drafted
> (~16:00 IST) `ORB_Convic_Live` read `force_paper` / `qty_lots: 10`. Between
> the draft and the apply (~18:12 IST) the operator armed it: `force_live` /
> `qty_lots: 1` (the live-ramp step). `runtime_mode` was never touched by the
> script; the first apply pass hardcoded `qty_lots: 10` from the stale read and
> was corrected to `1` on a second idempotent pass (18:14 IST) — see
> **Applied** below.

`runtime_mode` / `runtime_mode_source` are **not** touched by the script
(`params` only). `ORB_Convic_Live` is `force_live` in the `live_enabled`
session `fbdfeccd…`, so it routes real orders — `qty_lots` **must** be the
live-ramp step (1), and stays ≤ the active `per_trade_lot_cap` (3).
`ORB_Convic_Paper` is `force_paper`. 0 open positions system-wide and 0
non-terminal ORB_Convic StrategyRuns at apply time — nothing acted on the
transient `qty_lots: 10`.

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

**Unchanged, carried verbatim:** `qty_lots` (`ORB_Convic_Live` only — set to
**1** to match the operator's live-ramp step; see the concurrent-change note
above), `orb_entry_cutoff_time` `"10:15"`, `require_prior_day_trend` true,
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

**`qty_lots` on `ORB_Convic_Live` — the one thing that needed care.** It is
`force_live` in a `live_enabled` session and active `risk_limit_configs` v18
has `per_trade_lot_cap = 3`; an explicit `qty_lots` is **rejected, never
clamped**, above the cap. `qty_lots: 1` (the operator's ramp step) is correct
and stays under the cap. The first apply pass wrote a stale `10` and was
corrected on a second pass (see **Applied**).

## Rollback — `params` verbatim as they stood at apply time (2026-09-09 ~18:12 IST)

Restoring these + (for `_Live`) leaving `runtime_mode = force_live` /
`runtime_mode_source = manual` reverts this change entirely.

### `ORB_Convic_Live` `7329fdf0-aef6-4b11-bec2-385d1c3a5c81`  (was `force_live`, `qty_lots: 1`)

```json
{"qty_lots": 1, "stop_pct": 0.22, "exit_legs": [{"kind": "core", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}, {"kind": "runner", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.8, "trail_activation_fraction": 0.12}, {"kind": "target", "stop_pct": 0.22, "target_pct": 0.33, "qty_fraction": 0.5, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}], "target_pct": 0.33, "trail_lock_fraction": 0.6, "orb_entry_cutoff_time": "10:15", "require_prior_day_trend": true, "entry_rsi_block_ce_above": 75, "entry_rsi_block_pe_below": 25, "entry_require_confirm_bar": true, "max_or_range_nifty_points": 65, "trail_activation_fraction": 0.12}
```

> `qty_lots` read `10` when this note was first drafted; it was `1` by apply
> time. Use `1` for a rollback.

### `ORB_Convic_Paper` `76b61473-075f-4b59-bb31-ab985195f255`

```json
{"stop_pct": 0.22, "exit_legs": [{"kind": "core", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}, {"kind": "runner", "stop_pct": 0.18, "no_target": true, "qty_fraction": 0.25, "use_structure": true, "trail_lock_fraction": 0.8, "trail_activation_fraction": 0.12}, {"kind": "target", "stop_pct": 0.22, "target_pct": 0.33, "qty_fraction": 0.5, "use_structure": true, "trail_lock_fraction": 0.6, "trail_activation_fraction": 0.12}], "target_pct": 0.33, "trail_lock_fraction": 0.6, "orb_entry_cutoff_time": "10:15", "require_prior_day_trend": true, "entry_rsi_block_ce_above": 75, "entry_rsi_block_pe_below": 25, "entry_require_confirm_bar": true, "max_or_range_nifty_points": 65, "trail_activation_fraction": 0.12}
```

## Target `params` (what the script writes)

### `ORB_Convic_Live`

```json
{"qty_lots": 1, "stop_pct": 0.2, "target_pct": 0.66, "trail_activation_fraction": 0.18, "trail_lock_fraction": 0.7, "max_or_range_nifty_points": 70, "orb_entry_cutoff_time": "10:15", "require_prior_day_trend": true, "entry_rsi_block_ce_above": 75, "entry_rsi_block_pe_below": 25, "entry_require_confirm_bar": true, "exit_legs": [{"kind": "core", "qty_fraction": 0.6, "stop_pct": 0.2, "target_pct": 0.66, "trail_activation_fraction": 0.18, "trail_lock_fraction": 0.7, "use_structure": true}, {"kind": "runner", "qty_fraction": 0.4, "stop_pct": 0.22, "target_pct": 0.5, "trail_activation_fraction": 0.24, "trail_lock_fraction": 0.8, "use_structure": true}]}
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

## Applied — 2026-09-09

- **~18:12 IST** — `ops_update_orb_conviction_2leg_exit_2026_09_09.py` run on
  the box (`.venv/bin/python /tmp/…`). Both rows staged + committed: C1 (w70),
  C2 (2-leg exit), C3 (top-level = Leg A). Script preflight
  (`deserialize` + `validate_exit_leg_templates` + `_build_strategy`) passed
  for both rows before the write. **Caught at apply:** `ORB_Convic_Live` had
  been armed `force_live` / `qty_lots: 1` since the note was drafted; the first
  pass wrote a stale `qty_lots: 10`.
- **~18:14 IST** — script `qty_lots` literal corrected `10 → 1`, re-scp'd
  (sha256 `608b3965…`, box == local worktree), re-run. Idempotent: only
  `ORB_Convic_Live.qty_lots` moved (`10 → 1`); `ORB_Convic_Paper` = "no change".
- **Verify (over SSH):**
  - `ORB_Convic_Live` — `is_enabled=t`, `runtime_mode=force_live` /
    `runtime_mode_source=manual` (**unchanged**), `qty_lots=1`, `w=70`, 2 exit
    legs, top-level `stop_pct 0.2 / target_pct 0.66 / trail_activation 0.18 /
    trail_lock 0.7`, entry gate intact. `updated_at` 18:14:20 UTC.
  - `ORB_Convic_Paper` — `is_enabled=t`, `runtime_mode=force_paper` /
    source NULL (**unchanged**), no `qty_lots`, same w70 + 2-leg + top-level.
    `updated_at` 18:12:20 UTC.
  - `scripts/qc_paper_configs_live.py` over all 12 enabled configs →
    `ALL STRUCTURAL CHECKS PASSED`. Both ORB_Convic rows: `[1]` no inert keys,
    `[2]` exit_legs OK 2 legs sum 1.0, `[3]` leg keys OK, `[4]` constructs
    `ORBConvictionStrategy`, `[6]` n10 `[6,4]` / n3 `[2,1]` / n2 `[1,1]` /
    n1 dominant leg (0.6). (`qc`'s `enabled=False` line is a cosmetic artifact
    of the `||`-concatenated dump — `is_enabled` is `t` for both, confirmed
    directly.)
  - `ROUTES LIVE (3): EMA_Convic_Live, OI_Convic_Live, ORB_Convic_Live`.
- **Pending:** next `auto_spawner` spawn (2026-09-10 ~09:00 IST) — confirm the
  new `StrategyRun` picks up w70 and the first `Signal.exit_legs` is the 2-leg
  shape.
