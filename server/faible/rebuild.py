"""`faible rebuild` and `faible checksum` (hard rule 1, definition of done §11).

Rebuild truncates every derived table and replays the whole log through the projectors.
The checksum is a sha256 over every derived row in primary-key order, in canonical form,
so "rebuild reproduces identical derived tables" is a string comparison.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from faible.canonical import canonical_json, iso_utc
from faible.models import DERIVED_TABLES, Event
from faible.projectors import project


def _plain(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        return iso_utc(v)
    if isinstance(v, Decimal):
        return str(v.normalize()) if v == v.to_integral_value() else str(v)
    return v


@dataclass(frozen=True)
class Checksum:
    total: str
    tables: dict[str, tuple[int, str]]  # table → (row count, sha256)


def derived_checksum(session: Session) -> Checksum:
    session.flush()
    tables: dict[str, tuple[int, str]] = {}
    total = hashlib.sha256()
    for model in sorted(DERIVED_TABLES, key=lambda m: m.__tablename__):
        mapper = inspect(model)
        cols = [c.key for c in mapper.column_attrs]
        pk = [getattr(model, str(c.key)) for c in mapper.primary_key]
        h = hashlib.sha256()
        count = 0
        for row in session.execute(select(*[getattr(model, c) for c in cols]).order_by(*pk)):
            h.update(
                canonical_json({c: _plain(v) for c, v in zip(cols, row, strict=True)}).encode()
            )
            h.update(b"\n")
            count += 1
        digest = h.hexdigest()
        tables[model.__tablename__] = (count, digest)
        total.update(f"{model.__tablename__}:{digest}\n".encode())
    return Checksum(total=total.hexdigest(), tables=tables)


def rebuild(session: Session, batch_size: int = 1000) -> int:
    """Replay the log into empty derived tables. Returns the number of events replayed.
    Runs in the caller's transaction: a failed rebuild leaves the old tables intact."""
    names = ", ".join(m.__tablename__ for m in DERIVED_TABLES)
    session.execute(text(f"TRUNCATE {names}"))
    session.expunge_all()
    replayed = 0
    last_seq = 0
    while True:
        batch = list(
            session.scalars(
                select(Event).where(Event.seq > last_seq).order_by(Event.seq).limit(batch_size)
            )
        )
        if not batch:
            break
        for event in batch:
            project(session, event)
            last_seq = event.seq
            replayed += 1
        session.expunge_all()
    session.flush()
    return replayed
