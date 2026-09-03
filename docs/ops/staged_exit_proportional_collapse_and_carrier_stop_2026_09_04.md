# Staged-exit: proportional leg collapse + whole-position carrier stop + LIVE gate — session record (2026-09-04)

**Status: Parts 2, 3a, 3b, 3c implemented (commit `eab6613`, pushed +
deployed to OCI), then QC'd the same day — one real idempotency gap found
and fixed (commit `4b20054`). Part 3d (reconciliation leg-awareness) then
built the same day — staged-exit LIVE readiness is now code-complete. Part 1
(the 3 new staged configs) has a tested, ready-to-run script
(`apply_sb6_2leg_configs.py`), not yet applied to the live OCI DB. 1585
backend tests pass, ruff + mypy clean. Local commits `4b20054`/`3b19147` plus
Part 3d/Part 1 (uncommitted as of this edit) are all still local-only, not
yet pushed/deployed — see "What is pending" below and the repo's own
`git log`/`git status` for the current, authoritative state.**

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

## QC pass findings (2026-09-04, same day)

Holistic QC pass over Parts 2/3a/3b/3c before Part 3d/Part 1 proceed, per
the standing "always QC, real-money system" discipline. One real gap found
and fixed, two doc-staleness nits fixed alongside it — commit `4b20054`.

**Found + fixed: `build_carrier_stop_plan` was not idempotent.**
`build_position_exit_legs` has always guarded against being called twice for
the same position (`position_has_exit_legs` → return the existing legs
unchanged) — its own docstring names the exact scenario: "a retried
`_open_position_from_fill` via `_apply_resolved_pending_order`" (i.e.
`reconcile_pending_live_orders` re-resolving the same LIVE entry order a
second time). `build_carrier_stop_plan`, added by this same session right
after `build_position_exit_legs` in the same call site
(`_open_position_from_fill`), had no equivalent check — it unconditionally
inserted a fresh `StopPlan` row. `stop_plans.position_id` is DB-unique, so a
retry would raise `IntegrityError` instead of no-op'ing like the rest of the
flow. **Confirmed via a direct repro**, not just static reading: wrote a
throwaway test that called `build_position_exit_legs` then
`build_carrier_stop_plan` twice for the same position (mirroring what a
retried `_open_position_from_fill` does) — second call raised
`sqlalchemy.exc.IntegrityError: duplicate key value violates unique
constraint "stop_plans_position_id_key"`. Fixed with an existing-row check
at the top of `build_carrier_stop_plan`, mirroring `build_position_exit_legs`
exactly; kept as a permanent regression test
(`test_carrier_stop_creation_is_idempotent`), confirmed failing before the
fix (`git stash` the fix, rerun, red) and passing after.

Currently dormant in production — the same "inert until a staged config
goes live" reasoning as Part 3b itself, since every enabled `exit_legs`
config is `runtime_mode=force_paper` and this retry path only ever runs for
`Order.mode == OrderMode.LIVE` orders. But it is a distinct gap from Part 3d
(which is about the *exit*-order reconciliation path,
`_apply_resolved_pending_exit_order`) — this one is on the *entry*-fill
path, `_apply_resolved_pending_order`. **Must be closed before any live
staged-exit test, same as Part 3d** — it now is, but worth tracking as a
separate item since it wasn't previously named as a Part-3d blocker.

**Also fixed, doc-only, no behavior change**: `evaluate_open_position`'s
comment claiming a legged position "has no StopPlan" (it does, since Part
3c — just never read as an exit-decision input there, the branch to
`evaluate_leg_position` happens first); `_alert_collapsed`'s docstring,
which still described both paper-mode collapse reasons as unconditionally
paper-tagged after this session's own refactor made the "fill not a lot
multiple" reason carry the position's real `is_live` (intentional and
correct — a genuinely anomalous LIVE fill deserves CRITICAL/LIVE, unlike
routine 1-lot collapse — the docstring just hadn't caught up).

1581 backend tests pass (up from 1580), ruff + mypy clean. No schema change,
not yet pushed/deployed — see repo `git log` for current state.

---

## Part 3d — reconciliation leg-awareness (DONE, 2026-09-04, same day)

`_apply_resolved_pending_exit_order` and `close_position_from_external_fill`
are now leg-aware, closing this session's own remaining live-readiness gap.

- **New shared helper** `finalize_all_open_legs_from_one_fill`
  (`exit_legs.py`) — closes every still-OPEN leg of a legged position against
  ONE already-filled `Order` row, without placing any new broker order (the
  fill already happened). Retires the carrier `StopPlan` first (clears
  `resting_order_id`, marks `TRIGGERED`) so each leg's own carrier-resize/
  cancel bookkeeping is a pure no-op — nothing left to resize/cancel, the
  carrier is either the fill itself or already superseded by it. Naturally
  idempotent: only currently-OPEN legs are touched, so a retry (all legs
  already CLOSED) is a no-op. Used by two callers:
  - `_apply_resolved_pending_exit_order`'s `is_protective_stop` branch, when
    the position is legged — the whole-position carrier stop firing closes
    every remaining leg at once, not the position as a single unit.
  - `close_position_from_external_fill` — a recovered broker fill (auto-repair
    or `POST /positions/{id}/manual-reconcile`) closes every leg against that
    one fill.
