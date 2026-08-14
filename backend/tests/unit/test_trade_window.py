"""`core.clock.is_within_global_trading_window` — the 09:31-15:09 IST
boundary new entries must respect. End is exclusive so a new entry can
never land in the same instant `TradingSession.cutoff_time` (also 15:09,
see `app/domain/session/models.py`) force-closes everything. Gates on
whatever timestamp it's given (a bar's `bucket_start` in production, via
`strategy_engine.runner.run_cycle`) — parametrized here with plain IST
datetimes since the function itself is timestamp-source-agnostic.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.clock import IST, is_within_global_trading_window


@pytest.mark.parametrize(
    ("hour", "minute", "second", "expected"),
    [
        (9, 30, 59, False),
        (9, 31, 0, True),
        (11, 0, 0, True),
        (15, 8, 59, True),
        (15, 9, 0, False),
        (15, 30, 0, False),
        (8, 0, 0, False),
        (0, 0, 0, False),
    ],
)
def test_boundary_cases(hour: int, minute: int, second: int, expected: bool) -> None:
    ts = datetime(2026, 1, 1, hour, minute, second, tzinfo=IST)
    assert is_within_global_trading_window(ts) is expected
