from __future__ import annotations

import uuid
from datetime import date

from app.core.market_utils import is_expiry_day, is_trading_day

_INSTRUMENT_ID = uuid.uuid4()


def test_tuesday_is_expiry_day() -> None:
    assert is_expiry_day(_INSTRUMENT_ID, date(2026, 8, 18)) is True  # real NIFTY expiry


def test_monday_is_not_expiry_day() -> None:
    assert is_expiry_day(_INSTRUMENT_ID, date(2026, 8, 17)) is False


def test_thursday_is_not_expiry_day() -> None:
    assert is_expiry_day(_INSTRUMENT_ID, date(2026, 8, 20)) is False


def test_weekday_is_a_trading_day() -> None:
    assert is_trading_day(date(2026, 8, 18)) is True  # Tuesday


def test_saturday_is_not_a_trading_day() -> None:
    assert is_trading_day(date(2026, 8, 15)) is False


def test_sunday_is_not_a_trading_day() -> None:
    assert is_trading_day(date(2026, 8, 16)) is False
