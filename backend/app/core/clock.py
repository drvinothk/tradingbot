"""NTP drift and disk-space health checks. Pure check functions — they report,
they don't act. The Scheduler module (Phase 1+) calls these on a periodic
loop and is what actually triggers a degraded_mode transition on failure;
keeping the decision out of this module means these checks stay trivially
unit-testable without a trading_session or DB in scope at all.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import ntplib

# India market hours (cutoff_time on trading_sessions, EOD square-off, etc.)
# are wall-clock IST — everything else in this codebase is deliberately UTC
# (see e.g. app.core.db.base.utcnow), so any comparison against a
# session's cutoff_time must explicitly convert through this, not assume
# datetime.now(UTC) is close enough.
IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt_utc: datetime) -> datetime:
    """Converts any timezone-aware datetime to IST. Named for the common
    case (a UTC-stored column like `PriceBar.bucket_start`), but works for
    any aware input — `astimezone` doesn't care what zone it's already in.
    """
    return dt_utc.astimezone(IST)


# The only window new strategy entries may fire in, IST. Deliberately
# distinct from `market_data.market_hours`'s ~08:30-16:00 data-connectivity
# window — REST/WS may still connect and flow data outside this range, it
# just must never result in a new trade. The upper bound matches
# `TradingSession.cutoff_time`'s default (see `app/domain/session/models.py`)
# so a position can never be opened in the same instant EOD force-close
# fires — end is exclusive for that same reason.
TRADE_WINDOW_START = time(9, 31)
TRADE_WINDOW_END = time(15, 9)


def is_within_global_trading_window(ts_utc: datetime) -> bool:
    """Gates new strategy entries by the timestamp of the *data* being
    evaluated (typically a bar's `bucket_start`), not wall-clock `now()` —
    `strategy_engine.runner.run_cycle` is the caller, passing the latest
    completed bar it's about to evaluate. Falls back to `now_ist()` when no
    bar exists yet for an instrument (a strategy that doesn't consume bars,
    e.g. `SyntheticStrategy`, or an instrument before its first bar
    completes) — `to_ist` on an already-IST datetime is a no-op, so that
    fallback works through this same function unchanged.
    """
    return TRADE_WINDOW_START <= to_ist(ts_utc).time() < TRADE_WINDOW_END


# One minute past TRADE_WINDOW_END: a strategy run still SCANNING (zero open
# positions) this late has nothing left to protect, since no new entry can
# fire past TRADE_WINDOW_END anyway — see strategy_engine.runner's
# _maybe_stop_for_eod for the actual stop logic. Deliberately wall-clock
# `now()`-based, not bar-timestamp-based like TRADE_WINDOW_END/START above —
# "should this background thread tear itself down now" needs real current
# time, not a bar that may lag it.
EOD_SCANNING_STOP_TIME = time(15, 10)


def is_past_eod_scanning_stop(ts_utc: datetime) -> bool:
    return to_ist(ts_utc).time() >= EOD_SCANNING_STOP_TIME


@dataclass(frozen=True)
class ClockCheckResult:
    ok: bool
    drift_seconds: float | None
    error: str | None = None


@dataclass(frozen=True)
class DiskCheckResult:
    ok: bool
    free_gb: float
    total_gb: float


def check_ntp_drift(
    server: str = "pool.ntp.org",
    max_drift_seconds: float = 2.0,
    timeout: float = 5.0,
) -> ClockCheckResult:
    """Compares local system time against an NTP server. `ok=False` on
    exceeding max_drift_seconds OR on failure to reach the server at all —
    a network hiccup and a genuinely drifted clock are both reasons this
    check shouldn't be trusted, and callers should treat them the same way
    (log + retry next cycle), not distinguish them.
    """
    try:
        client = ntplib.NTPClient()
        response = client.request(server, version=3, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any network/library failure is "not ok"
        return ClockCheckResult(ok=False, drift_seconds=None, error=str(exc))

    ntp_time = datetime.fromtimestamp(response.tx_time, tz=UTC)
    local_time = datetime.now(UTC)
    drift = (local_time - ntp_time).total_seconds()

    return ClockCheckResult(ok=abs(drift) <= max_drift_seconds, drift_seconds=drift)


def check_disk_space(path: str = "/", min_free_gb: float = 2.0) -> DiskCheckResult:
    """A full disk silently breaks DB writes and audit logging — arguably the
    worst failure mode for a system whose safety property is "if it isn't
    audited, it didn't happen" — so this is a first-class health signal, not
    an afterthought.
    """
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    return DiskCheckResult(ok=free_gb >= min_free_gb, free_gb=free_gb, total_gb=total_gb)
