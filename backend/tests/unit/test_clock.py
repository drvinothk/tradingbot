"""`check_ntp_drift`'s fallback-server behavior. Real, live bug fixed
2026-08-18: `pool.ntp.org` is unreachable from the OCI deployment (blocked
at the cloud egress level, same pattern as Angel One's REST gateway), which
this codebase's own `HealthCheckScheduler` was treating as "clock can't be
trusted" and dropping any live-enabled session to `degraded_mode` over --
even though the box's own system clock was genuinely synchronized the
whole time via OCI's internal NTP service. See `check_ntp_drift`'s own
docstring for the full incident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import ntplib

from app.core.clock import check_ntp_drift


def _fake_response(offset_seconds: float = 0.0) -> MagicMock:
    response = MagicMock()
    response.tx_time = (datetime.now(UTC).timestamp()) - offset_seconds
    return response


def test_primary_success_never_tries_fallback(monkeypatch):
    calls: list[str] = []

    def fake_request(self, server, version=3, timeout=5.0):
        calls.append(server)
        return _fake_response()

    monkeypatch.setattr(ntplib.NTPClient, "request", fake_request)

    result = check_ntp_drift(server="pool.ntp.org", fallback_server="169.254.169.254")

    assert result.ok is True
    assert calls == ["pool.ntp.org"]


def test_primary_unreachable_falls_back_and_succeeds(monkeypatch):
    calls: list[str] = []

    def fake_request(self, server, version=3, timeout=5.0):
        calls.append(server)
        if server == "pool.ntp.org":
            raise ntplib.NTPException("No response received from pool.ntp.org.")
        return _fake_response()

    monkeypatch.setattr(ntplib.NTPClient, "request", fake_request)

    result = check_ntp_drift(server="pool.ntp.org", fallback_server="169.254.169.254")

    assert result.ok is True
    assert calls == ["pool.ntp.org", "169.254.169.254"]


def test_both_unreachable_reports_not_ok_with_the_last_error(monkeypatch):
    def fake_request(self, server, version=3, timeout=5.0):
        raise ntplib.NTPException(f"No response received from {server}.")

    monkeypatch.setattr(ntplib.NTPClient, "request", fake_request)

    result = check_ntp_drift(server="pool.ntp.org", fallback_server="169.254.169.254")

    assert result.ok is False
    assert result.drift_seconds is None
    assert "169.254.169.254" in result.error


def test_excessive_drift_from_primary_is_not_masked_by_a_fallback_attempt(monkeypatch):
    """A real drift reading from the primary is a real answer -- must not
    keep shopping the fallback server for a more convenient one.
    """
    calls: list[str] = []

    def fake_request(self, server, version=3, timeout=5.0):
        calls.append(server)
        return _fake_response(offset_seconds=10.0)

    monkeypatch.setattr(ntplib.NTPClient, "request", fake_request)

    result = check_ntp_drift(
        server="pool.ntp.org", max_drift_seconds=2.0, fallback_server="169.254.169.254"
    )

    assert result.ok is False
    assert result.drift_seconds is not None
    assert calls == ["pool.ntp.org"]


def test_fallback_disabled_with_none_only_tries_primary(monkeypatch):
    calls: list[str] = []

    def fake_request(self, server, version=3, timeout=5.0):
        calls.append(server)
        raise ntplib.NTPException("No response received.")

    monkeypatch.setattr(ntplib.NTPClient, "request", fake_request)

    result = check_ntp_drift(server="pool.ntp.org", fallback_server=None)

    assert result.ok is False
    assert calls == ["pool.ntp.org"]
