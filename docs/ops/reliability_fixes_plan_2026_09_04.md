# Reliability fixes — planning doc (2026-09-04)

Four issues found during the 2026-09-04 live-trading incident review (stuck
paper position, failed square-off, Telegram alert spam, Market
Terminal/Control Room disagreement). Each gets a Phase 1 (Implementation
Plan) + Phase 2 (QC & Blast Radius Audit), per explicit request — QC is not
to be combined or skipped. Nothing in this doc has been implemented yet;
this is the planning artifact to work through one issue at a time.

Status key: 🟢 root cause confirmed, plan is solid — 🟡 root cause confirmed,
plan is a first draft — 🔴 not yet root-caused, Phase 1 is a discovery step
only.

---

## Issue 1 🟢 — Paper positions aren't protected from mid-flight live re-routing

**Symptom** (2026-09-04 incident): `VWAP_RSI_modified` opened a position
correctly as PAPER (`force_paper` was active). `force_paper` was lifted
~30 min later while the position was still open. The next protective-stop
dispatch re-evaluated *current* routing, resolved the real Shoonya broker,
and fired 5 real SELL orders for a position Shoonya never had — all
rejected, retries exhausted, position stuck.

**Root cause**: `get_execution_broker()`
(`backend/app/modules/broker_adapter/composition.py:459-479`) has a rule
pinning a position/order to the **real** broker forever once opened LIVE:

```python
if position is not None and _position_opened_live(position):
    return _real_broker_or_raise(...)
if order is not None and order.mode == OrderMode.LIVE:
    return _real_broker_or_raise(...)
if not is_strategy_routed_live(trading_session, strategy_run):
    return get_execution_mock()
... # falls through to real broker
```

There is **no symmetric rule** pinning a position/order to the **mock**
broker once opened PAPER. A paper position falls straight through to
`is_strategy_routed_live()`, which reflects *current* config, not the
config at entry time.

