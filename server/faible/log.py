"""The append-only, hash-chained event log (hard rule 1, spec §3).

`append_events` is the only writer. It validates each event against its payload schema,
skips duplicates by `event_id` (idempotent ingest), chains hashes under an advisory lock
(so concurrent appends cannot fork the chain), and runs the projectors for each accepted
event in the same transaction. It never commits: the caller owns the transaction, so an
HTTP batch or a seed script is all-or-nothing at the database level.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from faible.canonical import GENESIS_HASH, canonical_json, event_hash
from faible.events import EVENT_TYPES, Evidence, Source, validate_payload
from faible.models import Event
from faible.projectors import project

# Arbitrary constant: the key of the Postgres advisory lock that serialises appends.
APPEND_LOCK_KEY = 0x7FA1B1E


class EventIn(BaseModel):
    """An event as submitted by a client. `recorded_at` is not accepted: the server sets it."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    type: str
    occurred_at: AwareDatetime
    evidence: Evidence
    payload: dict[str, Any]


@dataclass(frozen=True)
class AppendResult:
    event_id: uuid.UUID
    status: Literal["accepted", "duplicate", "conflict", "rejected"]
    seq: int | None = None
    reason: str | None = None


def _validation_reason(err: ValidationError) -> str:
    first = err.errors()[0]
    loc = ".".join(str(p) for p in first["loc"]) or "payload"
    return f"invalid payload: {loc}: {first['msg']}"


def _normalised(event: Event) -> str:
    """A stored payload as the current schema would normalise it. An approved optional field
    added later (e.g. generation.status.credits_cost) must not turn a re-sent event into a
    conflict. Stored rows are never rewritten; this is only for the comparison."""
    try:
        return canonical_json(validate_payload(event.type, event.payload))
    except (KeyError, ValidationError):
        return canonical_json(event.payload)


def append_events(
    session: Session,
    items: list[EventIn],
    *,
    source: Source,
    actor_user_id: uuid.UUID | None = None,
) -> list[AppendResult]:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": APPEND_LOCK_KEY})
    last = session.execute(
        select(Event.hash).order_by(Event.seq.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = last or GENESIS_HASH

    results: list[AppendResult] = []
    for item in items:
        if item.type not in EVENT_TYPES:
            results.append(
                AppendResult(item.event_id, "rejected", reason=f"unknown event type {item.type!r}")
            )
            continue
        try:
            payload = validate_payload(item.type, item.payload)
        except ValidationError as err:
            results.append(AppendResult(item.event_id, "rejected", reason=_validation_reason(err)))
            continue

        existing = session.scalar(select(Event).where(Event.event_id == item.event_id))
        if existing is not None:
            same = (
                existing.type == item.type
                and _normalised(existing) == canonical_json(payload)
                and existing.occurred_at == item.occurred_at
            )
            results.append(
                AppendResult(
                    item.event_id,
                    "duplicate" if same else "conflict",
                    seq=existing.seq,
                    reason=None if same else "event_id already used for a different event",
                )
            )
            continue

        # Server clock, never the client's (spec §3). Microsecond precision = timestamptz.
        recorded_at = datetime.now(UTC)
        occurred_at = item.occurred_at.astimezone(UTC)
        h = event_hash(
            event_id=item.event_id,
            type=item.type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            actor_user_id=actor_user_id,
            source=source,
            evidence=item.evidence,
            payload=payload,
            prev_hash=prev_hash,
        )
        event = Event(
            event_id=item.event_id,
            type=item.type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            actor_user_id=actor_user_id,
            source=source,
            evidence=item.evidence,
            payload=payload,
            prev_hash=prev_hash,
            hash=h,
        )
        session.add(event)
        session.flush()
        project(session, event)
        prev_hash = h
        results.append(AppendResult(item.event_id, "accepted", seq=event.seq))
    return results


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    first_broken_seq: int | None = None
    reason: str | None = None


def verify_log(session: Session, batch_size: int = 1000) -> VerifyResult:
    """Walk the chain in `seq` order; report the first event whose hash or link is wrong."""
    prev_hash = GENESIS_HASH
    checked = 0
    last_seq = 0
    while True:
        batch = list(
            session.scalars(
                select(Event).where(Event.seq > last_seq).order_by(Event.seq).limit(batch_size)
            )
        )
        if not batch:
            return VerifyResult(ok=True, checked=checked)
        for e in batch:
            if e.prev_hash != prev_hash:
                return VerifyResult(
                    False, checked, e.seq, "prev_hash does not match the previous event's hash"
                )
            recomputed = event_hash(
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
            if recomputed != e.hash:
                return VerifyResult(
                    False, checked, e.seq, "hash mismatch: event content was altered"
                )
            prev_hash = e.hash
            checked += 1
            last_seq = e.seq
