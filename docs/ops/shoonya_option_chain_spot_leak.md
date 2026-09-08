# Shoonya option-chain spot-price leak — problem & solution roadmap

**Opened:** 2026-09-08. **Status:** Rails 1+2+4 **built** on branch
`fix/option-chain-plausibility-rails` (not yet merged/deployed). Monitoring window
opens once deployed. Guard (`tick_plausibility.py`) stays regardless.

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

### Root-cause hypotheses (ranked; needs one live data point to confirm)

1. **Wrong `token` from `GetOptionChain`** (most likely). Recurrence of the
   2026-08-12 weekly-token class of bug — `GetOptionChain` hands back a `token`
   that isn't that option's (stale/recycled NFO token, or index token). `GetQuotes`
   then faithfully prices *that* instrument. Plausibly worsened this cycle by the
   `api.shoonya.com` HTTP 502s during the restart's token warm-up leaving the
   scrip-master / SearchScrip map partially stale.
2. **Noren `GetQuotes` degenerate default** for a never-traded contract — returns
   `lp` = underlying last price instead of `0` / theoretical. Known Noren quirk;
   fits the far-OTM rows but not the ~ATM ones.
3. **Stale cached token** — `_remember_token` cached a token that has since been
   recycled to another instrument.

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

### Rail 5 — self-healing re-sync  *(LATER, if it persists)*

When >X% of a chain's ATM window is implausible for N consecutive fetches, trigger
a targeted `scrip_master` / `sync_instrument_master` refresh for that
underlying+expiry instead of waiting for the daily contract-sync scheduler.

### Rail 6 — source diversity  *(LATER / bigger lift)*

A broker echoing spot on unquoted strikes is a broker-feed limitation.
CLAUDE.md already designates **TrueData `getoptionchain`** as the fallback for
exactly this; Alice Blue is a market-data provider now. Requires a
provider-agnostic seam for `get_option_chain` (today it is Shoonya-only, on
`BrokerPort`, independent of `MARKET_DATA_PROVIDER`). WS / order updates are
unaffected — order-update pushes over Shoonya WS are an accelerator only, not
load-bearing (REST `OrderBook` poll + ack-timeout idempotency check are the real
nets).

## Monitoring plan

After Rails 1+2+4 deploy, watch the logs for a couple of days:
- `GetOptionChain … rows carried a token that disagreed with the trusted
  scrip-master token` — Rail 1 firing. Frequent → hypothesis 1/3 confirmed;
  expect the `record_option_chain_snapshot` drop count to fall toward ~0.
- `option chain … dropped N/M entries as implausible (no_book=…, no_arb=…)` —
  the `no_arb` count persisting *after* Rail 1 means hypothesis 2 (Noren returns
  spot for a *correct* token) — that's the Rail 5/6 case.
- Any `option_chain_degraded` CRITICAL alert (bad price on an open position).

If drops stay materially non-zero after a few days, proceed to Rail 5, then Rail 6.
