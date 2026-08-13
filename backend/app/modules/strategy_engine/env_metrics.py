"""VIX/PCR environment metrics — a real data pipeline (India VIX feed, PCR
computed from `OptionChainSnapshot` OI) doesn't exist yet; see the build
plan / memory for the deferred design. `get_latest_env_metrics` is a stub
so callers, the payload shape, and the eventual filter wiring are all ready
for it — the only thing that needs to change once the pipeline is real is
this function's body, not any call site.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.modules.strategy_engine.interface import EnvPayload


def get_latest_env_metrics(db: Session, instrument_id: uuid.UUID) -> EnvPayload | None:
    """Always `None` until the VIX/PCR pipeline exists — every env-filter
    call site is naturally a no-op as a result, not a fake pass/fail.
    """
    return None
