"""NTP drift and disk-space health checks. Pure check functions — they report,
they don't act. The Scheduler module (Phase 1+) calls these on a periodic
loop and is what actually triggers a degraded_mode transition on failure;
keeping the decision out of this module means these checks stay trivially
unit-testable without a trading_session or DB in scope at all.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
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
