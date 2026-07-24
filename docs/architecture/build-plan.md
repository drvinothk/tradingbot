# Algorithmic Trading Platform — Build Plan

> Copied into the repo from the original Claude Code plan-mode approval so it
> travels with the codebase (a fresh session/clone otherwise has no way to
> see it). See `CLAUDE.md` at the repo root for current phase status.

## Context

The user wants a personal algorithmic options-trading system for Nifty/Bank Nifty,
starting with Shoonya (Finvasia) as the sole broker for now but built broker-agnostic
internally, local-first now and cloud-ready later. Two source documents ("App specific
info.docx", "Trading framework.docx") already define detailed requirements: a
non-AI deterministic execution core (AI stays optional/advisory, wired in later via
credentials supplied in a file), paper-trading-first discipline, one execution
authority, full auditability, fail-safe-over-fail-open, and six specific intraday
option-scalping strategies with strict shared risk governance (max 2 concurrent live
positions, max 3-5 live trades/day, pause after 2 consecutive losses, hard daily loss
cap, mandatory stop-loss on every trade).

This plan turns that blueprint into a buildable sequence. It also folds in gaps I
identified during review (margin checks, instrument-master refresh, clock-drift
handling, reconciliation cadence, rate limiting) and three decisions the user just
made: prefer Docker for Postgres/Redis if available (installing it if not, since it
aligns with the later cloud stages), verify the home connection's static-IP situation
with Airtel before relying on it, and use an **activity-based** sleep inhibitor (not a
blanket market-hours sleep-disable) tied to whether a trading session is actively
scanning or has open positions.

Nothing has been built yet — this is a from-scratch repo. The plan below is the
design; Phase 0 is the first thing to actually implement.

## Locked architectural decisions

- **Modular monolith**, not 13 deployed microservices. Same logical boundaries as the
  source doc's service table, but as internal Python modules in one FastAPI process.
  Split into separate deployables only at Stage 3 (cloud), if ever needed.
- **Python + FastAPI + PostgreSQL + Redis + React.** Shoonya's current API uses a
  genuine OAuth-style browser redirect (confirmed by reading shoonya.com/api-documentation
  directly — the earlier README-based conclusion that it was a direct TOTP-only REST
  call was wrong, see the Phase 5 note below). Confirmed also: only one WebSocket
  connection is supported per session — this constrains the Broker Adapter to a single
  shared WS, not one per consuming module.
- **Local password auth** (Argon2), schema shaped so external OIDC can be swapped in
  later without migration. No external auth integration now.
- **React SPA** over REST + WebSocket (live P&L/position/order updates need it).
- Workspace/tenant placeholder column added now; no multi-tenant logic behind it.
- **Broker-agnostic port/adapter pattern**: every module depends on an abstract
  `broker_port` interface; a **mock/replay adapter** is built first and used for all
  strategy/risk/execution development, with the real Shoonya adapter swapped in later
  behind the same interface, unchanged by callers.
- **Docker Desktop (WSL2 backend) for Postgres + Redis**, app runs natively on
  Windows. Check whether Docker Desktop is already installed at the start of Phase 0;
  if not, install it — preferred over a native Postgres/Redis install because it keeps
  local dev consistent with the Stage 2/3 containerized direction the doc already
  commits to.
- **IP/CGNAT hedge**: don't block the whole plan on this, but don't ignore it either.
  Action item in Phase 0: contact Airtel (or check the account portal) to confirm
  whether the current broadband plan includes a static public IP or is behind CGNAT.
  Architecturally, hedge both ways now — the Shoonya adapter's network path (host/port
  it connects through) is a config value, not hardcoded, so if CGNAT turns out to be a
  blocker, a small always-on static-IP relay (cheap VPS proxying only the broker
  connection) can be introduced later without touching adapter code.
- **Sleep policy is activity-based, not time-based**: a small "sleep inhibitor"
  component (Windows `SetThreadExecutionState`, called from the always-on backend
  process) is acquired whenever a `trading_session` is actively scanning market data
  or holds an open position, and released once the session ends and all positions are
  flat. Normal sleep is otherwise left alone. This lives in the Scheduler/Session
  module, not as an OS-level power-plan schedule.
