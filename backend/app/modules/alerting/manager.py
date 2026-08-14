"""Ops-Hardening Phase 2. Centralized alert dispatch — every alert is a
dual-write: a `SystemAlert` DB row (the same shape `HealthCheckScheduler`
already writes) plus a best-effort Telegram notification. The DB write is
the source of truth (already readable via `GET /system-alerts`, per the
existing recovery-panel batch); Telegram is a convenience notification
layered on top, never the only record of an alert.

Threading model matches every other background worker in this codebase
(`HealthCheckScheduler`, `MarketDataScheduler`, `PositionManager`,
`StrategyRunner`, `FailoverMarketDataProvider`): plain `threading`, no
asyncio anywhere. `send_alert` is called synchronously from whichever
background thread raised the alert (e.g. `StrategyRunner`'s own loop) —
the Telegram HTTP call is a plain, bounded-timeout `httpx.post`, not
offloaded to a second thread, since these calls are already off any hot
path (once per stalled cycle, not per tick) and a hard 3s timeout bounds
the worst case precisely.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.domain.ops.models import AlertSeverity, SystemAlert

logger = logging.getLogger("app.alerting.manager")

_TELEGRAM_TIMEOUT_SECONDS = 3.0

# Logged once per process, not once per missed alert — a genuinely
# unconfigured Telegram bot would otherwise spam a warning on every single
# alert this process ever raises for its entire lifetime.
_warned_missing_config = False


def _send_telegram(message: str) -> None:
    global _warned_missing_config
    telegram = get_settings().telegram
    bot_token = telegram.bot_token.get_secret_value()
    if not bot_token or not telegram.chat_id:
        if not _warned_missing_config:
            logger.warning(
                "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                "missing) -- alerts will be written to SystemAlert only"
            )
            _warned_missing_config = True
        return

    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": telegram.chat_id, "text": message},
            timeout=_TELEGRAM_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        # Never lets a Telegram outage take down the caller's own cycle --
        # the SystemAlert row (written before this call) is already the
        # durable record regardless of what happens here.
        logger.warning("Telegram alert dispatch failed", exc_info=True)


def send_alert(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    severity: AlertSeverity,
    category: str,
    message: str,
    trading_session_id: uuid.UUID | None = None,
) -> SystemAlert:
    """Writes the `SystemAlert` row first, then attempts Telegram — matching
    `audit_service.record_event`'s own contract, this only flushes, never
    commits; the caller's surrounding transaction (already open in every
    real call site: `StrategyRunner`'s per-cycle `session_scope()`,
    `HealthCheckScheduler`'s own loop) owns the commit.
    """
    alert = SystemAlert(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trading_session_id=trading_session_id,
        severity=severity,
        category=category,
        message=message,
        created_at=datetime.now(UTC),
    )
    db.add(alert)
    db.flush()

    _send_telegram(f"[{severity.value.upper()}] {category}: {message}")

    return alert
