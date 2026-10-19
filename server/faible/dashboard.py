"""Artist-scoped history and all-time studio summaries for directors/admins."""

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faible.models import Activity, Asset, Shot, User
from faible.views import _activity_view

TERMS: list[str] = json.loads(Path(__file__).with_name("prompt_terms.json").read_text())


def warnings(prompt: str) -> list[str]:
    def normal(value: str) -> str:
        return " ".join(
            "".join(
                c for c in value.lower() if c.isascii() and (c.isalnum() or c.isspace())
            ).split()
        )

    value = normal(prompt)
    return [term for term in TERMS if normal(term) in value]


def dashboard_data(
    session: Session,
    user: User,
    artist_id: uuid.UUID | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    manager = user.role in {"admin", "director"}
    if not manager and artist_id and artist_id != user.id:
        raise HTTPException(403, "You can only view your own dashboard")
    users = list(session.scalars(select(User).order_by(User.email))) if manager else [user]
    if artist_id and artist_id not in {artist.id for artist in users}:
        raise HTTPException(404, "Artist not found")
    scope = artist_id if manager else user.id
    names = {artist.id: artist.email for artist in users}
    shots = {shot.id: shot.code for shot in session.scalars(select(Shot))}
    summaries: dict[uuid.UUID, dict[str, Any]] = {}
    for artist in users:
        if scope and artist.id != scope:
            continue
        summaries[artist.id] = {
            "id": artist.id,
            "email": artist.email,
            "display_name": artist.display_name,
            "role": artist.role,
            "active_shot": shots.get(artist.active_shot_id) if artist.active_shot_id else None,
            "active_shot_set_at": artist.active_shot_set_at,
            "last_login_at": artist.last_login_at,
            "last_generation_at": None,
            "generation_count": 0,
            "unknown_cost_count": 0,
            "statuses": {},
            "credits": {},
            "models": {},
            "shots": {},
        }
    # Summaries cover the complete stored history, independently of pagination.
    aggregate = select(
        Activity.captured_by_user_id,
        Activity.service,
        Activity.model_id,
        Activity.shot_id,
        Activity.status,
        Activity.credits_currency,
        func.count(),
        func.coalesce(func.sum(Activity.credits_cost), 0),
        func.count(Activity.credits_cost),
        func.max(Activity.started_at),
    ).group_by(
        Activity.captured_by_user_id,
        Activity.service,
        Activity.model_id,
        Activity.shot_id,
        Activity.status,
        Activity.credits_currency,
    )
    if scope:
        aggregate = aggregate.where(Activity.captured_by_user_id == scope)
    for (
        uid,
        service,
        model,
        shot_id,
        state,
        currency,
        count,
        spend,
        known,
        latest,
    ) in session.execute(aggregate):
        if uid not in summaries:
            continue
        summary = summaries[uid]
        summary["generation_count"] += count
        summary["unknown_cost_count"] += count - known
        summary["statuses"][state] = summary["statuses"].get(state, 0) + count
        summary["credits"][currency] = summary["credits"].get(currency, Decimal(0)) + spend
        if summary["last_generation_at"] is None or latest > summary["last_generation_at"]:
            summary["last_generation_at"] = latest
        buckets: list[tuple[str, object, dict[str, Any]]] = [
            ("models", (service, model), {"service": service, "model_id": model}),
            (
                "shots",
                shot_id,
                {
                    "shot_id": shot_id,
                    "shot_code": shots.get(shot_id, "Unassigned") if shot_id else "Unassigned",
                },
            ),
        ]
        for bucket, key, label in buckets:
            entry = summary[bucket].setdefault(key, {**label, "count": 0, "credits": {}})
            entry["count"] += count
            entry["credits"][currency] = entry["credits"].get(currency, Decimal(0)) + spend
    for summary in summaries.values():
        for bucket in ["models", "shots"]:
            summary[bucket] = sorted(
                summary[bucket].values(), key=lambda entry: entry["count"], reverse=True
            )
            for entry in summary[bucket]:
                entry["credits"] = {
                    currency: str(spend) for currency, spend in entry["credits"].items()
                }
        summary["credits"] = {
            currency: str(spend) for currency, spend in summary["credits"].items()
        }
    query = select(Activity)
    count_query = select(func.count()).select_from(Activity)
    if scope:
        query = query.where(Activity.captured_by_user_id == scope)
        count_query = count_query.where(Activity.captured_by_user_id == scope)
    total = session.scalar(count_query) or 0
    rows = []
    for activity in session.scalars(
        query.order_by(Activity.started_at.desc(), Activity.id.desc()).offset(offset).limit(limit)
    ):
        item = _activity_view(session, activity, names)
        item["email"] = (
            names.get(activity.captured_by_user_id, "Unknown")
            if activity.captured_by_user_id
            else "Unknown"
        )
        item["shot_code"] = (
            shots.get(activity.shot_id, "Unassigned") if activity.shot_id else "Unassigned"
        )
        item["warnings"] = warnings(activity.prompt or "")
        rows.append(item)
    output_ids = {
        output["asset_id"] for row in rows for output in row["outputs"] if output["asset_id"]
    }
    assets = (
        {
            asset.id: asset
            for asset in session.scalars(select(Asset).where(Asset.id.in_(output_ids)))
        }
        if output_ids
        else {}
    )
    for row in rows:
        for output in row["outputs"]:
            asset = assets.get(output["asset_id"])
            output["stored"] = bool(asset and asset.stored)
            if asset:
                output["media_type"] = asset.media_type
    return {
        "is_admin": manager,
        "artists": [{"id": artist.id, "email": artist.email} for artist in users],
        "summaries": list(summaries.values()),
        "activities": rows,
        "history": {
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        },
    }
