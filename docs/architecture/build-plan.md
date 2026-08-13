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
        clock.py                     # NTP drift check (startup + periodic) -> SystemAlert / mode signal
        rate_limiter.py              # token-bucket wrapping outbound broker calls
        locking.py                   # Postgres advisory lock -> Execution Service singleton
        idempotency.py               # idempotency key generation + atomic pre-dispatch persistence
        sleep_inhibitor.py           # Windows SetThreadExecutionState, singleton via get_sleep_inhibitor();
                                      # acquired/released around strategy-run scanning + open positions
        # redis_client.py / eventbus.py, both originally planned here, were
        # deliberately never built. Redis runs in Docker but nothing uses
        # it; event-triggered reconciliation (the one thing eventbus.py was
        # for) is a direct synchronous call from dispatch_trade_intent/
        # close_position instead, inside the same LOCK_EXECUTION_SINGLETON
        # scope — better than pub/sub here, since it gets transactional
        # consistency for free instead of having to re-solve ordering/
        # delivery guarantees for a single in-process consumer. Revisit
        # only if a second independent async consumer appears (e.g.
        # WebSocket push on order events) or the process splits into
        # multiple deployables (Stage 3).
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
            auth.py                  # OAuth browser-redirect (User ID+password+OTP on Shoonya's site) -> code -> GenAcsTok
            rest_client.py  ws_client.py  normalizer.py
        market_data/
          # option_chain.py, originally planned as its own file, was never
          # split out — record_option_chain_snapshot lives in ingestion.py
          # instead (one ~40-line function reusing ingestion.py's own
          # SessionFactory/model imports; nothing substantial enough to
          # warrant a separate file).
          ingestion.py  indicators/ (VWAP, EMA9, EMA20)  registry.py
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
      # Actual layout: auth.py, strategies.py, sessions.py (also mounts
      # broker_accounts_router), execution.py (orders+positions, folded
      # into one file rather than split), reports.py, instruments.py,
      # audit.py (list + verify-chain, added in the post-Phase-5 hardening
      # pass), shoonya.py (mounted without the /api/v1 prefix — see its own
      # module comment). No dedicated risk.py/admin.py — nothing yet needs
      # a write surface those files were reserved for.
      api/v1/ (auth, strategies, sessions, execution, reports, instruments, audit, shoonya)
      api/websocket/ (quotes_ws, positions_ws, alerts_ws)  # still an empty stub — see Phase 3's own note
      workers/ (market_data_worker.py, reconciliation_worker.py, scheduler_worker.py, strategy_worker.py)
    migrations/                      # Alembic
    tests/unit/ integration/ fixtures/ (recorded tick/depth/chain sessions for deterministic replay)
    alembic.ini  pyproject.toml
  frontend/
    # Actual layout: src/features/{auth,sessions,strategies,reports}/ —
    # no dedicated orders/positions/risk/audit/admin pages yet (the
    # backend endpoints exist for orders/positions/audit; frontend
    # pages for them are a real gap, not yet built).
    src/features/ (auth, sessions, strategies, reports)
    src/shared/ (api client + types, hooks/ — useActiveSessionMode)
    src/app/ (routing, Layout.tsx + ModeBanner.tsx — the always-visible
              mode/broker banner, added in the post-Phase-5 hardening pass)
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

**Phase 2 — Shared strike-ranking engine + Risk Service** — ✅ done
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

*Phase 2 amendments (decisions made during implementation, not re-derivations of
the design above):*
- **`SyntheticTradeOutcome` + `trading_sessions.cumulative_realized_pnl` /
  `consecutive_losses`** — Phase 3's real `trade_outcomes`/Position lifecycle
  doesn't exist yet, so Risk Service's daily-loss-cap / daily-target-profit /
  consecutive-loss checks had no real data to evaluate against. Added a
  Phase-2-only `synthetic_trade_outcomes` table (deliberately named apart from
  Phase 3's `trade_outcomes`) plus two running-total columns on
  `trading_sessions`, updated by `risk_engine.service.record_synthetic_outcome`
  after the synthetic strategy stub "closes" a dispatched TradeIntent with a
  small synthetic P&L. Phase 3's real fill-driven P&L recording replaces this
  call site; the table itself is never read by anything else.
- **`system_alerts` pulled forward from the full Ops schema** — "breaching a
  limit visibly blocks further approvals and raises an alert" needed somewhere
  for that alert to land. Only `system_alerts` was built now; `metric_series`
  and `scheduler_job_runs` stay deferred until a phase actually needs them.
- **`signals`/`trade_intents.qty_lots` is a lot count, not raw quantity** —
  matches the existing rule that lot size is always read server-side from
  `option_contracts → instruments.lot_size`, never client/strategy-supplied.
  Phase 3's Execution Service converts `qty_lots × lot_size` into the absolute
  quantity `OrderRequest.qty` expects at dispatch time.
- **Pre-trade analytics' "P&L at breakeven" is always `0.0`**, not computed
  from `breakeven_price`. `breakeven_price` (strike ± premium) is a
  held-to-expiry, underlying-scale figure; entry/stop/target are same-day
  option-premium levels. The two aren't on the same price scale, so plugging
  `breakeven_price` into the premium-delta P&L formula produced a nonsense
  number — a scratch trade (exit at the entry premium) is 0 P&L by
  definition, which is what the table now reports.
- **Bug fix in `app/core/modes/transitions.py`**: `paper_only → kill_switch`
  only allowed `MANUAL`/`SYSTEM` triggers, not `RISK` — found because Risk
  Service's daily-loss-cap breach (a `RISK`-triggered transition, same as
  from the two live-adjacent modes) failed against a `paper_only` session in
  a real-Postgres test run. Per the build plan's own "no soft step-down on a
  loss breach" rule, this should apply the same way regardless of safe-mode;
  added `Trigger.RISK` to that edge, verified against
  `tests/unit/test_transitions_table.py`'s structural invariants (none of
  which forbid it) and the full existing state-machine test suite.

*QC pass findings (post-implementation review, before Phase 3 started):*
- **Bug fix: an approval-required trade never closed.** The auto-execute path
  (`SyntheticStrategy.run_cycle`) synthetically closed a dispatched
  TradeIntent immediately; `POST /trade-approvals/{id}/approve` dispatched
  one too but never closed it — an approved trade sat `DISPATCHED` forever,
  permanently holding a concurrency slot and a same-strike lock for the rest
  of the session. Factored the close logic into one shared
  `strategy_engine.service.close_dispatched_trade_intent_synthetically`, used
  by both dispatch paths. Caught by a regression test that fails against the
  old code and passes against the fix.
- **Bug fix: trade-approval lookup wasn't workspace-scoped.**
  `_get_pending_approval_or_404` did a bare `db.get()` with no ownership
  check — every other lookup in `app/api/v1/strategies.py` filters by
  `user.workspace_id`, this one didn't, so a user could approve/reject
  another workspace's pending trade by knowing its UUID (`PendingTradeApproval`
  has no `workspace_id` column of its own; fixed by joining through
  `TradeIntent.workspace_id`). Also caught by a regression test that fails
  against the old code.
