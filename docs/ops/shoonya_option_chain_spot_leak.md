# Shoonya option-chain spot-price leak — problem & solution roadmap

**Opened:** 2026-09-08. **Status:** Rails 1+2+4 merged (`d8b1ca0`) + deployed
2026-09-08. **Root cause confirmed 2026-09-10** (see "Root cause — RESOLVED"
below) and a follow-up mitigation (`fix/option-chain-degenerate-quote-safety`,
Changes A/B/C) built. Guard (`tick_plausibility.py`) stays regardless.

## Root cause — RESOLVED 2026-09-10 (was hypothesis 2)

A full session's OCI logs + the live DB token map settle it:

- **Rail 1 substitution log: completely silent.** Every `GetOptionChain` row
  `token` already equals the trusted scrip-master token.
- **DB token map is clean.** `NIFTY15SEP26P23500 → 47298`, `C23500 → 47297`,
  contiguous `47289–47306` across the ATM strikes, all distinct, **none** equal
  the NSE index tokens `26000`/`26009` or a futures token, zero collisions, zero
  empty `broker_token`s.
- **Every drop tagged `no_book`** (`bp1=sp1=v=0`), with `ltp` tracking the NIFTY
  spot as it drifts through the session.
- **Affected strikes are ATM** (`C23400`/`P23500`/`P23550`…), the most liquid
  contracts on the exchange — not deep-OTM illiquid rows.
- **222 chain fetches with ≥1 drop on 2026-09-10** — essentially every ~60s
  cycle, 1–10 of the 28 ATM-window strikes, rotating.
- Request path verified: fresh body/headers per call, `httpx.Client` thread-safe,
  `parse_option_chain_entry` reads `ltp` **only** from the `GetQuotes` response.

**So:** we send a correct `GetQuotes(uid, "NFO", <correct token>)`; Shoonya's
Noren backend intermittently returns a valid HTTP-200 body with an **empty book
and `lp` = underlying spot** for that correct NFO option token. Hypothesis 1
(wrong token) and hypothesis 3 (stale cached token) are **disproven**. Rail 1 and
Rail 5 (targeted re-sync) therefore cannot help — the tokens are already right.
This is a Shoonya feed-quality defect.

### Follow-up mitigation — `fix/option-chain-degenerate-quote-safety`

- **Change A** (`shoonya/adapter.py::get_option_chain`): when a row's `GetQuotes`
  comes back with an empty book on a token we hold, retry that single strike
  (`_CHAIN_QUOTE_RETRY_ATTEMPTS=2`, `_CHAIN_QUOTE_RETRY_SLEEP_S=0.15`), bounded to
  `_CHAIN_QUOTE_MAX_RETRIED_ROWS=10` per fetch. One aggregated WARNING per fetch
  (`… N/M rows came back with an empty book … retried R, recovered K`). Attacks
  the source so the persisted snapshot stays whole for ranking / Control Room /
  drift checks / `current_contract_price`'s fallback.
- **Change B** (`execution_engine/paper/service.py::current_contract_price`): if
  the last-resort `broker.get_quote()` is itself implausible
  (`is_plausible_option_tick`), return the last persisted snapshot tick (ungated
  by freshness — Rail 2 guarantees it was plausible when written); only if
  nothing plausible exists anywhere is the implausible quote returned. Covers
  both callers (`PositionManager`, `eod_square_off`).
- **Change C** (`execution_engine/paper/position_manager.py::_run_cycle`): if the
  price `current_contract_price` returned is *still* implausible, log a WARNING
  and `continue` — skip archiving it as LTP and skip `evaluate_open_position` for
  that position that cycle (broker-side resting stop unaffected; next ~3s cycle
  retries). Hard guarantee a spot-shaped premium can never fire a fabricated
  `TARGET`/`SPREAD`/`TRAIL` exit.
- **Deferred (2B):** Alice Blue per-contract WS as a Shoonya-independent price
  source for open positions — needs a market-hours test that AB's WS delivers
  per-*contract* option ticks (the "no per-contract WS" note is Shoonya-specific).

