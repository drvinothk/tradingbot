"""Ops-Hardening Phase 2: app.modules.alerting.manager.send_alert -- the
dual-write (SystemAlert row + best-effort Telegram) every alert-raising
call site (StrategyRunner's watchdog, future health-check wiring) goes
through.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

import app.modules.alerting.manager as alerting_manager
from app.config.settings import get_settings
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.modules.alerting.manager import send_alert


@pytest.fixture(autouse=True)
def _reset_missing_config_warning_flag():
    """`_warned_missing_config` is a module-level "logged once per process"
    flag -- reset around each test so one test's trigger doesn't silently
    suppress another test's assertion about it (not asserted on directly
    today, but this keeps the flag from leaking state across tests either
    way, matching this file's own isolation discipline).
    """
    alerting_manager._warned_missing_config = False
    yield
    alerting_manager._warned_missing_config = False


@pytest.fixture(autouse=True)
def _telegram_unconfigured(monkeypatch):
    """Every real deployment of this test suite must never fire a real
    Telegram HTTP call regardless of what's in a local credentials file --
    default every test to "unconfigured" explicitly rather than relying on
    telegram.env simply not existing on whichever machine runs this suite.
    """
    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bot_token", SecretStr(""))
    monkeypatch.setattr(settings.telegram, "chat_id", "")


def test_send_alert_writes_a_system_alert_row(db: Session, workspace):
    alert = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="something happened",
    )
    db.flush()

    row = db.get(SystemAlert, alert.id)
    assert row is not None
    assert row.workspace_id == workspace.id
    assert row.severity == AlertSeverity.WARNING
    assert row.category == "test_category"
    assert row.message == "something happened"
    assert row.trading_session_id is None


def test_send_alert_skips_telegram_when_unconfigured(db: Session, workspace, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append((a, kw)))

    send_alert(
        db, workspace_id=workspace.id, severity=AlertSeverity.INFO, category="x", message="y"
    )

    assert calls == []


def test_send_alert_calls_telegram_when_configured(db: Session, workspace, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bot_token", SecretStr("fake-token"))
    monkeypatch.setattr(settings.telegram, "chat_id", "12345")

    calls: list[tuple] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append((a, kw)))

    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="stall",
        message="feed is stalled",
    )

    assert len(calls) == 1
    (url,), kwargs = calls[0]
    assert url == "https://api.telegram.org/botfake-token/sendMessage"
    assert kwargs["json"] == {"chat_id": "12345", "text": "[CRITICAL] stall: feed is stalled"}
    assert kwargs["timeout"] == alerting_manager._TELEGRAM_TIMEOUT_SECONDS


def test_send_alert_survives_telegram_failure(db: Session, workspace, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bot_token", SecretStr("fake-token"))
    monkeypatch.setattr(settings.telegram, "chat_id", "12345")

    def _raise(*a, **kw):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "post", _raise)

    # Must not raise -- the SystemAlert row is already the durable record;
    # a Telegram outage is never allowed to propagate into the caller's own
    # background cycle.
    alert = send_alert(
        db, workspace_id=workspace.id, severity=AlertSeverity.CRITICAL, category="x", message="y"
    )

    assert db.get(SystemAlert, alert.id) is not None


def test_send_alert_uses_given_trading_session_id(db: Session, workspace):
    session_id = uuid.uuid4()

    alert = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.INFO,
        category="x",
        message="y",
        trading_session_id=session_id,
    )

    row = db.get(SystemAlert, alert.id)
    assert row is not None
    assert row.trading_session_id == session_id
