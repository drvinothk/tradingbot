# Trading Bot — Project Guide

Personal algorithmic options-trading platform for Nifty/Bank Nifty, broker-agnostic
internally with Shoonya (Finvasia) as the first real broker. Local-first now,
cloud-ready later. Non-AI deterministic execution core; AI stays optional/advisory,
wired in later via a credentials file.

**Full build plan (architecture, schema, phase-by-phase spec): [docs/architecture/build-plan.md](docs/architecture/build-plan.md) — read this first for any non-trivial change.**

## Status: Phase 0-4 + frontend + Phase 7 complete; Phase 5 in progress (Phase 6 blocked on it)

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
- 👈 **Phase 5 in progress** — Shoonya Broker Adapter. As of the
  2026-08-04 live-verification sessions (an OCI cloud deployment against a
  real account — see `docs/architecture/build-plan.md`'s Phase 5 section
  and the Addendum for full detail), the REST/option-chain pipeline is
  genuinely live-proven end-to-end for both NIFTY and BANKNIFTY: real
  OAuth login, real `GetOptionChain`/`GetQuotes`/`SearchScrip`/`TPSeries`
  calls, real strategies reaching `scanning` against real market data. Ten
  real, live-only bugs were found and fixed along the way (wrong
  content-type on every POST body, futures-anchored `GetOptionChain`
  always returning the monthly chain regardless of requested expiry,
  missing live quote fields, a missing `strprc` field on every real
  *weekly* option row, a zombie `StrategyRun` left behind on a broker
  failure, `instruments`/`option_contracts` never re-syncing after a real
  login, spurious futures/decoy rows polluting the instrument picker, and
  more — see the build plan for the complete list). **WebSocket auth is
  still broken** — retested fresh during live market hours specifically to
  rule out a "market was closed" theory from the first session, and it
  still returns `NOT_OK` on every attempt; now a confirmed broker-side
  issue, not a timing artifact. A 2026-08-05 live session against the OCI
  deployment (`68.233.110.76`) added a configurable `SHOONYA_WS_AUTH_SOURCE`
  + uid/actid diagnostic logging (`ws_client.py`) and conclusively ruled out
  two more candidate causes: the OAuth `client_id`'s `_U` suffix
  (`FA44103_U`) does **not** leak into the WS frame — live-logged
  `uid='FA44103' actid='FA44103'`, both clean, matching, exactly per the
  reference implementation — and `source` makes no difference (`API`,
  `WEB`, `MOB` all rejected identically, instantly). Every plausible
  client-side cause is now exhausted; Shoonya support has been emailed with
  this exact evidence. Since strategies need `price_bars` that
  only ever came from WS ticks, a broker-agnostic **REST-polling fallback**
  was built into `MarketDataIngestionService` (a health-watchdog falls an
  underlying back to polling `BrokerPort.get_price_history` if WS delivers
  no tick within a grace window) — live-proven: real OHLC `price_bars` and
  `EMA9` populating every minute for both underlyings with zero WS. An
  **order-ack-timeout fallback** (`ShoonyaBrokerAdapter.place_order` checks
  `OrderBook` by idempotency key before concluding a genuinely-ambiguous
  `PlaceOrder` failure means the order never went through — avoiding a real
  duplicate-order risk) is unit-tested but not yet live-verified. All of
  this sits on branch `fix/shoonya-option-chain-expiry-anchor`, not yet
  committed or merged to `main` — deliberately, since it's Shoonya-specific
  work kept isolated per the build plan's own branch note.
  **Still open, per the build plan's actual "done when" bar**: a real
  multi-session paper soak (today produced no live signal — market closed
  minutes after the REST-fallback work finished), a reconciliation
  dry-run against real (empty) broker positions, and the paper-vs-live
  signal comparison from the Addendum — none of these are done yet. One
  smaller Addendum item also remains open: broker error taxonomy for IP
  mismatch/TOTP drift specifically (needs live `emsg` evidence that
  doesn't exist). (Shoonya support has now been emailed about WS, per
  above.)
  **2026-08-11 update — WS auth root-caused and fixed, live-confirmed.**
  Shoonya support replied (personalized to this account's client ID):
  their recent OAuth migration changed the WS auth payload shape entirely
  — connect message type `"t": "a"` (not `"t": "c"`), token field
  `"accesstoken"` (not `susertoken`), success ack `{"t": "ak", "s": "OK"}`
  (not `{"t": "ck", "s": "OK"}`). REST/WS hosts unchanged. Applied in
  `ws_client.py`'s `_authenticate`, live-verified the same day via a
  one-shot diagnostic against the real account: `{"connected": true,
  "auth_ok": true, "ack": {"t": "ak", "s": "OK", "uid": "FA44103"}}` — the
  auth handshake succeeds for the first time since Phase 5 began. **Not
  yet confirmed**: real tick streaming past the auth handshake itself
  (`_receive_loop`, real `subscribe`/`tk`/`tf` messages) — needs a test
  during real market hours, since a closed market may not stream
  regardless of auth succeeding. See `ws_client.py`'s own docstring for
  the full writeup.
  **2026-08-12 update — a second real bug found via that exact test,
  fixed, not yet redeployed/reverified.** Ran `diagnose_ws_ticks` live
  during real market hours (10:59 IST) against a real, correctly-tokened
  NIFTY contract (`NIFTY18AUG26C24400`): auth and warm-up both succeeded,
  `subscribe_quotes` raised nothing, but `ticks_received: 0`. Root cause:
  `ShoonyaWSClient._send` only ever wrote to the socket when given an
  explicit `ws=` connection object — only `_run`'s own background thread
  ever passed one (via `_resubscribe_all`/heartbeat). `subscribe()`/
  `unsubscribe()`, called from any other thread (a real strategy's
  ingestion thread, or the diagnostic itself), went through the same
  `_send` with no `ws=`, which silently did nothing beyond updating
  `_entries_by_key` — correct only if a *future* reconnect would pick it
  up via `_resubscribe_all`. On an already-connected client (the normal
  case — connect once, then every later call just extends the same
  subscription) there is no future reconnect, so the subscribe request
  was dropped forever with zero error anywhere in the stack. This fully
  explains why auth succeeding never once produced a real tick. Fixed by
  having `_run` publish the live `Connection` to `self._live_ws` once
  connected, and having `_send` always write to whatever `_live_ws`
  currently is rather than requiring the caller to supply one — two new
  regression tests (`test_subscribe_sends_immediately_on_an_already_live_
  connection`, `test_unsubscribe_sends_immediately_on_an_already_live_
  connection`) exercise this directly. 496/496 tests pass, ruff/mypy
  clean. **Not yet redeployed to OCI or reverified live** — this was
  found and fixed in the same session as the symbol-format audit above;
  next session (or later this one) needs to deploy this file, restart
  the backend (kills running strategies — Shoonya reconnect + strategy
  restart required after), and re-run the same live diagnostic to
  confirm real ticks now arrive.
- ✅ **Phase 7** — Strategies 4 & 5 (OI/Volume Confirmed, Liquidity
  Sweep/Reversal), built out of order ahead of Phase 6 since neither needs
  Shoonya — both paper-only, against the mock broker, five of six
  strategies now live. OI/Volume Confirmed's "confirmed" half lives in a
  new chain-participation-weighted `StrikeRankingConfig` mode
  (`min_oi`/`min_volume` hard floor + doubled OI/volume score weights), not
  a temporal OI-buildup signal — deliberately, since
  `record_option_chain_snapshot` is only ever called once per run
  (`start_strategy` time) and a delta-based design would never fire in
  real operation. That single-snapshot-per-run gap is real and affects
  every strategy's ranking freshness, not just this one — flagged, not
  fixed, see the build plan's Phase 7 amendments. Liquidity Sweep/Reversal
  is a genuine break-and-reverse pattern (wicks beyond a rolling level,
  closes back inside it), sharing ORB's new `compute_range_high_low`
  helper but not Batch E's `touch_and_confirm` (different shape, same
  "don't force a shared helper across a real behavioral difference"
  reasoning that also kept `structure_level` strategy-owned there). Full
  design reasoning and the extended 5-strategy concurrency e2e proof in
  the build plan under "Phase 7 amendments".

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
- **`LOCK_EXECUTION_SINGLETON`/`LOCK_RISK_EVALUATION_QUEUE`/`LOCK_AUDIT_CHAIN`
  are transaction-scoped advisory locks (`pg_advisory_xact_lock`), not
  session-scoped** — changed after a real incident: session-scoped locks
  (`pg_advisory_lock`/`pg_advisory_unlock`) leak permanently the moment any
  caller commits while still holding one, because `SQLAlchemy Session.commit()`
  releases the connection back to the pool, and the `finally: pg_advisory_unlock`
  a session-scoped lock needs can then run on a *different* pooled connection
  than the one that acquired it — it silently no-ops, and the original
  connection goes back into the pool still holding the lock forever. Found
  live during Phase 7's browser verification (every `POST /strategies/{id}/start`
  started hanging after a few minutes of real multi-strategy traffic); full
  root-cause writeup in `docs/architecture/build-plan.md`'s Phase 7 section
  and `core/locking.py`'s own module docstring. Transaction-scoped locks have
  no separate unlock call — release is automatic at whatever commit/rollback
  ends the transaction — making the leak structurally impossible. Reentrancy
  is preserved: a second acquisition of the same key by the same
  session/transaction is still a fast no-op, same as before, which is what
  keeps `dispatch_trade_intent`/`close_position` (holding
  `LOCK_EXECUTION_SINGLETON`) safe to run an event-triggered reconciliation
  pass that can itself call `transition_mode` (which acquires the *same*
  lock) without deadlocking — but this only holds because nothing in this
  codebase ever acquires `LOCK_RISK_EVALUATION_QUEUE` before
  `LOCK_EXECUTION_SINGLETON`; keep that ordering invariant if you add a new
  call path that touches both. One real behavior change from this fix: a
  lock acquired now stays held until whatever *outer* transaction eventually
  commits, not just the `with advisory_lock(...)` block's own scope — a
  reduction in concurrency, not a correctness issue, consistent with this
  system's own low trade-volume governance. `LOCK_PROCESS_SINGLETON` is
  unaffected — it deliberately keeps the session-scoped, dedicated-connection
  pattern (a raw connection held open for the process lifetime, never
  returned to the pool), since it must outlive any single transaction.
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
- **`BrokerPort.unsubscribe_quotes` must join its stream thread before
  returning, not just signal it to stop.** `MockBrokerAdapter`'s background
  `_stream_loop` only checks the stop event *between* iterations
  (`_stream_stop.wait(...)`), so a caller that tears down its DB/session
  right after `unsubscribe_quotes()` returns can race an in-flight
  `on_tick` callback still landing a `QuoteTick` insert afterward. Present
  since Phase 1, only surfaced during Phase 5 when a new test finally
  exercised `MarketDataIngestionService.stop()` for real — the resulting
  stray `QuoteTick` FK'd to an `Instrument` that a later test's cleanup
  then couldn't delete, cascading into ~40 unrelated tests erroring
  whenever the race lost. Fixed with a bounded `.join(timeout=5.0)` in
  `unsubscribe_quotes` once `_subscribed` empties. Any future real
  `BrokerPort` adapter's `unsubscribe_quotes` (Shoonya's `ws_client.py`
  included) needs the same guarantee — "stopped" must mean no more
  callbacks fire, not just "asked to stop."
