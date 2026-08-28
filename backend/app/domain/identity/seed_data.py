"""Starter roles/permissions from the source blueprint. Used by the Phase 0
seed script and by Alembic's initial data migration — kept as plain data here
so both can import the same source of truth instead of duplicating it.
"""

from __future__ import annotations

PERMISSIONS: dict[str, str] = {
    "auth.login": "Can log into the platform",
    "strategy.view": "Can view strategy configuration and status",
    "strategy.edit": "Can create/modify strategy configuration",
    "session.start": "Can start a trading session",
    "session.stop": "Can stop a trading session",
    "papertrade.execute": "Can run strategies in paper mode",
    "livetrade.execute": "Can run strategies in live mode",
    "broker.manage": "Can add/edit broker account configuration",
    "risk.override": "Can override risk limits, clear kill_switch/reconciliation_lock",
    "audit.view": "Can read the audit log",
    "reports.export": "Can export reports/scorecards",
}

ROLES: dict[str, list[str]] = {
    "Admin": list(PERMISSIONS.keys()),
    "Trader": [
        "auth.login",
        "strategy.view",
        "session.start",
        "session.stop",
        "papertrade.execute",
        "livetrade.execute",
        "reports.export",
    ],
    "Viewer": [
        "auth.login",
        "strategy.view",
        "reports.export",
    ],
    "Auditor": [
        "auth.login",
        "strategy.view",
        "audit.view",
        "reports.export",
    ],
}