- **Engine/UI decoupling**: the backend (API + background workers) runs as a
  persistent Windows process (Windows Service, via `pywin32`/NSSM) independent of any
  browser tab. Closing the UI must never affect a live position — this is enforced by
  construction (UI has no execution code, only reads/commands over the network),
  not just by convention.
- **Kill-switch default behavior**: on entering `kill_switch`, existing live positions
  are frozen and alerted, not auto-square-off'd — an automatic square-off triggered by
  what might be a connectivity glitch could itself misfire. This is a config flag,
  revisitable later, but freeze-and-alert is the safer starting default.
- **Same-strike collision handling**: "max 2 concurrent live positions" is a global
  cap across all strategies (not per-strategy), enforced by Risk Service. This and
  the budget/concurrency/trade-count checks all share one fix: Risk Service evaluates
  TradeIntents one at a time through a single serialized queue (detailed in the
  schema section below) rather than concurrently, so two strategies can never
  independently enter the same strike, or jointly overspend budget, in the gap
  between two parallel check-then-act evaluations.
- Backtesting against historical data is explicitly out of scope for now — paper
  trading is the validation mechanism, matching the source doc's own build order.
- **Execution mode is orthogonal to safety mode.** Paper vs guarded-live vs live is
  the session-level safety mode (state machine above). Independently, each running
  strategy is started in either **Auto-execute** (fires the moment Risk approves) or
  **Approval-required** (Risk-approved intents pause and wait for an explicit human
  approve/reject) — either can be combined with either safety mode, e.g. approval-
  required-in-paper while learning to trust a new strategy, or auto-execute-in-paper
  to gather stats fast. See the new section below for the full design.
- **Cash is the default funding mode**, with MTF (Margin Trading Facility) available
  as an explicit opt-in per broker account, since the user expects to trade mostly
  with cash on hand but wants the option open. Capital-required and margin
  calculations must be funding-mode-aware from the start rather than assuming one or
  the other.
- **Startup recovery, not a cold start.** If the backend process restarts (crash,
  Windows update, manual restart) while a `trading_session` shows open positions, it
  must not come back up idle — `main.py`'s startup sequence checks for an active
  session with open positions and immediately triggers a reconciliation pass plus
  resumption of stop/trail management before doing anything else. Without this, a
  crash during a live position becomes an unmanaged naked position until someone
  notices, which defeats the whole safety design. This is the concrete mitigation for
  the source doc's "local machine sleep/reboot" scenario.
- **Disk space is a monitored health signal**, not an assumption. The Scheduler's
  periodic health checks (alongside NTP drift and WS liveness) include free-disk-space
  on the DB volume, alerting and tripping `degraded_mode` below a threshold — a full
  disk otherwise silently breaks DB writes and audit logging, which is a uniquely bad
  failure mode for a system whose core safety property is "if it isn't audited, it
  didn't happen."
- **Wrong-broker-account protection**: since the schema supports multiple broker
  accounts per user from day one, the session-start form (same screen as the daily
  plan) always requires an explicit, named broker-account selection with no
  remembered default silently reused, and the mode banner/running-strategies view
  keeps the active broker account's label visible at all times.

## Repository structure

