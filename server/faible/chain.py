"""Prompt chains (spec §2.3): the ordered activities that led to an asset.

Walk backwards from the asset: its producing activity (an `ActivityOutput` edge, or a human
`link.asserted` to an activity), then that activity's inputs, recursively. When an asset has
no producer, follow its best perceptual match (an `inferred` edge) to an *older* asset —
preferring one that has a producer, then the smallest distance — so a re-encoded upload
still connects to the generation it came from. Otherwise the asset is an origin (a reference
image, an upload, something made outside the services).

Each step carries its evidence and confidence; a chain is only as strong as its weakest
edge, and the UI shows that edge. Computed on demand from the derived tables.
"""

import difflib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from faible.models import (
    Activity,
    ActivityInput,
    ActivityOutput,
    Asset,
    AssetSimilarity,
    Link,
    ReferenceAsset,
    User,
)
from faible.views import _num

MAX_STEPS = 200


def word_diff(before: str | None, after: str | None) -> list[dict[str, str]]:
    """Word-level diff: [{op: equal|insert|delete, text}], whitespace preserved."""
    a = re.findall(r"\S+|\s+", before or "")
    b = re.findall(r"\S+|\s+", after or "")
    out: list[dict[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append({"op": "equal", "text": "".join(a[i1:i2])})
        else:
            if i2 > i1:
                out.append({"op": "delete", "text": "".join(a[i1:i2])})
            if j2 > j1:
                out.append({"op": "insert", "text": "".join(b[j1:j2])})
    return out


def asset_view(a: Asset, label: str | None = None) -> dict[str, Any]:
    return {
        "id": a.id,
        "sha256": a.sha256,
        "media_type": a.media_type,
        "width": a.width,
        "height": a.height,
        "duration_ms": a.duration_ms,
        "stored": a.stored,
        "registered": a.registered,
        "evidence": a.evidence,
        "first_seen_at": a.first_seen_at,
        "label": label,
    }


@dataclass
class _Walk:
    session: Session
    names: dict[uuid.UUID, str]
    labels: dict[uuid.UUID, str]
    activities: dict[uuid.UUID, Activity] = field(default_factory=dict)
    matches: list[dict[str, Any]] = field(default_factory=list)
    origins: dict[uuid.UUID, Asset] = field(default_factory=dict)
    seen: set[uuid.UUID] = field(default_factory=set)

    def producers(self, asset_id: uuid.UUID) -> list[Activity]:
        via_output = select(ActivityOutput.activity_id).where(ActivityOutput.asset_id == asset_id)
        via_link = select(Link.target_id).where(
            Link.asset_id == asset_id, Link.target_type == "activity"
        )
        return list(
            self.session.scalars(
                select(Activity)
                .where(or_(Activity.id.in_(via_output), Activity.id.in_(via_link)))
                .order_by(Activity.started_at)
            )
        )

    def best_match(self, asset: Asset) -> tuple[Asset, AssetSimilarity] | None:
        sims = self.session.scalars(
            select(AssetSimilarity).where(
                or_(AssetSimilarity.asset_a == asset.id, AssetSimilarity.asset_b == asset.id)
            )
        )
        candidates = []
        for s in sims:
            other = self.session.get_one(Asset, s.asset_b if s.asset_a == asset.id else s.asset_a)
            if other.id in self.seen or other.first_seen_at > asset.first_seen_at:
                continue  # matches point back in time only
            has_producer = bool(self.producers(other.id))
            candidates.append(
                (not has_producer, s.distance, other.first_seen_at, str(other.id), other, s)
            )
        if not candidates:
            return None
        best = min(candidates, key=lambda c: c[:4])
        return best[4], best[5]

    def visit(self, asset_id: uuid.UUID) -> None:
        stack = [asset_id]
        while stack and len(self.activities) < MAX_STEPS:
            aid = stack.pop()
            if aid in self.seen:
                continue
            self.seen.add(aid)
            asset = self.session.get(Asset, aid)
            if asset is None:
                continue
            producers = self.producers(aid)
            if producers:
                for act in producers:
                    if act.id in self.activities:
                        continue
                    self.activities[act.id] = act
                    for inp in self.session.scalars(
                        select(ActivityInput).where(ActivityInput.activity_id == act.id)
                    ):
                        if inp.asset_id:
                            stack.append(inp.asset_id)
                continue
            found = self.best_match(asset)
            if found:
                source, sim = found
                self.matches.append(
                    {
                        "type": "match",
                        "at": asset.first_seen_at,
                        "derived": asset_view(asset, self.labels.get(asset.id)),
                        "source": asset_view(source, self.labels.get(source.id)),
                        "method": sim.method,
                        "distance": sim.distance,
                        "confidence": sim.confidence,
                        "evidence": sim.evidence,
                    }
                )
                stack.append(source.id)
            else:
                self.origins[aid] = asset


def _activity_step(
    session: Session, a: Activity, names: dict[uuid.UUID, str], labels: dict[uuid.UUID, str]
) -> dict[str, Any]:
    def edge(asset_id: uuid.UUID | None) -> dict[str, Any] | None:
        if asset_id is None:
            return None
        asset = session.get(Asset, asset_id)
        return asset_view(asset, labels.get(asset_id)) if asset else None

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
        "type": "activity",
        "at": a.started_at,
        "id": a.id,
        "kind": a.kind,
        "service": a.service,
        "model_id": a.model_id,
        "status": a.status,
        "prompt": a.prompt,
        "negative_prompt": a.negative_prompt,
        "seed": a.seed,
        "parameters": a.parameters,
        "credits_cost": _num(a.credits_cost),
        "credits_currency": a.credits_currency,
        "evidence": a.evidence,
        "captured_by": names.get(a.captured_by_user_id) if a.captured_by_user_id else None,
        "shot_id": a.shot_id,
        "service_job_id": a.service_job_id,
        "inputs": [
            {
                "role": i.role,
                "asset": edge(i.asset_id),
                "service_asset_id": i.ref_service_asset_id,
                "evidence": i.evidence,
                "confidence": i.confidence,
            }
            for i in inputs
        ],
        "outputs": [
            {
                "index": o.index,
                "asset": edge(o.asset_id),
                "service_asset_id": o.service_asset_id,
                "evidence": o.evidence,
                "confidence": o.confidence,
            }
            for o in outputs
        ],
    }


def _context(session: Session) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    names = {u.id: u.display_name for u in session.scalars(select(User))}
    labels = {r.asset_id: r.label for r in session.scalars(select(ReferenceAsset))}
    return names, labels


def _assemble(session: Session, walk: _Walk) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [
        _activity_step(session, a, walk.names, walk.labels) for a in walk.activities.values()
    ]
    steps += walk.matches
    steps.sort(key=lambda s: (s["at"], 0 if s["type"] == "match" else 1))
    previous_prompt: str | None = None
    weakest = 1.0
    for s in steps:
        if s["type"] == "activity":
            if s["prompt"]:
                s["prompt_diff"] = (
                    word_diff(previous_prompt, s["prompt"]) if previous_prompt else None
                )
                previous_prompt = s["prompt"]
            unresolved = [i for i in s["inputs"] if i["asset"] is None]
            s["unresolved_inputs"] = len(unresolved)
        else:
            weakest = min(weakest, s["confidence"])
    return {
        "steps": steps,
        "origins": [asset_view(o, walk.labels.get(o.id)) for o in walk.origins.values()],
        "weakest_confidence": weakest,
        "truncated": len(walk.activities) >= MAX_STEPS,
    }


def asset_chain(session: Session, asset_id: uuid.UUID) -> dict[str, Any] | None:
    asset = session.get(Asset, asset_id)
    if asset is None:
        return None
    names, labels = _context(session)
    walk = _Walk(session, names, labels)
    walk.visit(asset_id)
    return {"asset": asset_view(asset, labels.get(asset_id)), **_assemble(session, walk)}


def union_chain(session: Session, asset_ids: list[uuid.UUID]) -> dict[str, Any]:
    """A shot's chain view: the union of the chains of all its versions (spec §2.3)."""
    names, labels = _context(session)
    walk = _Walk(session, names, labels)
    for aid in asset_ids:
        walk.visit(aid)
    return _assemble(session, walk)
