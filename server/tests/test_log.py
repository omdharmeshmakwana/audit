"""The event log: validation, idempotency, hash chaining, append-only, verify-log."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from faible.canonical import GENESIS_HASH, event_hash
from faible.log import EventIn, append_events, verify_log
from faible.models import Event

T0 = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)


def project_event(code: str = "DEMO", event_id: uuid.UUID | None = None) -> EventIn:
    return EventIn(
        event_id=event_id or uuid.uuid4(),
        type="project.created",
        occurred_at=T0,
        evidence="originated",
        payload={"project_id": str(uuid.uuid4()), "name": code, "code": code},
    )


def rows(session: Session) -> list[Event]:
    return list(session.scalars(select(Event).order_by(Event.seq)))


def test_append_chains_hashes(session: Session) -> None:
    results = append_events(session, [project_event("A"), project_event("B")], source="script")
    assert [r.status for r in results] == ["accepted", "accepted"]
    a, b = rows(session)
    assert a.prev_hash == GENESIS_HASH
    assert b.prev_hash == a.hash
    for e in (a, b):
        assert e.hash == event_hash(
            event_id=e.event_id,
            type=e.type,
            occurred_at=e.occurred_at,
            recorded_at=e.recorded_at,
            actor_user_id=e.actor_user_id,
            source=e.source,
            evidence=e.evidence,
            payload=e.payload,
            prev_hash=e.prev_hash,
        )


def test_recorded_at_is_server_time(session: Session) -> None:
    before = datetime.now(UTC)
    append_events(session, [project_event()], source="extension")
    (e,) = rows(session)
    assert e.occurred_at == T0
    assert (
        before - timedelta(seconds=1) <= e.recorded_at <= datetime.now(UTC) + timedelta(seconds=1)
    )


def test_duplicate_event_id_is_idempotent(session: Session) -> None:
    ev = project_event()
    append_events(session, [ev], source="extension")
    (r,) = append_events(session, [ev], source="extension")
    assert r.status == "duplicate"
    assert len(rows(session)) == 1


def test_same_event_id_different_content_is_a_conflict(session: Session) -> None:
    ev = project_event("A")
    append_events(session, [ev], source="extension")
    other = project_event("B", event_id=ev.event_id)
    (r,) = append_events(session, [other], source="extension")
    assert r.status == "conflict"
    assert len(rows(session)) == 1


def test_unknown_type_and_bad_payload_are_rejected_per_item(session: Session) -> None:
    bad_type = EventIn(
        event_id=uuid.uuid4(), type="shot.exploded", occurred_at=T0, evidence="observed", payload={}
    )
    bad_payload = EventIn(
        event_id=uuid.uuid4(),
        type="project.created",
        occurred_at=T0,
        evidence="observed",
        payload={"name": "x"},
    )
    extra_field = project_event()
    extra_field.payload["surprise"] = 1
    good = project_event()
    results = append_events(session, [bad_type, bad_payload, extra_field, good], source="extension")
    assert [r.status for r in results] == ["rejected", "rejected", "rejected", "accepted"]
    assert "unknown event type" in (results[0].reason or "")
    assert len(rows(session)) == 1


def test_naive_occurred_at_is_rejected() -> None:
    with pytest.raises(ValueError):
        EventIn(
            event_id=uuid.uuid4(),
            type="project.created",
            occurred_at=datetime(2026, 1, 1),
            evidence="observed",
            payload={},
        )


def test_events_table_is_append_only(session: Session) -> None:
    append_events(session, [project_event()], source="script")
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text("UPDATE events SET source = 'x'"))
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text("DELETE FROM events"))


def test_verify_log_passes_then_finds_tampering(session: Session) -> None:
    append_events(session, [project_event(c) for c in "ABCD"], source="script")
    assert verify_log(session).ok

    third = rows(session)[2]
    # Deliberate bypass of the append-only trigger (superuser only) to simulate tampering.
    session.execute(text("SET LOCAL session_replication_role = replica"))
    session.execute(
        text("UPDATE events SET payload = jsonb_set(payload, '{name}', '\"EVIL\"') WHERE seq = :s"),
        {"s": third.seq},
    )
    session.execute(text("SET LOCAL session_replication_role = origin"))
    session.expire_all()

    result = verify_log(session)
    assert not result.ok
    assert result.first_broken_seq == third.seq
    assert "hash mismatch" in (result.reason or "")


def test_verify_log_detects_a_removed_event(session: Session) -> None:
    append_events(session, [project_event(c) for c in "ABC"], source="script")
    second_seq = rows(session)[1].seq
    session.execute(text("SET LOCAL session_replication_role = replica"))
    session.execute(text("DELETE FROM events WHERE seq = :s"), {"s": second_seq})
    session.execute(text("SET LOCAL session_replication_role = origin"))
    session.expunge_all()
    result = verify_log(session)
    assert not result.ok
    assert result.first_broken_seq == second_seq + 1
    assert "prev_hash" in (result.reason or "")


def test_floats_and_unicode_survive_the_jsonb_round_trip(session: Session) -> None:
    ev = EventIn(
        event_id=uuid.uuid4(),
        type="generation.requested",
        occurred_at=T0,
        evidence="observed",
        payload={
            "activity_id": str(uuid.uuid4()),
            "service": "higgsfield",
            "prompt": "Kashmiri kahwa teahouse — café, 日本",
            "parameters": {"denoise": 0.2, "sharpen": 1e-7, "scale": 1.0, "big": 2**53},
            "credits_cost": "1.5",
        },
    )
    append_events(session, [ev], source="extension")
    session.expire_all()
    assert verify_log(session).ok
