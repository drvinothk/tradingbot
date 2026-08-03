"""Tests for the audit read API (`GET /audit`, `GET /audit/verify`) added
because `audit_events` had no read path at all before — direct DB access
was the only way to inspect it, despite the schema being explicitly
designed to be queryable by trade/user/broker-account/strategy/session.
Calls the route functions directly (same shape as
`test_api_execution_and_reports.py`'s direct `submit_signal` calls) rather
than through a full TestClient+login harness — the RBAC gate itself
(`require_permission`) is already covered by `test_auth_and_rbac.py`, so
this file focuses on the query/workspace-scoping logic and `verify_chain`
wiring.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.api.v1.audit import list_audit_events, verify_audit_chain
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User, Workspace
from app.modules.audit_service.service import record_event


def _record(db: Session, workspace_id: uuid.UUID, event_type: str, **kwargs: object) -> None:
    record_event(
        db,
        workspace_id=workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.SYSTEM_HEALTH,
        event_type=event_type,
        **kwargs,  # type: ignore[arg-type]
    )


def test_list_audit_events_is_workspace_scoped(db: Session, workspace: Workspace, user: User):
    other_workspace = Workspace(id=uuid.uuid4(), name=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other_workspace)
    db.flush()

    _record(db, workspace.id, "test.mine")
    _record(db, other_workspace.id, "test.not_mine")
    db.flush()

    events = list_audit_events(
        trading_session_id=None, strategy_config_id=None, limit=200, db=db, user=user
    )

    event_types = {e.event_type for e in events}
    assert "test.mine" in event_types
    assert "test.not_mine" not in event_types


def test_list_audit_events_filters_by_trading_session(
    db: Session, workspace: Workspace, user: User
):
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()
    _record(db, workspace.id, "test.session_a", trading_session_id=session_a)
    _record(db, workspace.id, "test.session_b", trading_session_id=session_b)
    db.flush()

    events = list_audit_events(
        trading_session_id=session_a, strategy_config_id=None, limit=200, db=db, user=user
    )

    assert {e.event_type for e in events} == {"test.session_a"}


def test_verify_chain_reports_intact_for_untampered_events(
    db: Session, workspace: Workspace, user: User
):
    _record(db, workspace.id, "test.one")
    _record(db, workspace.id, "test.two")
    db.flush()

    result = verify_audit_chain(db=db, user=user)

    assert result.intact is True
    assert result.first_broken_event_id is None


def test_verify_chain_detects_tampering(db: Session, workspace: Workspace, user: User):
    from app.domain.audit.models import AuditEvent

    _record(db, workspace.id, "test.one")
    db.flush()

    event = db.query(AuditEvent).filter(AuditEvent.workspace_id == workspace.id).one()
    event.payload = {"tampered": True}
    db.flush()

    result = verify_audit_chain(db=db, user=user)

    assert result.intact is False
    assert result.first_broken_event_id == event.id


def test_verify_chain_since_seq_skips_a_break_before_the_checkpoint(
    db: Session, workspace: Workspace, user: User
):
    """A documented historical chain break (see `verify_chain`'s own
    docstring) is permanent in an append-only log — this proves
    `since_seq` is what actually lets a caller get a meaningful "intact
    since we started trusting this" answer instead of a permanent
    false-positive on every future check.
    """
    from app.domain.audit.models import AuditEvent

    _record(db, workspace.id, "test.one")
    _record(db, workspace.id, "test.two")
    db.flush()

    broken = (
        db.query(AuditEvent)
        .filter(AuditEvent.workspace_id == workspace.id)
        .order_by(AuditEvent.seq.asc())
        .limit(1)
        .one()
    )
    broken.payload = {"tampered": True}
    db.flush()

    checkpoint_seq = (
        db.query(AuditEvent)
        .filter(AuditEvent.workspace_id == workspace.id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
        .one()
    ).seq

    from_genesis = verify_audit_chain(since_seq=None, db=db, user=user)
    assert from_genesis.intact is False
    assert from_genesis.first_broken_event_id == broken.id

    since_checkpoint = verify_audit_chain(since_seq=checkpoint_seq, db=db, user=user)
    assert since_checkpoint.intact is True
    assert since_checkpoint.first_broken_event_id is None
    assert since_checkpoint.since_seq == checkpoint_seq


def test_verify_chain_since_seq_still_detects_tampering_after_the_checkpoint(
    db: Session, workspace: Workspace, user: User
):
    from app.domain.audit.models import AuditEvent

    _record(db, workspace.id, "test.checkpoint")
    db.flush()
    checkpoint_seq = (
        db.query(AuditEvent).filter(AuditEvent.workspace_id == workspace.id).one()
    ).seq

    _record(db, workspace.id, "test.after")
    db.flush()

    after = (
        db.query(AuditEvent)
        .filter(AuditEvent.workspace_id == workspace.id, AuditEvent.seq > checkpoint_seq)
        .one()
    )
    after.payload = {"tampered": True}
    db.flush()

    result = verify_audit_chain(since_seq=checkpoint_seq, db=db, user=user)

    assert result.intact is False
    assert result.first_broken_event_id == after.id
