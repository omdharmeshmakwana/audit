"""Projectors: event → derived tables (spec §5).

Each projector is a pure function of (current derived state, event): it reads only the
event and the derived tables, takes every id and timestamp from the event (never `now()`,
never a sequence), and writes only derived tables. Replaying the log in `seq` order
therefore rebuilds the derived tables exactly (`faible rebuild`).

Edges between activities and assets are written as soon as they are known and resolved
later: an input may name an asset by service id before its bytes are hashed; an output
may be a URL before the worker hashes it. `_resolve_pending` runs whenever an asset or a
location appears and fills in `asset_id` on any edge that points at it.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from faible.models import (
    Activity,
    ActivityInput,
    ActivityOutput,
    Asset,
    AssetLocation,
    Download,
    Entity,
    Event,
    Link,
    Project,
    ReferenceAsset,
    Sequence,
    ServiceAccount,
    Shot,
    ShotEntity,
    ShotVersion,
    UnknownPayload,
    User,
    asset_id_for,
    service_account_id_for,
)

TERMINAL = {"succeeded", "failed", "cancelled"}

Projector = Callable[[Session, Event], None]


def _uuid(v: Any) -> uuid.UUID:
    return uuid.UUID(str(v))


def _opt_uuid(v: Any) -> uuid.UUID | None:
    return uuid.UUID(str(v)) if v else None


def _dec(v: Any) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


# ---------------------------------------------------------------- assets and resolution


def _ensure_asset(session: Session, sha256: str, at: datetime, evidence: str) -> Asset:
    """An asset known only by its hash (named by a version, link or reference)."""
    asset = session.get(Asset, asset_id_for(sha256))
    if asset is None:
        asset = Asset(
            id=asset_id_for(sha256),
            sha256=sha256,
            stored=False,
            registered=False,
            first_seen_at=at,
            evidence=evidence,
        )
        session.add(asset)
        session.flush()
    return asset


def _add_location(
    session: Session,
    asset: Asset,
    service: str,
    url: str,
    service_asset_id: str | None,
    at: datetime,
    is_alive: bool | None,
) -> None:
    loc = session.get(AssetLocation, (asset.id, service, url))
    if loc is None:
        session.add(
            AssetLocation(
                asset_id=asset.id,
                service=service,
                url=url,
                service_asset_id=service_asset_id,
                seen_at=at,
                is_alive=is_alive,
            )
        )
    else:
        loc.seen_at = max(loc.seen_at, at)
        loc.service_asset_id = loc.service_asset_id or service_asset_id
        if is_alive is not None:
            loc.is_alive = is_alive
    session.flush()


def _resolve_pending(session: Session, asset: Asset) -> None:
    """Point every unresolved edge that names this asset (by hash or by location) at it."""
    locs = list(session.scalars(select(AssetLocation).where(AssetLocation.asset_id == asset.id)))
    loc_urls = [loc.url for loc in locs if loc.url]
    loc_ids = [(loc.service, loc.service_asset_id) for loc in locs if loc.service_asset_id]

    input_match = [ActivityInput.sha256 == asset.sha256]
    if loc_urls:
        input_match.append(ActivityInput.ref_url.in_(loc_urls))
    for service, sid in loc_ids:
        input_match.append(
            (ActivityInput.ref_service == service) & (ActivityInput.ref_service_asset_id == sid)
        )
    session.execute(
        update(ActivityInput)
        .where(ActivityInput.asset_id.is_(None), or_(*input_match))
        .values(asset_id=asset.id, sha256=asset.sha256)
    )

    # Outputs match by location only on the producing activity's own service.
    pending = session.execute(
        select(ActivityOutput, Activity.service)
        .join(Activity, Activity.id == ActivityOutput.activity_id)
        .where(ActivityOutput.asset_id.is_(None))
    ).all()
    for out, service in pending:
        hit = out.sha256 == asset.sha256
        hit = hit or bool(out.url and out.url in loc_urls)
        hit = hit or bool(out.service_asset_id and (service, out.service_asset_id) in loc_ids)
        if hit:
            out.asset_id = asset.id
            out.sha256 = asset.sha256

    session.execute(
        update(Download)
        .where(Download.asset_id.is_(None), Download.sha256 == asset.sha256)
        .values(asset_id=asset.id)
    )
    session.flush()


def _find_asset_for_location(
    session: Session, service: str, url: str | None, service_asset_id: str | None
) -> uuid.UUID | None:
    conds = []
    if url:
        conds.append(AssetLocation.url == url)
    if service_asset_id:
        conds.append(
            (AssetLocation.service == service)
            & (AssetLocation.service_asset_id == service_asset_id)
        )
    if not conds:
        return None
    return session.scalar(
        select(AssetLocation.asset_id).where(or_(*conds)).order_by(AssetLocation.seen_at).limit(1)
    )


def asset_registered(session: Session, e: Event) -> None:
    p = e.payload
    asset = session.get(Asset, asset_id_for(p["sha256"]))
    if asset is None:
        asset = Asset(
            id=asset_id_for(p["sha256"]),
            sha256=p["sha256"],
            first_seen_at=e.occurred_at,
            evidence=e.evidence,
        )
        session.add(asset)
    if not asset.registered:
        # The first registration describes the asset; hash-only mentions before it don't.
        asset.evidence = e.evidence
    asset.registered = True
    for field in ("media_type", "width", "height", "duration_ms", "fps", "bytes", "phash"):
        if p.get(field) is not None:
            setattr(asset, field, p[field])
    asset.stored = asset.stored or bool(p.get("stored"))
    session.flush()
    for loc in p.get("locations", []):
        _add_location(
            session,
            asset,
            loc["service"],
            loc.get("url") or "",
            loc.get("service_asset_id"),
            e.occurred_at,
            None,
        )
    _resolve_pending(session, asset)


def asset_location_seen(session: Session, e: Event) -> None:
    p = e.payload
    asset = _ensure_asset(session, p["sha256"], e.occurred_at, e.evidence)
    _add_location(
        session,
        asset,
        p["service"],
        p.get("url") or "",
        p.get("service_asset_id"),
        e.occurred_at,
        p.get("is_alive"),
    )
    _resolve_pending(session, asset)


# ---------------------------------------------------------------- generations


def _activity_for(session: Session, p: dict[str, Any]) -> Activity | None:
    if p.get("activity_id"):
        return session.get(Activity, _uuid(p["activity_id"]))
    return session.scalar(
        select(Activity).where(
            Activity.service == p["service"], Activity.service_job_id == p["service_job_id"]
        )
    )


def generation_requested(session: Session, e: Event) -> None:
    p = e.payload
    account_id = None
    if p.get("service_account"):
        sa = p["service_account"]
        account_id = service_account_id_for(
            p["service"], sa.get("workspace_id"), sa.get("service_user_id")
        )
        if session.get(ServiceAccount, account_id) is None:
            session.add(
                ServiceAccount(
                    id=account_id,
                    service=p["service"],
                    workspace_id=sa.get("workspace_id"),
                    service_user_id=sa.get("service_user_id"),
                    first_seen_at=e.occurred_at,
                )
            )
    activity = Activity(
        id=_uuid(p["activity_id"]),
        kind=p["kind"],
        service=p["service"],
        model_id=p.get("model_id"),
        model_version=p.get("model_version"),
        status=p["status"],
        started_at=e.occurred_at,
        ended_at=e.occurred_at if p["status"] in TERMINAL else None,
        service_job_id=p.get("service_job_id"),
        parameters=p.get("parameters") or {},
        prompt=p.get("prompt"),
        negative_prompt=p.get("negative_prompt"),
        seed=p.get("seed"),
        credits_cost=_dec(p.get("credits_cost")),
        credits_currency=p.get("credits_currency") or "credits",
        evidence=e.evidence,
        captured_by_user_id=e.actor_user_id,
        service_account_id=account_id,
        shot_id=_opt_uuid(p.get("shot_id")),
    )
    session.add(activity)
    session.flush()
    for pos, inp in enumerate(p.get("inputs", [])):
        asset_id = None
        if inp.get("sha256") and session.get(Asset, asset_id_for(inp["sha256"])) is not None:
            asset_id = asset_id_for(inp["sha256"])
        if asset_id is None:
            asset_id = _find_asset_for_location(
                session, p["service"], inp.get("url"), inp.get("service_asset_id")
            )
        sha = inp.get("sha256")
        if asset_id is not None and sha is None:
            sha = session.get_one(Asset, asset_id).sha256
        session.add(
            ActivityInput(
                activity_id=activity.id,
                position=pos,
                role=inp.get("role") or "reference",
                asset_id=asset_id,
                sha256=sha,
                ref_service=p["service"] if inp.get("service_asset_id") else None,
                ref_service_asset_id=inp.get("service_asset_id"),
                ref_url=inp.get("url"),
                evidence=e.evidence,
                confidence=1.0,
            )
        )
    session.flush()


def generation_status(session: Session, e: Event) -> None:
    activity = _activity_for(session, e.payload)
    if activity is None:
        return  # status for an activity we never saw requested: kept in the log only
    activity.status = e.payload["status"]
    if activity.status in TERMINAL:
        activity.ended_at = e.occurred_at
    if e.payload.get("credits_cost") is not None:
        activity.credits_cost = _dec(e.payload["credits_cost"])
    session.flush()


def generation_completed(session: Session, e: Event) -> None:
    p = e.payload
    activity = _activity_for(session, p)
    if activity is None:
        return
    activity.status = "succeeded"
    activity.ended_at = e.occurred_at
    if p.get("credits_cost") is not None:
        activity.credits_cost = _dec(p["credits_cost"])
    session.flush()
    for out in p.get("outputs", []):
        asset_id = None
        if out.get("sha256") and session.get(Asset, asset_id_for(out["sha256"])) is not None:
            asset_id = asset_id_for(out["sha256"])
        if asset_id is None:
            asset_id = _find_asset_for_location(
                session, activity.service, out.get("url"), out.get("service_asset_id")
            )
        sha = out.get("sha256")
        if asset_id is not None and sha is None:
            sha = session.get_one(Asset, asset_id).sha256
        row = session.get(ActivityOutput, (activity.id, out["index"]))
        if row is None:
            row = ActivityOutput(
                activity_id=activity.id, index=out["index"], evidence=e.evidence, confidence=1.0
            )
            session.add(row)
        row.asset_id = asset_id
        row.sha256 = sha
        row.url = out.get("url")
        row.service_asset_id = out.get("service_asset_id")
        row.media_type = out.get("media_type")
    session.flush()


def download_observed(session: Session, e: Event) -> None:
    p = e.payload
    sha = p.get("sha256")
    asset_id = (
        asset_id_for(sha) if sha and session.get(Asset, asset_id_for(sha)) is not None else None
    )
    session.add(
        Download(
            id=_uuid(p["download_id"]),
            user_id=e.actor_user_id,
            service=p.get("service"),
            filename=p["filename"],
            url=p.get("url"),
            bytes=p.get("bytes"),
            sha256=sha,
            asset_id=asset_id,
            hash_method=p.get("hash_method"),
            occurred_at=e.occurred_at,
            evidence=e.evidence,
        )
    )
    session.flush()


# ---------------------------------------------------------------- production


def user_registered(session: Session, e: Event) -> None:
    p = e.payload
    session.add(
        User(
            id=_uuid(p["user_id"]),
            email=p["email"],
            display_name=p["display_name"],
            role=p["role"],
            created_at=e.occurred_at,
        )
    )
    session.flush()


def user_logged_in(session: Session, e: Event) -> None:
    user = session.get(User, _uuid(e.payload["user_id"]))
    if user is not None:
        user.last_login_at = e.occurred_at
        session.flush()


def project_created(session: Session, e: Event) -> None:
    p = e.payload
    session.add(
        Project(
            id=_uuid(p["project_id"]),
            name=p["name"],
            code=p["code"],
            status=p["status"],
            created_at=e.occurred_at,
        )
    )
    session.flush()


def sequence_created(session: Session, e: Event) -> None:
    p = e.payload
    session.add(
        Sequence(
            id=_uuid(p["sequence_id"]),
            project_id=_uuid(p["project_id"]),
            name=p["name"],
            order=p["order"],
        )
    )
    session.flush()


def shot_created(session: Session, e: Event) -> None:
    p = e.payload
    seq = session.get_one(Sequence, _uuid(p["sequence_id"]))
    session.add(
        Shot(
            id=_uuid(p["shot_id"]),
            sequence_id=seq.id,
            project_id=seq.project_id,
            code=p["code"],
            description=p.get("description") or "",
            status=p["status"],
            credit_budget=_dec(p.get("credit_budget")),
        )
    )
    session.flush()


def shot_updated(session: Session, e: Event) -> None:
    shot = session.get_one(Shot, _uuid(e.payload["shot_id"]))
    changes = e.payload["changes"]
    if changes.get("description") is not None:
        shot.description = changes["description"]
    if changes.get("status") is not None:
        shot.status = changes["status"]
    if changes.get("credit_budget") is not None:
        shot.credit_budget = _dec(changes["credit_budget"])
    session.flush()


def shot_active_set(session: Session, e: Event) -> None:
    user = session.get(User, _uuid(e.payload["user_id"]))
    if user is not None:
        user.active_shot_id = _opt_uuid(e.payload.get("shot_id"))
        user.active_shot_set_at = e.occurred_at
        session.flush()


def entity_created(session: Session, e: Event) -> None:
    p = e.payload
    session.add(
        Entity(
            id=_uuid(p["entity_id"]),
            project_id=_uuid(p["project_id"]),
            entity_type=p["entity_type"],
            name=p["name"],
            description=p.get("description") or "",
        )
    )
    session.flush()


def entity_reference_added(session: Session, e: Event) -> None:
    p = e.payload
    entity = session.get_one(Entity, _uuid(p["entity_id"]))
    asset = _ensure_asset(session, p["sha256"], e.occurred_at, e.evidence)
    session.merge(
        ReferenceAsset(
            entity_id=entity.id, asset_id=asset.id, entity_type=entity.entity_type, label=p["label"]
        )
    )
    session.flush()


def shot_entity_added(session: Session, e: Event) -> None:
    entity = session.get_one(Entity, _uuid(e.payload["entity_id"]))
    session.merge(
        ShotEntity(
            shot_id=_uuid(e.payload["shot_id"]), entity_id=entity.id, entity_type=entity.entity_type
        )
    )
    session.flush()


def shot_version_created(session: Session, e: Event) -> None:
    p = e.payload
    asset = _ensure_asset(session, p["sha256"], e.occurred_at, e.evidence)
    session.add(
        ShotVersion(
            id=_uuid(p["version_id"]),
            shot_id=_uuid(p["shot_id"]),
            asset_id=asset.id,
            version_number=p["version_number"],
            created_by_user_id=e.actor_user_id,
            created_at=e.occurred_at,
            status=p["status"],
            ever_submitted=p["status"] == "submitted",
            notes=p.get("notes") or "",
        )
    )
    session.flush()


def shot_version_status_changed(session: Session, e: Event) -> None:
    version = session.get_one(ShotVersion, _uuid(e.payload["version_id"]))
    version.status = e.payload["status"]
    version.ever_submitted = version.ever_submitted or version.status == "submitted"
    session.flush()


def link_asserted(session: Session, e: Event) -> None:
    p = e.payload
    asset = _ensure_asset(session, p["sha256"], e.occurred_at, e.evidence)
    session.add(
        Link(
            id=_uuid(p["link_id"]),
            asset_id=asset.id,
            target_type=p["target_type"],
            target_id=_uuid(p["target_id"]),
            asserted_by_user_id=e.actor_user_id,
            reason=p.get("reason"),
            occurred_at=e.occurred_at,
            evidence="asserted",
            confidence=1.0,
        )
    )
    session.flush()
    if p["target_type"] == "shot":
        # "Assign to shot": the generations that produced this asset now belong to the shot.
        producing = select(ActivityOutput.activity_id).where(ActivityOutput.asset_id == asset.id)
        session.execute(
            update(Activity).where(Activity.id.in_(producing)).values(shot_id=_uuid(p["target_id"]))
        )
        session.flush()


def adapter_unknown_payload(session: Session, e: Event) -> None:
    p = e.payload
    session.add(
        UnknownPayload(
            id=e.event_id,
            service=p["service"],
            method=p.get("method"),
            url=p["url"],
            sample=p["sample"],
            occurred_at=e.occurred_at,
        )
    )
    session.flush()


def _noop(session: Session, e: Event) -> None:
    """Event types whose derived tables arrive in a later phase (audit flags: Phase 4)."""


PROJECTORS: dict[str, Projector] = {
    "user.registered": user_registered,
    "user.logged_in": user_logged_in,
    "project.created": project_created,
    "sequence.created": sequence_created,
    "shot.created": shot_created,
    "shot.updated": shot_updated,
    "shot.active_set": shot_active_set,
    "entity.created": entity_created,
    "entity.reference_added": entity_reference_added,
    "shot.entity_added": shot_entity_added,
    "generation.requested": generation_requested,
    "generation.status": generation_status,
    "generation.completed": generation_completed,
    "asset.registered": asset_registered,
    "asset.location_seen": asset_location_seen,
    "download.observed": download_observed,
    "shot_version.created": shot_version_created,
    "shot_version.status_changed": shot_version_status_changed,
    "link.asserted": link_asserted,
    "audit.flag_raised": _noop,
    "audit.flag_dismissed": _noop,
    "adapter.unknown_payload": adapter_unknown_payload,
}


def project(session: Session, event: Event) -> None:
    PROJECTORS[event.type](session, event)