- **Bug fix: unlocked check-then-act in `start_strategy`.** "At most one
  active run per strategy" was checked and inserted without the advisory-lock
  discipline every other check-then-act in this codebase uses — two
  concurrent start requests for the same strategy could both pass the check
  before either committed. Wrapped in `LOCK_EXECUTION_SINGLETON` (reused
  rather than a new named lock, same reasoning mode transitions already share
  it for). Risk evaluation itself was never affected by this gap — it stays
  correctly serialized under `LOCK_RISK_EVALUATION_QUEUE` regardless — but
  the strategy-run bookkeeping wasn't, and the project's own stated #1
  failure mode is exactly this class of unlocked check-then-act.
- **Test-isolation bug in the new tests themselves**: an early version of the
  approval-flow test committed `Instrument`/`OptionContract` rows (no
  workspace scoping) to the shared test-DB engine without cleaning them up,
  which broke unrelated Phase 1 tests (`test_instrument_sync.py`) that assume
  a clean table — the exact trap CLAUDE.md's "Test DB isolation" convention
  already warns about. Fixed with an explicit try/finally cleanup; full suite
  re-run twice in a row afterward to confirm no leakage.
- Added a direct test for `SyntheticStrategyRunner` itself (the actual
  "on a timer" background-thread mechanism) — everything else only exercised
  `run_cycle` directly, leaving the timer/threading wrapper with zero
  coverage despite being literally what the Phase 2 build bullet asked for.

**Phase 3 — Execution Service (paper only) + reconciliation + reporting v1** — ✅ done
Execution Service with singleton lock + idempotency-before-dispatch, paper path only
(simulated fills/stop/trail/exit against mock prices, zero Shoonya code); full
Order/OrderEvent/Position/StopPlan/TrailPlan/TradeOutcome lifecycle; Reconciliation
Service built now against the paper-vs-local case (event-triggered + polling) so
Phase 6's real-broker case is additive, not a rewrite; Reporting v1 (daily report +
scorecard: win rate, avg win/loss, profit factor, max drawdown, slippage,
signal-vs-execution count) — this is the strategy graduation dashboard; the
Approval-required path built end-to-end against the synthetic strategy stub
(`pending_trade_approvals` lifecycle, approve/reject/expire) since paper mode is
the right place to prove out the approval workflow before real strategies exist.
**Websocket push was not built** — there's no frontend yet to push to (the repo's
`api/websocket/` package is still an empty stub); the approval workflow's DB-level
lifecycle is fully proven via the API/tests instead. This is a real, deliberate
scope gap, not an oversight — worth building alongside whichever phase first
builds a frontend.
Done when: the synthetic strategy runs a full paper lifecycle including EOD forced
square-off, a scorecard renders real paper numbers, an injected inconsistency is
correctly flagged by reconciliation, a manual test of Approval-required mode shows
the trade preview, approves one intent, rejects another, and lets a third expire
untouched, and — this is the first real test of the startup-recovery hook — killing
the backend process mid-paper-position and restarting it resumes stop/trail
management correctly instead of coming back up idle. All verified: full automated
suite (integration tests for dispatch/close/stop/target/trail/EOD/reconciliation/
reporting/startup-recovery) plus a manual live-server walkthrough (approve one,
reject one, force-expire a third, inject a broker-side mismatch and see it
flagged, kill and restart the process mid-open-position and see `PositionManager`
resume).

*Phase 3 amendments (decisions made during implementation, not re-derivations of
the design above):*
- **Paper execution routes through `BrokerPort.place_order`/`get_positions`,
  not a separate tick-based simulator.** `execution_engine/paper/service.py`
  calls the same order-placement/position-query methods a real adapter would,
  against whichever adapter `broker_adapter/composition.py`'s `get_broker()`
  resolves to (`MockBrokerAdapter` through Phase 5). This reuses Phase 1's
  already-fully-built order/position simulation and — more importantly — gives
  Reconciliation Service a genuine broker-side position book to diff local
  `positions` against, rather than inventing one. Phase 5/6 become pure
  dependency-injection swaps of `get_broker`'s resolution; Execution Service
  itself doesn't change.
