"""Read models over the derived tables, for the API (and later the web UI)."""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faible.models import (
    Activity,
    ActivityInput,
    ActivityOutput,
    Asset,
    Entity,
    Link,
    Project,
    Sequence,
    Shot,
    ShotVersion,
    User,
)


def _num(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


def project_tree(session: Session, project_id: uuid.UUID) -> dict[str, Any] | None:
    project = session.get(Project, project_id)
    if project is None:
        return None
    sequences = session.scalars(
        select(Sequence).where(Sequence.project_id == project_id).order_by(Sequence.order)
    )
    shots_by_seq: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for shot in session.scalars(
        select(Shot).where(Shot.project_id == project_id).order_by(Shot.code)
    ):
        shots_by_seq.setdefault(shot.sequence_id, []).append(
            {
                "id": shot.id,
                "code": shot.code,
                "description": shot.description,
                "status": shot.status,
            }
        )
    entities = session.scalars(
        select(Entity).where(Entity.project_id == project_id).order_by(Entity.name)
    )
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "code": project.code,
            "status": project.status,
        },
        "sequences": [
            {"id": s.id, "name": s.name, "order": s.order, "shots": shots_by_seq.get(s.id, [])}
            for s in sequences
        ],
        "entities": [{"id": e.id, "entity_type": e.entity_type, "name": e.name} for e in entities],
    }


def _activity_view(session: Session, a: Activity, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    inputs = session.scalars(
        select(ActivityInput)
        .where(ActivityInput.activity_id == a.id)
        .order_by(ActivityInput.position)
    )
    outputs = session.scalars(
        select(ActivityOutput)
        .where(ActivityOutput.activity_id == a.id)
        .order_by(ActivityOutput.index)
    )
    return {
        "id": a.id,
        "kind": a.kind,
        "service": a.service,
        "model_id": a.model_id,
        "status": a.status,
        "started_at": a.started_at,
        "ended_at": a.ended_at,
        "service_job_id": a.service_job_id,
        "prompt": a.prompt,
        "negative_prompt": a.negative_prompt,
        "seed": a.seed,
        "parameters": a.parameters,
        "credits_cost": _num(a.credits_cost),
        "credits_currency": a.credits_currency,
        "evidence": a.evidence,
        "captured_by": names.get(a.captured_by_user_id) if a.captured_by_user_id else None,
        "shot_id": a.shot_id,
        "inputs": [
            {
                "role": i.role,
                "sha256": i.sha256,
                "asset_id": i.asset_id,
                "service_asset_id": i.ref_service_asset_id,
                "resolved": i.asset_id is not None,
                "evidence": i.evidence,
            }
            for i in inputs
        ],
        "outputs": [
            {
                "index": o.index,
                "sha256": o.sha256,
                "asset_id": o.asset_id,
                "service_asset_id": o.service_asset_id,
                "media_type": o.media_type,
                "resolved": o.asset_id is not None,
                "evidence": o.evidence,
            }
            for o in outputs
        ],
    }


def _user_names(session: Session) -> dict[uuid.UUID, str]:
    return {u.id: u.display_name for u in session.scalars(select(User))}


def shot_detail(session: Session, shot_id: uuid.UUID) -> dict[str, Any] | None:
    shot = session.get(Shot, shot_id)
    if shot is None:
        return None
    seq = session.get_one(Sequence, shot.sequence_id)
    project = session.get_one(Project, shot.project_id)
    names = _user_names(session)
    versions = session.execute(
        select(ShotVersion, Asset.sha256)
        .join(Asset, Asset.id == ShotVersion.asset_id)
        .where(ShotVersion.shot_id == shot_id)
        .order_by(ShotVersion.version_number)
    ).all()
    activities = list(
        session.scalars(
            select(Activity).where(Activity.shot_id == shot_id).order_by(Activity.started_at)
        )
    )
    spend = sum((a.credits_cost or Decimal(0) for a in activities), Decimal(0))
    links = session.execute(
        select(Link, Asset.sha256)
        .join(Asset, Asset.id == Link.asset_id)
        .where(Link.target_type == "shot", Link.target_id == shot_id)
        .order_by(Link.occurred_at)
    ).all()
    return {
        "shot": {
            "id": shot.id,
            "code": shot.code,
            "description": shot.description,
            "status": shot.status,
            "credit_budget": _num(shot.credit_budget),
            "sequence": seq.name,
            "project": project.code,
        },
        "credit_spend": _num(spend),
        "versions": [
            {
                "id": v.id,
                "version_number": v.version_number,
                "status": v.status,
                "sha256": sha,
                "asset_id": v.asset_id,
                "created_by": names.get(v.created_by_user_id) if v.created_by_user_id else None,
                "created_at": v.created_at,
                "notes": v.notes,
            }
            for v, sha in versions
        ],
        "activities": [_activity_view(session, a, names) for a in activities],
        "links": [
            {
                "sha256": sha,
                "asserted_by": names.get(link.asserted_by_user_id)
                if link.asserted_by_user_id
                else None,
                "reason": link.reason,
            }
            for link, sha in links
        ],
    }


def my_captures(session: Session, user_id: uuid.UUID, limit: int = 10) -> dict[str, Any]:
    recent = list(
        session.scalars(
            select(Activity)
            .where(Activity.captured_by_user_id == user_id)
            .order_by(Activity.started_at.desc())
            .limit(limit)
        )
    )
    shot_codes = {s.id: s.code for s in session.scalars(select(Shot))}
    unassigned = session.scalar(
        select(func.count())
        .select_from(Activity)
        .where(Activity.captured_by_user_id == user_id, Activity.shot_id.is_(None))
    )
    items = []
    for a in recent:
        first_out = session.scalar(
            select(ActivityOutput.sha256)
            .where(ActivityOutput.activity_id == a.id, ActivityOutput.sha256.is_not(None))
            .order_by(ActivityOutput.index)
            .limit(1)
        )
        items.append(
            {
                "id": a.id,
                "service": a.service,
                "kind": a.kind,
                "model_id": a.model_id,
                "status": a.status,
                "prompt": (a.prompt or "")[:140],
                "started_at": a.started_at,
                "credits_cost": _num(a.credits_cost),
                "shot_id": a.shot_id,
                "shot_code": shot_codes.get(a.shot_id) if a.shot_id else None,
                "output_sha256": first_out,
            }
        )
    return {"unassigned_count": unassigned or 0, "recent": items}
