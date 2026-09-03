# Staged-exit: proportional leg collapse + whole-position carrier stop + LIVE gate — session record (2026-09-04)

**Status: Parts 2, 3a, 3b, 3c implemented + self-verified (1580 backend tests
pass, ruff + mypy clean). Committed + pushed + deployed to OCI this session.
Part 3d (reconciliation leg-awareness) and Part 1 (the 3 new staged configs)
are PENDING — see "What is pending" below.**

Full plan (with the Phase-2 QC / blast-radius audit that preceded the code):
`~/.claude/plans/fizzy-weaving-hopcroft.md` on this machine (not in the repo).

---

## Objective / context

Came out of a structure-break tuning discussion. `structure_break_persistence_seconds`
on `Test` / `Test 1` / `Test 4` (currently `120`) is an untuned 2026-08-24
reactive placeholder, stacked on while the PE-direction bug (`f546b20`) and the
tick-corruption bug (`2c9a511`) were both still live. Three related pieces of
work:

1. **A/B the structure-break params in paper** — keep each current config, add
   one new staged config per strategy (2-leg 50:50, legs run
   `structure_break_persistence_seconds: 6` + `structure_break_atr_multiplier: 0.6`).
   *(Part 1 — config only, not yet applied.)*
2. **Proportional leg collapse** — stop collapsing a staged position to a
   single exit just because it has fewer lots than legs; keep as many legs as
   the lots allow. *(Part 2 — done.)*
3. **Open the LIVE gate for staged exits** — with one whole-position resting
   SL-LMT at the broker, so live testing of the staged-exit engine can start.
   *(Parts 3a/3b/3c — done. Part 3d — pending.)*

---

## Part 2 — Proportional leg collapse (DONE)

**`allocate_leg_lots_floored(total_lots, fractions) -> (kept_lots, dropped_indices)`**
— new pure helper in `backend/app/domain/strategy/exit_legs.py`, *alongside* the
untouched `allocate_leg_lots` (its pinned tests still pass).
- `total_lots <= 1` → `([], all indices)` — caller collapses to a single exit.
- else → keep the `min(len, total_lots)` largest-fraction legs (ties → lower
  index), 1 lot each, distribute the excess by largest-remainder (ties → later
  leg — same rule as `allocate_leg_lots`). Every kept leg ≥ 1,
  `sum == total_lots`.
- Examples: `6 / [.3,.3,.2,.2] → [2,2,1,1]`; `2 / [.4,.3,.3] → [1,1] drop leg 2`;
  `10 / [.4,.3,.3] → [4,3,3]`.