**Rail 1 was redesigned during QC** — the original "check the GetQuotes response's
echo fields" plan had a logic gap (it compared the response against the token we
*sent*, not the token the contract *should* have) and an unverified dependency
(no evidence which identity field Shoonya's `GetQuotes` echoes). Built instead as
a cross-check against the **trusted scrip-master token** already cached in
`ShoonyaBrokerAdapter._token_by_symbol` (populated by `get_instrument_master`'s
NFO static-file path / `warm_token_cache`): when a `GetOptionChain` row's `token`
disagrees with the trusted one, price with the trusted token and never overwrite
the trusted cache entry with the row's suspect one. No dependency on GetQuotes
response shape. When no trusted entry exists yet (right after a restart, before
sync) it falls through to today's behaviour and Rail 2 is the net.

## Problem

`record_option_chain_snapshot` routinely logs, on a ~25–60s cadence, several:

```
ERROR app.market_data: REJECTED implausible option-chain entry for 'NIFTY08SEP26C23650'
  … ltp=23671.15 bid=0.0000 ask=0.0000 volume=0 — looks like a leaked underlying/
  wrong-instrument value … Dropping this entry …
```

The rejected rows carry `ltp ≈ current NIFTY spot`, `bid=ask=volume=0`. Some are
~ATM strikes on the 0-DTE expiry — which on expiry day are the *most* liquid
contracts on the exchange and should never have an empty book. So this is not
"illiquid strike, no market" — it is a **wrong price for the right symbol**, or a
**right price for the wrong instrument**.

### Pricing path (all REST, not WS)

`record_option_chain_snapshot` → `BrokerPort.get_option_chain(underlying, expiry)`
(Shoonya adapter) →
1. `GetOptionChain` → structural rows only (`token`, `tsym`, `strprc`, `optt`) —
   live-confirmed to carry **no** quote fields.
2. per row: `GetQuotes(uid, exch, token)` → `{lp, bp1, sp1, v, oi}`.
3. `normalizer.parse_option_chain_entry(row, symbol, quote)` → `ltp = quote["lp"]`.

The leaked value therefore comes back **inside a `GetQuotes` response** whose `lp`
is spot-shaped and whose book is empty.

### Root-cause hypotheses (ranked) — SETTLED 2026-09-10

**See "Root cause — RESOLVED 2026-09-10" at the top of this file.** The live
evidence lands squarely on a variant of hypothesis 2, and disproves 1 and 3
(tokens verified correct in the DB and by Rail 1's silence). Kept here for the
record:

1. ~~**Wrong `token` from `GetOptionChain`**~~ — **disproven.** Rail 1 never
   fired; DB `broker_token`s are the correct contiguous NFO block.
2. **Noren `GetQuotes` degenerate response** — **confirmed**, but broader than
   "never-traded contract": it hits *ATM* strikes too, intermittently, ~every
   60s fetch, rotating. Returns `lp` = underlying spot with an empty book for a
   correct token.
3. ~~**Stale cached token**~~ — **disproven**, same evidence as (1).

### Impact today

- **Log noise** — every rejected row is an `ERROR` on every refresh; real ERRORs
  get buried. This is the main visible cost.
- **Strike-selection degradation** — *if* enough of the ATM±3 window is dropped,
  ranking has fewer real candidates. Needs confirming whether zero-liquidity
  entries already score ~0 and are structurally unselectable (they should, via
  `StrikeRankingConfig` spread/volume/depth weights). If so, practical trading
  risk is already contained and this is mostly correctness + hygiene.
- **No known bad fill** — the guard has been dropping these since 2026-09-03.

## Solutions

### Rail 1 — trusted-token cross-check  *(BUILT)*

`shoonya/adapter.py::get_option_chain` — per row, compare `row["token"]` against
`_token_by_symbol[symbol]` (trusted scrip-master token). On disagreement: use the
trusted token for `GetQuotes`, don't cache the row's token, collect the
substitution. One aggregated `WARNING` per fetch listing
`(symbol, row_token, trusted_token)`. Catches hypotheses 1 & 3 at the source
against a trusted reference; no GetQuotes-shape dependency.

### Rail 2 — no-arbitrage premium bounds  *(BUILT)*

Replace the flat `MAX_PLAUSIBLE_OPTION_PREMIUM = 5000` magic number with bounds
derived from strike + spot (both already fetched inside `get_option_chain`):
- call: `0 ≤ premium ≤ max(0, spot−strike) + EXTRINSIC_CAP·spot`
- put:  `0 ≤ premium ≤ max(0, strike−spot) + EXTRINSIC_CAP·spot`
- `EXTRINSIC_CAP ≈ 0.05` (tunable; generous even for 0-DTE ATM).

A leaked `lp = spot` fails the upper bound for every strike in the ATM window.
Structural (tracks what an option can *mathematically* be worth) → no re-tuning per
underlying or volatility regime. Falls back to today's flat-ceiling + zero-book
check when spot is unavailable (0). Added as a **new** `is_plausible_option_entry`
alongside the existing `is_plausible_option_tick` (unchanged — the WS/`current_
contract_price`/preservation-quote call sites don't have reliable spot in hand).

### Rail 4 — log hygiene + real signal  *(BUILT)*

- One aggregated `WARNING` per chain fetch: `"option chain NIFTY 2026-09-08:
  dropped 6/15 entries as implausible (no_book=4, no_arb=2); 4 consecutive
  fetch(es) affected. Samples: [...]"`. Replaces the per-row `ERROR`.
- Per-drop reason tag (`no_book` vs `no_arb`) — after a day this says which
  hypothesis (wrong token vs Noren returning spot for a correct token) dominates.
- Consecutive-fetch streak per `(underlying, expiry)` (module global, locked).
- Telegram-eligible CRITICAL `SystemAlert` (`option_chain_degraded`, added to
  `TELEGRAM_ALLOWED_CATEGORIES` + `TELEGRAM_SUGGESTED_ACTIONS` + Control Room's
  `ATTENTION_ALERT_CATEGORIES`) **only** when a dropped `contract_symbol` matches
  a contract this system holds OPEN — attributed to that position's own
  workspace/session/mode. Self-resolves on the next clean fetch.
- Sustained degradation with **no** open position affected → `ERROR` log only
  (streak ≥ 4). No workspace to attribute a bare infra alert to here, and no live
  risk to interrupt anyone over.
- **Deferred:** `metric_series` rows (`option_chain_implausible_ratio`). Would
  need `workspace_id` threaded through `record_option_chain_snapshot`'s 3 callers;
  the aggregated log + reason tags carry the monitoring signal for now. Add later
  if a graph is wanted.

QC refinements (post-implementation review):
- **A** — the clean-fetch auto-resolve UPDATE is gated on an in-memory
  `_option_chain_alerted_keys` set, so a near-always-clean fetch never
  sequential-scans `system_alerts` (no index covers `category`+`dedup_key`
  without `workspace_id`). Restart clears the set → a pre-restart standing alert
  falls back to `alert_housekeeping` / the next drop→clean cycle.
- **B** — when a dropped contract is held by both a paper and a live position,
  the alert is attributed to the LIVE one (`send_alert` paper-suppresses
  `mode=PAPER`).
- **C** — Rail 1 also substitutes the trusted token when the GetOptionChain row's
  token is *missing* (the exact 2026-08-12 empty-`broker_token` bug), not only
  when it disagrees.

### Rail 5 — self-healing re-sync  *(RULED OUT 2026-09-10)*

Would trigger a targeted `scrip_master` / `sync_instrument_master` refresh when
>X% of the ATM window is implausible for N consecutive fetches. **Not useful
here** — the tokens are already correct (Rail 1 silent, DB map clean), so a
re-sync changes nothing. Kept documented only in case a genuine token-corruption
recurrence (the 2026-08-12 class) ever shows up again with Rail 1 firing.

### Rail 6 — source diversity  *(the real structural fix; deferred)*

A broker returning spot on ATM strikes is a broker-feed limitation with no
in-Shoonya fix. TrueData `getoptionchain` was the designated fallback but
**TrueData is not currently subscribed**; **Alice Blue** is the only backup
provider today. Alice Blue has no `get_option_chain`, but it *can* subscribe to
individual NFO option tokens over its Noren-family WS (`alice_blue_scrip_master`
already maps them). Path: prove AB's WS delivers per-*contract* option ticks
during market hours (untested — the "no per-contract WS" note is Shoonya-only),
then have `current_contract_price`'s first branch / `PositionManager` prefer the
AB feed for open-position pricing. This removes the dependency on Shoonya's
flaky REST for anything safety-critical and is the reason AB exists as a backup.
Order-update pushes over Shoonya WS are unaffected (an accelerator only — REST
`OrderBook` poll + ack-timeout idempotency check are the real nets).

## Monitoring plan

After `fix/option-chain-degenerate-quote-safety` (Changes A/B/C) deploys, watch:
- `GetOptionChain … rows came back with an empty book … retried R, recovered K`
  — Change A firing. `recovered ≈ retried` → an immediate retry clears it, and
  the `record_option_chain_snapshot` `dropped N/M` count should fall toward ~0.
  `recovered ≪ retried` → the retry needs a longer delay or Shoonya is genuinely
  down for that strike for seconds → tune `_CHAIN_QUOTE_RETRY_*` or escalate to
  Rail 6 (Alice Blue).
- `last-resort broker quote for … was implausible … using the last persisted
  option-chain snapshot instead` — Change B catching what A missed.
- `implausible price for … skipping stop/target/trail this cycle` — Change C, the
  hard stop. Should be rare; if frequent, the snapshot is also going stale →
  Rail 6.
- Any `option_chain_degraded` CRITICAL alert (a dropped entry on an open
  position) — unchanged canary.

If drops stay materially non-zero after a few days, proceed to Rail 6.