- 👈 **Market-data/execution decoupling (Angel One)** — new work, not yet
  live-verified, on top of everything above: since Shoonya's WS never once
  worked live, market data now comes from a second, independent broker-
  agnostic port, `BaseMarketDataProvider`
  (`app/modules/market_data/providers/base.py`), separate from `BrokerPort`
  — Shoonya stays execution-only, untouched. `AngelOneMarketDataProvider`
  (`providers/angel_one.py`) does the real work: REST login (`loginByPassword`
  + server-generated TOTP via `pyotp`, a genuine difference from Shoonya's
  dormant `totp_secret`), then SmartStream over `smartapi-python`'s
  `SmartWebSocketV2` — binary tick unpacking is delegated to that SDK
  entirely (`angel_ws_client.py`'s own docstring explains why: a hand-rolled
  byte-offset guess with no live account to verify against is a categorically
  worse risk than a JSON parsing bug). A new `ScripMasterService`
  (`market_data/scrip_master.py`) bridges Angel's own daily scrip-master file
  to this system's *existing* `Instrument`/`OptionContract` rows by
  structural match (underlying/expiry/strike/option_type — never string
  comparison), storing the mapping in a new `broker_symbol_map` table
  (migration `0012`) plus an in-memory cache; also writes a `shoonya`
  passthrough mapping for interface symmetry, in case execution's own broker
  changes later. `PositionManager` now prices stop/target/trail checks from
  this live feed (`get_latest_tick`), falling back to a one-cycle
  `broker.get_quote()` (Shoonya) read if the feed hasn't delivered a tick
  fresh enough (`market_data.freshness`'s existing staleness classification,
  reused) — full decoupling, per an explicit user call that Shoonya's own
  feed is too fragile to keep pricing anything on. `MARKET_DATA_PROVIDER`
  env var (`"angel_one"` / `"shoonya"` / `"mock"`, default `"mock"` — zero
  behavior change unless opted in) selects the provider via
  `market_data/provider_composition.get_market_data_provider()`, mirroring
  `broker_adapter/composition.py`'s own lazy-singleton pattern. FINNIFTY is
  indexed/mapped in the scrip-master parser for future-proofing only — not
  wired into strategies, the mock universe, or Shoonya's own
  `KNOWN_UNDERLYINGS` yet, by explicit scope decision. Two REST endpoints
  (`loginByPassword`'s exact header/body shape, the scrip-master file schema
  and its strike-×100/`DDMMMYYYY`-expiry quirks) are confirmed from a
  user-supplied Angel One doc extraction; the historical-candle endpoint
  (`get_price_history`, needed to preserve `MarketDataIngestionService`'s
  existing WS→REST-fallback resilience feature) is this session's own
  researched-not-live-verified addition, flagged in
  `angel_rest_client.get_candle_data`'s own docstring the same way every
  other unconfirmed Shoonya claim in this file already is. `smartapi-python`
  pulled in two undeclared transitive dependencies (`logzero`,
  `websocket-client`) discovered only by actually installing and importing
  it — both pinned explicitly in `pyproject.toml` with that discovery
  recorded in a comment. 412/412 backend tests pass (up from ~350),
  `ruff`/`mypy` clean, migration `0012` tested both directions locally. **Not
  live-verified against a real Angel One account** — no credentials exist
  yet; first real test needs a populated `config/credentials/angel_one.env`
  and a live market-hours session, same "deployed and reachable is step one,
  not done" discipline this file's own Phase 5 section already learned the
  hard way.
  **QC pass findings** (same "always self-review, real-money system"
  discipline as every other phase — see the top of this doc's process
  notes): one real, serious bug caught and fixed before this landed —
  `PositionManager`'s new per-position live-subscribe call (added to
  `_open_position_from_fill`/`close_position` in the first cut) went through
  `market_data.registry.ensure_ingestion_running`, whose default
  `session_factory` is the *production* `session_scope`, not a test's own
  isolated DB. That path is called directly, with test-owned brokers, by
  dozens of existing tests across this codebase — exactly the same trap this
  file's own Phase 3 section already documents for why `PositionManager`
  itself is started explicitly rather than implicitly from dispatch. Caught
  by manually diffing real dev-DB row counts before/after a full test run
  (found via that check, not by a failing test — the inserts happened to
  silently no-op since the test fixtures' symbols never existed in the real
  dev DB, so no actual corruption occurred, confirmed by checking every
  affected table's row timestamps against the run date). Fixed by having
  `PositionManager` subscribe directly on its own `market_data_provider`
  instance instead (in-memory only, no DB session in the call at all) and
  tracking subscribed symbols per-instance rather than via the module-level
  registry singleton. A second, related bug surfaced while fixing the
  first: `MockBrokerAdapter`'s background stream thread has no
  pre-subscribe delay, so a test that resolves the *default* market-data
  provider (an unrelated `get_broker()` singleton, not the test's own
  explicit `broker=` override) could race a real tick against the test's
  intended price — fixed by giving `test_position_manager.py` a
  `_NullMarketDataProvider` test double so every existing test keeps
  exercising the deterministic `broker.get_quote` fallback path it always
  relied on, plus two new dedicated tests
  (`test_run_once_prices_from_the_live_feed_in_preference_to_broker_get_quote`,
  `test_run_once_falls_back_to_broker_quote_when_live_tick_is_stale`) that
  actually exercise the live-tick-takes-priority path itself, which had zero
  coverage until this pass.
  **2026-08-05 live deployment session** — deployed to the OCI VM
  (`68.233.110.76`, no git on that box; deployed via a tarball over SSH,
  not a git pull) with real Angel One credentials for the first time.
  Found and fixed three more real, live-only bugs: (1) `_sync_angel_one_scrip_master`
  read a `ScripMasterSyncLog` row's attributes after its `session_scope()`
  had already closed — the identical `DetachedInstanceError` trap
  `record_option_chain_snapshot`'s own docstring already warns about,
  just a fresh instance of it; (2) the credentials file
  (`config/credentials/angel_one.env`) had Windows CRLF line endings from
  its Windows origin, and a shell `echo ... >>` append (adding
  `ANGELONE_AUTH_PROXY` after the fact) landed with no preceding newline,
  silently merging it onto the previous key's value — `pydantic-settings`
  read it as `auth_proxy=""` with no error, so the bug only surfaced as a
  mysterious "proxy configured but not applied" symptom, not a crash;
  fixed by rewriting the file clean rather than trusting further shell
  appends. **Live-confirmed, not guessed**: `apiconnect.angelone.in` (the
  *authenticated* REST gateway) consistently times out from the OCI VM's
  IP while Angel's own *public* scrip-master endpoint, Shoonya's API, and
  general internet all respond instantly from that same IP — and the
  identical authenticated request from an unrelated residential IP also
  responds instantly. Confirmed a cloud-IP-range block (or an
  unpropagated whitelist — can't fully distinguish the two from outside),
  not a code bug. (3) Added `AngelOneSettings.auth_proxy`
  (`ANGELONE_AUTH_PROXY`) — an optional HTTP(S) proxy `AngelOneRestClient`
  routes both `loginByPassword` *and* `getCandleData` through (both hit
  the same blocked gateway; there's no reason to expect one behaves
  differently from the other), explicitly never applied to the WebSocket
  (`angel_ws_client.py`, a different host, `smartapisocket.angelone.in`,
  and latency-sensitive by design) or the scrip-master download (a third
  host, already confirmed reachable directly). A user-supplied proxy fix
  request also asked for proxy timeouts to raise `BrokerAuthError` —
  implemented as `BrokerConnectivityError` instead (already what the
  existing `except httpx.HTTPError` produces, `httpx.ProxyError` included)
  since a proxy being unreachable is "retry next cycle," never "credentials
  are dead" — misclassifying it would have risked
  `PositionManager._handle_broker_auth_error` firing a spurious
  `degraded_mode` transition on a guarded-live/live session over a
  transient relay blip, not just an inaccurate label. **Result: the proxy
  fix works** — `loginByPassword` now succeeds live, `_resume_strategy_runners`
  reported "Resumed 1 strategy runner(s)" with zero auth errors, confirmed
  via real systemd/journalctl logs on the OCI box, not just a local test.
  **WebSocket still open**: `smartapisocket.angelone.in` (all 4 of its
  resolved IPs) also timed out via a raw TLS test from the OCI VM,
  contradicting the working assumption that only the REST gateway was
  blocked — one connection attempt did briefly succeed (reached `on_open`)
  before the subscribe failed with "socket is already closed," so this may
  be flaky connectivity rather than an absolute block, or may simply be
  the market being closed at test time (18:0x IST, well past close) closing
  idle connections server-side — the two causes weren't distinguishable
  from this session's evidence alone. Per explicit user decision, WS stays
  direct (no proxy) for now; next step is retesting during real market
  hours before touching this further — see `angel_ws_client.py`'s own
  reconnect-backoff loop, which is already retrying on its own and needs no
  new code either way.
  **2026-08-06 live market-hours retest** — WebSocket retested specifically
  during real trading hours (~11-13:00 IST) to finally settle the
  "flaky connectivity vs market closed" question the prior session
  couldn't resolve: still fails identically (`socket is already closed`
  on subscribe), ruling out "market was closed" for good — this is a real,
  persistent issue independent of timing. Two external suggestions were
  evaluated against the actual installed `smartapi-python` SDK source
  (not assumed): a claimed "malformed subscribe payload" bug doesn't
  apply — `SUBSCRIBE_ACTION = 1` is a plain SDK-internal int this
  codebase never touches, since `subscribe()` builds its own JSON
  entirely inside the SDK; a claimed "binary parsing crash" fix doesn't
  apply either, matching `angel_ws_client.py`'s own pre-existing
  docstring reasoning for delegating parsing to the SDK. Same session
  found and fixed two real, live bugs instead: (1) a stale Angel One
  token retried `getCandleData` silently for ~12 hours overnight,
  exhausting the endpoint's real rate-limit budget before anyone
  noticed (`HTTP 403: Access denied because of exceeding access rate`)
  — fixed with a `BrokerRateLimitedError` (a `BrokerConnectivityError`
  subclass, deliberately not `BrokerAuthError` — relogin doesn't reset
  a rate-limit counter) that backs the REST-poll loop off 300s instead
  of the normal ~25s interval specifically on that rejection, plus
  `BrokerAuthError` now also covering `get_candle_data`'s own
  "Invalid Token" soft-failure (`status: false`, HTTP 200) so
  `get_price_history` clears the cached token and forces a genuine
  fresh login next call instead of retrying the same dead token
  forever — the actual root cause of the overnight exhaustion. Each
  fix has a dedicated test verified to fail without it and pass with
  it (reverted in turn, confirmed the exact live error reproduces,
  restored), not just written after the fact. (2) Nothing previously
  stopped strategy scanning or market-data polling outside market
  hours at all — added `MarketHoursGatedProvider`, wrapping at the
  `BaseMarketDataProvider` composition level (`provider_composition.py`)
  rather than inside any one concrete provider, so the 08:30-16:00 IST
  policy applies uniformly regardless of which real provider
  (`angel_one` today, `shoonya` if ever selected again) is configured
  — deliberately excluding `"mock"`, which has no real API/rate-limit
  concept to protect and is the default every test/local-dev run
  resolves to (confirmed: full suite unaffected, mock-backed tests stay
  time-of-day-independent). `MarketDataScheduler` (same background-
  thread shape as `HealthCheckScheduler`) acts on the phase transitions:
  a fresh login entering pre-market (08:30), a 5-minute liveness check
  through pre-market, a hard disconnect at 16:00. `MARKET_DATA_
  ALLOW_OFFHOURS_TESTING` (off by default, deliberately never set in the
  tracked `.env` — a scoped `systemctl set-environment` for the live
  deployment or an inline env var locally is the intended one-off
  override, per explicit design discussion of the exact overnight-
  hammering risk a permanent `true` would reintroduce) is the escape
  hatch for testing a real provider outside those hours without
  weakening the gate for everyone. 25 new tests, all via `run_once()`/
  monkeypatched time, no real threading or wall-clock dependency.
  447/447 passing, ruff/mypy clean, both fixes live-verified on the OCI
  deployment (the new "backing off 300s" log line firing correctly; the
  new "Market-data phase transition: startup -> active_market" line
  firing correctly on restart at 12:59 PM IST). Full checksum-verified
  sync between local, GitHub, and the OCI deployment confirmed after
  each deploy, same discipline as every other live session recorded in
  this file.

## Known open items

- **2026-08-12: Shoonya WS is now the live `MARKET_DATA_PROVIDER`, real
  end-to-end tick streaming confirmed.** `MARKET_DATA_PROVIDER=shoonya` set
  on the OCI `.env`. Found and fixed three real, live-only bugs to get here:
  (1) `ShoonyaWSClient._send` only wrote to the socket when handed an
  explicit `ws=` — `subscribe()`/`unsubscribe()` from any thread other than
  the background connection thread silently no-op'd on an already-open
  connection, so a real subscribe request could be dropped forever with no
  error (fixed via a tracked `self._live_ws` reference); (2) Noren's
  touchline feed sends a complete snapshot once (`"tk"`/`"dk"`) then only
  partial updates after that (`"tf"`/`"df"`, carrying just the changed
  fields) — `parse_tick` required `lp` unconditionally, so any partial
  update that didn't touch price raised `NormalizationError` and got
  silently dropped, losing real ticks; fixed via a per-token
  `_last_known_by_key` merge before parsing; (3) `subscribe_quotes` on an
  underlying (not an option contract) had no fallback token resolution —
  `MarketDataIngestionService` subscribes to the underlying's own tick
  *before* any strategy has called `get_option_chain` in-process, so the
  very first `start_strategy` against a fresh Shoonya-backed provider would
  500. Also fixed: `app.main._sync_mock_instrument_universe` ran
  unconditionally on every restart (mock is always the broker before a
  human reconnects Shoonya), silently reactivating the mock's synthetic
  "nearest Thursday" expiry over already-correct real data — now skipped
  once real data exists; and `market_data.registry.reset_for_reconnect`,
  called from `oauth_callback` when `MARKET_DATA_PROVIDER=shoonya`, so a
  reconnect actually rebuilds ingestion against the new broker instead of
  staying silently stuck on the mock-wrapped provider resolved at startup.
  **Live-confirmed**: real, continuous `price_bars` for NIFTY/BANKNIFTY
  with correct spot values, zero errors, after all fixes deployed. The
  `/shoonya/ws-tick-diagnostic` endpoint itself still reports
  `ticks_received: 0` when Shoonya is the active provider — not a
  production bug, a diagnostic-tool limitation: `ShoonyaWSClient` is one
  shared connection with a single `on_tick` callback fixed at
  construction, and since ingestion now always subscribes first, the
  diagnostic's own callback never gets wired in even though its
  subscription is genuinely honored on the wire. Left as-is; the thing it
  was built to verify (real ticks flowing) is proven true by the
  `price_bars` data itself.
- **Market-data provider priority is a user preference, not yet a codified
  fallback mechanism — recorded here for whoever picks this up next.**
  Current/default: Shoonya WS primary, Angel One WS as the intended
  fallback (not yet built — `MARKET_DATA_PROVIDER` is a single string,
  no automatic failover between providers exists; a provider outage today
  just means no data, not a switchover). Explicitly acceptable to the user
  for now — building the real fallback is deliberately deferred, not an
  oversight. Two future scenarios already indicated, so any fallback
  design should support swapping which provider is primary vs. fallback,
  not hardcode Shoonya-primary: (a) subscribing to TrueData → TrueData
  primary, Angel One fallback; (b) Angel One WS working without the proxy
  workaround (see the Angel One section above — proxy currently required
  from the OCI VM's IP) → Angel One primary, Shoonya fallback. Whoever
  builds this: a provider-agnostic failover wrapper at the
  `provider_composition.py` composition-root level (mirroring
  `MarketHoursGatedProvider`'s own wrapping pattern) is the natural shape,
  not a rewrite of any individual provider.
- **2026-08-12: likely root cause found for the option-chain zero-price gap
  below — `SearchScrip` is returning an empty `token` field for every
  recently-synced NIFTY/BANKNIFTY option row.** Live evidence: the
  `option_contracts` rows synced before ~2026-08-10 (an old, since-
  deactivated BANKNIFTY batch, 22 rows) have a real `broker_token`; every
  row synced since (all of NIFTY's current 42 active rows, and BANKNIFTY's
  current 42) has `broker_token=''`. Symbols/strikes/expiries in the same
  rows are correct — only the token is missing. Confirmed live via
  `/shoonya/ws-tick-diagnostic`: `resolve_token` fails with "no cached
  broker token" for a real, currently-listed NIFTY contract, because
  `ShoonyaBrokerAdapter._remember_token` only caches a token when one is
  actually present (`if token: ...`) — an empty string from the broker is
  silently never cached. Without a token, nothing can subscribe to or
  quote that contract via WS *or* REST — this plausibly explains the
  zero-price `GetQuotes` symptom below too (same missing token feeding
  `GetOptionChain`'s per-strike pricing calls), not just today's WS-tick
  test. Timing lines up with Shoonya's own OAuth migration (same one that
  broke the WS auth payload, fixed 2026-08-11). **Not yet raised with
  Shoonya support** — worth doing given they were responsive and fixed the
  WS issue quickly; cite the exact before/after `broker_token` evidence
  above, not just "GetQuotes returns zero."
  **2026-08-12, same session -- bigger correction underneath this:**
  Shoonya's own official static scrip master
  (`https://api.shoonya.com/NFO_symbols.txt.zip`, a real, live, public,
  daily-updated file -- confirmed by downloading and inspecting it
  directly, not assumed) shows NIFTY's and BANKNIFTY's real current
  near-term expiries are **18-AUG-2026 and 25-AUG-2026 respectively --
  both Tuesday** -- and **`13-AUG-2026` does not exist in that file at
  all**. The "confirmed correct" `13-AUG-2026` (Thursday) data this
  session spent two days treating as ground truth (including a live
  spot-price cross-check that seemed to validate it) was itself wrong --
  likely a `SearchScrip`-side echo of stale/legacy data, not a real
  listed contract. The earlier "BANKNIFTY 25-AUG-2026 is stale, ~31,000
  strikes are unrealistic" conclusion was also wrong -- that was an
  under-sampled read (3 rows, not sorted by strike) of what's actually a
  complete, real, correctly-priced chain spanning up through ~59,000
  (right at real spot). **Immediate fix applied** via a one-off script
  (deliberately not committed to the repo, not integrated into
  `sync_instrument_master` -- see below): deactivated the wrong
  `2026-08-13` rows for both underlyings (84 rows), and upserted 284
  real, ATM-centered contracts for `2026-08-18` (NIFTY) / `2026-08-25`
  (BANKNIFTY) with real broker tokens read directly from the scrip
  master file. Final DB state verified correct: NIFTY 162 active
  contracts (all real tokens), BANKNIFTY 144 active (122 newly inserted
  + 22 pre-existing deep-OTM rows from before the token bug, silently
  reactivated by the normal sync's own not-yet-expired-contract
  reactivation logic once `SearchScrip` echoed them again).
  **Deliberately deferred to a future session**: the real daily-download
  pipeline (scheduled fetch, parse, bulk upsert, `SearchScrip` demoted to
  fallback) -- this session's fix is a manual, one-time correction only,
  chosen explicitly over rushing a permanent pipeline late in an
  already-long session. When building it, mirror
  `AngelOneMarketDataProvider`'s own `ScripMasterService` pattern
  (`market_data/scrip_master.py`) rather than inventing a new shape --
  same problem, same broker family (Noren-based), already-proven design
  in this codebase.
  **2026-08-12, follow-up: the CE/PE-suffix symbol convention this
  codebase has treated as confirmed-correct for months may itself trace
  back to the same bad Aug-13 dataset.** The scrip-master-corrected rows
  above were initially given reconstructed symbols in that CE/PE-suffix
  style (`BANKNIFTY25AUG2657900PE`) to match existing convention --
  live-rejected by a real `GetOptionChain` call: `HTTP 400 Invalid
  Trading Symbol`. Shoonya's own scrip master (and the one older
  BANKNIFTY batch that predates the token bug and was genuinely
  resynced successfully) both use a **P/C-suffix** format instead
  (`BANKNIFTY25AUG26P57900`) -- fixed for these 284 rows via a second
  one-off script (symbol column only, nothing else touched).
  **Audited the same day, made P/C-suffix the default wherever a
  Shoonya symbol appears in code.** Result: `normalizer.py` itself was
  never actually wrong -- `_strike_from_tsym`'s own regex and its
  docstring already expected P/C-suffix, built months ago from a real,
  live-observed row (`NIFTY04AUG26C18500`); `to_place_order_payload`
  never constructs a symbol at all, it passes `request.contract_symbol`
  through verbatim, so a correct DB value flows through correctly with
  no code change needed. The wrong convention existed only in two
  places: this session's own one-off correction script (already fixed)
  and Shoonya's test fixtures across `test_shoonya_normalizer.py`,
  `test_shoonya_rest_client.py`, `test_shoonya_adapter.py`,
  `test_shoonya_ws_client.py`, `test_api_shoonya.py` (all updated to
  P/C-suffix for consistency, even where the exact string was only ever
  opaque pass-through data functionally) -- see `normalizer.py`'s own
  module docstring for the full, authoritative writeup, now the single
  place this format is documented. **Deliberately scoped to Shoonya
  only** -- Angel One, TrueData, and the mock adapter each have their
  own, unrelated, already-correct conventions and were left untouched.
- **Designated fallback for Shoonya's option-chain zero-price gap: TrueData's
  live option-chain REST API.** Live-found 2026-08-11: Shoonya's own
  `GetQuotes`-per-contract option-chain pricing returned zero live price on
  every strike, continuously, for a full paper session (both before and
  after market close) — no exceptions, no parse errors, just no usable
  price, which `market_data/freshness.py`'s `classify_option_chain` then
  correctly refuses to trade on (forces `DEAD`, skips the strategy cycle,
  by design — see that function's own docstring). Root cause on Shoonya's
  side still unconfirmed as of this note. TrueData has a genuine, separate
  live option-chain source that isn't Shoonya-dependent at all —
  `getoptionchain` on `analytics.truedata.in` (REST, poll-based) and/or
  `TD_live.start_option_chain(...)` (WS-native, continuously updating off
  the same real-time feed `TrueDataProvider` already uses for underlying
  ticks) — see that module's own docstring for the confirmed API shape.
  **Not implemented** — this is the designated fallback plan if Shoonya's
  gap turns out to be persistent/systemic rather than a one-off, not
  something built into the code yet. Whoever picks this up next: it would
  mean `BrokerPort.get_option_chain`'s current Shoonya-only scope needs
  either a second implementation route or a provider-agnostic seam
  (mirroring the market-data/execution split `BaseMarketDataProvider`
  already established for ticks) — a real architecture decision, not a
  drop-in swap.
- **Shoonya IP whitelist / static-IP situation** — user was checking with Airtel
  about CGNAT vs static IP; outcome not yet recorded here. Relevant when Phase 5
  configures `SHOONYA_PRIMARY_IP`/`SHOONYA_BACKUP_IP`.
- ~~Shoonya Authentication doc deep-dive~~ — done this session: GetQuotes is
  10/sec & 200/min, order placement 20/sec & 200/min per service instance
  (shoonya.com FAQ). `core/rate_limiter.py`'s existing conservative default
  (5/sec, burst 10) already sits safely under both; docstring updated to
  record the confirmed numbers, no behavior change needed.
- **Shoonya REST host path is unconfirmed and has a real discrepancy** —
  `ShoonyaSettings.api_host`/`ws_host` (`app/config/settings.py`) have said
  `NorenWClientAPI`/`NorenWSAPI` since Phase 0's own research, but this
  session's Phase 5 research found the official Shoonya-Dev GitHub org's own
  wrapper hardcoding `NorenWClientTP`/`NorenWSTP` instead. Left unchanged
  (a primary-source claim shouldn't be silently overwritten by secondary
  research with no live account to arbitrate) but flagged in
  `settings.py` itself — first thing to try if `GenAcsTok`/any REST call
  404s once real credentials exist.
- **Shoonya adapter auth transport is a hedge, not a confirmed fact** —
  `rest_client.py` sends the access token both as classic Noren `jKey` (POST
  body) and as an OAuth-style `Authorization: Bearer` header simultaneously,
  since research didn't settle which one Shoonya's OAuth variant actually
  needs. Harmless either way; simplify once a real account confirms it.
- ~~Shoonya error-scenario → mode-transition wiring~~ — done this session,
  scoped down from the original plan: there's no periodic Scheduler
  health-check loop to wire into (NTP/disk checks only ever ran once at
  startup — that gap predates Phase 5 and applies to more than just
  Shoonya, so it's still open, see below). Instead, `PositionManager.
  run_once` — the one place that already polls the broker repeatedly per
  session — catches the new generic `BrokerAuthError`
  (`broker_adapter/base/errors.py`, broker-agnostic on purpose) and moves
  the session to `degraded_mode`. Turned up a real design bug while wiring
  this: `paper_only → degraded_mode` isn't a legal edge in
  `core/modes/transitions.py` at all — `degraded_mode` only exists to
  protect *live* money (`paper_plus_guarded_live`/`live_enabled`), and
  Phase 5 is still paper-only. So for the traffic Phase 5 actually
  produces, a broker auth failure is correctly just logged, not escalated
  — confirmed by two new tests (`test_position_manager.py`) for both the
  guarded-live case (does transition) and the paper-only case (doesn't).
- ~~No periodic Scheduler health-check loop exists for anything~~ — **done
  this session** for NTP/disk: `scheduler/health_check.py`'s
  `HealthCheckScheduler`, a 5-minute background timer (same thread shape as
  `PositionManager`, started/stopped from `app.main`'s lifespan) that now
  reacts to a failing `core/clock.py` check by moving any
  `paper_plus_guarded_live`/`live_enabled` session to `degraded_mode` and
  writing a `SystemAlert`, not just logging. Broker auth failures are still
  only checked per-`PositionManager`-cycle, not by this new timer loop —
  that specific wiring (named in `ShoonyaBrokerAdapter`'s own docstring as
  "the next concrete step") remains open, distinct from the health-check
  loop's existence which is now done. Full design in
  `docs/architecture/build-plan.md`'s Addendum section.
- ~~🔴 `LOCK_EXECUTION_SINGLETON` can get stuck held forever on a pooled
  connection~~ — **fixed.** Found during Phase 7's own browser verification
  (three strategies running against one session; every subsequent
  `POST /strategies/{id}/start` hung indefinitely after a few minutes).
  Root cause fully confirmed afterward (live `pg_locks`/`pg_stat_activity`
  diagnostics + a systematic audit of all 9 call sites of
  `LOCK_EXECUTION_SINGLETON`/`LOCK_RISK_EVALUATION_QUEUE`/`LOCK_AUDIT_CHAIN`
  + external research into the identical documented bug in other projects):
  `SQLAlchemy Session.commit()` releases the connection back to the pool,
  so any `db.execute()` after a `commit()` — including the old
  `finally: pg_advisory_unlock(...)` a session-scoped lock needs — could
  land on a *different* pooled connection than the one that acquired the
  lock. The unlock silently no-ops on the wrong connection; the original
  connection returns to the pool still holding the lock, forever, invisible
  to any per-request diagnostic. Latent since Phase 4
  (`approve_trade_approval`/`reject_trade_approval` both commit while still
  holding the lock, by design), never triggered before because nothing had
  exercised real concurrent connection-pool churn this hard until Phase 7's
  live multi-strategy test. **Fixed by converting `LOCK_EXECUTION_SINGLETON`,
  `LOCK_RISK_EVALUATION_QUEUE`, and `LOCK_AUDIT_CHAIN` from session-scoped
  (`pg_advisory_lock`/`pg_advisory_unlock`) to transaction-scoped
  (`pg_advisory_xact_lock`)** in `core/locking.py` — release is now
  automatic, tied to whatever connection the transaction commits or rolls
  back on, making this leak class structurally impossible. Added a
  `lock_timeout` alongside it as defense in depth (any future stuck
  acquisition fails loud within 10s instead of hanging silently).
  `LOCK_PROCESS_SINGLETON` was deliberately left unchanged (a different,
  already-correct dedicated-connection pattern). Regression tests in
  `tests/integration/test_locking.py` prove the exact leak mechanism
  deterministically (two raw connections, lock on one, unlock-attempt on
  the other — confirmed still held), confirm reentrancy and cross-session
  mutual exclusion are preserved, and exercise concurrent `start_strategy`
  calls end-to-end. Live-reproduced the original failure against the fix
  (four strategies started concurrently against one session, `PositionManager`s
  cycling every 1-3s for 2+ minutes, zero locks ever caught held in
  `pg_locks`, a fourth strategy start completed in 0.1s where it used to
  hang forever) — see `docs/architecture/build-plan.md`'s Phase 7 section
  for the full root-cause writeup.
- ~~Frontend has no "Connect Shoonya" button~~ — done this session: the
  Sessions page has a connection-status card + "Connect Shoonya" button
  (`frontend/src/features/sessions/SessionsPage.tsx`) that opens
  `/shoonya/login-url`'s authorize URL in a new tab and polls `/shoonya/
  status` on window focus. Found and fixed a real bug while verifying it
  live: `vite.config.ts`'s dev proxy only forwarded `/api`, not `/shoonya`
  (which lives outside `/api/v1` on purpose — see `api/v1/shoonya.py`'s own
  comment), so the button silently 404'd against Vite's own dev server
  instead of ever reaching the backend. Added a matching `/shoonya` proxy
  rule.
- ~~Addendum hardening batch (get_margin, metric_series + health-check
  loop, emergency square-off, DB backup/restore)~~ — **done this session.**
  Four Shoonya-independent gaps from the build plan's Addendum section, all
  implemented, tested (279/279 backend tests pass, `ruff`/`mypy` clean),
  and live-verified: `BrokerPort.get_margin` (+ `MockBrokerAdapter`/Shoonya
  implementations + real Risk Service wiring, replacing the old
  `capital_required > 0` stub), `metric_series` + `GET /metrics` + the
  periodic health-check loop (verified by hitting the live endpoint after
  a real scheduler cycle), the margin-breach emergency-square-off
  auto-trigger (`PositionManager._check_margin_breach`, `ExitReason
  .MARGIN_BREACH`), and `ops/scripts/backup_db.ps1`/`restore_db.ps1` (the
  restore drill was actually run against a throwaway DB, row counts
  verified against the source, not just scripted). Two scope corrections
  found during planning, both recorded in the build plan's Addendum: the
  manual "exit all" square-off button and EOD auto-square-off already
  existed from Phase 3 (only the margin-breach trigger was new), and
  `ShoonyaBrokerAdapter`'s own docstring had already anticipated the
  health-check loop as the natural next step for wiring in its error
  taxonomy. Full design reasoning in `docs/architecture/build-plan.md`'s
  Addendum section (each item now struck through there).
- ~~`GET /audit/verify` reports `intact: false` on this dev database~~ —
  **root-caused and fixed this session.** A live integration QC pass run
  after the Addendum batch above found two historical chain-*link* forks
  (seq 470, seq 489 — every row's own hash is independently valid, only
  two `prev_hash` pointers are stale), both from before the transaction-
  scoped `LOCK_AUDIT_CHAIN` fix and the same root cause as the documented
  `LOCK_EXECUTION_SINGLETON` incident above. A full scan confirmed zero new
  forks since, across real concurrent load. Since a hash chain can't be
  repaired without defeating its own purpose, `verify_chain`/
  `GET /audit/verify` gained an optional `since_seq` checkpoint parameter
  instead — `GET /audit/verify?since_seq=489` reports `intact: true` on
  this database today, verified live. Full write-up in
  `docs/architecture/build-plan.md`'s Addendum section.
- **Guardrail-layer proposal evaluated; four genuine gaps closed** — an
  externally-drafted "broker-agnostic guardrail layer" proposal (order state
  machine, stale-quote protection, pre-trade validation, risk rails, a
  recovery UI, as a new parallel `core/`/`adapters/`/`ui/` module tree) was
  found ~90% already built more maturely under different names; the parallel
  module tree was rejected outright as a competing system against the
  locked modular-monolith decision. Four real, verified gaps were built as
  small additions to the existing structure instead: a quote/option-chain
  freshness gate (`market_data/freshness.py`, closing the Phase 7-flagged
  single-snapshot-per-run staleness gap for real via an actual refresh, not
  just a block, plus generalizing the manual-approval price-drift check to
  AUTO-mode dispatch), tick-size/freeze-quantity pre-trade checks in
  `evaluate_trade_intent` (`Instrument.freeze_qty` nullable and
  operator-supplied — real NSE freeze quantities are never a fact to
  hardcode), opt-in `MockBrokerAdapter` fault injection
  (`queue_fill_scenario`/`simulate_disconnect`, default behavior
  byte-identical, confirmed via the full suite before/after) alongside a
  fix for a real latent `close_position` crash risk the fault injection
  would have exposed, and a recovery panel (`GET /system-alerts`,
  `GET /sessions/{id}/reconciliation-runs`, a new frontend page) surfacing
  data that was already written but never readable. 312/312 backend tests
  pass (up from 281), every batch live-verified against the real dev server.
  Full design reasoning and the specific safety/blast-radius checks run
  before each change in `docs/architecture/build-plan.md`'s own section.
- ~~`StrategyRunner` never survived a backend restart~~ — **fixed
  2026-08-05.** Found live: three real restarts in one session (deploying
  the WS diagnostic patch above) each silently zombied every running
  strategy — `strategy_runs.status` stayed `scanning` forever (an
  in-process `threading.Thread`, `api.v1.strategies._RUNNERS`, with
  nothing durable behind it), while nothing was actually happening: no
  market-data ingestion, no `evaluate()` cycles, no signals.
  `GET /strategies/running` kept reporting it as live regardless, since it
  reads DB rows, not runner liveness. Root cause: `instrument_id`/
  `expiry_date` were request-only params to `POST /strategies/{id}/start`,
  never persisted anywhere — the only place that combination lived was the
  in-memory `Strategy` object inside the runner thread itself, making a
  resume impossible even in principle. Migration `0011` adds both (+
  `interval_seconds`) as nullable columns (no backfill possible for
  existing rows); `start_strategy` now persists them; a new
  `app.main._resume_strategy_runners`, called from `lifespan` alongside
  the existing `PositionManager` recovery check, rebuilds each `ACTIVE`
  session's non-stopped runs on startup. Rows predating the migration are
  skipped, not crashed on. Broker-agnostic — this is the orchestration
  layer, unrelated to which `BrokerPort` adapter is wired in, so it
  applies regardless of whether Shoonya or a future broker is in use.
  370/370 backend tests pass (up from 366); migration tested both
  directions locally before being written up here. **Not yet applied to
  the live OCI database** — needs `alembic upgrade head` run there
  deliberately as its own step, not bundled into a routine restart, given
  it's a real schema change against live data.
- ~~Stale `2026-08-06` mock-seeded option contracts polluting the NIFTY/
  BANKNIFTY expiry dropdown~~ — **fixed 2026-08-05**: `UPDATE
  option_contracts SET is_active = false WHERE expiry_date =
  '2026-08-06'` run directly against the live OCI Postgres (84 rows,
  verified read-only both before and after).
- ~~Dropdown `<select>`/`<option>` text invisible in dark mode~~ — **fixed
  and deployed 2026-08-05**: `select`/`option` used `background:
  transparent`/`color: inherit`; Chromium renders the open popup against
  its own default (light) background once the control has any author
  color styling, but doesn't extend that to `option` unless set
  explicitly, leaving light-on-dark text invisible except on the
  hovered/selected row. Explicit `--bg`/`--fg` theme variables
  (`frontend/src/index.css`) fix it for every dropdown in the app (all
  plain `<select>`/`<option>`, no custom combobox components). Deployed to
  the live OCI nginx site (`/var/www/trading-bot/dist`, a separate path
  from the repo's own `frontend/dist` — worth remembering next frontend
  deploy) — verified live via computed styles in both color schemes.
- **GitHub repo**: [drvinothk/tradingbot](https://github.com/drvinothk/tradingbot),
  `main` branch. Phase 2 is committed locally (not yet pushed as of that commit);
  Phase 3's changes are uncommitted in the working tree as of this note — check
  `git log`/`git status` rather than trusting this line, and commit/push only
  when explicitly asked to.