A near-identical incident was already found and half-fixed on 2026-08-19
(`resolve_broker_for_position`'s own docstring) — that fix made broker
resolution per-position instead of shared-per-cycle, but never added the
paper-side pin. This is the surviving half of that bug.

### Phase 1: Implementation Plan

**Touchpoint**: `broker_adapter/composition.py:get_execution_broker` only.
No schema/migration — `_position_opened_live()` and `Order.mode` are
already correctly populated at dispatch (confirmed: today's entry order
*was* tagged `'paper'` correctly; only the *exit* routing was wrong).

**Change**: insert two new rules immediately after the existing LIVE-pin
checks, before the `is_strategy_routed_live` fallthrough:

```python
if position is not None and _position_opened_live(position):
    return _real_broker_or_raise(...)                 # unchanged
if order is not None and order.mode == OrderMode.LIVE:
    return _real_broker_or_raise(...)                 # unchanged
if position is not None and not _position_opened_live(position):
    return get_execution_mock()                        # NEW — symmetric pin
if order is not None and order.mode == OrderMode.PAPER:
    return get_execution_mock()                        # NEW — symmetric pin
if not is_strategy_routed_live(trading_session, strategy_run):
    return get_execution_mock()                        # unchanged
... # real broker, now only reached for a brand-new dispatch (no position/order yet)
```

- Update the docstring's rule numbering (1, 1b, 2, 3, 4 → insert 1c, 1d)
  to keep the existing "priority order" documentation accurate — this
  function has been mis-documented once before; don't repeat that.
- Update `is_strategy_routed_live`'s own docstring cross-reference if it
  claims to be the sole live/paper predicate for an *existing* position —
  it should stay accurate only for *new* dispatch decisions.
- New unit tests (mirror the existing 2026-08-19 regression tests in
  `test_execution_broker_gating.py`):
  - position opened PAPER, session `live_enabled`, strategy currently NOT
    `force_paper` → resolves mock (currently would resolve real — this is
    the exact reproduction of today's bug).
  - position opened PAPER, `force_paper` set at entry then cleared before
    exit → still resolves mock.
  - symmetric `order`-only variant (no position yet).
  - confirm all existing LIVE-pin tests are unaffected.
- Rollout: pure logic narrowing (removes a wrong "escalate to real" path,
  adds no new real-broker path) — safe to deploy as a normal change, no
  feature flag needed.

### Phase 2: QC & Blast Radius Audit

**1. Regression & side-effect analysis**
- Is there any *legitimate* scenario where a paper position's exit should
  go live? No — a paper position must never place a real order, full
  stop. No legitimate behavior is removed by this change.
- `positions.opening_order_id` is `NOT NULL` in the schema, so
  `_position_opened_live`'s defensive `opening_order is None → False`
  branch should be unreachable in practice — low risk, but confirm no
  data-integrity path (e.g. a bad migration or manual SQL fix) has ever
  left a dangling reference.
- `PositionManager`'s regular ~3s monitoring cycle also calls
  `resolve_broker_for_position` for price reads (not just closes) — after
  this fix, a paper-opened position whose strategy is now live-routed
  correctly polls the mock for pricing too, not just for closing. This is
  a strictly beneficial side-effect (consistent pricing source), not a
  regression, but confirm the mock's `get_quote()` always returns a usable
  price for an aged position (it should — it's the existing paper-fill
  path, unchanged).

**2. Systemic risks**
- Performance: negligible — a cheap branch reorder in an already-hot-path
  function, no new DB queries.
- Masking risk: if some *other*, currently-unknown bug ever causes a
  position to be wrongly opened PAPER when it should have been LIVE, this
  fix would now "helpfully" keep it paper forever instead of the current
  behavior (a loud rejection storm that at least signals something is
  wrong). Mitigation: today's behavior is strictly worse (silent misroute
  to a real order), but add a distinct WARNING log line (not a Telegram
  alert — this isn't urgent, just worth a trail) whenever the new
  paper-pin branch fires while the strategy is *currently* live-routed, so
  a genuine paper-vs-should-be-live anomaly stays visible without ever
  risking a real order to surface it.

**3. Parallel feature parity**
- All six callers of `resolve_broker_for_position` — `PositionManager`,
  `close_position`, `protective_stop.py` (×2, placement and resync),
  `eod_square_off.py`, `exit_legs.py` — share this exact code path. Since
  the fix lives inside `get_execution_broker` itself, all six inherit it
  automatically; none has its own independent live/paper decision logic
  that would need a separate patch. Confirm this during implementation by
  re-reading each call site once more, not just trusting the grep from
  this session.
- `dispatch_trade_intent`'s *new-entry* resolution (no `position`/`order`
  passed) is correctly untouched — a new entry should always reflect
  current routing.
- `reconcile_pending_live_orders` (order-only, explicitly
  `Order.mode == LIVE`-filtered) is out of scope, confirmed already.

---

## Issue 2 🟡 — No recovery path once a close exhausts its retries

**Symptom**: once `close_position` hits `_MAX_EXIT_ORDER_ATTEMPTS` (5), it
alerts `exit_order_attempts_exhausted` and gives up permanently. Manual
square-off just re-runs the same `close_position` logic and hits the
identical wall. Only `POST /positions/{id}/manual-reconcile` (a
structurally different code path) actually worked today — and this isn't
new: the user reports the same dead end recurred yesterday too, resolved
only by hand after market close.

**Why manual-reconcile succeeds where close_position doesn't**: `close_
position_from_external_fill` (`execution_engine/paper/service.py:1306`)
never calls `broker.place_order()` at all — it derives `order_mode =
OrderMode(opening_order.mode)` directly from the position's own opening
order and writes a synthetic closing fill. It sidesteps the broker
entirely instead of retrying it.

### Phase 1: Implementation Plan

**Touchpoints**: `close_position`'s exhaustion block
(`service.py:~1020-1046`), `close_position_from_external_fill`
(`service.py:1306`), `reconciliation/service.py:_attempt_auto_repair`
(`~line 153`, already does "check `broker.get_recent_trades()`, recover a
real fill, or fall back to a synthetic reconcile" — currently only invoked
from `run_reconciliation`'s mismatch loop).

**Change**: when `close_position` reaches the exhaustion branch, instead of
alerting-and-returning-`None`, call the *same* `_attempt_auto_repair` logic
already proven in production (today's manual-reconcile is a human-triggered
instance of nearly this same code path) — extract/reuse it rather than
duplicating:
1. Look for a real matching fill via `broker.get_recent_trades()`.
2. Found → close via `close_position_from_external_fill(...,
   exit_reason=RECONCILED)`.
3. Not found (today's exact case — broker never had this position at
   all) → the existing `_attempt_auto_repair` broker-flat handling already
   covers this; reuse it rather than re-deriving.
4. Only if *that* also fails (e.g. broker unreachable) → keep the existing
   `exit_order_attempts_exhausted` CRITICAL alert as the final fallback —
   but rewrite its message to point at the endpoint that actually works
   (today's text says "needs manual square-off," which is misleading
   until Issue 2 itself is fixed).
- Wrap the new broker call in its own try/except — a transient network
  blip during auto-repair must not crash the exhaustion-handling block
  (same "never raises" discipline already applied to
  `place_protective_stop`/`cancel_resting_protective_stop`).
- New tests: exhaustion → auto-repair finds nothing real → falls through
  to today's alert (regression-safe); exhaustion → auto-repair recovers a
  real fill → position closes automatically, no CRITICAL alert.

### Phase 2: QC & Blast Radius Audit

**1. Regression & side-effect analysis**
- `_attempt_auto_repair` has only ever run inside `run_reconciliation`'s
  loop and its own locking assumptions there — confirm it's safe to call
  from inside `close_position`'s already-held `LOCK_EXECUTION_SINGLETON`
  scope (reentrant, per the 2026-09-02 precedent set for `close_position_
  from_external_fill` itself) before reusing it here.
- `exit_order_attempts_exhausted` will fire less often after this fix (by
  design) — anyone/anything watching that alert category's volume as a
  health signal needs to know the baseline shifts down; not a bug, but
  worth a one-line note wherever alert volume is reviewed.
- `get_recent_trades()` must behave correctly (clean empty list, not an
  exception) for every broker in active use, including mock — confirmed
  added 2026-09-02 for Shoonya/mock/backtest; re-verify the mock path
  specifically, since that's the exact one this incident needs.

**2. Systemic risks**
- Adds a real broker REST call directly into `close_position`'s critical
  path, at the exact moment 5 prior real order attempts already just
  happened in a tight loop — must go through the same shared rate limiter
  as every other broker call (`core/rate_limiter.py`), not bypass it.
- **Highest-value QC item**: false-positive attribution risk.
  `_attempt_auto_repair`'s matching logic was built for
  `run_reconciliation`'s symbol-level netting context; reusing it from
  `close_position`'s single-position context needs explicit re-review to
  confirm it can never attribute the *wrong* real trade (e.g. a different
  position sharing the same contract/symbol) to this position's close.
  Get this wrong and it silently misreports a position as closed at the
  wrong price/fill.

**3. Parallel feature parity**
- `exit_legs.py`'s per-leg exit function has the *identical* fixed-
  idempotency-key exhaustion gap, already flagged in CLAUDE.md as
  "deliberately left latent... multi-leg is paper-only today." Decide
  explicitly: fix it in the same pass, or re-confirm the "latent until
  multi-leg-live" deferral still holds — don't let it default-inherit a
  partial fix by accident.
- `eod_square_off.py`'s forced-close path — verify whether it calls
  `close_position` internally (inherits this fix for free) or has its own
  independent retry/exhaustion logic (would need the identical treatment
  separately).
- Manual square-off (`sessions.py`) and `protective_stop.py`'s
  cancel/resync paths call into `close_position`/`resolve_broker_for_
  position` — confirm they inherit this fix transitively, no separate
  patch needed.

---

## Issue 3 🟢 — Alerts never resolve until a 24h silence timer expires

**Symptom**: a `reconciliation_mismatch` alert fixed within minutes (via
manual-reconcile) kept re-pushing to Telegram every 15 minutes for the
rest of the day, with no way to acknowledge it — confirmed via 15-minute-
spaced Telegram sends in the logs, and confirmed no manual-resolve endpoint
exists anywhere in the API.

**Root cause**: `SystemAlert.is_resolved` is only ever written by
`AlertHousekeepingScheduler`'s hourly sweep, which closes a row once
`last_seen_at` is older than `system_alert_collapse_window_hours` (default
**24**). `system_alerts.py` has exactly one endpoint — a read-only `GET` —
no write path exists.

### Phase 1: Implementation Plan

**Step 1 — immediate, zero-code mitigation**: set
`APP_SYSTEM_ALERT_COLLAPSE_WINDOW_HOURS` (e.g. to `2`) via `systemctl
set-environment` on the box, restart. Safe because of the existing
row-collapse behavior — a genuinely still-recurring issue keeps bumping its
own `last_seen_at` and never ages out early; only an issue that's stopped
recurring benefits from a shorter window. Deployable same-day, independent
of Steps 2/3.

**Step 2 — permanent, primary fix**: in `run_reconciliation`
(`reconciliation/service.py:320`), after computing `mismatches` for a pass,
when the list is empty for a given `(workspace_id, trading_session_id,
order_mode)`, bulk-`UPDATE` any still-unresolved `reconciliation_mismatch`
`SystemAlert` rows for that session to `is_resolved=True,
resolved_at=now()`. Mirrors the existing bulk-UPDATE shape already used in
`alert_housekeeping.py` — no new table/column needed.

**Step 3 — general safety valve**: add `POST /system-alerts/{id}/resolve`
(gated `risk.override`, same permission as manual-reconcile), sets
`is_resolved=True, resolved_at=now()`, writes an audit event. Wire a
"Resolve" button into the Control Room Attention panel per unresolved row.
Covers every other alert category with the same structural gap that
doesn't have an obvious "next verified-clean state" to hook auto-resolution
into.

### Phase 2: QC & Blast Radius Audit

**1. Regression & side-effect analysis**
- Could "empty mismatches this pass" ever be true while a *different*
  mismatch is still genuinely outstanding on the same session? No —
  `mismatch_signature` already folds in every currently-mismatched symbol
  for the whole pass, so an empty list means zero mismatches session-wide
  right now. Safe by construction to resolve every standing alert for that
  session.

**2. Systemic risks**
- Bulk-resolving on every clean pass adds a write on every `PositionManager`
  poll cycle (~3s) even when there's nothing to resolve. Gate the `UPDATE`
  behind "only run if at least one unresolved row exists for this
  session" to avoid a wasted write every few seconds indefinitely.

**3. Parallel feature parity**
- `exit_order_unfilled`, `protective_stop_cancel_unresolved`,
  `strategy_run_stalled`, `market_data_failover_switch`, and others share
  the identical "only the 24h timer resolves it" gap. Step 3's general
  endpoint covers all of them as a manual fallback. Step 2's pattern
  (auto-resolve on next verified-good state) is reconciliation-specific;
  the same idea should be wired for `exit_order_unfilled`/`exit_order_
  attempts_exhausted` specifically inside Issue 2's fix (resolve the alert
  the moment the position actually, successfully closes) — do that in the
  same pass as Issue 2, not as an afterthought.

---

## Issue 4 🟡 — Market Terminal vs. Control Room disagree on what's open

**Symptom**: while position `281a2bb6…` was stuck (Issues 1/2 in
progress), Market Terminal kept showing it open while Control Room showed
nothing open.

**Root-caused during Issue 5's implementation work** (the local repo turned
out to have both `frontend/src/features/market-terminal/
MarketTerminalPage.tsx` and `frontend/src/features/control-room/
ControlRoomPage.tsx` — the OCI box's stale checkout that blocked the
original investigation was never the real blocker, only a wrong place to
look). **Independent bug, not just a symptom of Issues 1/2** — real
mechanism found, but a fix was not implemented this pass; this needs its
own design decision before touching code.

### What was found

Both pages ultimately read from the same backend (`GET /strategies/
running`, `GET /positions`, `GET /orders`), but via two structurally
different client-side paths:

- **Market Terminal** reads `RunningStrategyOut.open_position` directly,
  per strategy run — no session-identity assumption at all.
- **Control Room**'s entire trade table is scoped to *one specific
  `trading_session_id`* per bucket, chosen by `useSessionBuckets()`
  (`frontend/src/shared/hooks/useSessionBuckets.ts`): it buckets every
  session by broker type (`mock` → Paper, anything else → Live) and picks
  the single most-recently-started `status === 'active'` session in each
  bucket. `usePositions(liveSession.id)`/`usePositions(paperSession.id)`
  then query `/positions?trading_session_id=...` scoped to *only* that one
  chosen session — any position belonging to a *different* currently-
  active session is silently invisible to Control Room, no matter how real
  it is.

**Why "active" doesn't mean what it sounds like**: confirmed directly in
`create_session`'s own comment (`api/v1/sessions.py:148-156`) — *nothing in
this codebase ever transitions `TradingSession.status` to `ENDED`*. Every
session ever created stays `status='active'` forever; the only guard
against collision is "at most one active session per broker account, per
calendar day." So `useSessionBuckets`'s "most recently started active
session" heuristic only works correctly if there is truly one broker
account per bucket ever used. It buckets by **broker type**, not by a
specific broker account — if more than one non-mock (or more than one
mock) `BrokerAccount` has ever had a session started against it (e.g. a
second live-classified broker account created for testing, even briefly),
and that session's `started_at` is more recent than the account actually
being traded on, Control Room's entire Live (or Paper) trade view silently
points at the wrong session. Market Terminal has no equivalent failure
mode since it never picks "the one session" at all.

### Phase 2: QC & Blast Radius Audit (partial — no fix implemented, so most
of this is deferred to whoever picks this up next)

**1. Regression & side-effect analysis** — not applicable yet, no code
changed.

**2. Systemic risks** — the underlying issue (`TradingSession.status` never
reaching `ENDED`) is broader than just this display bug: any other code
that queries by `status == 'active'` expecting "currently in use" carries
the same risk. Worth a follow-up grep for other `status == 'active'`
call sites before assuming this is Control Room-only.

**3. Parallel feature parity** — any other page using `useSessionBuckets`
(none found yet besides Control Room, but not exhaustively checked) would
share the exact same exposure.

**Two candidate fixes, not decided**:
- (a) Backend: give sessions a real lifecycle — an explicit `ENDED`
  transition when a new session starts for the same broker account (or
  when the trading day rolls over), so `status='active'` genuinely means
  "in use right now" again.
- (b) Frontend: make `useSessionBuckets` key off a specific, explicitly-
  configured "the broker account actually in use" rather than "whichever
  broker-typed account most recently had a session started," removing the
  ambiguity without touching session lifecycle semantics at all.

(a) is the more correct, durable fix (the `ENDED`-never-fires gap is a real
gap on its own, independent of this display bug) but is a bigger change
with its own blast radius (anything else assuming a session stays active
forever); (b) is smaller and Control-Room-scoped but treats the symptom,
not the underlying lifecycle gap. Recommend deciding this explicitly with
the user before implementing either.

---

## Issue 5 🟢 — Session-wide consecutive-loss pause is a deadlock; redesign to a per-strategy auto live↔paper circuit breaker

**Symptom / motivation**: today's live session hit `consecutive_loss_
pause_active` after 2 losses on the session-wide counter
(`trading_sessions.consecutive_losses` vs. `risk_limit_configs.
consecutive_loss_pause_threshold`), and stayed blocked for the rest of the
session — the counter only resets on a win, but new entries are blocked
while paused, so there is no path back to a win. In practice this is an
indefinite same-day lockout, not a bounded pause, and it applies
session-wide (one strategy's losses can pause every other strategy too).

**Researched best practice** (MQL5, LuxAlgo, TradingGenie, prop-firm
risk-desk write-ups): a bounded, *time-based* cooldown — not a win-gated
pause — is the standard pattern, directly analogous to the software
circuit-breaker state machine (Closed → Open → Half-Open). Typical
numbers found: 2–3 consecutive losses as the trip threshold; this
system's own data shows roughly one signal per 30-45 minutes per
strategy, so a cooldown needs to be at least that long to be meaningful.

**Design evolution (recorded for context — final version below supersedes
all of this)**: the first draft blocked new entries during the cooldown.
Refined once during design review to auto-flip the strategy to
`force_paper` instead of blocking — this gives real observational
evidence during the cooldown (paper fills, not silence) and, critically,
means a strategy already on manual `force_paper` can never trip this
mechanism at all (no live losses to count) — which is what makes deferring
"should paper strategies be pausable too" a non-issue rather than a
decision that had to be made. **Hard dependency**: this design only works
safely once **Issue 1** lands — flipping `runtime_mode` mid-position is
exactly the mechanism that caused today's incident; Issue 1's fix (a
position/order stays pinned to whichever broker it was actually opened
against) is what makes flipping `runtime_mode` mid-flight safe. Issue 5
must not be implemented before Issue 1 is deployed and verified.

### Final agreed design (locked 2026-09-04)

- **Strategy is always active** — never blocked, never stopped. Only
  `runtime_mode` toggles between live and paper; an already-open position
  is untouched either way (pinned per Issue 1's fix).
- **Severity filter** (unchanged from the original draft, applies at every
  tier): a losing trade only counts toward the streak if it's *severe* —
  `abs(realized_pnl) ≥ 0.5 × qty × lot_size × |entry_price −
  original_stop_price|`, where `original_stop_price` is the immutable
  `trade_intents.stop_price` captured at dispatch (never a since-trailed
  value). Exits with no clean stop reference (`structure_break`, EOD
  square-off, `margin_breach`) always count as severe. A marginal loss is
  invisible to the counter: no increment, no reset. Only counts when the
  strategy was actually live-routed for that trade — paper losses (manual
  or auto-cooldown) never feed this counter.
- **Ladder**:
  1. **Tier 0 (Normal)** — live-routed as configured.
  2. **Trip 1** — 3 consecutive severe live losses → auto-flip to paper,
     **60 min**.
  3. **Auto-resume** — after 60 min, auto-flip back to *full* live trading
     (not a single trial trade — genuine resumption).
  4. **Trip 2** — the very next severe loss after that resume (single
     loss, not another streak of 3) → auto-flip to paper, **90 min** →
     auto-resume to live after.
  5. **Trip 3** — next severe loss after that → auto-flip to paper for
     the **rest of the trading day**, no further auto-resume attempt
     today. Strategy keeps actively trading in paper through end of
     session. Resets fresh to Tier 0 at the next day's new session.
  6. **Any win, at any point** (while live-routed) → full reset to Tier 0.
- **Manual override always wins**: a human editing `runtime_mode` at any
  time — flipping back to live early, or choosing to stay/stop on paper —
  cancels any pending auto-timer and clears the auto-cooldown state. The
  strategy stays under manual control until a *new* trip starts a fresh
  auto-cycle. If never touched, the ladder runs itself end-to-end.
- **Provenance tracking**: every auto-flip is tagged distinctly from a
  manual one (`runtime_mode_source: circuit_breaker | manual`), so the
  strategy list can show "auto-parked, resumes at HH:MM" vs. "you set
  this" without ambiguity, and so manual edits know what to cancel.
- **Notifications**: both the trip (flip-to-paper) and the resume
  (flip-to-live) get their own alert — the whole point is being able to
  "let it run and evaluate later," which only works if both transitions
  are visible after the fact.
- **Explicitly out of scope for this pass**: reducing position size
  instead of/alongside flipping mode. Parked on the future-ideas list.

### Phase 1: Implementation Plan

**Schema change** — split across two tables, deliberately:
- On `strategy_runs` (resets naturally per trading day, already survives a
  backend restart via the existing resume path):
  - `consecutive_severe_losses: int, default 0`
  - `cooldown_tier: int, default 0` — 0=normal, 1=after trip 1, 2=after
    trip 2, 3=paper-for-rest-of-day (terminal for today).
  - `cooldown_until: timestamp, nullable` — when the current auto-paper
    window ends; `NULL` when not in an active timed cooldown (including
    tier 3, which has no timer).
- On `strategy_configs` (the field that actually affects routing, and
  where the manual-edit endpoint already lives):
  - `runtime_mode_source: 'manual' | 'circuit_breaker', nullable` —
    which actor last set `runtime_mode`; `NULL`/`manual` for anything set
    via the existing human-facing endpoint.
- New Alembic migration, nullable/defaulted columns, no backfill needed.

**Logic touchpoints**:
1. **Trip detection**, at position-close time (`service.py:~885`, same
   place `trading_session.consecutive_losses` is updated today) — replace
   the session-wide update with a per-`strategy_run` one:
   - Only evaluated when the trade was live-routed. Compute severity per
     the formula above.
   - Win → reset `consecutive_severe_losses=0, cooldown_tier=0,
     cooldown_until=NULL` (only meaningful if tier was already >0 — a
     normal live win at tier 0 is already a no-op).
   - Severe loss at tier 0 → increment counter; at 3 → `cooldown_tier=1,
     cooldown_until=now()+60min, consecutive_severe_losses=0`, **and**
     write `strategy_configs.runtime_mode=FORCE_PAPER,
     runtime_mode_source='circuit_breaker'` (acquire a per-strategy-config
     lock around this read-modify-write — see Phase 2, point 1).
   - Severe loss at tier 1 → `cooldown_tier=2, cooldown_until=now()+90min`
     (runtime_mode stays FORCE_PAPER — already flipped).
   - Severe loss at tier 2 → `cooldown_tier=3, cooldown_until=NULL`
     (stays FORCE_PAPER, no further timer today).
   - Marginal loss → no state change.
   - Send the trip alert (Telegram) on any tier increment.
2. **Auto-resume check**, added to `StrategyRunner.run_cycle` alongside
   the existing `resolve_qty_lots` per-cycle re-resolution
   (`runner.py:~332`) — the natural home, since it already re-reads
   config every cycle for this exact strategy_run: if `cooldown_tier ∈
   {1,2}` and `now() ≥ cooldown_until`, flip `strategy_configs.
   runtime_mode` back to `NULL` (live) with `runtime_mode_source=
   'circuit_breaker'`, clear `cooldown_tier=0, cooldown_until=NULL` on
   the strategy_run (same per-config lock as above), send the resume
   alert. Wrap the firewall-check path this re-enables in a try/except
   for `ConfigurationError` (see Phase 2, point 5) — fail toward staying
   on paper, never crash the cycle.
3. **Manual-edit endpoint change**, `api/v1/strategies.py:~705` — any
   human-initiated `runtime_mode` write must also clear `cooldown_tier=0,
   cooldown_until=NULL` on the current strategy_run and set
   `runtime_mode_source='manual'`, so a stale auto-timer can never later
   override a deliberate manual decision.
4. **Remove** the session-wide `consecutive_loss_pause_active` check in
   `risk_engine.service.evaluate_trade_intent` (`~line 592`) entirely —
   confirmed replace, not augment (see Phase 2 audit below for what that
   removal costs and why it's acceptable).
5. New unit tests: severity formula (severe vs. marginal, both directions
   of the boundary); full ladder walk including auto-resume timing; win
   resets at every tier; marginal losses never move the counter;
   no-clean-stop-reference exits always severe; a manual edit mid-cooldown
   cancels the pending auto-timer; two concurrent writers (trip vs.
   resume) can't corrupt state; existing open positions keep being
   monitored/exited unaffected by any flip (regression-tests Issue 1's fix
   at the same time); per-strategy isolation (one strategy's ladder never
   affects a sibling).

### Phase 2: QC & Blast Radius Audit

**1. Two independent background writers can race on the same row**
Trip detection runs on `PositionManager`'s background thread (position
close); auto-resume runs on `StrategyRunner`'s own, separate background
thread (`run_cycle`) — confirmed these are genuinely distinct
classes/threads in source. Both can write `strategy_configs.runtime_mode`
for the same strategy. A trip landing at the exact moment a resume was
about to fire (or vice versa) is a real race. Needs a per-strategy-config
advisory lock around the read-modify-write in both places — the same
discipline this codebase already applies to every other cross-thread
mutable-state touchpoint (`LOCK_EXECUTION_SINGLETON` et al.) — not a bare
unguarded UPDATE.

**2. Manual override must actively cancel, not just get raced against**
Confirmed in source (`api/v1/strategies.py:705-706`) that the manual-edit
endpoint today is a simple field diff/write with no concept of clearing a
pending auto-cooldown. Without Phase 1 point 3 above, "manual always wins"
is only true until the next `run_cycle` tick silently reverts it — a
real regression risk if this specific change is missed.

**3. Audit attribution — precedent already exists, low risk**
Confirmed `ActorType.SYSTEM` already exists and is already used for other
non-human-triggered audit events in this codebase (mode transitions,
reconciliation auto-recovery). Auto-flip/auto-resume writes should reuse
this existing pattern, not invent a new one.

**4. Daily reset correctness**
`runtime_mode` lives on `strategy_configs` (persists across days);
tier/counter/`cooldown_until` live on `strategy_runs` (naturally per-day).
Confirm the trip/resume logic always writes across both tables together
(never updates one without the other) — a partial write (e.g. tier reset
on a new day's fresh `strategy_run` while a stale `runtime_mode=
FORCE_PAPER, source=circuit_breaker` lingers from yesterday with no
matching cooldown state to ever clear it) would strand a strategy on
paper indefinitely with no visible reason.

**5. Firewall interaction on auto-resume**
`get_execution_broker`'s Phase-7 instrument firewall only fires when
resolving a real broker. If `active_live_instruments` was tightened while
a strategy was mid-cooldown, auto-resume could hit `ConfigurationError`
unexpectedly. Must fail toward staying on paper (log + alert), never
crash `run_cycle` over this — already captured in Phase 1 point 2, called
out again here because it's the one place this feature could turn a
config change elsewhere into an unhandled exception.

**6. Off-hours edge case — benign, but document it as intentional**
If `cooldown_until` lands after the strategy's own EOD cutoff, `run_cycle`
has already stopped executing for the day, so the resume check simply
never fires until tomorrow's fresh run (Tier 0). No special-casing needed
— but worth documenting so a "why didn't it resume" question later isn't
mistaken for a bug.

**7. Regression: what removing the session-wide check actually costs**
The session-wide check protected against *many different strategies*
each losing a little at once — a pattern a per-strategy breaker can't see
(worst case: 8 strategies each take 2 severe losses — 16 total — before
any one of them individually trips). This gap is real but well-bounded by
two mechanisms that stay untouched: `max_concurrent_positions` (currently
2 — only two positions can ever be open across the whole session at once,
regardless of strategy count, which throttles how much can go wrong
simultaneously) and `daily_loss_cap` (currently ₹40,000 — an absolute
backstop on total session damage regardless of which/how many strategies
caused it). The pattern-detection layer moves to per-strategy; the
dollar-amount backstop stays session-wide. Confirmed acceptable given
these two mitigants, not overlooked.
- 3 existing tests need rewriting, not just new tests added:
  `test_evaluate_trade_intent_consecutive_loss_pause`,
  `test_consecutive_loss_pause_does_not_block_a_paper_routed_strategy`
  (note: this test's docstring documents the *opposite* guarantee from a
  deliberate 2026-08-19 fix — the new design's "a strategy already on
  manual force_paper can never trip this" property is what keeps that
  guarantee true in spirit even though the mechanism changes),
  `test_record_trade_outcome_effects_increments_consecutive_losses`.
- Do **not** drop `trading_sessions.consecutive_losses`/`risk_limit_
  configs.consecutive_loss_pause_threshold` columns in this same change —
  leave them inert/unused, drop later once the new system's proven.
  Keeps this change's migration additive-only.
- The risk-decision rejection reason `consecutive_loss_pause_active`
  disappears entirely (no replacement reason needed — a cooling-down
  strategy no longer rejects at the risk-gate at all, it just doesn't
  generate live orders because it's routed to paper) — anything
  downstream (dashboards, alert text) matching the old string needs
  updating.

**8. Parallel feature parity**
- `max_trades_per_day`'s live-only counting (confirmed in source: explicit
  `Order.mode == OrderMode.LIVE` filter, already scoped per
  `strategy_config_id`) already correctly excludes paper trades taken
  during a cooldown from counting against the live daily cap — confirmed
  correct by construction, no change needed.
- Reconciliation's existing mode-scoping (2026-08-19 fix) already handles
  a session with mixed live/paper positions — this feature produces more
  frequent mode-mixing within a single strategy's day than before, but not
  a new *kind* of mixing; confirmed no new reconciliation gap.
- The auto-spawner's daily idempotency (`_has_run_today`/`_has_active_
  run`) keys off strategy_run existence, not `runtime_mode` value —
  confirmed repeated flips can't cause a duplicate spawn or a skipped one.
- Frontend: needs a clear "auto-parked, resumes at HH:MM" indicator
  (surfacing `cooldown_tier`/`cooldown_until`/`runtime_mode_source` on
  whatever endpoint already reports strategy status, e.g. `GET
  /strategies/running`) distinct from "you manually set this to paper" —
  the exact ambiguity that made today's incident confusing to diagnose in
  the first place must not be reintroduced by an unlabeled automatic
  version of the same state.
- Reporting/trade-log exports: a strategy's trades within one day can now
  legitimately span live and paper more often than before (multiple
  flips per day instead of at most one manual change) — cosmetic/display
  concern only, not a correctness issue, but worth a glance once built to
  confirm per-trade mode is still clearly labeled in exports.

---

## Suggested sequencing

1. **Issue 1** — root cause of today's actual incident, single well-scoped
   touchpoint, plan is solid.
2. **Issue 2** — directly related, reuses code Issue 1's investigation
   already surfaced (`close_position_from_external_fill`,
   `_attempt_auto_repair`); doing it right after 1 while the context is
   fresh is efficient, and it independently matters (covers non-paper
   causes of exhaustion too).
3. **Issue 3** — fully independent, Step 1 (env var) can actually be done
   today with zero code risk regardless of when 1/2 land; Steps 2/3 whenever
   convenient.
4. **Issue 5** — **hard dependency on Issue 1**: the auto-flip mechanism
   is only safe once a position/order stays pinned to whichever broker it
   was actually opened against. Do not implement before Issue 1 is
   deployed and verified live.
5. **Issue 4** — re-check only after 1 and 2 are deployed; likely needs no
   separate fix at all.
