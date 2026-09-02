"""GET /market-data/telemetry (Advanced page's "Market Data Adapter
telemetry" card) -- read-only composition over `quote_ticks`/`price_bars`/
`indicator_snapshots`/`option_chain_snapshots`. Exercised as a direct
function call (same lighter-weight style `test_api_market_data_candles.py`
already uses), since the permission wiring itself
(`require_permission("session.start")`) is already covered elsewhere -- the
real risk surface here is the per-symbol composition this endpoint does
itself (NIFTY/BANKNIFTY feed freshness + indicators + PCR, INDIA VIX with
its own wider freshness thresholds and no PCR).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.api.v1.market_data import get_market_data_telemetry
from app.core.clock import now_ist
from app.domain.market.models import (
    IndicatorSnapshot,
    Instrument,
    OptionChainSnapshot,
    OptionContract,
    OptionType,
    QuoteTick,
)


def _instrument(db: Session, symbol: str) -> Instrument:
    inst = Instrument(id=uuid.uuid4(), symbol=symbol, exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(inst)
    db.flush()
    return inst


def _tick(db: Session, instrument_id: uuid.UUID, *, seconds_ago: float) -> None:
    db.add(
        QuoteTick(
            id=uuid.uuid4(),
            instrument_id=instrument_id,
            ltp=100.0,
            bid=99.5,
            ask=100.5,
            volume=0,
            oi=None,
            ts=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        )
    )
    db.flush()


def _indicator(db: Session, instrument_id: uuid.UUID, *, name: str, value: float) -> None:
    db.add(
        IndicatorSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument_id,
            indicator_name=name,
            timeframe="60s",
            value=value,
            ts=datetime.now(UTC),
        )
    )
    db.flush()


def _row_by_symbol(result, symbol: str):
    return next(row for row in result.underlyings if row.symbol == symbol)


def test_telemetry_includes_india_vix_alongside_the_tradable_underlyings(db: Session, user):
    _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    _instrument(db, "INDIA VIX")

    result = get_market_data_telemetry(db=db, user=user)

    symbols = {row.symbol for row in result.underlyings}
    assert symbols == {"NIFTY", "BANKNIFTY", "INDIA VIX"}


def test_telemetry_vix_uses_its_own_wider_freshness_thresholds(db: Session, user):
    vix = _instrument(db, "INDIA VIX")
    _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    # 70s would read DEGRADED/STALE under the tradable-underlying thresholds
    # (30s degraded / 120s stale) but must read LIVE under VIX's own wider
    # ones -- see freshness.VIX_UI_THRESHOLDS.
    _tick(db, vix.id, seconds_ago=70)

    result = get_market_data_telemetry(db=db, user=user)

    vix_row = _row_by_symbol(result, "INDIA VIX")
    assert vix_row.feed_state == "live"
    assert vix_row.pcr_oi is None
    assert vix_row.pcr_vol is None


def test_telemetry_reports_latest_indicator_values_per_symbol(db: Session, user):
    nifty = _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    _instrument(db, "INDIA VIX")
    _indicator(db, nifty.id, name="RSI14", value=55.5)
    _indicator(db, nifty.id, name="EMA9", value=24100.25)
    _indicator(db, nifty.id, name="EMA20", value=24050.0)
    _indicator(db, nifty.id, name="VWAP", value=24080.75)

    result = get_market_data_telemetry(db=db, user=user)

    nifty_row = _row_by_symbol(result, "NIFTY")
    assert nifty_row.rsi14 == 55.5
    assert nifty_row.ema9 == 24100.25
    assert nifty_row.ema20 == 24050.0
    assert nifty_row.vwap == 24080.75

    bank_row = _row_by_symbol(result, "BANKNIFTY")
    assert bank_row.rsi14 is None
    assert bank_row.ema9 is None


def test_telemetry_indicators_use_the_most_recent_snapshot(db: Session, user):
    nifty = _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    _instrument(db, "INDIA VIX")
    db.add(
        IndicatorSnapshot(
            id=uuid.uuid4(),
            instrument_id=nifty.id,
            indicator_name="RSI14",
            timeframe="60s",
            value=40.0,
            ts=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db.flush()
    _indicator(db, nifty.id, name="RSI14", value=61.0)  # newer

    result = get_market_data_telemetry(db=db, user=user)

    assert _row_by_symbol(result, "NIFTY").rsi14 == 61.0


def test_telemetry_pcr_none_when_no_option_chain_snapshot_exists(db: Session, user):
    _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    _instrument(db, "INDIA VIX")

    result = get_market_data_telemetry(db=db, user=user)

    nifty_row = _row_by_symbol(result, "NIFTY")
    assert nifty_row.pcr_oi is None
    assert nifty_row.pcr_vol is None
    assert nifty_row.pcr_age_seconds is None


def test_telemetry_computes_pcr_from_the_latest_snapshot_at_the_nearest_expiry(
    db: Session, user
):
    nifty = _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    _instrument(db, "INDIA VIX")
    today = now_ist().date()
    expiry = today + timedelta(days=3)

    db.add(
        OptionContract(
            id=uuid.uuid4(),
            instrument_id=nifty.id,
            expiry_date=expiry,
            strike=24000,
            option_type=OptionType.CE,
            symbol="NIFTY-TELEMETRY-TEST-CE",
            is_active=True,
        )
    )
    db.flush()

    chain_data = [
        {"option_type": "CE", "oi": 1000, "volume": 500},
        {"option_type": "PE", "oi": 1500, "volume": 300},
    ]
    snapshot_ts = datetime.now(UTC) - timedelta(minutes=2)
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=nifty.id,
            expiry_date=expiry,
            ts=snapshot_ts,
            chain_data=chain_data,
        )
    )
    db.flush()

    result = get_market_data_telemetry(db=db, user=user)

    nifty_row = _row_by_symbol(result, "NIFTY")
    assert nifty_row.pcr_oi == 1500 / 1000
    assert nifty_row.pcr_vol == 300 / 500
    assert nifty_row.pcr_age_seconds is not None
    assert 90 <= nifty_row.pcr_age_seconds < 150


def test_telemetry_calculated_symbols_empty_for_mock_broker(db: Session, user):
    """The `_reset_broker_singleton` autouse fixture leaves every test on a
    fresh `MockBrokerAdapter` by default -- `calculated_symbols` (the
    Shoonya-only volume-proxy telemetry) must stay empty rather than error.
    """
    _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    _instrument(db, "INDIA VIX")

    result = get_market_data_telemetry(db=db, user=user)

    assert result.calculated_symbols == []


def _install_fake_shoonya_adapter_with_volume_proxy():
    """Mirrors `test_api_shoonya.py`'s own `_install_fake_shoonya_adapter`
    helper -- network-free `ShoonyaBrokerAdapter` construction, just with a
    real `ShoonyaWSClient` wired in and one volume-proxy mapping fed a
    frame, so `get_volume_proxy_symbols()` has something real to report.
    """
    from pydantic import SecretStr

    from app.config.settings import ShoonyaSettings
    from app.modules.broker_adapter import composition
    from app.modules.broker_adapter.base.contracts import AuthResult
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter
    from app.modules.broker_adapter.shoonya.ws_client import ShoonyaWSClient

    class _FakeRestClient:
        def close(self):
            pass

    settings = ShoonyaSettings(
        client_id="TESTCID",
        secret_code=SecretStr("TESTSECRET"),
        user_id="FA12345",
        api_host="https://api.shoonya.test/NorenWClientAPI",
        ws_host="wss://api.shoonya.test/NorenWSAPI/",
    )
    auth_result = AuthResult(session_token="tok", account_id="FA12345")
    adapter = ShoonyaBrokerAdapter(settings, auth_result, rest_client=_FakeRestClient())

    ws = ShoonyaWSClient(
        "wss://api.shoonya.test/NorenWSAPI/",
        uid="FA12345",
        actid="FA12345",
        access_token="tok",
        on_tick=lambda t: None,
    )
    ws.subscribe([("NIFTY", "NSE", "26000")])
    ws.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY29SEP26F", "NFO", "68407"))
    ws._handle_message(
        '{"t": "tf", "e": "NFO", "tk": "68407", "lp": "24300.0", "v": "1002500"}'
    )
    ws._handle_message('{"t": "tf", "e": "NSE", "tk": "26000", "lp": "24100.0"}')
    adapter._ws = ws
    composition.set_broker(adapter)


def test_telemetry_includes_calculated_symbols_for_shoonya_volume_proxy(db: Session, user):
    _instrument(db, "NIFTY")
    _instrument(db, "BANKNIFTY")
    _instrument(db, "INDIA VIX")
    _install_fake_shoonya_adapter_with_volume_proxy()

    result = get_market_data_telemetry(db=db, user=user)

    assert len(result.calculated_symbols) == 1
    row = result.calculated_symbols[0]
    assert row.target_symbol == "NIFTY"
    assert row.source_symbol == "NIFTY29SEP26F"
    assert row.subscribed is True
    assert row.last_price == 24300.0
    assert row.last_cum_volume == 1_002_500