```
trading-bot/
  backend/
    app/
      main.py                        # app factory; startup: NTP check, mode-machine init, singleton lock check
      config/
        settings.py                  # pydantic Settings: DBSettings, RedisSettings, ShoonyaSettings, RiskDefaults
        credentials/                 # gitignored; Shoonya + future AI-provider secrets; restricted file perms
        environments/local.env.example, cloud.env.example
      core/                          # cross-cutting infra, no trading logic
        security/                    # Argon2 hashing, session tokens, RBAC dependency
        db/                          # SQLAlchemy engine/session, declarative Base
        redis_client.py
        clock.py                     # NTP drift check (startup + periodic) -> SystemAlert / mode signal
        rate_limiter.py              # token-bucket wrapping outbound broker calls
        locking.py                   # Postgres advisory lock -> Execution Service singleton
        idempotency.py               # idempotency key generation + atomic pre-dispatch persistence
        eventbus.py                  # in-process pub/sub: order-state-change -> reconciliation trigger
        sleep_inhibitor.py           # Windows SetThreadExecutionState, acquired/released by session+position state
        modes/
          state_machine.py           # the 6-mode state machine
          transitions.py             # guards/preconditions per transition
      domain/                        # ORM models + pydantic schemas, one subpackage per bounded context
        identity/  market/  strategy/  risk/  execution/  broker/  audit/  ops/
      modules/                       # the logical services as internal modules
        identity_access/
        broker_adapter/
          base/broker_port.py        # abstract interface: authenticate, place/modify/cancel, get_positions, subscribe_quotes...
          base/contracts.py          # broker-agnostic DTOs
          mock/                      # replay/mock adapter — used Phases 1-4 and in all tests, forever
          shoonya/                   # ALL Shoonya-specific code lives ONLY here
            auth.py                  # TOTP + direct REST login -> susertoken
            rest_client.py  ws_client.py  normalizer.py
        market_data/
          ingestion.py  indicators/ (VWAP, EMA9, EMA20)  option_chain.py
        strategy_engine/
          interface.py               # shared Strategy ABC — emits Signal/TradeIntent ONLY, no Order/Position access
          strike_ranking/            # shared engine: spread/volume/OI/premium-fit/depth
          strategies/
            orb.py  vwap_pullback.py  ema_micro_pullback.py
            oi_volume_confirmed.py  liquidity_sweep_reversal.py  depth_imbalance_scalp.py
        risk_engine/                 # sole enforcer of all limits, incl. margin/funds check
        execution_engine/            # sole order writer; singleton-locked, idempotent
          paper/  live/
        reconciliation/              # event-triggered + polling (5-15s market hours)
        audit_service/
        reporting/
        scheduler/                   # session open/close, health checks, EOD square-off, daily instrument/strike sync
        ai_extension/                # advisory-only; read-only ports; never imports execution/risk write paths
      api/v1/ (auth, strategies, sessions, orders, positions, risk, reports, audit, admin)
      api/websocket/ (quotes_ws, positions_ws, alerts_ws)
      workers/ (market_data_worker.py, reconciliation_worker.py, scheduler_worker.py, strategy_worker.py)
    migrations/                      # Alembic
    tests/unit/ integration/ fixtures/ (recorded tick/depth/chain sessions for deterministic replay)
    alembic.ini  pyproject.toml
  frontend/
    src/features/ (auth, sessions, strategies, orders, positions, risk, reports, audit, admin)
    src/shared/
    src/app/ (routing, layout, websocket provider, always-visible mode banner)
  ops/
    docker/docker-compose.local.yml  # Postgres + Redis only; app runs natively
    docker/Dockerfile.backend, Dockerfile.frontend  # used starting Stage 2
    scripts/                         # Windows Service install/launcher scripts
    windows_service/                 # pywin32 service wrapper for the backend process
  docs/architecture/ docs/runbooks/  # kill-switch procedure, reconciliation-lock review, EOD checklist
  .env.example  .gitignore
```

Key rule this layout encodes: every module depends on `broker_adapter/base/broker_port.py`,
never on `shoonya/` directly. A composition-root in `main.py` decides whether `mock`
or `shoonya` gets injected. This is what makes Phases 1-4 buildable and fully testable
with zero Shoonya code, and what keeps "broker-agnostic" real rather than aspirational.

## Database schema (outline)

**Identity & access**: `workspaces` (placeholder, single row) · `users` (email,
Argon2 hash, workspace FK) · `roles` / `permissions` / `role_permissions` ·
`user_roles` · `sessions` (login sessions) · `broker_accounts` (incl. primary/backup
IP) · `user_broker_access`.

**Trading session & mode**: `trading_sessions` (mode enum, cutoff_time, status,
**budget_amount, daily_target_profit, daily_loss_cap, funding_mode, broker_account_id
FK, entries_paused_reason** [nullable enum: null / daily_target_reached / admin_pause]
— the first four pre-filled from `risk_limit_configs` defaults but editable by the
user at session start; field names deliberately match `risk_limit_configs` 1:1 so the
override relationship is unambiguous) · `session_mode_transitions` (from/to mode,
trigger_type, triggered_by, reason — full audit trail of every mode change).

