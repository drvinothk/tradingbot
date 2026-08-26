# Lessons Learned — Trading Bot

A standing reference for issues actually hit while building, integrating, and
live-testing this app, why they happened, and what to watch for so they don't
recur — for this project or a similar one. Not a spec (see
`docs/architecture/build-plan.md` for that) and not a status log (see
`CLAUDE.md`) — this is the "what bit us and why" file. Update it whenever a
real, non-obvious issue is found and fixed, the same discipline this project
already applies to "QC pass findings" in the build plan.

---

## 1. Broker integration (Shoonya, but the pattern is general to any broker API)

**A broker's own published docs and reference SDKs are a starting point, not
ground truth — budget real time for a live-account verification pass before
trusting any of it.** Nearly every Shoonya-specific bug found this project hit
was a case where the documented/reference behavior didn't match what the real
API actually did. None of these were discoverable without a real account:

- **Every POST body needed a raw, unencoded `jData=<json>&jKey=<token>` string**,
  not a normal JSON body or a form-encoded dict — the server does a naive
  string-prefix split, not real form decoding. `httpx`'s `data={...}` percent-
  encodes the JSON and breaks it silently (no obvious error, just a rejected
  request).
- **`GetOptionChain`'s `tsym` anchor must be a real, currently-listed contract
  symbol** — never any form of the underlying's own name ("NIFTY", "Nifty 50",
  URL-encoded variants all rejected as "Invalid Trading Symbol").
- **Anchoring `GetOptionChain` on a *futures* contract silently returns the
  *monthly* chain**, regardless of what expiry was actually requested — NFO
  futures are monthly-only, and there's no separate expiry parameter to the
  call. The chain always follows the anchor's own expiry. Fix: anchor on a
  real *option* contract matching the exact requested expiry, found via
  `SearchScrip`, and raise explicitly rather than silently substituting the
  wrong expiry when no match exists.
- **`GetOptionChain` rows are purely structural** (`token`/`tsym`/`strprc`/
  `optt`/...) — they never carry live quote fields (`lp`/`bp1`/`sp1`/`v`/`oi`),
  despite `GetQuotes`/WS touchline pushes using those exact field names. Real
  pricing needs a separate `GetQuotes` (or WS) call per contract.
- **`SearchScrip` omits `strprc` (strike price) on real weekly option rows** —
  not an isolated bad row, every single weekly-tagged row for the day tested.
  Monthly rows carried it fine. The strike is still recoverable from the
  trading symbol's own fixed suffix (`NIFTY04AUG26C18500` → strike `18500`),
  so treat a missing expected field as "parse a fallback from elsewhere if
  possible," not just "this row is broken."
- **A broker's own `SearchScrip`/instrument search can return more than you
  asked for** — searching "NIFTY"/"BANKNIFTY" on NFO also matched futures
  contracts and unrelated substring hits (an ETF called `NIFTYNXT50...`).
  Anything that turns search results into permanent DB rows needs its own
  filter for "is this actually the kind of instrument I meant," not an
  assumption that a text search is precise.
- **`TPSeries` (historical candles) does return real OHLC for NSE *index*
  tokens** (contrary to an unresolved community report claiming index
  historical queries return nothing) — but volume is genuinely always zero on
  that token type, while a derivative contract's own token on the same call
  returns real volume. Don't assume a documented/reported limitation is
  universal without checking your own account; also don't assume a field
  being `0` is a bug when it's real broker-side data.
- **A network-level failure on a state-changing call (place order) is
  genuinely ambiguous** — a timeout doesn't tell you whether the broker
  received and processed the request. Blindly retrying risks a real duplicate
  order. If the broker echoes back anything you submitted (e.g. remarks/
  client-order-id), use that to look up the real outcome via order history
  before ever assuming failure.
- **WebSocket auth can fail for reasons that have nothing to do with your
  code.** Every plausible client-side cause was checked and ruled out live
  (correct token type, correct field values, correct IP, both documented
  hosts, three URL forms, retested specifically during market hours to rule
  out a timing theory) and the auth frame was still rejected identically every
  time. At that point, stop guessing at the wire format and escalate to the
  broker's own support with the specific reproducible case — don't keep trying
  variations that differ only in what an AI or a forum post speculated might
  matter.