- **A process-wide broker singleton is the actual composition root** the docs
  already promised ("a composition-root in `main.py` decides whether `mock` or
  `shoonya` gets injected") — `broker_adapter/composition.py`'s `get_broker()`,
  lazily constructing one `MockBrokerAdapter` for the process. Meaningful only
  because `LOCK_PROCESS_SINGLETON` already guarantees one process; same
  reasoning `api.v1.strategies._RUNNERS`'s in-memory dict already relies on.
- **`PositionManager` is started explicitly at strategy-start, not implicitly
  from `dispatch_trade_intent`.** The natural-seeming hook (auto-start
  management the moment a position opens) would spawn a real background
  thread from inside unit/integration tests that call `dispatch_trade_intent`
  directly with a test-owned broker — that thread would poll the *production*
  DB via `PositionManager`'s default `session_scope`, the exact "background
  thread silently queries the wrong database" trap
  `strategy_engine.strategies.synthetic.SyntheticStrategyRunner` already had
  to design around. Instead, `api.v1.strategies.start_strategy` starts it
  (mirroring exactly where `SyntheticStrategyRunner` itself starts), and
  `app.main`'s startup-recovery check resumes it after a crash.
- **Reconciliation escalates to `reconciliation_lock` only from
  `paper_plus_guarded_live`/`live_enabled`**, matching
  `ALLOWED_TRANSITIONS` exactly (there is no `paper_only → reconciliation_lock`
  edge). A `paper_only` mismatch is flagged (`SystemAlert` + a
  `reconciliation_runs` row) but not mode-blocked, since there's no live
  money at risk yet — free groundwork for Phase 6, not a Phase 3 behavior
  change.
- **One generic trailing-stop rule stands in for per-strategy trailing**,
  since that arrives with real strategies in Phase 4: activates once
  unrealized profit reaches 50% of the entry→target distance; once active,
  locks in 50% of favorable movement beyond activation, monotonically
  tightening only. Implemented as an independent level
  (`trail_plans.current_stop_price`) that is *never* written back onto
  `stop_plans.stop_price` — see QC finding below for why that distinction
  matters.
- **No live scheduler daemon exists for EOD square-off/reconciliation**,
  consistent with every other periodic job in this codebase (the daily
  instrument sync job is the same shape) — `PositionManager`'s own poll loop
  covers both automatically per session, and `POST /sessions/{id}/square-off`
  / `POST /sessions/{id}/reconcile` expose the same logic on demand for
  manual testing.

*QC pass findings (post-implementation review, before Phase 4 started):*
- **Bug fix: trailing-stop logic fired a spurious exit on its own activation
  tick.** `evaluate_open_position`'s trail-hit check used `price <=
  new_trail_stop` (favorable side); on the exact tick the trail activates or
  tightens, `new_trail_stop` is derived from that same `price`, so the two
  are equal and a `<=` fires immediately instead of only once price later
  pulls back through the level. Fixed to strict `<`/`>`. Caught by a direct
  unit test exercising three ticks (activate, tighten, pull back) that failed
  against the old code.
- **Bug fix: the trail was silently turning every later exit into a "stop
  hit".** The same function used to write the trailed level back onto
  `stop_plans.stop_price`, which meant `evaluate_open_position`'s step-1 stop
  check (checked before the trail step) started matching on the *trailed*
  level too — a genuine `TRAIL` exit was misreported as `STOP`, making
  `ExitReason.TRAIL` effectively unreachable once a trail had ever tightened.
  Fixed by keeping the trailed level entirely in `trail_plans.current_stop_price`
  and never touching `stop_plans.stop_price` after it's set at dispatch time.
- **Bug fix: pending trade approvals never expired on their own.** The build
  plan calls for "a background Scheduler job expires anything left pending
  past `expires_at`"; only a lazy check inside
  `api.v1.strategies.approve_trade_approval` existed (an approval only
  actually flipped to `EXPIRED` if someone happened to click Approve on it
  after the window closed) — found live, manually, when a genuinely stale
  approval sat `pending` through several `PositionManager` poll cycles.
  Added `strategy_engine.service.expire_stale_pending_approvals`, called by
  `PositionManager` every cycle alongside its EOD/reconciliation checks;
  verified both by unit tests and by re-confirming live that a backdated
  `expires_at` actually flips to `EXPIRED` (approval + TradeIntent + audit
  event) within one poll cycle of a manager running for that session.
- **Bug fix (test-only): the `orders` ↔ `positions` circular-FK cleanup
  pattern used across several test files' teardown broke as soon as a test
  actually closed a position.** Nulling `orders.position_id` to break the
  cycle is wrong for exit orders specifically — they always have
  `trade_intent_id=NULL`, so nulling `position_id` too leaves both FK columns
  null, violating `ck_order_exactly_one_of_intent_or_position`. Every
  occurrence of this pattern (introduced earlier in the same phase, so not
  yet exercised against a real closed position until QC) was fixed to break
  the cycle via `positions.closing_order_id` (nullable) instead, deleting
  exit orders before positions and entry orders after.
- **Gap found (fixed): event-triggered reconciliation was never actually
  wired in.** The approved design called for `dispatch_trade_intent`/
  `close_position` to each run a reconciliation pass immediately after —
  only `PositionManager`'s polling cadence existed. Added the event-triggered
  call to both functions (safe to nest under the already-held
  `LOCK_EXECUTION_SINGLETON`: Postgres session-level advisory locks are
  reentrant per session, so a nested `transition_mode` call taking the same
  lock cannot self-deadlock). This surfaced a second-order test-isolation
  bug: two existing test files' cleanup blocks didn't know about the
  `broker_sync_states` rows this now creates on every dispatch, so their
  teardown started failing FK checks and corrupting shared test-DB state for
  unrelated tests — fixed by adding the missing cleanup, same FK-safe-order
  reasoning as everywhere else.

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

*Phase 4 amendments (implementation-time decisions):*
- **Frontend stayed API-only**, confirmed with the user before implementation:
  the "running-strategies dashboard" is `GET /strategies/running`, verified via
  Swagger/manual QC. `frontend/src/` is still untouched; the React SPA is
  deferred to a dedicated later phase.
- **A new `price_bars` table**, not just the existing `indicator_snapshots`
  scalars — `IndicatorEngine`/`BarAggregator` already built a completed `Bar`
  (O/H/L/C/V) internally every cycle just to feed EMA, then discarded it.
  `IndicatorEngine.on_tick` now returns `(indicator_values, completed_bar)`;
  `market_data.ingestion` persists the bar alongside whatever indicators
  changed. All three real strategies need genuine candle structure (opening
  range, pullback extremes, confirmation closes), not just a scalar.
- **Per-method trailing, structure-break, and spread-blowout exits** —
  `TradeProposal` gained three new optional fields (`trail_activation_fraction`,
  `trail_lock_fraction`, `structure_level`), threaded through `Signal`/
  `TradeIntent` unchanged from Phase 3's shape otherwise. `structure_level`
  lives on the *existing* `stop_plans` row (not a new table) — a second,
  independent invalidation level on the *underlying's* own price, checked
  after stop/target but before the trail step in `evaluate_open_position`.
  Two new `ExitReason`s: `STRUCTURE_BREAK` (underlying crossed the strategy's
  own structural level) and `SPREAD_BLOWOUT` (option's live bid/ask width
  exceeded a fixed, generic `SPREAD_BLOWOUT_PCT` — deliberately not
  per-strategy, unlike the trail). All three are `None`/inactive for
  `SyntheticStrategy`, which is unchanged.
- **`ConfirmationFilterStrategy`** (`strategy_engine/common_rules.py`) is the
  "implemented generically once" template method the build plan calls for:
  `evaluate()` enforces full-candle-completion (never re-fires on the same
  bar) and no-signal-while-in-position, then delegates to each strategy's
  `check_setup(db, strategy_run, latest_bar)`. `get_recent_completed_bars`
  supports both a fixed window (`since`/`until` — ORB's opening range) and a
  trailing window (`limit` — VWAP Pullback/EMA Micro-pullback's last-N-bars
  need). **2026-08-13 correction**: ORB's window was originally anchored to
  `strategy_run.started_at` (restart-safe, but not actually the real 9:15-9:30
  NSE opening range if a run started later than 9:15) — changed to a fixed
  9:15 IST anchor derived from the bar's own timestamp, which is both the
  real opening range *and* restart-safe (doesn't depend on `started_at` at
  all). Guards against a gapped 9:15-9:30 window (fewer than `or_minutes`
  persisted bars) by skipping rather than computing from partial data.
- **The strategy runner was generalized, not duplicated a fourth time** —
  Phase 2's `SyntheticStrategyRunner` (bespoke, hardcoded to one class) is
  gone; `strategy_engine/runner.py`'s `StrategyRunner` + standalone `run_cycle`
  function drive all four strategies identically. `run_cycle` also refreshes
  `strategy_run.status` (`IN_POSITION` vs `SCANNING`) from whether the run
  actually has an open Position right now — ground truth, correct regardless
  of which path (auto-dispatch / approval-required / risk-rejected) a given
  cycle took.
- **`strategy_configs.strategy_type`** (plain `String`, not an `enum.StrEnum`
  column like this file's other status fields — new strategy types arrive in
  later phases without a migration touching an existing constraint) is what
  `api.v1.strategies.start_strategy` maps to a concrete `Strategy` class;
  `POST /strategies` validates it against a known-types set at creation time.
- **Entry logic, concretely** (no per-strategy spec existed anywhere before
  this phase): ORB fires on the first bar closing beyond the opening range
  (`or_minutes`, anchored to the fixed 9:15 IST session open as of
  2026-08-13 — see correction above) in either direction, once per
  direction per run; VWAP Pullback/EMA Micro-pullback both fire on a
  pullback-bar-touches-then-confirmation-bar-closes-back-through pattern
  against VWAP or EMA9 (with EMA9>EMA20/EMA9<EMA20 as the trend filter)
  respectively. All three still call the unchanged strike-ranking engine
  (`atm_range=3` default already matched "ATM±3") for the actual contract, and
  stop/target stay percentage-based on the option premium — there's no
  options-pricing model in this system to translate an underlying-index
  structural level directly into a premium level, so direction/timing comes
  from the underlying's technicals and stop/target stay the same
  percentage-based shape `SyntheticStrategy` already used, tuned per strategy.

*QC pass findings (post-implementation review, before Phase 5 starts):*
- **Bug fix: `migrations/env.py` only imported 4 of the 9 domain packages**
  (`audit, identity, market, session` — missing `broker, execution, ops, risk,
  strategy`, all added by Phase 2/3 without this file being updated). The first
  autogenerate run for this phase's own migration tried to *drop* every table
  those five packages define (`orders`, `positions`, `trade_intents`,
  `risk_decisions`, `system_alerts`, and a dozen more) — caught before it was
  ever applied by reading the generated migration rather than blindly running
  it. Fixed the import list; the resulting real (unrelated) schema drift this
  uncovered — `sessions`/`users`' unique-index shape not matching what the
  current model produces, a stale index on `trading_sessions.broker_account_id`
  — was split into its own migration (0007) so Phase 4's migration (0008)
  contains only Phase 4's own changes.
- **Gap found (fixed): `MarketDataIngestionService`/`IndicatorEngine` were
  built in Phase 1 but nothing ever actually started one outside tests.**
  Phase 2/3's synthetic strategy only ever needed a point-in-time
  `OptionChainSnapshot` + the latest underlying `QuoteTick`, so this went
  unnoticed for three phases. Phase 4's real strategies need genuinely live
  `price_bars`/`indicator_snapshots`, so `market_data/registry.py`'s
  `ensure_ingestion_running` is now called from `start_strategy`, same
  explicit-start-at-strategy-start shape `ensure_position_manager_running`
  already established.
- **Bug fix: a `MarketDataIngestionService` instance *per underlying
  instrument* silently drops every underlying's ticks except whichever
  instrument subscribed most recently.** `BrokerPort.subscribe_quotes`'s own
  docstring says a broker connection is a *single shared connection*
  ("Shoonya only supports one connection per session"), and
  `MockBrokerAdapter` reflects that literally — one `on_tick`/`on_depth`
  callback slot, `self._on_tick = on_tick` on every call. The registry's first
  cut (one service per instrument) had each one's `subscribe_quotes` call
  overwrite the previous one's callback; found live during this phase's own
  manual QC (BankNifty ticks flowing, Nifty's completely silent — the
  giveaway was the two instruments' `indicator_snapshots` counts differing by
  two orders of magnitude over the same time window instead of being
  comparable). Fixed by sharing one `MarketDataIngestionService` instance
  across every underlying (`MarketDataIngestionService.start` already
  accumulates `_symbol_map` across repeated calls, and re-subscribing the same
  bound callback is a no-op change to the mock's single slot) — matches the
  documented single-connection contract instead of fighting it. Regression
  test: `test_market_data_registry.py`.
- **Gap found and fixed (same-day follow-up, after the phase's own QC pass):**
  `broker_adapter.composition.get_broker()`'s lazily-constructed
  `MockBrokerAdapter()` singleton had no instrument universe (`instruments=None`
  defaults to `[]`), so `get_option_chain()` against the live process singleton
  always returned an empty chain — `record_option_chain_snapshot`/
  `rank_from_latest_snapshot` silently found nothing to rank, even though
  ticks/orders/positions all worked fine (hash-seeded by symbol string,
  independent of `self._instruments`). Present since Phase 1 — Phase 2/3's
  synthetic strategy had the identical exposure in live use, just never
  noticed until a real strategy's manual QC needed a live option chain.
  Fixed: `get_broker()` now seeds `MockBrokerAdapter(instruments=
  build_mock_universe(_next_weekly_expiry()))` instead of a bare instance, and
  `app.main`'s startup sequence gained a step (`_sync_mock_instrument_universe`,
  a no-op once a non-mock broker is configured) that runs
  `sync_instrument_master` against that same seeded instance so
  `instruments`/`option_contracts` DB rows match what it actually quotes.
  Verified live: `get_option_chain("NIFTY", expiry)` went from 0 entries to 42
  (21 strikes × CE/PE) against the real dev DB and a running server.

**Frontend SPA (React) — first real cut** — ✅ done
Phase 4 deliberately stayed API-only (see its amendments above); this is the
first real implementation of the "React SPA over REST" decision locked in
this doc's "Locked architectural decisions" section. Goal: a genuinely usable
SPA — login, create/manage sessions, create/start/stop strategies (all four
types), watch them run live, approve/reject pending trades, view reports —
not a read-only dashboard stub. WebSocket push stayed out of scope (REST +
polling is enough for what exists today; this doc already frames WebSocket
as needed "later" once a frontend exists to push to).

Four small, read-only, workspace-scoped GET endpoints were added first
(`GET /sessions`, `/broker-accounts`, `/strategies`, `/instruments`) —
every write endpoint already existed, but nothing let the frontend list
anything to populate dropdowns/history. `/instruments` also returns each
instrument's distinct `OptionContract.expiry_date`s so the start-strategy
form can offer a real expiry dropdown. `GET /strategies/running`'s
`pending_approval_count: int` became `pending_approvals:
list[PendingApprovalOut]` (full rows: `approval_id`, side, qty_lots,
entry_price, expires_at) — a bare count can't drive an inline Approve/Reject
button, which needs the approval's own id.

Stack: Vite + React 19 + TypeScript, TanStack Query (`refetchInterval` for
the running-strategies poll, cache invalidation on every mutation), React
Router (four pages: Running Strategies / Sessions / Strategies / Reports,
plus Login), hand-written CSS (no framework — internal tool, not a product),
hand-written TS types mirroring the Pydantic `*Out` models (no OpenAPI
codegen), cookie auth via `vite.config.ts`'s same-origin dev proxy to
`127.0.0.1:5000` (no FastAPI CORS middleware needed for local dev). No
frontend test tooling in this first cut — verified instead by driving the
real dev server end-to-end with the Browser tool.

*QC pass findings (this session's manual browser QC, before Phase 5 starts):*
- **Bug fix: `GET /strategies/running` 500'd on every real request.**
  `list_running_strategies` called `.value` on `run.execution_mode`,
  `run.status`, `position.side`, and `trade_intent.side` — all four are
  `String`-column-typed with a `StrEnum` type hint (`Mapped[ExecutionMode]`
  etc.), not an actual `sqlalchemy.Enum` column. A row loaded fresh from the
  DB (any session other than the one that just wrote it — i.e. every real
  request, since `get_db` hands out a new session per request) comes back
  as a plain `str`, which has no `.value`. This endpoint had never been
  exercised by any test before this session added the first one that
  round-trips it through a real second DB session — it had been silently
  broken since Phase 4 shipped it. Fixed by switching to `str(...)`, which
  is safe for both a live `StrEnum` member (its own `__str__` returns
  `.value`) and a plain reloaded `str`. Regression test in
  `test_api_strategies.py` now asserts on the running-strategies response
  shape directly, closing the gap that let this ship unexercised.
- **Bug fix: concurrent Approve/Reject clicks could deadlock Postgres.**
  Found live: two rapid clicks on the same pending approval's Approve
  button produced a genuine `DeadlockDetected` between the two requests'
  unlocked `pending_trade_approvals` UPDATEs and a `PositionManager`
  background poll — Postgres's own deadlock detector aborted one request
  (a raw 500, not a clean 409), though no double-dispatch occurred. Root
  cause: `approve_trade_approval`/`reject_trade_approval`'s
  `approval.status != PENDING` check was an unlocked check-then-act, the
  same class of race this doc's "Idempotency and single-writer discipline"
  section calls out — `start_strategy`'s own "at most one active run"
  check already wraps itself in `LOCK_EXECUTION_SINGLETON` for exactly this
  reason. Both endpoints now do the same; safe to nest with
  `dispatch_trade_intent`'s own use of the same lock since Postgres
  session-level advisory locks are reentrant per session (documented
  elsewhere in this file). Regression test: a second `approve` call on an
  already-approved approval must return a clean 409, not 500.

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
(empty) broker positions passes. **Plus the paper-vs-live signal comparison in the
"Addendum" section below** — added after this phase's initial build, so treat it as
part of this phase's actual done-when bar, not optional.

*Phase 5 progress (this session — no Shoonya account existed to verify any of
this live, so "done when" above is not yet met):*
- **Built**: `broker_adapter/shoonya/{auth,rest_client,ws_client,normalizer,
  adapter}.py`, exactly the module layout this doc's own "Repository
  structure" section already specified. `ShoonyaBrokerAdapter` implements
  every `BrokerPort` method and slots into `composition.get_broker()` with
  zero upstream changes, proving the broker-agnostic boundary actually holds
  four phases later. `api.v1.shoonya` adds `/shoonya/login-url` (returns the
  OAuth authorize URL), `/shoonya/callback` (completes `GenAcsTok`, installs
  the adapter via `set_broker`), `/shoonya/status` — mounted *without* the
  usual `/api/v1` prefix since `SHOONYA_REDIRECT_URL` is a fixed URL the
  user registers on Shoonya's own API key form
  (`http://127.0.0.1:5000/shoonya/callback`); prefixing it would silently
  break that registration. 41 new unit/integration tests, all against mocked
  HTTP (`httpx.MockTransport`) or pure in-memory logic — none hit a real
  Shoonya endpoint, since none exist to hit.
- **Two-step construction, not `MockBrokerAdapter`'s one-liner**:
  `ShoonyaBrokerAdapter` is never built until `/shoonya/callback` already
  has a completed `OAuthSession` in hand — nothing server-side can run
  Shoonya's browser+TOTP login itself. `authenticate()` on the finished
  instance just returns the `AuthResult` it was constructed with.
- **`get_instrument_master` deliberately narrows to NIFTY/BANKNIFTY**
  (`KNOWN_UNDERLYINGS` in `adapter.py`) via `SearchScrip`, not a bulk
  per-exchange scrip-master file download — this system never trades
  anything else (`mock_universe.py` has the identical hardcoded scope), so
  a literal "every tradable instrument on the exchange" reading would just
  mean syncing thousands of stock F&O contracts nothing here will ever
  touch. A deliberate interpretation, not an oversight — noted in
  `adapter.py`'s own docstring.
- **Every genuinely uncertain assumption is flagged, not guessed silently**
  — recorded in `CLAUDE.md`'s "Known open items" (worth reading before
  touching this code with real credentials): the REST host path
  (`NorenWClientAPI` vs `NorenWClientTP` — official Shoonya-Dev GitHub
  disagrees with this doc's own Phase 0 research and neither could be
  confirmed live), and the auth transport hedge (`rest_client.py` sends the
  access token both as classic Noren `jKey` in the POST body *and* as an
  `Authorization: Bearer` header, since it's unclear which one Shoonya's
  OAuth variant actually reads). `normalizer.py` is deliberately the only
  place any of this can be wrong — every parse function raises a specific
  `NormalizationError` naming the missing field rather than a bare
  `KeyError`, so a real-account mismatch is a small, obvious diff there,
  not a redesign.