`entries_paused_reason` is deliberately **not** the same thing as `degraded_mode` —
hitting a profit target is a goal being met, not a fault, and conflating the two would
make a good day look like an incident in the audit trail and force the same
heavyweight recovery path as a real degraded state. It's a lightweight flag Risk
Service checks independently of the 6-state machine below: when set, new entries are
blocked (existing positions still fully managed) but clearing it back to null is a
single un-gated Admin toggle, not a formal mode transition.

**Strategy runtime**: `strategy_runs` (strategy_config_id FK, trading_session_id FK,
`execution_mode` enum[auto, approval_required], `status` enum[scanning, in_position,
paused, stopped], started_at, stopped_at, started_by_user_id) — one row per "Run
Strategy X" command; this is what the running-strategies dashboard queries.
`pending_trade_approvals` (trade_intent_id FK unique, strategy_run_id FK, `status`
enum[pending, approved, rejected, expired], capital_required, breakeven_price,
pnl_scenarios jsonb, expires_at, decided_by_user_id, decided_at) — created whenever
a Risk-approved TradeIntent belongs to an approval-required run; auto-expired by the
Scheduler if not acted on before `expires_at` so a stale decision can never silently
fire.

**Market/instrument**: `instruments` · `option_contracts` · `instrument_master_sync_log`
(daily strike/expiry sync job) · `quote_ticks` (high volume, needs a retention/rollup
decision before Phase 5) · `depth_snapshots` · `option_chain_snapshots` ·
`indicator_snapshots`.

**Strategy**: `strategy_configs` (name, params, `status` = research/paper/
paper_plus_guarded_live/live — the graduation field) · `signals` · `trade_intents`
(`idempotency_key` unique, not null, `status` = pending_risk / risk_rejected /
pending_approval / human_rejected / expired / dispatched — a Risk-approved intent
that a human later rejects, or that expires unactioned, must be distinguishable from
one that actually executed, otherwise the paper-trading scorecard would silently
overcount "approved" trades that never happened).

**Risk**: `risk_limit_configs` (versioned: max_concurrent, max_trades_per_day,
consecutive_loss_pause_threshold, daily_loss_cap, daily_target_profit,
per_trade_lot_cap — these are the *system defaults* that pre-fill a session's
editable budget/target/max-loss fields above) · `risk_decisions` (approve/reject,
reasons, checked_margin, funding_mode used, and the pre-trade analytics snapshot:
capital_required, breakeven_price, pnl_scenarios — computed once at approval time so
the numbers shown to the user match exactly what Risk evaluated).

Lot size for every `risk_decisions`/`orders` row is always read server-side from
`option_contracts` → `instruments.lot_size`, never accepted as a client- or
strategy-supplied value — this removes "wrong lot size" as a class of error entirely
rather than relying on a limit check to catch it after the fact.

Risk Service evaluates TradeIntents **one at a time through a single serialized
queue** (the same advisory-lock mechanism used for the Execution singleton), not
concurrently. This is what actually makes the concurrency cap, daily trade count,
budget-vs-committed-capital check, and same-strike lock correct — all four are
check-then-act sequences that would otherwise have a race window if two strategies'
intents were evaluated in parallel (e.g. two intents each individually fit the
remaining budget but together exceed it). Serializing costs nothing here: the
system's own governance caps trading at a handful of trades a day, so there is no
throughput to sacrifice for this safety property.

