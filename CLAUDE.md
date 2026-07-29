# Trading Bot — Project Guide

Personal algorithmic options-trading platform for Nifty/Bank Nifty, broker-agnostic
internally with Shoonya (Finvasia) as the first real broker. Local-first now,
cloud-ready later. Non-AI deterministic execution core; AI stays optional/advisory,
wired in later via a credentials file.

**Full build plan (architecture, schema, phase-by-phase spec): [docs/architecture/build-plan.md](docs/architecture/build-plan.md) — read this first for any non-trivial change.**

## Status: Phase 0-4 complete, Phase 5 next

- ✅ **Phase 0** — Auth (Argon2) + RBAC, hash-chained audit log, the full 6-state safe
  operating-mode state machine (`paper_only` / `paper_plus_guarded_live` /
  `live_enabled` / `degraded_mode` / `reconciliation_lock` / `kill_switch`), Postgres
  advisory locking (execution singleton, process singleton, serialized audit writes),
  Windows sleep inhibitor, NTP/disk health checks, rate limiter, Windows Service
  wrapper, CI.
- ✅ **Phase 1** — `BrokerPort` abstract interface + DTOs, full mock/replay broker
  adapter (random-walk price simulator), market/instrument domain schema, synthetic
  Nifty/Bank Nifty universe generator, idempotent instrument-sync job (doubles as
  initial seed + daily refresh), Market Data ingestion service, VWAP/EMA9/EMA20
  indicator engine with bar aggregation.
- ✅ **Phase 2** — shared strike-ranking engine (ATM±N, spread/volume/OI/premium-fit/
  depth scoring), Risk Service (versioned `risk_limit_configs`, margin stub,
  concurrency/daily-trade/consecutive-loss/budget/same-strike checks, full audit +
  `system_alerts` wiring), pre-trade analytics stored on every `risk_decisions` row,
  the daily-plan endpoint, and a synthetic strategy stub (+ timer-driven runner)
  proving the full Signal→TradeIntent→RiskDecision→audit loop. See the build plan's
  Phase 2 section for the "Phase 2 amendments" (implementation-time decisions + the
  `paper_only → kill_switch` transitions-table bug) and "QC pass findings" (a
  post-implementation review found and fixed three more real bugs: an
  approval-required trade that never closed, a trade-approval lookup that wasn't
  workspace-scoped, and an unlocked check-then-act in strategy start) recorded
  there.
- ✅ **Phase 3** — the real Order/Position/StopPlan/TrailPlan/TradeOutcome lifecycle
  (paper, against `MockBrokerAdapter` via `BrokerPort.place_order`/`get_positions` —
  not a separate tick-based simulator), `PositionManager` (per-session background
  poller: stop/target/trail checks, EOD forced square-off, proactive
  pending-approval expiry, periodic reconciliation), Reconciliation Service
  (event-triggered from every dispatch/close + polling), Reporting v1 (daily
  report + scorecard: win rate, avg win/loss, profit factor, max drawdown,
  slippage, signal-vs-execution counts), and the startup-recovery hook doing real
  work for the first time (resumes `PositionManager` for any session found
  `ACTIVE` with an open position after a restart). See the build plan's Phase 3
  section for the "Phase 3 amendments" (the paper-executes-through-BrokerPort
  design decision, why `PositionManager` is started explicitly at strategy-start
  rather than implicitly from dispatch) and "QC pass findings" (two real bugs in
  the generic trailing-stop logic, a missing proactive approval-expiry sweep, and
  a circular-FK test-cleanup trap) recorded there.
- ✅ **Phase 4** — the real `Strategy` interface (`ConfirmationFilterStrategy`
  template method) plus ORB, VWAP Pullback, and EMA Micro-pullback replacing
  the Phase 2 synthetic stub, a generalized `StrategyRunner` (one implementation
  for all four strategies, not a fourth bespoke copy), real OHLC candle
  persistence (`price_bars`, previously discarded once EMA read it), per-method
  trailing + structure-break/spread-blowout exits, `strategy_configs
  .strategy_type` strategy-selection, and `GET /strategies/running`. All three
  strategies run concurrently, across multiple sessions, in a mix of auto and
  approval-required mode. See the build plan's Phase 4 section for the "Phase 4
  amendments" (per-strategy entry logic, since no spec existed before this
  phase; why `structure_level` lives on the existing `stop_plans` row, not a
  new table) and "QC pass findings" — four real, non-obvious bugs, none of
  them in the three strategies themselves: `migrations/env.py` missing 5 of 9
  domain packages (would have dropped most of the Phase 2/3 schema on the next
  autogenerate), market data ingestion never actually started outside tests
  since Phase 1, `MockBrokerAdapter`'s single-callback-slot streaming design
  silently dropping every underlying's ticks except whichever one subscribed
  most recently, and `get_broker()`'s live singleton having no instrument
  universe (fixed same-day, as a follow-up once QC surfaced it) — all
  recorded there.
