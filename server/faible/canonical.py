"""Canonical JSON and event hashing (spec §3).

RFC 8785-style: sorted keys, no insignificant whitespace, UTF-8, no NaN/Infinity.
Timestamps are hashed as UTC ISO-8601 with microseconds and a `Z` suffix, which is
exactly what a Postgres `timestamptz` round-trips.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime in the event log")
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def event_hash(
    *,
    event_id: uuid.UUID,
    type: str,
    occurred_at: datetime,
    recorded_at: datetime,
    actor_user_id: uuid.UUID | None,
    source: str,
    evidence: str,
    payload: dict[str, Any],
    prev_hash: str,
) -> str:
    """sha256 of the canonical form of an event row minus `hash` (and minus `seq`, which the
    database assigns; ordering is protected by `prev_hash` instead)."""
    row = {
        "event_id": str(event_id),
        "type": type,
        "occurred_at": iso_utc(occurred_at),
        "recorded_at": iso_utc(recorded_at),
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "source": source,
        "evidence": evidence,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()
