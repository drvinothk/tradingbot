# Trading Bot — Project Guide

Personal algorithmic options-trading platform for Nifty/Bank Nifty, broker-agnostic
internally with Shoonya (Finvasia) as the first real broker. Local-first now,
cloud-ready later. Non-AI deterministic execution core; AI stays optional/advisory,
wired in later via a credentials file.

**Full build plan (architecture, schema, phase-by-phase spec): [docs/architecture/build-plan.md](docs/architecture/build-plan.md) — read this first for any non-trivial change.**

## Status: Phase 0 + Phase 1 complete, Phase 2 next

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
- 👈 **Phase 2 is next** — shared strike-ranking engine, Risk Service (limits, margin
  check, pre-trade analytics), the daily-plan endpoint (budget/target/loss/funding-mode
  entry), a synthetic strategy stub to prove the Signal→TradeIntent→RiskDecision→audit
  path before real strategies exist. Full spec in the build plan under "Phase 2".

A QC pass was done after Phase 1 (see git log) that found and fixed several real bugs
— worth reading `git log -p` on that commit if touching auth, sessions, the mock
adapter, or `main.py`'s lifespan, since the fixes encode non-obvious reasoning.

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

**Tests**: `./.venv/Scripts/python -m pytest` — auto-creates and drops an isolated
`<DB_NAME>_test` database (see `tests/conftest.py`); never touches the dev DB. Run
`ruff check .` and `mypy app tests` before considering anything done — both are
enforced in CI and kept at zero errors throughout Phase 0/1.

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
  this explicitly, not assume it away.
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

## Known open items

- **Shoonya IP whitelist / static-IP situation** — user was checking with Airtel
  about CGNAT vs static IP; outcome not yet recorded here. Relevant when Phase 5
  configures `SHOONYA_PRIMARY_IP`/`SHOONYA_BACKUP_IP`.
- **Shoonya Authentication doc deep-dive** — the OAuth flow (browser redirect,
  `GenAcsTok` token exchange) is confirmed and documented in the build plan's Phase 5
  section, but the exact rate limits weren't accessible programmatically from their
  docs site; worth a manual look before Phase 5's rate-limiter tuning.
- **GitHub repo**: [drvinothk/tradingbot](https://github.com/drvinothk/tradingbot),
  `main` branch, pushed and up to date as of the Phase 0+1 commit.
