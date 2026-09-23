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
    AssetLocation,
    AssetSimilarity,
    Download,
    Entity,
    Link,
    Project,
    ReferenceAsset,
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


def asset_detail(session: Session, asset_id: uuid.UUID) -> dict[str, Any] | None:
    """Metadata, locations, producing activities, similar assets, downloads, versions, links."""
    a = session.get(Asset, asset_id)
    if a is None:
        return None
    names = _user_names(session)
    locations = session.scalars(
        select(AssetLocation)
        .where(AssetLocation.asset_id == asset_id)
        .order_by(AssetLocation.seen_at)
    )
    producers = session.scalars(
        select(Activity)
        .join(ActivityOutput, ActivityOutput.activity_id == Activity.id)
        .where(ActivityOutput.asset_id == asset_id)
        .order_by(Activity.started_at)
    )
    consumers = session.scalars(
        select(Activity)
        .join(ActivityInput, ActivityInput.activity_id == Activity.id)
        .where(ActivityInput.asset_id == asset_id)
        .order_by(Activity.started_at)
    )
    sims = session.scalars(
        select(AssetSimilarity)
        .where((AssetSimilarity.asset_a == asset_id) | (AssetSimilarity.asset_b == asset_id))
        .order_by(AssetSimilarity.confidence.desc())
        .limit(10)
    )
    similar = []
    for s in sims:
        other = session.get_one(Asset, s.asset_b if s.asset_a == asset_id else s.asset_a)
        similar.append(
            {
                "asset_id": other.id,
                "sha256": other.sha256,
                "media_type": other.media_type,
                "stored": other.stored,
                "distance": s.distance,
                "confidence": s.confidence,
                "method": s.method,
                "evidence": s.evidence,
            }
        )
    shot_codes = {sh.id: sh.code for sh in session.scalars(select(Shot))}
    label = session.scalar(select(ReferenceAsset.label).where(ReferenceAsset.asset_id == asset_id))

    def act(x: Activity) -> dict[str, Any]:
        return {
            "id": x.id,
            "service": x.service,
            "kind": x.kind,
            "model_id": x.model_id,
            "status": x.status,
            "prompt": x.prompt,
            "started_at": x.started_at,
            "credits_cost": _num(x.credits_cost),
            "evidence": x.evidence,
            "captured_by": names.get(x.captured_by_user_id) if x.captured_by_user_id else None,
            "shot_code": shot_codes.get(x.shot_id) if x.shot_id else None,
        }

    return {
        "asset": {
            "id": a.id,
            "sha256": a.sha256,
            "media_type": a.media_type,
            "width": a.width,
            "height": a.height,
            "duration_ms": a.duration_ms,
            "fps": a.fps,
            "bytes": a.bytes,
            "phash": a.phash,
            "stored": a.stored,
            "registered": a.registered,
            "evidence": a.evidence,
            "first_seen_at": a.first_seen_at,
            "label": label,
        },
        "locations": [
            {
                "service": loc.service,
                "url": loc.url,
                "service_asset_id": loc.service_asset_id,
                "seen_at": loc.seen_at,
                "is_alive": loc.is_alive,
            }
            for loc in locations
        ],
        "produced_by": [act(x) for x in producers],
        "used_by": [act(x) for x in consumers],
        "similar": similar,
        "downloads": [
            {
                "filename": d.filename,
                "by": names.get(d.user_id) if d.user_id else None,
                "at": d.occurred_at,
                "hash_method": d.hash_method,
            }
            for d in session.scalars(select(Download).where(Download.asset_id == asset_id))
        ],
        "versions": [
            {
                "shot_code": shot_codes.get(v.shot_id),
                "shot_id": v.shot_id,
                "version_number": v.version_number,
                "status": v.status,
            }
            for v in session.scalars(select(ShotVersion).where(ShotVersion.asset_id == asset_id))
        ],
        "links": [
            {
                "target_type": link.target_type,
                "target_id": link.target_id,
                "target_label": shot_codes.get(link.target_id)
                if link.target_type == "shot"
                else None,
                "by": names.get(link.asserted_by_user_id) if link.asserted_by_user_id else None,
                "reason": link.reason,
                "at": link.occurred_at,
            }
            for link in session.scalars(
                select(Link).where(Link.asset_id == asset_id).order_by(Link.occurred_at)
            )
        ],
    }


