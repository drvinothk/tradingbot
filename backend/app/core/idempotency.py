"""Idempotency key generation.

The actual "claim" step — inserting a `trade_intents`/`orders` row with a
unique `idempotency_key` in the same transaction that marks it dispatched —
is implemented at the repository level in Phase 2/3 once those tables exist,
not here; a generic claim helper can't know each table's other required
fields. This module only owns key generation, so every caller (Strategy
Service emitting a TradeIntent, Execution Service dispatching an Order) uses
the same format and there is exactly one place that decides what an
idempotency key looks like.
"""

from __future__ import annotations

import uuid


def new_idempotency_key() -> str:
    """One key per logical intent-to-act. Generate this once when the intent
    is first created (e.g. when a Strategy emits a TradeIntent) and carry it
    through retries — never regenerate on retry, or the whole point is lost.
    """
    return str(uuid.uuid4())