- ✅ **Frontend SPA (React) — first real cut** — a genuinely usable Vite +
  React + TypeScript SPA (`frontend/`), not just a read-only dashboard stub:
  Login, Running Strategies (poll `GET /strategies/running` every ~4s, inline
  Approve/Reject on pending trades, Stop), Sessions (create + kill-switch/
  square-off/reconcile), Strategies (create/start/stop, all four strategy
  types), Reports (daily report + scorecard). Four new read-only backend
  endpoints (`GET /sessions`, `/broker-accounts`, `/strategies`,
  `/instruments`) added first, since none existed before this — every prior
  phase only ever needed single-item lookups. TanStack Query for all server
  state/polling, React Router for the four pages, cookie auth via a
  same-origin dev proxy (no CORS needed). No WebSocket push yet (REST +
  polling is enough for what exists today); see the build plan's own
  section for the full page-by-page breakdown and the two real bugs this
  session's manual browser QC found and fixed in `list_running_strategies`/
  `approve_trade_approval`/`reject_trade_approval` (recorded there, same
  "QC pass findings" pattern as every other phase).
- 👈 **Phase 5 is next** — Shoonya Broker Adapter (real integration, still no
  live orders). Full spec in the build plan under "Phase 5".

QC passes were done after Phases 1, 2, 3, and 4 (see git log) that each found
and fixed several real bugs — worth reading `git log -p` on those commits if
touching auth, sessions, the mock adapter, `main.py`'s lifespan, the
risk/strategy modules, the execution/reconciliation modules, or market data
ingestion, since the fixes encode non-obvious reasoning.

## Running it locally

Requires Docker Desktop (Postgres 16 + Redis 7) and Python 3.11+.

```bash
# 1. Start Postgres + Redis
docker compose -f ops/docker/docker-compose.local.yml up -d

# 2. Backend setup (from backend/)
python -m venv .venv
./.venv/Scripts/pip install -e ".[dev,windows]"   # windows extra is pywin32, skip off-Windows
cp app/config/environments/local.env.example .env   # then edit DB_PASSWORD to match compose
cp app/config/credentials/shoonya.env.example app/config/credentials/shoonya.env  # only needed for Phase 5+

# 3. Migrate + seed
./.venv/Scripts/python -m alembic upgrade head
BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD="a-real-password-12+chars" \
  ./.venv/Scripts/python scripts/bootstrap_admin.py

# 4. Run
./.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 5000
# Swagger UI: http://127.0.0.1:5000/docs
```

```bash
# 5. Frontend (from frontend/, with the backend already running on :5000)
npm install
npm run dev
# http://localhost:5173 — vite.config.ts proxies /api to 127.0.0.1:5000,
# so the backend's session cookie works same-origin with no CORS setup.
```

