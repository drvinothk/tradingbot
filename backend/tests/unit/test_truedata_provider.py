"""Unit tests for `TrueDataProvider`. No credentials exist yet (2026-08-11)
and `truedata` is deliberately not installed in this venv (see
`pyproject.toml`'s own `truedata` extras-group comment), so every test here
injects fake `TD_live`/`TD_hist`-shaped objects via the `td_live_client=`/
`td_hist_client=` seams rather than touching the real library or network —
same "fakes mirror the SDK's already-parsed shape" style
`test_angel_one_provider.py` already uses.

These tests verify this codebase's own dispatch/mapping logic is internally
consistent with what `TrueDataProvider`'s own module docstring documents as
*confirmed* from reading `truedata` 7.0.1's actual installed source (the
symbol-keyed `live_data` dict, the "any callback fires -> re-scan live_data"
dispatch shape, `get_historic_data`'s real DataFrame column names). They
cannot and do not prove the still-unconfirmed parts (real connection
behavior, real bid/ask update cadence) are correct — that needs a real
account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.config.settings import TrueDataSettings
from app.core.clock import IST
from app.modules.market_data.providers.truedata_provider import TrueDataProvider, _row_to_candle

SETTINGS = TrueDataSettings(username="user1", password=SecretStr("pw1"))


def _fake_feed(
    symbol: str,
    *,
    ltp: float,
    timestamp: datetime,
    ttq: int = 0,
    oi: int | None = None,
    best_bid_price: float | None = None,
    best_bid_qty: int | None = None,
    best_ask_price: float | None = None,
    best_ask_qty: int | None = None,
) -> SimpleNamespace:
    """Mirrors the confirmed real attribute set on `truedata`'s own
    `tick_feed` dataclass (see module docstring) — a plain `SimpleNamespace`
    is enough since `TrueDataProvider` only ever reads attributes off it.
    """
    return SimpleNamespace(
        symbol=symbol,
        ltp=ltp,
        ttq=ttq,
        oi=oi,
        timestamp=timestamp,
        best_bid_price=best_bid_price,
        best_bid_qty=best_bid_qty,
        best_ask_price=best_ask_price,
        best_ask_qty=best_ask_qty,
    )


@dataclass
class _FakeTDLive:
    live_data: dict[str, SimpleNamespace] = field(default_factory=dict)
    start_live_data_calls: list[list[str]] = field(default_factory=list)
    stop_live_data_calls: list[list[str]] = field(default_factory=list)
    disconnected: bool = False

    def start_live_data(self, symbols):
        self.start_live_data_calls.append(list(symbols))

    def stop_live_data(self, symbols):
        self.stop_live_data_calls.append(list(symbols))

    def disconnect(self):
        self.disconnected = True

    def trade_callback(self, fn):
        return fn

    def bidask_callback(self, fn):
        return fn


@dataclass
class _FakeHistDataFrame:
    rows: list[SimpleNamespace] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.rows

    def itertuples(self, index: bool = False):
        return iter(self.rows)


@dataclass
class _FakeTDHist:
    historic_calls: list[tuple] = field(default_factory=list)
    result: _FakeHistDataFrame | None = None

    def get_historic_data(self, symbol, *, start_time, end_time, bar_size):
        self.historic_calls.append((symbol, start_time, end_time, bar_size))
        return self.result if self.result is not None else _FakeHistDataFrame()


def test_subscribe_ticks_calls_start_live_data_with_the_truedata_symbols():
    """Confirmed live 2026-08-17 (see module docstring): TrueData's real
    index-tick symbols are "NIFTY 50"/"NIFTY BANK", not this codebase's own
    internal "NIFTY"/"BANKNIFTY" -- the SDK call must see the translated
    name, while every other method (get_latest_tick included) keeps using
    the internal one.
    """
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)

    provider.subscribe_ticks(["NIFTY", "BANKNIFTY"], on_tick=lambda _t: None)

    assert td_live.start_live_data_calls == [["NIFTY 50", "NIFTY BANK"]]


def test_subscribe_ticks_passes_through_an_unmapped_symbol_unchanged():
    """Only the two known underlyings have a real TrueData translation
    (see module docstring) -- anything else passes through as-is rather
    than raising, matching `_to_truedata_symbol`'s own `dict.get` fallback.
    """
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)

    provider.subscribe_ticks(["SOMEOTHERSYMBOL"], on_tick=lambda _t: None)

    assert td_live.start_live_data_calls == [["SOMEOTHERSYMBOL"]]


def test_subscribe_ticks_is_idempotent_per_symbol():
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None)
    provider.subscribe_ticks(["NIFTY", "BANKNIFTY"], on_tick=lambda _t: None)

    # Second call must only request the genuinely new symbol.
    assert len(td_live.start_live_data_calls) == 2
    assert td_live.start_live_data_calls[1] == ["NIFTY BANK"]


def test_unsubscribe_ticks_clears_the_symbol_and_calls_stop_live_data():
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None)

    provider.unsubscribe_ticks(["NIFTY"])

    assert td_live.stop_live_data_calls == [["NIFTY 50"]]
    assert provider.get_latest_tick("NIFTY") is None


def test_update_signal_pushes_a_fresh_tick_when_live_data_changes():
    """Neither `trade_callback` nor `bidask_callback` carry a resolvable
    symbol in this version of the library (see module docstring) — any
    fire re-scans `td_live.live_data` for subscribed symbols instead.
    `live_data` is keyed by TrueData's own real symbol ("NIFTY 50"), but
    the resulting `Tick.contract_symbol`/`get_latest_tick` key stays this
    codebase's internal "NIFTY" (see the 2026-08-17 symbol-mapping note in
    the module docstring).
    """
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)
    received: list[float] = []
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: received.append(t.ltp))

    ts = datetime(2026, 8, 11, 9, 20, 0)
    td_live.live_data["NIFTY 50"] = _fake_feed(
        "NIFTY 50",
        ltp=24500.0,
        timestamp=ts,
        ttq=10,
        oi=1000,
        best_bid_price=24499.5,
        best_bid_qty=50,
        best_ask_price=24500.5,
        best_ask_qty=40,
    )

    provider._handle_any_update(None)  # noqa: SLF001

    assert received == [24500.0]
    tick = provider.get_latest_tick("NIFTY")
    assert tick is not None
    assert tick.ltp == 24500.0
    assert tick.bid == 24499.5
    assert tick.ask == 24500.5
    assert tick.volume == 10
    assert tick.oi == 1000


def test_update_signal_is_a_noop_when_timestamp_is_unchanged():
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)
    received: list[float] = []
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: received.append(t.ltp))

    ts = datetime(2026, 8, 11, 9, 20, 0)
    td_live.live_data["NIFTY 50"] = _fake_feed("NIFTY 50", ltp=24500.0, timestamp=ts)
    provider._handle_any_update(None)  # noqa: SLF001
    provider._handle_any_update(None)  # noqa: SLF001

    # Same feed, same timestamp both times -- only the first fire is real.
    assert received == [24500.0]


def test_update_signal_ignores_a_symbol_never_subscribed():
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None)

    # BANKNIFTY (as "NIFTY BANK") shows up in live_data (e.g. some other
    # caller subscribed it against the same shared td_live) but this
    # provider instance never subscribed it -- must not leak into
    # get_latest_tick.
    td_live.live_data["NIFTY BANK"] = _fake_feed(
        "NIFTY BANK", ltp=51000.0, timestamp=datetime(2026, 8, 11, 9, 20, 0)
    )
    provider._handle_any_update(None)  # noqa: SLF001

    assert provider.get_latest_tick("BANKNIFTY") is None


def test_get_price_history_maps_dataframe_rows_correctly():
    hist_df = _FakeHistDataFrame(
        rows=[
            SimpleNamespace(
                timestamp=datetime(2026, 8, 10, 9, 15, 0),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=10,
            )
        ]
    )
    td_hist = _FakeTDHist(result=hist_df)
    provider = TrueDataProvider(SETTINGS, td_hist_client=td_hist)
    start = datetime(2026, 8, 10, 3, 45, tzinfo=UTC)
    end = datetime(2026, 8, 10, 3, 46, tzinfo=UTC)

    candles = provider.get_price_history("NIFTY", start, end, timeframe_seconds=60)

    assert len(candles) == 1
    assert candles[0].open == 100.0
    assert candles[0].close == 100.5
    assert candles[0].volume == 10
    symbol, start_time, end_time, bar_size = td_hist.historic_calls[0]
    assert symbol == "NIFTY 50"
    # Confirmed real value (see module docstring): the library's own
    # `historical_decorator` normalizes spacing/pluralization anyway, but
    # this module sends the plain form directly.
    assert bar_size == "1min"
    # Confirms the IST conversion actually happened (not just passed
    # through as-is) -- see module docstring on why this is now a confirmed
    # fact, not a prediction.
    assert start_time == start.astimezone(IST).replace(tzinfo=None)
    assert end_time == end.astimezone(IST).replace(tzinfo=None)


def test_get_price_history_returns_empty_list_when_truedata_has_no_data():
    td_hist = _FakeTDHist(result=_FakeHistDataFrame(rows=[]))
    provider = TrueDataProvider(SETTINGS, td_hist_client=td_hist)

    candles = provider.get_price_history(
        "NIFTY", datetime(2026, 8, 10, tzinfo=UTC), datetime(2026, 8, 10, tzinfo=UTC), 60
    )

    assert candles == []


def test_get_price_history_raises_for_an_unmapped_timeframe():
    provider = TrueDataProvider(SETTINGS, td_hist_client=_FakeTDHist())

    with pytest.raises(ValueError, match="timeframe_seconds"):
        provider.get_price_history(
            "NIFTY", datetime(2026, 8, 10, tzinfo=UTC), datetime(2026, 8, 10, tzinfo=UTC), 45
        )


def test_row_to_candle_raises_loudly_on_a_missing_field_rather_than_defaulting():
    incomplete_row = SimpleNamespace(
        timestamp=datetime(2026, 8, 10, 9, 15, 0), open=100.0, high=101.0, low=99.0
    )
    with pytest.raises(AttributeError):
        _row_to_candle(incomplete_row)


def test_disconnect_clears_all_local_state():
    td_live = _FakeTDLive()
    provider = TrueDataProvider(SETTINGS, td_live_client=td_live)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None)
    td_live.live_data["NIFTY 50"] = _fake_feed(
        "NIFTY 50", ltp=100.0, timestamp=datetime(2026, 8, 11, 9, 20, 0)
    )
    provider._handle_any_update(None)  # noqa: SLF001
    assert provider.get_latest_tick("NIFTY") is not None

    provider.disconnect()

    assert td_live.disconnected is True
    assert provider.get_latest_tick("NIFTY") is None
