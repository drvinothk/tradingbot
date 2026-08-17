"""`TrueDataProvider` — a `BaseMarketDataProvider` implementation for
TrueData's real-time feed, via the official `truedata` PyPI package
(`TD_live` for streaming, `TD_hist` for REST historical bars — two
separate classes, not the single `TD` class an earlier, now-superseded
package name (`truedata-ws`) used).

**No credentials exist yet as of 2026-08-11 — nothing here has been
exercised against a live account.** Everything below is sourced from
directly reading `truedata` 7.0.1's own installed source
(`truedata/websocket/TD_live.py`, `truedata/websocket/TD_ws.py`,
`truedata/websocket/utils.py`, `truedata/history/TD_hist.py`,
`truedata/history/Historical_REST.py`, `truedata/history/utils.py` —
installed into a throwaway venv and read directly, not paraphrased from a
webpage) plus TrueData's own official WebSocket API PDF spec
(user-supplied `TD_API_Documents/TrueData Market Data API Documentation
v 2.6.pdf`). "Read the real source" is still not "live-verified against a
real account" — the same distinction this codebase already draws for
every other broker integration (see `angel_rest_client.get_candle_data`'s
own docstring for the precedent).

**Confirmed from `truedata` 7.0.1's actual source, replacing an earlier
version of this module built against `truedata-ws` 5.0.11 (PyPI's last
release under that name, Nov 2023 — likely abandoned in favor of the
current `truedata` package, same GitHub repo, before this session obtained
and read the current package directly):**
- `TD_live(login_id, password, url='push.truedata.in', live_port=8084,
  ...)` is real-time only; `TD_hist(login_id, password, hist_url=...)` is
  a *separate* class for REST historical bars — not one combined `TD`
  class. `TD_live`'s own default `live_port=8084` independently confirms
  the port fix `TrueDataSettings.live_port` already got from the official
  PDF spec (Production 8084 / Sandbox 8086) — two independent primary
  sources now agree.
- `start_live_data(symbols)` takes no `req_id` at all in this version —
  the whole request-id tracking layer an earlier version of this module
  built (`_req_id_by_symbol`/`_symbol_by_req_id`/`_next_req_id`) doesn't
  apply and has been removed. `td_live.live_data` is keyed directly by the
  (uppercased) symbol string, confirmed from `TD_ws.py`'s own message
  handler (`self.parent_app.live_data[symbol] = tick_feed(...)`).
- `trade_callback`/`bidask_callback` fire with the **raw positional list**
  straight off the wire (see `TD_ws.py`'s own `self.trade_callback
  (trade_tick)`), not a parsed dataclass — resolving which symbol a raw
  list belongs to needs the library's own internal `symbol_id_map`, which
  this module deliberately does not reach into (an internal attribute, not
  part of the documented public surface). Instead, both callbacks are used
  purely as a "something changed, go look" signal: on any fire, this
  module re-reads `td_live.live_data` for every symbol *this instance*
  subscribed, and pushes out a fresh `Tick` for whichever ones have a
  newer `.timestamp` than last seen. Confirmed field names on the
  resulting `tick_feed` objects, read directly from `truedata/websocket/
  utils.py`'s own dataclass definitions (not inferred): `.symbol`, `.ltp`,
  `.ttq` (volume), `.oi`, `.timestamp`, `.best_bid_price`,
  `.best_bid_qty`, `.best_ask_price`, `.best_ask_qty`.
- `TD_hist.get_historic_data(contract, start_time=, end_time=,
  bar_size="1 min", ...)` returns a **pandas DataFrame** (confirmed from
  `Historical_REST.parse_data`'s own `pd.read_csv(...)`), with real column
  names `timestamp,open,high,low,close,volume,oi` — matching the official
  PDF's own documented CSV sample exactly. An earlier version of this
  module guessed short keys (`'o'`/`'h'`/`'l'`/`'c'`/`'v'`) from a
  paraphrased README excerpt; that guess was wrong. `start_time`/
  `end_time` are passed as naive-IST `datetime`s, formatted internally via
  `.strftime('%y%m%dT%H:%M:%S')` — confirms this module's prior IST-
  conversion reasoning (`.astimezone(IST)` before stripping tzinfo) was
  the right call, now against a confirmed implementation rather than a
  prediction from the Angel One timezone-bug precedent.
- **`bar_size` spacing/pluralization is a non-issue, confirmed by reading
  `truedata/history/utils.py`'s own `historical_decorator`**: it strips
  spaces and a trailing `s` before ever building the REST request
  (`bar_size.replace(' ', '')`, then drops a trailing `'s'`) — so `"1
  min"`, `"1 mins"`, and `"1min"` all normalize to the same `interval=
  1min` the official PDF documents. This module uses the plain no-space
  form directly.
- `TD_live` also has a genuine, WS-native live option-chain feature,
  `start_option_chain(symbol, expiry, chain_length=10, bid_ask=False,
  greek=False)` (`truedata/websocket/TD_chain.py`'s `OptionChain`), built
  on the exact same live tick stream as everything else here — continuous,
  not a REST poll. Not used by this module (`BaseMarketDataProvider` is
  underlying-ticks-only by design; option-chain pricing is
  `BrokerPort.get_option_chain`'s job) but noted for whoever picks up the
  2026-08-11 fallback-strategy note in `docs/architecture/build-plan.md`
  regarding Shoonya's own option-chain zero-price gap.

**2026-08-17: live-verified against a real trial account for the first
time** (see the memory note this session wrote,
`project_truedata_live_tick_verification_2026_08_17`, for the full
account) — confirmed via a standalone isolated script, not through this
module directly, but two facts from that session feed directly into this
module's own correctness:
- **Real host/port confirmed**: `push.truedata.in:8086` — the *production*
  host, on the *sandbox* port. `TrueDataSettings.live_port`'s own
  pre-existing default (8084, from the official PDF's "Production" entry)
  needs overriding to 8086 for this trial account via
  `config/credentials/truedata.env`, not a code change.
  `wstest.truedata.in` (the "Sandbox > Test" link in TrueData's own
  onboarding email) is a red herring — it's a Cloudflare-fronted web page,
  not a data host; `TD_live` against it hangs forever with zero error.
- **Real index-tick symbol strings confirmed, and they do NOT match this
  codebase's own internal underlying symbols.** `TRADABLE_UNDERLYINGS`
  (`market_hours.py`) is `("NIFTY", "BANKNIFTY")` — but TrueData's real
  symbols are `"NIFTY 50"` and `"NIFTY BANK"`. Before this fix, this
  module passed the internal symbol straight through to
  `start_live_data`/`stop_live_data` with no translation, which would have
  silently subscribed to nothing against a real account. `_TO_TRUEDATA_
  SYMBOL` below (translated out on subscribe/unsubscribe/history, and
  back on every tick received via `_to_truedata_symbol`'s use as the
  `live_data` lookup key while `Tick.contract_symbol` stays the internal
  name) fixes this — the same
  "bridge this codebase's own symbol convention to the broker's real one"
  shape `AngelOneMarketDataProvider`'s `ScripMasterService` already uses,
  just a static 2-entry map here since only these two underlyings are ever
  subscribed via this interface (option-chain/OI/VIX are a separate,
  deliberately out-of-scope pipeline as of this note — see that memory
  entry for what's deferred and why).

**NOT confirmed — still inferred/unverified, flagged exactly like every
other unverified assumption in this codebase:**
- Real connection/login behavior against an actual account — `TD_live.
  __init__` blocks in `connect_websocket()` until `subscription_type` is
  set; untested whether this blocks forever or raises/times out on bad
  credentials, since no real account exists yet to try it against.
- Whether `td_live.live_data[symbol].best_bid_price`/`.best_ask_price`
  actually update on every bid/ask change in practice, or only at the
  touchline snapshot points the official PDF documents (pre-open, post
  pre-open close, and at first subscribe) — the source suggests both
  trade ticks *and* a separate bidask-only path can update these fields,
  but only a live feed can confirm the real update cadence.
- Real behavior of `stop_live_data`/reconnect/duplicate-login handling —
  `TD_ws.py`'s `LiveClient.reconnect()` exists but wasn't read in full
  this session; not exercised here beyond what `disconnect()` needs.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.config.settings import TrueDataSettings
from app.core.clock import IST
from app.modules.broker_adapter.base.contracts import DepthLevel, DepthSnapshot, PriceCandle, Tick
from app.modules.market_data.providers.base import (
    BaseMarketDataProvider,
    DepthCallback,
    TickCallback,
)

if TYPE_CHECKING:
    from truedata.history.TD_hist import TD_hist
    from truedata.websocket.TD_live import TD_live

logger = logging.getLogger("app.market_data.truedata")

# Confirmed real values (see module docstring) — the official PDF's own
# documented `interval=` values, which `historical_decorator` normalizes
# any equivalent spelling down to anyway. This system only ever asks for
# 60s today; the rest are here for completeness/future use.
_HISTORICAL_BAR_SIZE_BY_TIMEFRAME: dict[int, str] = {
    60: "1min",
    120: "2min",
    180: "3min",
    300: "5min",
    600: "10min",
    900: "15min",
    1800: "30min",
    3600: "60min",
}

# Confirmed live 2026-08-17 (see module docstring) -- this codebase's own
# internal underlying symbols (`market_hours.TRADABLE_UNDERLYINGS`) don't
# match TrueData's real index-tick symbol strings. Every subscribe/
# unsubscribe/history call translates out through this map; every tick
# received back is translated back to the internal symbol before being
# handed to a caller, so `price_bars`/indicator persistence keeps using the
# one convention the rest of this codebase already relies on.
_TO_TRUEDATA_SYMBOL: dict[str, str] = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
}


def _to_truedata_symbol(symbol: str) -> str:
    return _TO_TRUEDATA_SYMBOL.get(symbol, symbol)


def _row_to_candle(row: Any) -> PriceCandle:
    """`row` is one row of the pandas DataFrame `TD_hist.get_historic_data`
    returns (confirmed real shape — see module docstring), via
    `df.itertuples()`. Column names (`timestamp`/`open`/`high`/`low`/
    `close`/`volume`) are the real, confirmed ones from `Historical_REST.
    parse_data`'s own `pd.read_csv` header parsing, matching the official
    PDF's own documented CSV sample exactly.
    """
    ts = row.timestamp
    if not isinstance(ts, datetime):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return PriceCandle(
        bucket_start=ts.astimezone(UTC),
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=int(row.volume or 0),
    )


class TrueDataProvider(BaseMarketDataProvider):
    def __init__(
        self,
        settings: TrueDataSettings,
        *,
        td_live_client: TD_live | None = None,
        td_hist_client: TD_hist | None = None,
    ) -> None:
        """`td_live_client`/`td_hist_client`, when given, are used as-is
        instead of being lazily constructed — the same seam-injection
        style `AngelOneMarketDataProvider`'s own `rest_client=` override
        already uses in this codebase, and the only way to unit-test this
        class at all without `truedata` installed (see module docstring on
        why that's never a given in the shared .venv).
        """
        self._settings = settings
        self._td_live: TD_live | None = td_live_client
        self._td_hist: TD_hist | None = td_hist_client

        self._lock = threading.Lock()
        self._subscribed_symbols: set[str] = set()
        self._latest_ticks: dict[str, Tick] = {}
        self._last_pushed_ts: dict[str, datetime] = {}

        self._on_tick_external: TickCallback | None = None
        self._on_depth_external: DepthCallback | None = None

    # -- BaseMarketDataProvider: connection lifecycle -------------------------

    def connect(self) -> None:
        if self._td_live is not None:
            return  # already connected — idempotent per the interface's own contract

        # Lazy import: `truedata` is deliberately not a core dependency
        # (see pyproject.toml's own `truedata` extras group comment) — a
        # process that never selects MARKET_DATA_PROVIDER=truedata
        # shouldn't need it installed at all, same reasoning
        # `api.v1.shoonya`'s own docstring gives for never importing
        # anything Shoonya-specific at module scope.
        from truedata.websocket.TD_live import TD_live

        logger.info(
            "Connecting to TrueData live feed (%s:%d)...",
            self._settings.url,
            self._settings.live_port,
        )
        self._td_live = TD_live(
            self._settings.username,
            self._settings.password.get_secret_value(),
            url=self._settings.url,
            live_port=self._settings.live_port,
        )
        self._td_live.trade_callback(self._handle_any_update)
        self._td_live.bidask_callback(self._handle_any_update)
        logger.info("TrueData live connection established.")

    def disconnect(self) -> None:
        if self._td_live is not None:
            self._td_live.disconnect()
            self._td_live = None
        with self._lock:
            self._subscribed_symbols.clear()
            self._latest_ticks.clear()
            self._last_pushed_ts.clear()

    # -- BaseMarketDataProvider: ticks -----------------------------------------

    def subscribe_ticks(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        if self._td_live is None:
            self.connect()
        self._on_tick_external = on_tick
        self._on_depth_external = on_depth

        with self._lock:
            new_symbols = [s for s in symbols if s not in self._subscribed_symbols]
            if not new_symbols:
                return
            self._subscribed_symbols.update(new_symbols)

        assert self._td_live is not None
        self._td_live.start_live_data([_to_truedata_symbol(s) for s in new_symbols])

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        if self._td_live is None:
            return
        with self._lock:
            to_remove = [s for s in symbols if s in self._subscribed_symbols]
            if not to_remove:
                return
            for symbol in to_remove:
                self._subscribed_symbols.discard(symbol)
                self._latest_ticks.pop(symbol, None)
                self._last_pushed_ts.pop(symbol, None)
        self._td_live.stop_live_data([_to_truedata_symbol(s) for s in to_remove])

    def get_latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest_ticks.get(symbol)

    def _handle_any_update(self, _raw: Any) -> None:
        """Both `trade_callback` and `bidask_callback` fire with the raw
        positional list straight off the wire, keyed by an opaque
        symbol_id this module deliberately doesn't try to resolve itself
        (see module docstring) — instead, any callback fire is treated as
        "something changed, go check": `td_live.live_data` (keyed by
        symbol string, confirmed real) is read directly for every symbol
        this instance subscribed, pushing out only the ones whose
        `.timestamp` is newer than what was last pushed.
        """
        assert self._td_live is not None
        with self._lock:
            symbols = list(self._subscribed_symbols)

        for symbol in symbols:
            feed = self._td_live.live_data.get(_to_truedata_symbol(symbol))
            if feed is None or feed.timestamp is None:
                continue

            with self._lock:
                if self._last_pushed_ts.get(symbol) == feed.timestamp:
                    continue
                self._last_pushed_ts[symbol] = feed.timestamp
                bid = getattr(feed, "best_bid_price", None) or 0.0
                ask = getattr(feed, "best_ask_price", None) or 0.0
                oi = getattr(feed, "oi", None)
                tick = Tick(
                    contract_symbol=symbol,
                    ltp=float(feed.ltp or 0.0),
                    bid=float(bid),
                    ask=float(ask),
                    volume=int(feed.ttq or 0),
                    oi=int(oi) if oi is not None else None,
                    ts=datetime.now(UTC),
                )
                self._latest_ticks[symbol] = tick

            if self._on_tick_external is not None:
                self._on_tick_external(tick)
            if bid and ask and self._on_depth_external is not None:
                self._on_depth_external(
                    DepthSnapshot(
                        contract_symbol=symbol,
                        bid_levels=(
                            DepthLevel(
                                price=float(bid),
                                qty=int(getattr(feed, "best_bid_qty", 0) or 0),
                            ),
                        ),
                        ask_levels=(
                            DepthLevel(
                                price=float(ask),
                                qty=int(getattr(feed, "best_ask_qty", 0) or 0),
                            ),
                        ),
                        ts=tick.ts,
                    )
                )

    # -- BaseMarketDataProvider: historical candles ----------------------------

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        bar_size = _HISTORICAL_BAR_SIZE_BY_TIMEFRAME.get(timeframe_seconds)
        if bar_size is None:
            raise ValueError(
                f"TrueDataProvider has no bar_size mapping for "
                f"timeframe_seconds={timeframe_seconds!r}"
            )

        if self._td_hist is None:
            # A genuinely separate service under this library (its own REST
            # OAuth login, no relation to the live WS session) — see module
            # docstring. Lazy, same reasoning as `connect()`'s own import.
            from truedata.history.TD_hist import TD_hist

            self._td_hist = TD_hist(
                self._settings.username, self._settings.password.get_secret_value()
            )

        df = self._td_hist.get_historic_data(
            _to_truedata_symbol(underlying),
            start_time=start.astimezone(IST).replace(tzinfo=None),
            end_time=end.astimezone(IST).replace(tzinfo=None),
            bar_size=bar_size,
        )
        if df is None or df.empty:
            return []
        return [_row_to_candle(row) for row in df.itertuples(index=False)]
