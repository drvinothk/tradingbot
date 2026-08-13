"""`strategy_engine.runner.is_within_trade_window` — the 09:31-15:09 IST
boundary new entries must respect. End is exclusive so a new entry can
never land in the same instant `TradingSession.cutoff_time` (also 15:09,
see `app/domain/session/models.py`) force-closes everything.
"""

from __future__ import annotations

from datetime import time

import pytest

from app.modules.strategy_engine.runner import is_within_trade_window


@pytest.mark.parametrize(
    ("t", "expected"),
    [
        (time(9, 30, 59), False),
        (time(9, 31, 0), True),
        (time(11, 0, 0), True),
        (time(15, 8, 59), True),
        (time(15, 9, 0), False),
        (time(15, 30, 0), False),
        (time(8, 0, 0), False),
        (time(0, 0, 0), False),
    ],
)
def test_boundary_cases(t: time, expected: bool) -> None:
    assert is_within_trade_window(t) is expected