**Tests**: `./.venv/Scripts/python -m pytest` — auto-creates and drops an isolated
`<DB_NAME>_test` database (see `tests/conftest.py`); never touches the dev DB. Run
`ruff check .` and `mypy app tests` before considering anything done — both are
enforced in CI and kept at zero errors throughout Phase 0-4. There's no
frontend test tooling yet (see the build plan's frontend section); verify
frontend changes by driving the real dev server (`npm run build` for a type
check, then exercise it live).

## Conventions that matter (don't relitigate without reading the "why")

- **Broker-agnostic boundary**: every module talks to `app/modules/broker_adapter/base/broker_port.py`,
  never to a concrete adapter. The mock adapter (`.../mock/adapter.py`) is what
  Phases 1-4 are built and tested against; Phase 5 swaps in the real Shoonya adapter
  behind the same interface with zero upstream changes.
- **Mode changes go through one place**: `app/core/modes/state_machine.py`'s
  `transition_mode`/`enter_kill_switch`/`recover_from_degraded`. Nothing else should
  ever assign `TradingSession.mode` directly. The legal-transition table lives in
  `app/core/modes/transitions.py` and is covered by structural tests in
  `tests/unit/test_transitions_table.py` — if a change to that table breaks one of
  those tests, that's very likely a real safety regression, not a test to "fix".
  (Phase 2 found and fixed a real gap here: `paper_only → kill_switch` didn't allow
  a `RISK`-triggered transition, so Risk Service's daily-loss-cap breach couldn't
  reach kill_switch from a `paper_only` session — added `Trigger.RISK` to that edge,
  verified against every structural test in that file first.)
- **If it's not in `audit_events`, it didn't happen.** Every safety-relevant action
  (auth, mode transitions, later: risk decisions, orders) goes through
  `app/modules/audit_service/service.py`'s `record_event`, which maintains a
  SHA-256 hash chain (`verify_chain` detects tampering). Every phase's "done when"
  criteria in the build plan includes an audit-log check for this reason.
- **Idempotency and single-writer discipline**: `core/idempotency.py` +
  `core/locking.py` (`LOCK_EXECUTION_SINGLETON`, `LOCK_PROCESS_SINGLETON`,
  `LOCK_AUDIT_CHAIN`) exist because this system's #1 failure mode to avoid is a
  duplicate live order or two processes both believing they're the execution
  authority. Any new write path that could plausibly run twice needs to reason about
  this explicitly, not assume it away. Risk Service's `evaluate_trade_intent` reuses
  the same pattern via `LOCK_RISK_EVALUATION_QUEUE` (also from `core/locking.py`) —
  every TradeIntent is evaluated one at a time, never concurrently, which is what
  actually makes the concurrency cap, daily-trade-count, budget-vs-committed-capital,
  and same-strike checks race-free.
- **`qty_lots`, not raw quantity**: `signals`/`trade_intents.qty_lots` is a count of
  lots, never an absolute order quantity — lot size is always resolved server-side
  from `option_contracts → instruments.lot_size` (Risk Service's
  `compute_pre_trade_analytics`), matching the build plan's rule that a strategy or
  client must never be able to supply the wrong lot size. Phase 3's Execution
  Service is what multiplies `qty_lots × lot_size` into the absolute
  `OrderRequest.qty`.
- **`SessionLocal` has `autoflush=False`** (`core/db/session.py`) — a real bug was
  found and fixed in Phase 1 (`instrument_sync.py`) where a query ran before a
  preceding `db.add()` had been flushed, silently missing rows it should have seen.
  If a query needs to see something just added in the same function, flush first.
- **Test DB isolation**: `tests/conftest.py`'s `db` fixture wraps each test in a
  rolled-back transaction — safe by default. But some things (background-thread
  callbacks, full HTTP round-trips via `TestClient`) need *real* commits to behave
  like production, so several test files use a separate `session_factory` bound to
  the same isolated `engine` and clean up explicitly in fixture teardown instead of
  relying on rollback. When adding this pattern, clean up in FK-safe order using
  explicit IDs captured at creation time — three separate bugs were found and fixed
  in Phase 1/QC from cleanup that missed a table or relied on a subquery over
  already-deleted rows.
- **Decimal vs float**: Numeric/Decimal columns read back from Postgres don't compare
  reliably against raw Python floats (`Decimal('0.0500') != 0.05` is `True`). Route
  through `Decimal(str(x))` first if comparing. Same reasoning applies to Python's
  built-in `hash()` on strings — it's salted per-process (`PYTHONHASHSEED`) and will
  silently give different results across separate runs; use `zlib.crc32` or
  `hashlib` for anything that needs to be deterministic across processes (the mock
  adapter's price-seeding had this bug, fixed during QC).
- **Secrets**: never in the tracked `.env`/`shoonya.env` — both are gitignored, only
  `.example` variants are tracked. `backend/app/config/settings.py` documents which
  env file backs which settings group.
- **Paper execution goes through `BrokerPort`, not a separate simulator**:
  `execution_engine/paper/service.py` calls `place_order`/`get_positions` on
  whichever adapter `broker_adapter/composition.py`'s `get_broker()` resolves to
  (`MockBrokerAdapter` through Phase 5) — this reuses Phase 1's already-built
  order/position simulation and gives Reconciliation Service a genuine broker-side
  book to diff against, so Phase 6's real-broker case is a DI swap, not a rewrite.
- **`orders` ↔ `positions` is a circular FK pair**: an entry `Order` opens a
  `Position`; the `Position`'s `closing_order_id` then points back at the exit
  `Order`. Break the cycle via `positions.closing_order_id` (nullable) when
  deleting either — never via `orders.position_id`, which would leave an exit
  order (always `trade_intent_id=NULL`) with both FK columns null, violating
  `ck_order_exactly_one_of_intent_or_position`. Got this wrong once during Phase 3
  QC in a test cleanup block; same trap will resurface in any new test that closes
  a position and then tries to clean up.
- **`PositionManager` is started explicitly, not implicitly from dispatch.**
  `execution_engine/paper/registry.ensure_position_manager_running` is called from
  `api.v1.strategies.start_strategy` (and resumed from `app.main`'s
  startup-recovery check) — deliberately *not* from inside
  `dispatch_trade_intent` itself, because that function is called directly (with
  a test-owned `broker=`) from unit/integration tests, and auto-starting a real
  background thread there would spawn one per test run polling the *production*
  DB via `PositionManager`'s default `session_scope`.
- **Postgres session-level advisory locks are reentrant per session**: a second
  `pg_advisory_lock(key)` call for a key the same session already holds returns
  immediately and just increments an internal count (must be unlocked the same
  number of times). This is what makes it safe for `dispatch_trade_intent`/
  `close_position` (holding `LOCK_EXECUTION_SINGLETON`) to run an event-triggered
  reconciliation pass that can itself call `transition_mode` (which acquires the
  *same* lock) without deadlocking — but this only holds because nothing in this
  codebase ever acquires `LOCK_RISK_EVALUATION_QUEUE` before
  `LOCK_EXECUTION_SINGLETON`; keep that ordering invariant if you add a new
  call path that touches both.
- **A broker connection is a single shared stream, not one per instrument.**
  `BrokerPort.subscribe_quotes`'s own docstring says so ("Shoonya only
  supports one connection per session"), and `MockBrokerAdapter` reflects that
  literally — one `on_tick`/`on_depth` callback slot, overwritten on every
  call. `market_data/registry.py`'s `ensure_ingestion_running` therefore
  shares **one** `MarketDataIngestionService` instance across every underlying
  for the whole process, extending its subscription (`.start([symbol])`,
  which already accumulates `_symbol_map`) rather than constructing a new
  service per instrument — the latter was Phase 4's first cut, and it
  silently dropped every underlying's ticks except whichever one subscribed
  most recently. Same "one shared thing, not one per caller" shape as
  `broker_adapter.composition.get_broker()`'s own singleton.
- **`structure_level` lives on the existing `stop_plans` row, not a new
  table.** It's a second, independent invalidation level on the
  *underlying's* own price (opening-range boundary / pullback extreme / EMA9
  value) — distinct from `stop_price` (always on the option premium) but
  conceptually the same "risk management before profit" category, checked in
  `evaluate_open_position` after stop/target, before the trail step. `None`
  for any strategy that doesn't set one (`SyntheticStrategy`).
- **Real OHLC candles live in `price_bars`, populated from a `Bar` object
  that already existed.** `IndicatorEngine.on_tick` always built a completed
  `Bar` internally just to feed EMA9/EMA20, then discarded it once Phase 1-3
  only needed the EMA/VWAP scalar. Phase 4's strategies need real
  opening-range/pullback/confirmation-candle structure, so `on_tick` now
  returns `(indicator_values, completed_bar)` and `market_data.ingestion`
  persists the bar too — same instrument-only convention `indicator_snapshots`
  already uses, same `f"{timeframe_seconds}s"` timeframe string.
- **`TradingSession.cutoff_time` defaults to 15:20 IST — tests that build a
  `TradingSession` without setting it explicitly, then exercise anything that
  checks `now_ist().time() >= cutoff_time` (`PositionManager.run_once`,
  `scheduler.eod_square_off`), pass only while real wall-clock IST stays
  before that time.** Several test files' `trading_session` fixtures rely on
  the column default; `test_position_manager.py`'s tests that expect a
  position to stay OPEN started failing for real (not flaky — 100%
  reproducible) once a session's work ran past 15:20 IST, and were fixed by
  setting `cutoff_time=dt_time(23, 59)` explicitly in that fixture. Same trap
  will resurface in any other test file exercising this code path if worked
  on later in the day — set `cutoff_time` explicitly rather than trusting the
  default.

## Known open items

- **Shoonya IP whitelist / static-IP situation** — user was checking with Airtel
  about CGNAT vs static IP; outcome not yet recorded here. Relevant when Phase 5
  configures `SHOONYA_PRIMARY_IP`/`SHOONYA_BACKUP_IP`.
- **Shoonya Authentication doc deep-dive** — the OAuth flow (browser redirect,
  `GenAcsTok` token exchange) is confirmed and documented in the build plan's Phase 5
  section, but the exact rate limits weren't accessible programmatically from their
  docs site; worth a manual look before Phase 5's rate-limiter tuning.
- **GitHub repo**: [drvinothk/tradingbot](https://github.com/drvinothk/tradingbot),
  `main` branch. Phase 2 is committed locally (not yet pushed as of that commit);
  Phase 3's changes are uncommitted in the working tree as of this note — check
  `git log`/`git status` rather than trusting this line, and commit/push only
  when explicitly asked to.
