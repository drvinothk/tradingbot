"""Audit Service: append-only, hash-chained event log. `record_event` is the
only way any module should ever write to `audit_events` — if it's not in
this table, it didn't happen, so every write goes through the same
serialized path that keeps the hash chain valid.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.locking import LOCK_AUDIT_CHAIN, advisory_lock
from app.domain.audit.models import ActorType, AuditEvent, EventCategory


def _canonical_payload(fields: dict) -> str:
    return json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))


def record_event(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    actor_type: ActorType,
    event_category: EventCategory,
    event_type: str,
    actor_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    trading_session_id: uuid.UUID | None = None,
    broker_account_id: uuid.UUID | None = None,
    strategy_config_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> AuditEvent:
    """Writes one audit event under the audit-chain advisory lock, so
    "read the last row's hash, then insert" can never race with another
    writer and silently fork the chain. Caller is responsible for
    committing the surrounding transaction (this only flushes).
    """
    payload = payload or {}
    ts = datetime.now(UTC)

    with advisory_lock(db, LOCK_AUDIT_CHAIN):
        last = db.query(AuditEvent).order_by(AuditEvent.seq.desc()).first()
        prev_hash = last.hash if last is not None else None

        fields_for_hash = {
            "workspace_id": str(workspace_id),
            "ts": ts.isoformat(),
            "actor_type": actor_type.value,
            "actor_id": str(actor_id) if actor_id else None,
            "event_category": event_category.value,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            "trading_session_id": str(trading_session_id) if trading_session_id else None,
            "broker_account_id": str(broker_account_id) if broker_account_id else None,
            "strategy_config_id": str(strategy_config_id) if strategy_config_id else None,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        digest = hashlib.sha256(_canonical_payload(fields_for_hash).encode("utf-8")).hexdigest()

        event = AuditEvent(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            ts=ts,
            actor_type=actor_type,
            actor_id=actor_id,
            event_category=event_category,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            trading_session_id=trading_session_id,
            broker_account_id=broker_account_id,
            strategy_config_id=strategy_config_id,
            payload=payload,
            prev_hash=prev_hash,
            hash=digest,
        )
        db.add(event)
        db.flush()
        return event


def verify_chain(db: Session) -> tuple[bool, uuid.UUID | None]:
    """Walks the full chain in insertion order and recomputes each hash.
    Returns (True, None) if intact, or (False, id-of-first-broken-row) —
    for the reconciliation-lock / kill-switch resume checklist to call, not
    for hot-path use.
    """
    prev_hash: str | None = None
    for event in db.query(AuditEvent).order_by(AuditEvent.seq.asc()).yield_per(500):
        fields_for_hash = {
            "workspace_id": str(event.workspace_id),
            "ts": event.ts.isoformat(),
            "actor_type": event.actor_type,
            "actor_id": str(event.actor_id) if event.actor_id else None,
            "event_category": event.event_category,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": str(event.entity_id) if event.entity_id else None,
            "trading_session_id": str(event.trading_session_id)
            if event.trading_session_id
            else None,
            "broker_account_id": str(event.broker_account_id)
            if event.broker_account_id
            else None,
            "strategy_config_id": str(event.strategy_config_id)
            if event.strategy_config_id
            else None,
            "payload": event.payload,
            "prev_hash": prev_hash,
        }
        expected = hashlib.sha256(_canonical_payload(fields_for_hash).encode("utf-8")).hexdigest()
        if expected != event.hash or event.prev_hash != prev_hash:
            return False, event.id
        prev_hash = event.hash

    return True, None