- **A broker session/token normally lives only in process memory, never
  persisted** — a service restart means every user has to redo the broker
  login manually. Plan deploys and debugging cycles around this; it makes
  "just restart and see" much more expensive than it looks, and it means a
  fully automated recovery path can't exist without deciding to persist
  credentials somewhere (a real security tradeoff, not a small one).
- **Non-localhost OAuth redirect URIs generally must be HTTPS** — a bare-IP
  cloud VM with no domain needs a trick like `sslip.io` (resolves
  `<ip-with-dashes>.sslip.io` back to the IP) to get a real TLS cert via
  Let's Encrypt for a redirect URI the broker's portal will accept.
- **Confirm real rate limits from the broker's own docs/FAQ, don't guess a
  "safe-sounding" number** — and keep the client's limiter comfortably under
  the tightest documented limit across every call type sharing it, not tuned
  to the theoretical max.

## 2. Testing & local dev environment

- **A full test run can look "clean" while silently only running a fraction
  of the suite.** If the local Postgres/Docker isn't up, integration tests
  fail with connection timeouts that are easy to misread as flakiness rather
  than "the DB isn't running" — always confirm `docker ps` shows the expected
  containers healthy before trusting a "N passed" count, especially before
  deciding something is safe to ship.
- **Order-dependent test failures are real and worth chasing to true root
  cause, not dismissing as flaky.** A test can pass 100% in isolation and
  fail only as part of the full suite because a *different, seemingly
  unrelated* test's mocking gap lets a real side effect leak a committed row
  into the shared test database. Bisect by running file subsets rather than
  guessing — it's usually fast to narrow down and the fix is almost always
  "mock the new dependency that test didn't know it needed," not "make the
  failing test's assertion looser."
- **When a code path gains a new external side effect (e.g., a new call to a
  sync/write function), every existing test that exercises that code path
  needs to be checked for whether it now needs a new mock** — not just the
  tests written for the new behavior itself.
- **Rollback-isolated test fixtures and real-commit test fixtures don't mix
  safely in the same suite without care.** A test using a real commit (needed
  to simulate a background thread's own DB writes) is invisible to nothing —
  its committed rows are visible to every other test's queries for the rest
  of that test run, and must be cleaned up explicitly in teardown, in FK-safe
  order.
- **`hash()` on strings is salted per-process** (`PYTHONHASHSEED`) — never use
  it for anything that needs to be deterministic across separate runs (e.g.
  seeding synthetic/deterministic test data). Use `zlib.crc32` or `hashlib`
  instead.
- **`Decimal` read back from Postgres doesn't compare reliably against a raw
  Python `float`** (`Decimal('0.05') != 0.05` can be `True`) — route through
  `Decimal(str(x))` first.

## 3. Concurrency & data-integrity

- **Session-scoped advisory locks (`pg_advisory_lock`/`unlock`) are a real
  leak risk with a connection pool.** `Session.commit()` can return the
  connection to the pool; a later `pg_advisory_unlock()` call can land on a
  *different* pooled connection than the one that acquired the lock, silently
  no-op, and leave the original connection in the pool still holding the lock
  forever — invisible to any single-request diagnostic, only surfacing under
  real concurrent load. Prefer transaction-scoped locks
  (`pg_advisory_xact_lock`) wherever the lock's lifetime should match a single
  transaction — release becomes automatic and the leak class becomes
  structurally impossible.
- **"Stopped" must mean no more callbacks/writes can fire, not just "asked to
  stop."** A background thread that only checks a stop flag *between*
  iterations can race a caller that tears down its DB/session state
  immediately after calling `stop()`. Any stop/unsubscribe method for a
  background stream needs to actually join the thread (with a bounded
  timeout) before returning, not just signal it.
- **A duplicate live order is the failure mode to design around above nearly
  everything else** in a system that will eventually place real trades.
  Idempotency keys, single-writer locking, and ambiguous-failure fallbacks
  (see broker section above) all exist because of this one fear — treat any
  new write path that could plausibly run twice as a real design question,
  not an edge case to skip.

## 4. Deployment & launch

- **A cloud deployment's first real value is proving the parts that can't be
  tested any other way** — real auth, real wire formats, real rate limits,
  real IP whitelisting. Treat "deployed and reachable" as step one, not
  done; budget real live-debugging sessions, ideally during the market hours
  the app actually needs to work in, as their own project phase.
- **A small VM's memory budget matters once real concurrent load (multiple
  strategies, live ingestion, reconnect loops) starts running** — check
  `free -h` under real load, not just at idle, before assuming headroom is
  fine.