- **Error-scenario → mode-transition mapping, scoped down from the
  original plan.** No periodic Scheduler health-check loop exists to wire
  into — NTP/disk checks (`core/clock.py`) have only ever run once at
  startup despite that module's own docstring describing a future
  periodic loop; that gap predates Phase 5, applies to more than Shoonya,
  and is still open. Instead: `base/errors.py` adds a broker-agnostic
  `BrokerAuthError`/`BrokerConnectivityError` hierarchy (every Shoonya
  exception now inherits from these), and `PositionManager.run_once` — the
  one place that already polls the broker repeatedly per session — catches
  `BrokerAuthError` and reacts. Building this surfaced a real design bug:
  `paper_only → degraded_mode` isn't a legal edge in `core/modes/
  transitions.py` — `degraded_mode` exists to protect *live* money
  (`paper_plus_guarded_live`/`live_enabled` only), and Phase 5 is still
  paper-only throughout. So the actually-correct behavior for the traffic
  this phase produces is: log the failure, don't force an illegal
  transition. Two new tests in `test_position_manager.py` cover both
  paths — a guarded-live session does transition to `degraded_mode`, a
  paper-only one (matching Phase 5's real usage) doesn't and just logs.
  Also fixed the `ShoonyaSessionExpiredError`/session-expiry-marker
  matching that `adapter.py` had defined but never actually wired up to
  classify anything — moved into `rest_client.py`'s `_post`, right where
  the raw `Not_Ok` response is parsed.
- **Frontend "Connect Shoonya" button**, also done this session — a
  connection-status card + button on the Sessions page
  (`SessionsPage.tsx`) that opens `/shoonya/login-url`'s authorize URL in
  a new tab and polls `/shoonya/status` on window focus. Manual browser
  verification caught a real bug immediately: `vite.config.ts`'s dev
  proxy only forwarded `/api`, not `/shoonya` (which lives outside
  `/api/v1` on purpose, per `api/v1/shoonya.py`'s own comment about the
  fixed `SHOONYA_REDIRECT_URL`) — the button silently 404'd against
  Vite's own dev server, never reaching the backend at all. Fixed with a
  matching proxy rule.
- **QC finding along the way, unrelated to Shoonya itself**: writing a
  test that actually exercised `MarketDataIngestionService.stop()` (most
  existing tests monkeypatch it away) surfaced a real race —
  `MockBrokerAdapter.unsubscribe_quotes` signaled its stream thread to stop
  but never joined it, so an in-flight `on_tick` callback could still
  insert a `QuoteTick` row after a test's own cleanup had already deleted
  `QuoteTick`s, leaving one FK'd to an `Instrument` the same cleanup then
  failed to delete — a `uq_instrument_symbol_exchange` collision that
  cascaded into ~40 unrelated tests erroring whenever it won the race.
  Present since Phase 1; only surfaced now because Phase 5's own tests
  happened to add pressure in the right place. Fixed by joining the thread
  (bounded, 5s timeout) before `unsubscribe_quotes` returns. Confirmed
  clean across five full-suite runs after the fix.

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
manual broker-terminal intervention. **Plus every "Phase 5/6" item in the "Addendum"
section below** (periodic health-check loop, real margin/limit retrieval, order-ack-
timeout fallback, emergency square-off) and the "Before Phase 6" operational-floor
items (DB backup/restore, `metric_series` wiring) — added after this phase's
original text, all required before the manual sign-off checklist, not optional
extras.

**Phase 7 — Strategies 4 & 5** (OI/Volume confirmed, Liquidity sweep/reversal) — ✅ done
Same paper-first graduation path as Phase 4. Strategy 4 exercises a
chain-participation-weighted mode of the strike-ranking engine; Strategy 5 needs a
small shared level/structure helper (reusable with ORB's opening-range logic).
Done when: both strategies run concurrently in paper mode (mixed auto/
approval-required) alongside the three Phase 4 strategies for several sessions,
each with its own distinct scorecard, with no regression to the existing three.

*Phase 7 amendments (implementation-time decisions — no per-strategy spec
existed anywhere before this phase, same situation Phase 4's own entry-logic
amendments describe):*
- **A new shared helper, `common_rules.compute_range_high_low`**, factored
  out of ORB's own inline high/low-over-a-window computation so ORB and the
  new Strategy 5 share it (pure refactor, zero behavior change — confirmed
  against `test_orb_strategy.py`'s existing assertions before building
  anything new on top of it).
- **Strategy 4 (OI/Volume Confirmed)'s "confirmed" half lives in the
  strike-ranking engine, not a new temporal signal.** `StrikeRankingConfig`
  gained `min_oi`/`min_volume` (both default `0`, so every existing
  strategy's ranked output is provably unchanged — pinned by a new test).
  `rank_strikes` hard-filters below that floor, same treatment as the
  existing `max_spread_pct` filter. Strategy 4's own preset
  (`OI_VOLUME_RANKING_CONFIG`) doubles the OI/volume score weights and sets
  a real participation floor. The entry trigger itself is a rolling-window
  breakout — deliberately similar in shape to ORB (same
  `compute_range_high_low` helper, over a rolling last-N-bars window
  instead of a fixed session-anchored one) — since this strategy's point of
  difference is the strike selection, not a novel price pattern.
  **Why not a delta between consecutive chain snapshots** (the more
  literal reading of "OI/Volume *confirmed*" as a build-up signal): checked
  the actual call graph first — `record_option_chain_snapshot` is called
  exactly once, at `start_strategy` time (`api/v1/strategies.py`), and
  nothing since Phase 4 has ever refreshed it again for the life of a run.
  A delta-based design would silently never fire in real operation. Flagged
  as a separate, real, previously-undiscovered gap (affecting *every*
  strategy's strike-ranking freshness, not just this one) — not fixed here,
  since it's out of this phase's scope and doesn't block it.
- **Strategy 5 (Liquidity Sweep/Reversal)** is a real, distinct pattern
  from Strategy 4 despite sharing the range-helper: a bar that *wicks*
  beyond a recent rolling-window high/low but *closes* back inside it is a
  false breakout, traded in the reversal direction — the opposite shape
  from a continuation breakout. Deliberately does not reuse Batch E's
  `touch_and_confirm` helper (that's for a pullback-*to*-and-continue
  pattern, VWAP/EMA's territory; folding break-and-reverse into it would
  silently change one or the other, the same reasoning that already kept
  `structure_level` strategy-owned there). No per-direction fired-once
  guard, unlike ORB/Strategy 4 — the rolling window continuously shifts, so
  a later genuine setup must still be allowed to fire, same as VWAP/EMA.
- **Stop/target values** (Strategy 4: 0.11/0.18; Strategy 5: 0.10/0.16)
  picked to sit sensibly within the existing three strategies' range
  (ORB 0.12/0.20, VWAP 0.10/0.15, EMA 0.08/0.12), not derived from anywhere
  external — same "the strategy author picks a defensible number" pattern
  every prior phase's stop/target values already followed.
- Extended `test_phase4_strategies_e2e.py`'s concurrency proof from three
  strategies to all five (renamed
  `test_five_strategies_run_concurrently_across_two_sessions_mixed_modes`)
  rather than writing a separate Phase 7 e2e file — reuses the same
  fixture/cleanup machinery, and is the most direct proof of this phase's
  own "no regression to the existing three" criterion.

*QC pass findings (live browser verification, before Phase 7 was called
done):*
- **Bug fix: `LOCK_EXECUTION_SINGLETON` could leak permanently, freezing all
  future order dispatch/strategy starts.** Not caught by the automated
  suite (most tests use a rolled-back transaction that never reaches a real
  commit); found live, after running three strategies concurrently for a
  few minutes and watching every subsequent `POST /strategies/{id}/start`
  hang forever. Root cause, confirmed via `pg_locks`/`pg_stat_activity`
  (the stuck connection was `idle` with `COMMIT` as its last statement, not
  stuck mid-query) plus external research into the identical documented bug
  in other projects: `SQLAlchemy Session.commit()` releases the connection
  back to the pool, so any `db.execute()` after a `commit()` — including
  the `finally: pg_advisory_unlock(...)` a session-scoped advisory lock
  needs — can land on a *different* pooled connection than the one that
  acquired the lock. The unlock silently no-ops on the wrong connection;
  the original connection returns to the pool still holding the lock,
  forever, invisible to any per-request diagnostic. Latent since Phase 4
  (`approve_trade_approval`/`reject_trade_approval` both commit while still
  holding the lock, by design — the check-then-act sequence they guard
  needs to be durable before the lock releases), never triggered before
  because nothing had exercised real concurrent connection-pool churn this
  hard until Phase 7's live multi-strategy test. Audited all 9 call sites
  of `LOCK_EXECUTION_SINGLETON`/`LOCK_RISK_EVALUATION_QUEUE`/
  `LOCK_AUDIT_CHAIN`; 4 (`start_strategy`, `approve_trade_approval`,
  `reject_trade_approval`, and the Batch-C `create_session` fix) commit
  inside their lock scope and were genuinely vulnerable, 5 were not.
  **Fixed by converting all three lock names from session-scoped
  (`pg_advisory_lock`/`pg_advisory_unlock`) to transaction-scoped
  (`pg_advisory_xact_lock`)** in `core/locking.py` — release becomes
  automatic, tied to whatever connection the transaction commits or rolls
  back on, with no separate unlock statement, making this leak class
  structurally impossible rather than just less likely. Confirmed via
  research that this is the standard fix for this exact bug class, that
  session- and transaction-scoped locks on the same key share one lock
  space (so all callers of one lock name have to move together, not just
  the vulnerable ones), and that reentrancy — which
  `dispatch_trade_intent`'s nested `transition_mode` call depends on — is
  preserved under the transaction-scoped variant. `LOCK_PROCESS_SINGLETON`
  was deliberately left unchanged (a different, already-correct pattern: a
  dedicated raw connection held for the process lifetime, never returned to
  the pool). Added a `SET LOCAL lock_timeout` alongside the fix as defense
  in depth, converting any future stuck acquisition into a fast, loud
  failure instead of a silent hang. Regression tests in
  `tests/integration/test_locking.py` reproduce the exact leak pattern
  against a real Postgres, confirm reentrancy and cross-session mutual
  exclusion, and exercise concurrent `start_strategy` calls end-to-end.

**Phase 8 — Strategy 6** (Market Depth Imbalance Scalp — last, hardest)
Rolling-window persistent depth-imbalance detection (not single-snapshot), wider
ATM±5 (±7 analysis-only) scan, liquidity-reject-then-rank pipeline, tightest stops +
time-stop exit. Extended paper-only soak period; slippage is the primary graduation
gate given this is the most data-sensitive strategy.
Done when: Strategy 6 produces genuine signals (not silenced entirely by the
liquidity-reject filter) over an extended paper-only soak, with its scorecard's
slippage figure explicitly reviewed against the other five strategies' before any
live-graduation discussion starts.

**Phase 9 — AI Extension Service** (optional, once credentials supplied)
Read-only ports into Signals/market-data/audit, suggestion-event emission only,
enforced no-write at the type level (no TradeIntent/RiskDecision/Order constructors
reachable) plus a CI import-linter rule that fails the build if `ai_extension`
imports any execution/risk write path.
Done when: the CI import-linter rule fails the build against a deliberately-broken
test branch that has `ai_extension` import an execution/risk write path, proving
the enforcement is real rather than aspirational — mirroring how every other
phase's "done when" includes a check that fails against the old/broken code, not
just passes against the new.

**Phase 10/11 — Stage 2 (cloud packaging) / Stage 3 (split deployables)**: deferred
until Stage 1 is stable; no further design needed now beyond the module boundaries
already captured above.

## Addendum — hardening gaps found in plan review (nothing above renumbered)

Two external plan drafts were reviewed against this document (one pre-Shoonya,
one post-Shoonya) to sanity-check completeness before continuing Phase 5. Both
turned out to already be covered, phase-for-phase and more precisely, by
what's above — with the exceptions below, which are genuine gaps. These are
additive: they slot into the existing phases named, not new phases of their
own.

**Before Phase 6 (guarded live) — operational floor:**
- ~~Database backup/restore.~~ — **done this session** (Addendum hardening
  batch). `ops/scripts/backup_db.ps1` (`docker compose exec postgres
  pg_dump` to a timestamped file under the new, gitignored `ops/backups/`)
  and `ops/scripts/restore_db.ps1` (defaults to a throwaway
  `trading_bot_restore_drill` DB, *not* the real dev DB — restoring into
  `trading_bot` itself is a separate, explicit `-TargetDb` opt-in). The
  restore drill was actually run, not just scripted: backed up the real
  local dev DB, restored into the throwaway DB, spot-checked
  `trading_sessions`/`audit_events`/`users` row counts matched the source
  exactly, then dropped the throwaway DB.
- ~~`metric_series` wiring.~~ — **done this session.** `MetricSeries` model
  (`domain/ops/models.py`, migration `0009_metric_series.py`),
  `modules/ops/metrics_service.record_metric` (the write helper), and
  `GET /api/v1/metrics` (`api/v1/metrics.py`, reuses the `audit.view`
  permission rather than seeding a new RBAC code). First real writer is the
  new periodic health-check loop below — verified live end-to-end (started
  the real backend, hit the endpoint, got back real `ntp_drift_seconds`/
  `disk_free_gb` rows the scheduler had written on its own startup cycle).
- Structured/JSON logging is a nice-to-have, not a gate — current stdlib
  `logging` is fine for a single-process Windows Service. Revisit only if
  incident review off plain-text logs becomes painful.

**Phase 5/6 — live-data and live-order hardening:**
- ~~Periodic health-check loop, promoted from "known open item" to a Phase 6
  prerequisite.~~ — **done this session.** `scheduler/health_check.py`'s
  `HealthCheckScheduler` — same background-thread shape as `PositionManager`
  but one process-wide instance, 5-minute default interval, started/stopped
  in `app.main`'s lifespan alongside the position managers. A failing
  NTP/disk check now writes `metric_series` rows, moves any
  `paper_plus_guarded_live`/`live_enabled` session to `degraded_mode` (same
  legal-edge reasoning `PositionManager._handle_broker_auth_error` already
  established — `paper_only` has no edge there, so it's logged-and-alerted
  only), and writes a `SystemAlert` per affected workspace regardless of
  mode. `ShoonyaBrokerAdapter`'s own docstring had already named this loop
  as the next concrete step for wiring its error taxonomy into — that
  wiring itself is still open (see broker error taxonomy below), only the
  loop's existence is done.
- **Paper-vs-live signal comparison during the Phase 5 soak.** Still open —
  needs a real Shoonya session to compare against, unchanged by this
  session's work.
- ~~Real margin/limit retrieval — bigger than "replace the stub" implies.~~
  — **done this session**, all four parts named: (1) `MarginInfo` DTO
  (`contracts.py`) + `BrokerPort.get_margin` abstract method, (2)
  `MockBrokerAdapter.get_margin` — a generous synthetic figure net of
  open-position exposure, so paper mode stays capital-unconstrained same as
  before, (3) `ShoonyaRestClient.get_limits` + `normalizer.parse_margin` +
  `ShoonyaBrokerAdapter.get_margin` — same "researched, not live-verified"
  caveat as the rest of the Shoonya adapter (field names `cash`/`marginused`
  are the common Noren-OMS convention, unconfirmed against a real account),
  (4) Risk Service's `_check_margin_stub` replaced with a real
  `get_execution_broker(...).get_margin()` call, failing closed (rejects)
  on a `BrokerError` rather than treating it as approved.
- ~~Order-ack-timeout fallback to order-history lookup.~~ — **done in the
  2026-08-04 live-verification session.** `ShoonyaBrokerAdapter.place_order`
  now distinguishes a genuinely ambiguous failure (the `PlaceOrder`
  request-response round trip never completed — timeout, dropped
  connection) from a clean `stat: Not_Ok` rejection, by inspecting
  `ShoonyaApiError.__cause__` (Python's own exception chaining, already
  preserved by `rest_client._post`'s `raise ... from exc` — no new
  exception type needed). On the ambiguous case, checks `OrderBook` for a
  row whose `remarks` matches the request's own `idempotency_key` (echoed
  back verbatim by Shoonya) before ever concluding the placement failed —
  a caller blindly retrying after this specific ambiguity would risk
  placing a real duplicate order, the #1 failure mode this system exists
  to avoid. A clean rejection never triggers the lookup (no httpx cause) —
  confirmed by a dedicated test. Unit-tested (found order via fallback,
  genuinely-not-found re-raises, clean-rejection control case) but not yet
  live-verified against a real ambiguous network failure (hard to trigger
  on demand against a real broker) — same "researched, not live-verified"
  caveat as the rest of this adapter until a real occurrence confirms the
  `remarks` round-trip behavior.
- **Broker error taxonomy, completion.** Partially addressed alongside the
  item above (the ambiguous-vs-definitive distinction *is* a real taxonomy
  gap closed this session) but not complete — mapping specific scenarios
  this doc's own Phase 5 section names (IP mismatch, TOTP drift) to
  distinct exception types still needs live evidence of their actual
  `emsg` text, which doesn't exist yet. Still open for those specifically.
- ~~Emergency square-off — decided, scoped narrowly, does not change the
  kill-switch default.~~ — **done this session**, though two of the three
  legs turned out to already exist from Phase 3: (1) the manual "exit all"
  button — `POST /sessions/{id}/square-off` + the frontend's existing
  `squareOffMutation` — was already built and needed no change; (2) EOD
  forced square-off — unchanged, `PositionManager` already did this; (3)
  the one new narrow automatic trigger — `PositionManager._check_margin_breach`
  calls the new `get_margin` once per cycle for
  `paper_plus_guarded_live`/`live_enabled` sessions with open positions,
  and on a negative `available_margin` calls the new
  `run_margin_breach_square_off` (`scheduler/eod_square_off.py`, sharing a
  `_square_off_all_open_positions` helper with the EOD path, differing only
  in `ExitReason.MARGIN_BREACH` vs `ExitReason.EOD_SQUARE_OFF`), writes a
  `CRITICAL` `SystemAlert`, and leaves kill-switch and every other mode
  untouched — exactly the narrow scope decided here, not a connectivity- or
  reconciliation-lag trigger.
- **Known historical audit-chain gap in this dev database, root-caused and
  fixed — found and fixed this session, via a live integration QC pass run
  after the four items above.** `GET /audit/verify` reported `intact:
  false`. Forensic trace (recomputing every one of 1,168 events' own SHA-256
  and separately checking every `prev_hash` link, not just the first break
  `verify_chain` stops at): every row's own hash is internally valid — zero
  content corruption anywhere — but exactly two chain-*link* forks exist, at
  seq 470 (2026-07-26 06:28) and seq 489 (2026-07-29 05:25). At each, two
  `record_event` calls read the same "last row" and both computed
  `prev_hash` against it — i.e. two writers raced past what should have
  been mutual exclusion. Both predate the last commit (2026-07-29 12:46)
  and, per `core/locking.py`'s own docstring, predate the transaction-scoped
  `LOCK_AUDIT_CHAIN` fix — same root cause already found and fixed for
  `LOCK_EXECUTION_SINGLETON` (session-scoped `pg_advisory_lock` + connection
  pooling), just manifesting here as "two writers slipped through" rather
  than "one writer stuck forever." **Zero new forks from seq 490 through
  1168** — a range spanning Phase 7's own concurrent multi-strategy stress
  test and this session's own live QC run, both exactly the load that would
  reproduce this again if the fix were incomplete. It isn't recurring.
  A hash chain can't be "repaired" by recomputing a historical link without
  defeating the entire point of an append-only tamper-evident log — so the
  fix isn't touching the two rows, it's making `GET /audit/verify`/
  `verify_chain` capable of reporting a meaningful answer despite them:
  both gained an optional `since_seq` parameter (`audit_service/service.py`,
  `api/v1/audit.py`) that walks the chain from a documented checkpoint
  forward instead of from row 1 every time, using the anchor row's own
  (internally valid) hash as the starting `prev_hash`. No default `since_seq`
  is baked into the code — the right anchor is a fact about *this* database's
  own history, not about the schema, so callers pass it explicitly. For this
  dev database specifically: `GET /audit/verify?since_seq=489` reports
  `intact: true` today; verified live against the real dev DB, not just in
  tests.

## Guardrail-layer batch — external proposal evaluated, four genuine gaps closed

An external "broker-agnostic guardrail layer" proposal (order state machine,
stale-quote protection, pre-trade validation, risk rails, a recovery UI,
built as a new `core/`/`adapters/`/`ui/` module tree ahead of Shoonya
integration) was reviewed against this codebase. Verdict: ~90% of it is
already built, more maturely, under different names — `OrderStatus`/
`OrderEvent`, `RiskLimitConfig`/`evaluate_trade_intent`'s 9 gating checks,
`ReconciliationRun`/`BrokerSyncState`. Building the proposed parallel module
tree was rejected outright — it would create a second, competing
risk/execution/reconciliation system in the same codebase, against the
locked modular-monolith decision. Four pieces were confirmed as genuine,
verified gaps and built as small additions to the *existing* structure:

- **Quote/option-chain freshness gate.** Traced the actual signal-generation
  path first: strategies never call the broker live — every cycle reads the
  *latest DB row* via `strike_ranking.rank_from_latest_snapshot`, and
  `OptionChainSnapshot` was written exactly once, at `start_strategy` time
  (the Phase 7-flagged, previously-unfixed staleness gap). New
  `market_data/freshness.py` (`FreshnessState`: `LIVE`/`DEGRADED`/`STALE`/
  `DEAD` — trimmed from the proposal's 6-state list; `FALLBACK`/
  `RECOVERING` describe a live-WebSocket-reconnect state machine this
  DB-timestamp-age model doesn't have) — `ensure_fresh_option_chain` doesn't
  just gate, it refreshes via the existing `record_option_chain_snapshot`,
  which is the actual fix. Wired into `strategy_engine.runner.run_cycle`
  before `evaluate()`; every one of the 6 strategies already exposed
  `instrument_id`/`expiry_date` as plain attributes (confirmed by grep), so
  this needed zero changes to any strategy file. Also generalized the
  existing manual-approval price-drift check
  (`api.v1.strategies.approve_trade_approval`'s `FRESHNESS_TOLERANCE_PCT`,
  extracted not rewritten) into `evaluate_trade_intent`'s AUTO-mode path too
  (`price_drift_exceeded`) — this is what actually delivers the proposal's
  "price-band" ask, since no real NSE circuit-limit data exists anywhere in
  this codebase or Shoonya's parsed fields to check against instead. A real
  cross-session-visibility bug surfaced fixing the first failing test:
  `ensure_fresh_option_chain` now threads `run_cycle`'s own session through
  to the refresh (`session_factory` param) instead of opening a second,
  independently-committing connection — more correct (atomic with the rest
  of the cycle), not just a test workaround. `GET /strategies/running`
  gained a `data_freshness` field, live-verified against a real running
  strategy.
- **Pre-trade mechanical checks.** `tick_size_violation` and
  `freeze_qty_exceeded` added to `evaluate_trade_intent`. Empirically and
  structurally verified safe against every existing test/live-paper path
  before shipping as a hard reject (not just untested): `MockBrokerAdapter`
  seeds option premiums as whole numbers and never steps them (`get_quote`/
  `get_option_chain` always pass `step=False`; only a subscribed
  *underlying* symbol steps), and `compute_stop_target`'s clean multipliers
  preserve exact tick alignment — this only starts doing real rejecting work
  once Shoonya's fractional premiums flow through execution (Phase 6).
  `Instrument.freeze_qty` (migration `0010`) is nullable and
  operator-supplied on purpose — real NSE freeze quantities are exchange-
  published and periodically revised, never a fact to hardcode. Found and
  fixed a real regression risk while wiring `instrument_sync.py`: naively
  syncing `freeze_qty` from broker data would silently blank out an
  operator-set value back to `NULL` on every sync, since Shoonya's
  normalizer doesn't parse this field — fixed to only ever overwrite when
  the broker source actually supplies a value, proven by a dedicated
  regression test.
- **`MockBrokerAdapter` fault injection.** Opt-in only, by design — the
  blast radius of changing the *default* fill behavior was checked first
  (34 `MockBrokerAdapter(` instantiations / ~28 `place_order` references
  across 6-9 test files) and judged too risky. New `FillScenario` +
  `queue_fill_scenario`/`simulate_disconnect`; nothing queued is
  byte-identical to before this existed, confirmed by the full suite passing
  unmodified both before and after. Found a real, previously-latent bug
  while scoping this: `close_position` never branched on the exit order's
  status — a non-terminal/`None` `avg_fill_price` would crash `_dec(None)`.
  Never reachable in production (Mock always fills), but exercising
  reject/delay scenarios on exit orders would hit it immediately. Fixed
  narrowly: only a non-FILLED or price-less exit order is affected; it logs,
  writes one `SystemAlert` (`exit_order_unfilled`), and leaves the position
  `OPEN` for the next `PositionManager` cycle or a manual reconcile — normal
  exits are untouched.
- **Recovery panel.** `system_alerts` and `ReconciliationRun`/
  `BrokerSyncState` were both written but had no read path at all — new
  `GET /system-alerts` and `GET /sessions/{id}/reconciliation-runs`
  (workspace/session-scoped, same pattern as `audit.py`/`metrics.py`).
  Frontend: new types, a `useRecovery.ts` hook (4s poll, matching
  `RunningStrategiesPage`'s precedent), and a new Recovery page + nav link.
  `SessionsPage.tsx`'s existing Square-off/Reconcile/Kill-switch mutations
  were deliberately left untouched — the Recovery page reads its own data
  independently rather than threading typed responses through existing,
  already-working mutations. Live-verified: created a real session, clicked
  the existing Reconcile button, confirmed the new page showed the resulting
  run without a page refresh.

312/312 backend tests pass (up from 281), `ruff`/`mypy` clean throughout,
`npm run build` clean, every batch live-verified via the real dev server
(backend + frontend together for the recovery panel).

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