**`build_position_exit_legs`** (`backend/app/modules/execution_engine/paper/exit_legs.py`)
rewritten:
- 1-lot / bad-fill → `_alert_collapsed` (WARNING) + `return None` (byte-identical
  to today's 1-lot behaviour).
- ≥2 lots → stage across the kept legs, `leg_index` re-numbered `0..k-1`. If any
  leg was dropped → new `_alert_reduced` (**WARNING, DB-only, deliberately NOT
  on `TELEGRAM_ALLOWED_CATEGORIES`** — expected, benign fallback).
- "filled qty not a whole lot multiple" collapse guard kept, unchanged.

**Behaviour change to watch**: any staged config running fewer lots than legs
now stages a reduced set instead of collapsing. As of deploy time all three
enabled staged configs on OCI run enough lots for a full split
(`EMA_Micro_Conviction` `qty_lots: 10` → `[4,3,3]`, `OI_Volume_Conviction` /
`ORB_Conviction` at the paper default 10 → `[4,3,3]` / `[3,3,2,2]`), so nothing
changes for them right now. It only bites a staged config deliberately sized
below its leg count.

---

## Part 3a — Per-leg exit-order retry hardening (DONE)

`_close_leg_locked` had the same latent fixed-idempotency-key bug `close_position`
was fixed for in `206b7a0` (2026-09-02): a LIVE leg exit order that comes back
CANCELLED/REJECTED would retry forever against the dead order. Now mirrors the
single-exit fix exactly — explicit `{base, :retry1..:retry(N-1)}` key set,
`_MAX_EXIT_ORDER_ATTEMPTS = 5` cap, then `exit_order_attempts_exhausted`
CRITICAL alert (dedup per `position:leg_index`), leg left OPEN. Harmless today
(the mock always fills synchronously); needed for LIVE.

---

## Part 3b — LIVE gate opened (DONE)

The `if is_live: return None` guard in `build_position_exit_legs` is removed —
LIVE positions build legs exactly like paper. Rollout: no flag / per-config
opt-in (per the design call). After deploy, any config with `exit_legs`
staged-exits live once the session is `live_enabled` **and** its effective
`qty_lots >= 2` (LIVE default is 1 lot → Part-2 collapse-to-single, so the live
blast radius is zero until a live-routed config is deliberately sized up).

**⚠ Until Part 3d ships, do not take any `exit_legs` config off
`runtime_mode = force_paper` on a `live_enabled` session.** As of deploy time
every enabled staged config (`EMA_Micro_Conviction`, `OI_Volume_Conviction`,
`ORB_Conviction`) is `force_paper`, so the LIVE staged path is unreachable and
this deploy is inert with respect to live money — the gate is open in code but
nothing routes through it. If a staged config is later pinned live, a LIVE leg
exit that doesn't fill synchronously (or the carrier stop firing on a
disconnect) would reconcile as a whole-position close → `reconciliation_lock`
(a safe halt, but not the intended behaviour) until 3d lands.

---

## Part 3c — Whole-position carrier resting stop (DONE)