**Execution**: `orders` (`idempotency_key` unique, mode=paper/live, `filled_qty`) ·
`order_events` (raw broker events, retained for audit) · `positions` · `stop_plans`
(status includes `confirmed` — specifically to satisfy "verify the stop actually got
placed") · `trail_plans` · `trade_outcomes` (realized_pnl, slippage, exit_reason).

Stop-loss quantity is **always derived from `orders.filled_qty`, recomputed on every
fill event**, never fixed at the originally-requested quantity — this is what
actually closes the "partial fill with incorrect stop quantity" scenario the source
doc calls out: a partial fill followed by a later additional fill must trigger a stop
quantity adjustment, not leave the stop sized for the first fill only.

**Broker sync**: `broker_sync_states` · `reconciliation_runs` (trigger_type =
event/poll, mismatches_found, action_taken).

Modify/cancel requests get an explicit **in-flight state** (`orders.status` includes
`modify_pending` / `cancel_pending`, not just a binary sent/acked) with a timeout —
if the broker hasn't confirmed within a short window, Reconciliation actively
rechecks rather than the system silently assuming the modify/cancel succeeded. This
closes the "modify/cancel acknowledged late" scenario, which is otherwise easy to
mishandle optimistically.

**Audit**: `audit_events` — hash-chained (`prev_hash`/`hash`) for tamper-evidence,
queryable by trade/user/broker-account/strategy/session. Indexed on each of those FK
columns individually plus `(entity_type, entity_id)` and `ts` — this table is read
constantly by reporting, reconciliation review, and the running-strategies view, so
its query paths matter even though write volume is low.

**Ops**: `system_alerts` · `metric_series` · `scheduler_job_runs`.

Both `trade_intents.idempotency_key` and `orders.idempotency_key` are unique
constraints persisted in the same transaction that marks a TradeIntent "dispatched" —
this is what makes retries detect "already sent" rather than relying on convention.

## Safe operating mode state machine

States: `paper_only` (default) → `paper_plus_guarded_live` → `live_enabled`, plus
`degraded_mode`, `reconciliation_lock`, `kill_switch`.

Global rules:
1. Mode lives on `trading_sessions.mode`; every change writes `session_mode_transitions`
   + `audit_events` in one transaction, under the same Postgres advisory lock used for
   the Execution singleton.
2. `degraded_mode` remembers `prior_mode` for recovery.
3. `kill_switch` can only go back to `paper_only` — never directly to a live mode.
4. **Automatic transitions only ever move down in privilege.** Moving up (paper →
   guarded-live, guarded-live → live, degraded → any live mode, kill_switch → paper)
   always requires a manual, permissioned, reason-logged action — even when the
   system detects conditions look fine. This is fail-safe-over-fail-open applied
   concretely.

| From | To | Trigger |
|---|---|---|
| paper_only | paper_plus_guarded_live | manual (Admin), requires ≥1 graduated strategy + healthy preconditions |
| paper_only | kill_switch | manual, or fatal startup fault |
| paper_plus_guarded_live | live_enabled | manual (Admin), requires clean reconciliation state |
| paper_plus_guarded_live | paper_only | manual demotion, or all live strategies auto-paused by Risk Service |
| paper_plus_guarded_live / live_enabled | degraded_mode | recoverable fault: WS stale, clock drift over threshold, rate-limiter trip, transient Redis/DB failure, low disk space |
| paper_plus_guarded_live / live_enabled | reconciliation_lock | unresolved local-vs-broker mismatch |
| live_enabled | paper_plus_guarded_live | manual only — daily loss cap goes straight to kill_switch, never a soft step-down |
| any live/guarded mode | kill_switch | daily loss cap breached, second-writer lock contention, or manual |
| degraded_mode | prior_mode | fault cleared + health recheck passes, but still requires an Admin confirm click to resume anything above paper_only |
| reconciliation_lock | paper_only | manual only, after Admin reviews and resolves the mismatch |
| kill_switch | paper_only | manual only, reason required, pre-resume checklist (re-auth, clean reconciliation, NTP ok) |

Behavior notes: `kill_switch` freezes and alerts on existing live positions rather
than auto-square-off (decision above). `degraded_mode` blocks new entries but keeps
managing existing stop/trail/exit. `reconciliation_lock` freezes new entries for the
affected broker account only.

## Execution modes, daily plan, pre-trade analytics, and the running-strategies view

This section covers the trading-workflow features the user asked for directly, on
top of the safety-mode machinery above.

**Daily trading plan.** At the start of each trading session, the user is shown a
form pre-filled from `risk_limit_configs` defaults: budget (cash allocated for the
day), daily target profit, daily loss cap, and funding mode (cash/MTF). These are
editable per day and stored on `trading_sessions`. Two independent triggers watch
cumulative realized P&L for the session: hitting `daily_loss_cap` escalates straight
to `kill_switch` (per the existing rule — no soft step-down on a loss breach);
hitting `daily_target_profit` sets `entries_paused_reason = daily_target_reached` —
new entries blocked, existing positions still fully managed, clearable by the Admin
with a single toggle if they choose to keep trading — deliberately not a
`degraded_mode` transition, since reaching a profit goal isn't a fault condition and
audit_events should read as a good day, not an incident. Both checks run in Risk
Service, evaluated on every TradeIntent and after every closed trade.

**Auto-execute vs Approval-required.** Each `strategy_runs` row picks one. In
Auto-execute, a Risk-approved TradeIntent proceeds straight to Execution as already
designed. In Approval-required, Risk still evaluates and approves/rejects exactly as
normal (so the same limits apply either way) — but on approval, instead of
dispatching, it writes a `pending_trade_approvals` row and stops. The frontend gets a
websocket push and shows the trade preview (below); the user clicks Approve (proceeds
to Execution) or Reject (discarded, audited). A background Scheduler job expires
anything left pending past `expires_at` (default: the strategy's own setup-validity
window, e.g. the remainder of the current candle/trigger window) — an un-acted-on
approval must never silently fire later once conditions have moved on. Clicking
Approve itself re-runs a lightweight freshness check (current price/spread still
within tolerance of what was true when the intent was generated) immediately before
dispatch — a click is a stale instruction if the market has moved materially in the
seconds it took the human to decide, and firing the original numbers unmodified would
silently disagree with the pre-trade analytics the user just looked at. A failed
freshness check surfaces as "conditions changed, re-approve to confirm" rather than
dispatching silently.

**Pre-trade analytics ("what does this trade cost, and what happens").** Computed
by Risk Service at the moment a TradeIntent is approved (so it's available whether
the run is auto or approval-required, and is stored on `risk_decisions` for later
review either way):
- **Capital required** — premium × lot_size × qty for a long option, adjusted for
  funding mode (MTF reduces cash required per the account's leverage terms); this is
  also checked against remaining session budget (open positions' committed capital +
  this trade ≤ `budget_amount`), which is a new Risk gate alongside the existing
  concurrency/loss-count checks.
- **Breakeven** — strike ± premium depending on CE/PE.
- **P&L scenarios** — a small table using the strategy's own predefined stop and
  target for that trade: P&L at stop, P&L at breakeven, P&L at target, plus one
  stretch scenario beyond target. Because entry/stop/target are already required to
  be predefined before order placement (existing rule), this is a direct computation,
  not a new estimate.
This shows in the UI wherever a TradeIntent surfaces — the approval card in
Approval-required mode, and the live trade log / running-strategies view in
Auto-execute mode, so the information is always visible, just at different points in
the flow.

**Running-strategies view.** A dashboard panel, one row per active `strategy_runs`
entry: strategy name, execution mode (auto/approval-required), status (scanning /
in-position / paused / stopped), open positions, any pending approvals (with inline
approve/reject buttons), today's trade count vs the session's budget/target/loss
figures, and today's realized P&L for that strategy. This is the direct answer to
"if multiple strategies are run, give me a view of all of them running" — it's the
primary screen once more than one strategy is active.

**Execution commands (API surface).** `POST /sessions/{id}/daily-plan` (set
budget/target/max-loss/funding-mode at session start) · `POST
/strategies/{id}/start` (creates a `strategy_runs` row, choosing auto or
approval-required) · `POST /strategies/{id}/stop` · `POST
/trade-approvals/{id}/approve` / `.../reject` · plus the already-planned mode-
transition endpoints (promote, demote, kill-switch).

## Phase-by-phase build sequence

**Phase 0 — Foundations (no trading logic)** — ✅ done
Repo scaffold; Postgres+Redis via Docker Desktop (install if not already present);
Alembic wired with identity tables + seeded roles/permissions; Argon2 auth + RBAC
dependency; state-machine skeleton defaulting to `paper_only` with `kill_switch`
reachable from anywhere through one choke-point guard function; Audit Service wired
to auth events first; NTP/clock-drift check; disk-space health check; token-bucket
rate limiter utility; Postgres advisory-lock utility (also backs the serialized Risk
evaluation queue built in Phase 2); sleep-inhibitor component wired to
session/position state; startup-recovery check in `main.py` (no-op safe now, since
there are no positions yet, but the hook exists so Phase 3 can exercise it for real);
Windows Service wrapper for the backend process; CI (lint/test/migration check). Also
two research/verification action items: (a) read Shoonya's full
Authentication + rate-limit doc pages directly (the API docs site gates the detailed
sections behind navigation I couldn't reach programmatically) to lock exact
login/rate-limit specifics before Phase 5; (b) contact Airtel to confirm static-IP
availability on the current connection.
Done when: fresh setup boots, Admin logs in, mode shows `paper_only`, flipping to
`kill_switch` blocks a stub order call, the login and the transition are both in the
audit log, NTP check logs drift, CI is green.

**Phase 1 — Domain schema + market-data/option-chain layer (mock data)** — ✅ done
Full domain schema via Alembic (every table above, even unused ones, to lock the ERD
early); Instrument/OptionContract seed (Nifty/Bank Nifty master); Market Data Service
built against `broker_port` with the mock/replay adapter; VWAP/EMA9/EMA20 indicators;
option chain normalization; Scheduler skeleton running the daily instrument/strike
sync job against mock data.
Done when: mock ticks/depth/chain/indicators flow end-to-end on schedule, queryable
via API, with connectivity audit events and a simulated sync-job row.

**Phase 2 — Shared strike-ranking engine + Risk Service** — 👈 next
Strike-ranking engine (spread/volume/OI/premium-fit/depth, ATM±N configurable)
against Phase 1's mock chain; Risk Service (margin/funds check stubbed, max
concurrent positions [global], max trades/day, consecutive-loss pause, daily loss
cap, daily target-profit soft-stop, budget-vs-committed-capital check, versioned
`risk_limit_configs`, full audit wiring, same-strike soft lock during evaluation);
pre-trade analytics (capital required, breakeven, P&L scenarios) computed and stored
on every `risk_decisions` row; the daily-plan form (`POST /sessions/{id}/daily-plan`)
so budget/target/max-loss/funding-mode can actually be entered; a trivial synthetic
strategy stub emitting Signals/TradeIntents on a timer to prove the
Signal→TradeIntent→RiskDecision→audit skeleton before real strategy logic exists.
Done when: synthetic TradeIntents are approved/rejected per configurable limits
including the new budget/target checks, every decision carries a correct pre-trade
analytics snapshot, all decisions audited, breaching a limit visibly blocks further
approvals and raises an alert.

**Phase 3 — Execution Service (paper only) + reconciliation + reporting v1**
Execution Service with singleton lock + idempotency-before-dispatch, paper path only
(simulated fills/stop/trail/exit against mock prices, zero Shoonya code); full
Order/OrderEvent/Position/StopPlan/TrailPlan/TradeOutcome lifecycle; Reconciliation
Service built now against the paper-vs-local case (event-triggered + polling) so
Phase 6's real-broker case is additive, not a rewrite; Reporting v1 (daily report +
scorecard: win rate, avg win/loss, profit factor, max drawdown, slippage,
signal-vs-execution count) — this is the strategy graduation dashboard; the
Approval-required path built end-to-end against the synthetic strategy stub
(`pending_trade_approvals` lifecycle, approve/reject/expire, websocket push) since
paper mode is the right place to prove out the approval workflow before real
strategies exist.
Done when: the synthetic strategy runs a full paper lifecycle including EOD forced
square-off, a scorecard renders real paper numbers, an injected inconsistency is
correctly flagged by reconciliation, a manual test of Approval-required mode shows
the trade preview, approves one intent, rejects another, and lets a third expire
untouched, and — this is the first real test of the startup-recovery hook — killing
the backend process mid-paper-position and restarting it resumes stop/trail
management correctly instead of coming back up idle.

**Phase 4 — ORB, VWAP Pullback, EMA Micro-pullback (paper, mock data) — first major milestone**
The real shared Strategy interface, then the three confirmation-filter strategies
exactly as specified, each against the shared strike-ranking engine (ATM±3), each
strictly emitting Signals/TradeIntents only (enforced at interface level — no
dependency on Order/Position repositories). Common rules (full-candle completion,
mandatory stop from entry, per-method trailing activation, spread/structure-break
exit) implemented generically once, not duplicated per strategy. `POST
/strategies/{id}/start` wired for real so each of the three can be run independently
in auto or approval-required mode; the running-strategies dashboard built now, since
this is the first point where multiple concurrent runs are real rather than
synthetic.
Done when: all three strategies run concurrently, in a mix of auto and
approval-required mode, across multiple simulated sessions, with distinct comparable
scorecards and a running-strategies view showing all three plus any pending
approvals correctly.

**Phase 5 — Shoonya Broker Adapter (real integration, still no live orders)**
`broker_adapter/shoonya` per Phase 0's verified auth details — confirmed via
shoonya.com/api-documentation and the Shoonya-API-OAuth-Python repo: a browser
redirect to `oauth_authorize_url` + client_id + redirect_url, user logs in on
Shoonya's own site (User ID + password + OTP/TOTP), Shoonya redirects back to
`redirect_url` with a `code`, then POST `{code, checksum=SHA256(client_id+secret_code+code)}`
to `{api_host}/GenAcsTok` for the access token. `redirect_url` (e.g.
`http://127.0.0.1:5000/shoonya/callback`) only needs to be reachable by the user's own
browser on this machine, not the internet, since the redirect happens client-side.
Single shared WebSocket, reconnect/heartbeat; rate limiter wired in front of every
outbound call; swap Market Data's source from mock to Shoonya behind the unchanged
port interface; real daily instrument/strike sync; explicit handling for each broker
error scenario (invalid credentials, IP mismatch, TOTP drift, mid-session expiry, WS
drops) mapped to specific mode transitions and alerts. Order placement stays
paper-only — this phase proves real data + auth, deliberately separated from real
order placement.
Done when: the three strategies run in paper mode against real Shoonya data for
several sessions with stable reconnects, and a reconciliation dry-run against real
(empty) broker positions passes.

**Phase 6 — Guarded live execution, safeguards proven end-to-end**
Execution Service's live path (submit/modify/cancel/exit) against Shoonya;
`paper_plus_guarded_live` activated for real; Reconciliation upgraded to real
event-triggered + 5-15s polling with freeze/alert/manual-lock now live; stop-loss
placement verification implemented and tested; kill switch tested against a real
broker session; one-lot enforcement and per-strategy graduation gating (only an
Admin-flipped `strategy_configs.status` can receive a live TradeIntent). Documented
manual sign-off checklist before the first real-money trade.
Done when: one graduated strategy (likely ORB) executes a single real 1-lot trade
end-to-end with correct stop placement, correct reconciliation, full audit trail, no
manual broker-terminal intervention.

**Phase 7 — Strategies 4 & 5** (OI/Volume confirmed, Liquidity sweep/reversal)
Same paper-first graduation path as Phase 4. Strategy 4 exercises a
chain-participation-weighted mode of the strike-ranking engine; Strategy 5 needs a
small shared level/structure helper (reusable with ORB's opening-range logic).

**Phase 8 — Strategy 6** (Market Depth Imbalance Scalp — last, hardest)
Rolling-window persistent depth-imbalance detection (not single-snapshot), wider
ATM±5 (±7 analysis-only) scan, liquidity-reject-then-rank pipeline, tightest stops +
time-stop exit. Extended paper-only soak period; slippage is the primary graduation
gate given this is the most data-sensitive strategy.

**Phase 9 — AI Extension Service** (optional, once credentials supplied)
Read-only ports into Signals/market-data/audit, suggestion-event emission only,
enforced no-write at the type level (no TradeIntent/RiskDecision/Order constructors
reachable) plus a CI import-linter rule that fails the build if `ai_extension`
imports any execution/risk write path.

**Phase 10/11 — Stage 2 (cloud packaging) / Stage 3 (split deployables)**: deferred
until Stage 1 is stable; no further design needed now beyond the module boundaries
already captured above.

## Verification approach

Each phase has its own "done when" criteria above, checkable end-to-end without
needing later phases:
- Phases 0-4 are fully verifiable locally with the mock broker adapter and recorded
  fixture sessions in `tests/fixtures/` — no live network dependency, deterministic
  replay in CI.
- Phase 5 is verified against real Shoonya data in paper mode only — no financial
  risk, but real network/auth conditions.
- Phase 6 is the first phase with real money at stake, gated by the documented manual
  sign-off checklist and a single graduated strategy at 1 lot.
- Every phase's "done when" includes an audit-log check — if it's not in
  `audit_events`, it didn't happen, which doubles as an ongoing regression test for
  the audit subsystem itself.