- **New `finalize_leg_from_resolved_exit_order`** (`exit_legs.py`) — the
  late-resolution counterpart to `_close_leg` for a single per-leg exit order
  (`exit:{position}:{leg_index}[:retryN]`). Parses `leg_index` out of the
  order's own idempotency key (`_leg_index_from_idempotency_key`); no-ops if
  the key doesn't parse, the leg doesn't exist, or it's already CLOSED (same
  idempotent-by-construction reasoning). Used by
  `_apply_resolved_pending_exit_order`'s non-protective-stop branch, when the
  position is legged.
- `_apply_resolved_pending_exit_order` now branches on
  `position_has_exit_legs` before deciding which of the two helpers above (or
  the unchanged single-position `_finalize_position_close`) to call; the
  CANCELLED/REJECTED-discovered-late branch (clears a dead `resting_order_id`)
  needed no change — it already worked identically for a legged position's
  carrier.
- `close_position_from_external_fill` resolves a real broker
  (`resolve_broker_for_position`) and routes to
  `finalize_all_open_legs_from_one_fill` when the position is legged, instead
  of `_finalize_position_close`.
- Startup recovery (`_run_startup_recovery_check` / `_resume_strategy_runners`)
  needed no change, confirmed — both go through `run_full_reconciliation` /
  `close_position_from_external_fill`, now leg-aware for free.

**Verification**: 3 new dedicated tests in `test_exit_legs.py`
(`test_late_resolved_per_leg_exit_order_closes_only_that_leg`,
`test_late_resolved_carrier_stop_closes_all_remaining_legs`,
`test_close_position_from_external_fill_closes_all_legs`), each confirmed
failing before the fix (`git stash` the two source files, rerun, red) and
passing after. 1585 backend tests pass total (up from 1581), ruff + mypy
clean, no schema change.

**Staged-exit LIVE readiness is now code-complete** — the remaining live
blast-radius gate is operational, not code: no `exit_legs` config is routed
off `runtime_mode=force_paper` yet (see Part 3b's own note above; unchanged
by this).

---

## Part 1 — the 3 new staged configs (script ready, NOT yet applied to OCI)

`backend/scripts/apply_sb6_2leg_configs.py` — clones each base config's
CURRENT live `params`, adds the structure-break tuning + 2-leg `exit_legs`
spec below, and creates the new config via a direct DB session running the
exact same `validate_exit_leg_templates` call `POST /strategies` does (never
the raw-`psql`-INSERT shortcut that skips validation and fails-safe to
no-legs at signal time). Idempotent (skips a name that already exists).
`--dry-run` prints the exact params without writing anything — always run
that first.

| New config | strategy_type | clone of |
|---|---|---|
| `Test 1 (sb6-2leg)` | `ema_micro_pullback` | `Test 1` |
| `Test 4 (sb6-2leg)` | `vwap_pullback` | `Test 4` |
| `Test (sb6-2leg)` | `oi_volume_confirmed` | `Test` |

`params` delta merged onto each base config's own current params:

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

`runtime_mode` is hardcoded `force_paper` on all three (never inherited from
the base config — `Test 1`'s own `runtime_mode` is `NULL`, i.e. it follows
the session mode; a brand-new, unbacktested staged-exit variant must not
inherit that).

**Not run against the live OCI DB from this session** — this local session
has no live DB/SSH access (confirmed: even a read-only credential lookup was
blocked by the sandbox's own safety classifier), and applying it is a live
production-config mutation, the same category of action every other config
change in this repo's history goes through an explicit, separately-confirmed
step for. Self-verified everything reachable without that access: `--dry-run`
against the local dev DB runs clean (correctly reports all three base
configs not found there, confirming no crash/import/query error), and the
`exit_legs` payload itself independently validates via
`validate_exit_leg_templates` with zero errors. No restart needed once
applied (`auto_spawner` reads `is_enabled` per run; `_build_strategy` reads
`params` at spawn).

## What is pending

- **Apply Part 1's script to the live OCI DB** — `--dry-run` first, read the
  printed params, then apply for real. Needs live DB/SSH access this session
  doesn't have.
- **`EMA_Micro_Conviction` `qty_lots` for the eventual live test — DECIDED: 3,
  not 2.** Its 3-leg spec (fractions ~0.4/0.3/0.3) needs `qty_lots >= 3` for
  `allocate_leg_lots_floored` (Part 2) to keep all 3 legs; 2 lots drops the
  smallest-fraction leg (`[1,1]`, only 2 of 3 survive), 3 keeps all of them
  uniformly (`[1,1,1]`). Same "don't silently drop a configured leg when the
  lot count can instead just cover all of them" principle as Part 2 itself,
  applied consistently rather than left as a per-config inconsistency —
  **use 3 specifically if/when EMA_Micro_Conviction is the config chosen for
  the minimum-size live staged-exit test below.** Deliberately **not**
  applied to the live config now: an explicit `params.qty_lots` is
  mode-unaware (wins in both paper and live, a documented 2026-09-01
  gotcha), so setting it today would immediately shrink this
  still-`force_paper`, still-iterating config's *current* paper position
  size from its 10-lot default — a real, unrequested side effect, not just
  a same-day config tweak.
- The A/B comparison metric (structure-break-affected trades only: net PnL, max
  adverse excursion, winner→loser flips) — prefer the validated reconstruction
  harness on one live config over trusting parallel-config PnL (risk-engine
  slot competition biases it).
- A real minimum-size live staged-exit test (one config, `qty_lots: 2`, one
  trading day) — Part 3d is done, so this is now unblocked, just not yet run.