One broker SL-LMT for the whole position instead of one per leg (3 broker SLs
per position isn't practical — margin, cancel/modify race surface).

- **`build_carrier_stop_plan`** (`exit_legs.py`) — creates a carrier `StopPlan`
  for every legged position, in **both** modes (so paper's data shape and the
  resize/cancel code path match live). Trigger = **worst (lowest) leg
  `stop_price` × (1 − `_CARRIER_STOP_EXTRA_MARGIN_PCT` = 2%)**. Sitting *below*
  every leg stop means the ~3s app poll always closes a leg on its own stop
  first, so the carrier only ever fires when the poll is dead (crash /
  disconnect) — a pure backstop, never a concurrent second exit. LIVE also
  calls `place_protective_stop` (unchanged). Returns `None` if no leg carries a
  `stop_price`.
- **A `StopPlan` on a position that also has `position_exit_legs` is always a
  carrier** — never an exit-decision input (`evaluate_open_position` branches to
  `evaluate_leg_position` before its own stop check), only a holder for
  `resting_order_id` / `resting_order_price` and a resize anchor.
- **`_sync_carrier_stop_after_leg_close`** (`exit_legs.py`, called from
  `_finalize_leg_and_maybe_position` after `position.qty` is decremented):
  - position now flat → retire the carrier (LIVE: `cancel_resting_protective_stop`).
  - legs remain → re-anchor the trigger to the worst *still-open* leg stop and
    shrink to the remaining qty (LIVE: `resize_resting_protective_stop`).
- **`resize_resting_protective_stop`** — new in `backend/app/modules/execution_engine/paper/protective_stop.py`.
  One `ModifyOrder(qty=new_qty, trigger, limit)` (cancel+replace is impossible
  — the broker key is fixed `stop:{position_id}` and the mock/idempotency layer
  would just return the stale order). Never raises; a modify failure leaves the
  *larger* order armed (a too-big stop only ever fires on a dead poll, when
  closing the whole remainder is correct anyway) + a WARNING
  `protective_stop_resize_failed`.

Wired into `_open_position_from_fill` (`service.py`) — the legs branch now calls
`build_carrier_stop_plan` before returning.

---

## Verification performed

- **1580 backend tests pass** (was ~1553 pre-session), `ruff check .` clean,
  `mypy app tests` clean.
- New: `backend/tests/unit/test_exit_legs_allocation.py` (14 allocator cases,
  incl. a guard that `allocate_leg_lots` is unchanged).
- `backend/tests/integration/test_exit_legs.py`: carrier created with the
  expected trigger/qty; carrier shrinks as legs close and retires when flat;
  carrier absent when no leg has a stop; `resize_resting_protective_stop`
  shrinks qty + re-anchors; leg exit order retries with fresh `:retryN` keys
  and exhausts at 5 with a CRITICAL alert; proportional-collapse cases (1-lot →
  single, 2-lot/3-leg → `[1,1]` + `exit_legs_reduced`); LIVE now builds legs.
- Updated in place: `test_dispatch_creates_legs_and_a_carrier_stop_plan`
  (was `..._and_no_stop_plan`), `test_build_legs_now_supported_for_live_position`
  (was `..._returns_none_for_live_position`).
- No schema change / migration in any part.

---

## What is pending

### Part 3d — reconciliation leg-awareness (needed before any live staged test)

`_apply_resolved_pending_exit_order` (`service.py:1514`) is **not leg-aware**:
- A late-resolved `exit:{pos}:{leg}[:retryN]` fill → routes to
  `_finalize_position_close` (closes the *whole* position) instead of
  `_finalize_leg_and_maybe_position` (that one leg).
- A carrier `stop:` fill (disconnect backstop) → routes to
  `_finalize_position_close` as STOP instead of closing *all remaining legs*
  (`close_all_open_legs`) at the fill price.
- `stop:` CANCELLED/REJECTED discovered late → already handled correctly
  (clears `resting_order_id`).

Also: `close_position_from_external_fill` (auto-repair + `POST /positions/{id}/manual-reconcile`)
assumes a single-exit position — must close all open legs from the recovered
fill for a legged position. Startup recovery
(`_run_startup_recovery_check` / `_resume_strategy_runners`) then works for free
once the reconciliation loop is leg-aware.

### Part 1 — the 3 new staged configs (config insert on the live OCI DB)

Add three `strategy_configs` rows, `is_enabled=true`, `runtime_mode=force_paper`:

| New config | strategy_type | clone of |
|---|---|---|
| `Test 1 (sb6-2leg)` | `ema_micro_pullback` | `Test 1` |
| `Test 4 (sb6-2leg)` | `vwap_pullback` | `Test 4` |
| `Test (sb6-2leg)` | `oi_volume_confirmed` | `Test` |

`params` for each:

```json
{
  "structure_break_persistence_seconds": 6,
  "structure_break_atr_multiplier": 0.6,
  "exit_legs": [
    {"kind": "core",   "qty_fraction": 0.5, "use_structure": true,
     "trail_activation_fraction": 0.5, "trail_lock_fraction": 0.5},
    {"kind": "runner", "qty_fraction": 0.5, "use_structure": true,
     "no_target": true, "trail_activation_fraction": 0.7, "trail_lock_fraction": 0.8}
  ]
}
```

No restart needed (`auto_spawner` reads `is_enabled` per run; `_build_strategy`
reads `params` at spawn). Prefer the config endpoint (runs
`validate_exit_leg_templates`); a raw `psql` INSERT works but skips validation
and would fail-safe to no-legs at signal time.

### Other

- Decide `EMA_Micro_Conviction` `qty_lots` (2 → `[1,1]` drops a leg; 3 →
  `[1,1,1]`).
- The A/B comparison metric (structure-break-affected trades only: net PnL, max
  adverse excursion, winner→loser flips) — prefer the validated reconstruction
  harness on one live config over trusting parallel-config PnL (risk-engine
  slot competition biases it).
- A real minimum-size live staged-exit test (one config, `qty_lots: 2`, one
  trading day) once 3d lands.
