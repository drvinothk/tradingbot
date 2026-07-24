from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.audit.models import ActorType, AuditEvent, EventCategory
from app.domain.identity.models import User
from app.modules.audit_service.service import record_event, verify_chain


def test_chain_is_valid_after_several_events(db: Session, user: User):
    for i in range(5):
        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.AUTH,
            event_type=f"test.event.{i}",
        )
    ok, broken_id = verify_chain(db)
    assert ok, f"chain broken at {broken_id}"


def test_first_event_has_no_prev_hash(db: Session, user: User):
    event = record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.SYSTEM_HEALTH,
        event_type="test.first_event",
    )
    assert event.prev_hash is None


def test_each_event_links_to_the_previous_hash(db: Session, user: User):
    first = record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.SYSTEM_HEALTH,
        event_type="test.first",
    )
    second = record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.SYSTEM_HEALTH,
        event_type="test.second",
    )
    assert second.prev_hash == first.hash


def test_tampering_with_payload_is_detected(db: Session, user: User):
    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.AUTH,
        event_type="test.tamper_target",
        payload={"original": True},
    )
    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.AUTH,
        event_type="test.after_tamper_target",
    )

    tampered = (
        db.query(AuditEvent).filter(AuditEvent.event_type == "test.tamper_target").one()
    )
    tampered.payload = {"original": False}
    db.flush()

    ok, broken_id = verify_chain(db)
    assert not ok
    assert broken_id == tampered.id