def graph(
    session: Session, project_id: uuid.UUID, limit: int = 200, offset: int = 0
) -> dict[str, Any]:
    """PROV + production graph for a project (spec §5), paginated by activity.

    Nodes: activity, asset, shot. Edges: `used` (activity→asset), `generatedBy`
    (asset→activity), `inShot` (activity→shot), `versionOf` (asset→shot) and `similar`
    (asset~asset, inferred). Every edge carries evidence and confidence.
    """
    shots = {s.id: s for s in session.scalars(select(Shot).where(Shot.project_id == project_id))}
    total = (
        session.scalar(
            select(func.count()).select_from(Activity).where(Activity.shot_id.in_(list(shots)))
        )
        or 0
    )
    acts = list(
        session.scalars(
            select(Activity)
            .where(Activity.shot_id.in_(list(shots)))
            .order_by(Activity.started_at, Activity.id)
            .limit(limit)
            .offset(offset)
        )
    )
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    asset_ids: set[uuid.UUID] = set()
    for sh in shots.values():
        nodes[f"shot:{sh.id}"] = {"id": f"shot:{sh.id}", "type": "shot", "label": sh.code}
    for a in acts:
        nid = f"activity:{a.id}"
        nodes[nid] = {
            "id": nid,
            "type": "activity",
            "label": f"{a.service} {a.kind} {a.model_id or ''}".strip(),
            "prompt": a.prompt,
            "status": a.status,
            "at": a.started_at,
            "evidence": a.evidence,
        }
        if a.shot_id:
            edges.append(
                {
                    "from": nid,
                    "to": f"shot:{a.shot_id}",
                    "type": "inShot",
                    "evidence": a.evidence,
                    "confidence": 1.0,
                }
            )
        for i in session.scalars(select(ActivityInput).where(ActivityInput.activity_id == a.id)):
            if i.asset_id:
                asset_ids.add(i.asset_id)
                edges.append(
                    {
                        "from": nid,
                        "to": f"asset:{i.asset_id}",
                        "type": "used",
                        "role": i.role,
                        "evidence": i.evidence,
                        "confidence": i.confidence,
                    }
                )
        for o in session.scalars(select(ActivityOutput).where(ActivityOutput.activity_id == a.id)):
            if o.asset_id:
                asset_ids.add(o.asset_id)
                edges.append(
                    {
                        "from": f"asset:{o.asset_id}",
                        "to": nid,
                        "type": "generatedBy",
                        "evidence": o.evidence,
                        "confidence": o.confidence,
                    }
                )
    for v in session.scalars(select(ShotVersion).where(ShotVersion.shot_id.in_(list(shots)))):
        asset_ids.add(v.asset_id)
        edges.append(
            {
                "from": f"asset:{v.asset_id}",
                "to": f"shot:{v.shot_id}",
                "type": "versionOf",
                "version": v.version_number,
                "evidence": "asserted",
                "confidence": 1.0,
            }
        )
    for aid in asset_ids:
        row = session.get_one(Asset, aid)
        nodes[f"asset:{aid}"] = {
            "id": f"asset:{aid}",
            "type": "asset",
            "label": row.sha256[:12],
            "sha256": row.sha256,
            "media_type": row.media_type,
            "evidence": row.evidence,
        }
    if asset_ids:
        ids = list(asset_ids)
        for s in session.scalars(
            select(AssetSimilarity).where(
                AssetSimilarity.asset_a.in_(ids), AssetSimilarity.asset_b.in_(ids)
            )
        ):
            edges.append(
                {
                    "from": f"asset:{s.asset_a}",
                    "to": f"asset:{s.asset_b}",
                    "type": "similar",
                    "method": s.method,
                    "distance": s.distance,
                    "evidence": s.evidence,
                    "confidence": s.confidence,
                }
            )
    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "total_activities": total,
        "limit": limit,
        "offset": offset,
    }