- **An unused declared dependency (e.g. Redis wired into settings but never
  actually read anywhere in the app) is safe to skip entirely on a
  resource-constrained box** — confirm via a real grep across the codebase,
  don't assume a config value implies a real runtime dependency.
- **Don't assume "market closed" explains an unexpected failure without
  retesting during real market hours** — a timing theory feels plausible but
  can send debugging in the wrong direction for a long time if it's wrong.
  Retest the specific theory directly before building anything around it.

## 5. Process — how to actually work on this codebase (or hand it to an AI agent)

- **Trace the full call chain and check for side effects *before* deploying a
  live fix, not fix-by-fix as each new symptom appears.** A cascade of
  individually-correct live fixes (found the same day: an anchor bug, a
  parsing gap it exposed, a sync bug that gap was hiding, a picker-pollution
  bug the sync fix introduced) is a sign the investigation should have gone
  one or two levels deeper before the first deploy, not that the fixes were
  wrong. Each broker-login cycle in this app is a real, non-free cost (a
  manual browser OAuth round trip) — minimizing the number of live-deploy
  round trips is itself a real optimization worth planning for up front.
- **Don't guess at an external system's undocumented behavior** (a broker's
  wire format, an error message string, a field name) **when there's no live
  evidence for it yet.** A wrong guess here doesn't just fail — it can cost a
  full debug-deploy-relogin cycle to even discover it was wrong. Prefer
  leaving something explicitly flagged as unconfirmed over shipping a
  plausible-sounding guess.
- **Do a self-review pass after every non-trivial change, not just when
  asked** — check for internal contradictions, stale references, and claims
  that don't match what the actual code does. This is a real-money system
  eventually; the QC pass itself has repeatedly found genuine bugs the
  original change missed.
- **Verify a "should be true" belief by actually reading the code, not by
  trusting a docstring or an old status note.** Several bugs this session
  existed specifically because a docstring's claimed behavior ("that adapter
  syncs its own instrument master") had never actually been implemented —
  the comment was aspirational, not descriptive, and nothing caught the gap
  until it was traced end to end.
- **When something doesn't work, verify the actual current environment state
  before assuming the code is at fault** — e.g. confirm a required service
  (Docker, a background process) is actually running, confirm which
  git branch/commit is actually checked out, confirm the deployed code
  matches what was just written, before spending time debugging logic that
  may be perfectly fine.

## 6. Open risks going into live trading (not yet resolved as of this writing)

- WebSocket auth against the real Shoonya account has never once succeeded —
  a REST-polling fallback covers `price_bars`/EMA, but WS itself needs
  Shoonya's own support to resolve (see `ws_client.py`'s module docstring for
  the full reproducible case).
- Broker error taxonomy is incomplete for scenarios with no live evidence yet
  (IP mismatch, TOTP drift specifically) — don't guess the `emsg` text, wait
  for a real occurrence.
- The order-ack-timeout fallback (see section 1) is unit-tested but not yet
  live-verified against a real ambiguous network failure — genuinely hard to
  trigger on demand against a real broker.
- No real multi-session paper soak has completed yet, so no real signal →
  risk → order → position lifecycle has been proven end-to-end against live
  data — see `CLAUDE.md`'s Phase 5 status for the current bar.

## 7. Backtesting (`backend/scripts/run_backtest.py`)

See `docs/ops/backtest_vm_runbook.md` for the operational how-to (VM access,
deploying code/data, running a sharded overnight sweep). This section is the
"what bit us and why" for the harness itself — read before trusting a
backtest report's numbers at face value, and especially before comparing one
against real paper/live trading results.

- **The harness reuses the real production pipeline** (`strategy_engine
  .runner.run_cycle`, the real `Strategy` subclasses, `risk_engine.service
  .evaluate_trade_intent`) — it is not a reimplementation of any strategy's
  entry logic. That's exactly why it's trustworthy for strategy/execution
  logic, and exactly why it's blind to anything that lives *outside* that
  pipeline (see the risk-config point below).
- **A same-day replay against production's real paper-trading record
  (2026-08-26) found a real 26x PnL-scale bug**: `legacy`/`target_mult` exit
  modes always seed the stub strategy config with `params={"qty_lots": 1}`
  and use `UNDERLYING_META`'s illustrative `lot_size=25` for NIFTY (real
  NSE lot size is 65, and production's own paper default is 10 lots) —
  so every `legacy`-mode PnL figure this harness has ever produced needs
  `real_equivalent_pnl = raw_pnl × (real_qty_lots × real_lot_size) / (1 ×
  25)` to read as real ₹. Trade selection, timing, and exit reason are
  completely unaffected (stop/target/trail math is price-based, not
  lot-size-based) — only the PnL *magnitude* was ever wrong. Not yet fixed
  in the script itself; rescale by hand until it is.
- **Close-only, once-per-completed-bar pricing structurally under-fires any
  strategy whose entry pattern is an intrabar phenomenon.** Confirmed via
  the same same-day comparison: production (which evaluates every 30s off
  continuously-updating live ticks) fired ema_micro_pullback 10 times in one
  day; the identical strategy/underlying/expiry replayed through the
  backtest fired only once. A micro-pullback can trigger and resolve within
  a single 1-min candle, which close-only bar data can never see. This means
  every backtest run's numbers for an intrabar-sensitive strategy are a real
  *undercount* of actual opportunity, not just a generically-flagged
  limitation — weight ema_micro_pullback-style strategies' backtest results
  accordingly, and don't conclude "this strategy rarely fires" from a
  backtest report alone.
- **Strike selection typically lands within 1 strike-step of what production
  actually picked at the same nominal signal moment, not exactly on it.**
  Two compounding, already-documented causes: close-only bar pricing feeds a
  slightly different underlying spot into the ATM calculation than the
  continuous live tick production saw at that instant, and the backtest's
  bid/ask/depth are *synthetic* (derived from OI/volume as a liquidity
  proxy, since the historical REST source has no real order book) rather
  than the real broker depth production's strike-ranking actually scores
  against. Expect this drift; don't treat a 1-2 strike mismatch against a
  real trade as evidence the replay is broken.
- **The backtest's own seeded `RiskLimitConfig` is fully decoupled from
  whatever value production's real config row actually holds** — it
  hardcodes generous limits (e.g. `per_trade_lot_cap=10`) so real risk
  checks (tick-size, same-strike locking, margin) still execute without
  being gated by arbitrary limits. This means the harness can validate that
  the *risk-check logic* runs correctly, but can never reproduce a bug that
  only exists because production's actual config value was wrong (a real
  example: a `per_trade_lot_cap_exceeded` check that was misapplied to paper
  trades in production for several hours one morning — the backtest's own
  config never had a value low enough to trigger the same rejection,
  regardless of which commit it ran). Don't expect a backtest to catch a
  production config/environment bug; it only validates strategy and
  execution logic against whatever config it's told to seed.
- **Smoke-test every strategy (single expiry, seconds each) before
  committing to a full/overnight sharded run.** This is what caught a real
  filename mismatch (`fetch_truedata_futures_underlying_history.py` writes
  `underlyings/<u>_FUT_1min.csv`; `--underlying-source futures_proxy` reads
  `underlyings/<u>_underlying_proxy_1min.csv`) before it could waste hours
  of overnight compute — 4/5 strategies passed immediately, VWAP Pullback
  failed with a clear "missing file" error instead of silently producing
  zero trades. Still not fixed in code as of this writing (worked around by
  copying the file under the expected name); check both scripts agree on
  the filename before assuming this is resolved.
- **TrueData's underlying-index bars (NIFTY/BANKNIFTY) always carry
  `volume=0`** (a real index has no traded volume of its own) — VWAP
  Pullback will structurally never fire against this data, confirmed to
  also be true in live production (same `volume=0` on index ticks), not a
  backtest-only artifact. A zero-signal day for this strategy in either
  system is expected, not a bug to chase.
