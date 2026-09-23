"""Projectors: each event type's effect on the derived tables. Written before the projectors."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.log import EventIn, append_events
from faible.models import (
    Activity,
    ActivityInput,
    ActivityOutput,
    Asset,
    AssetLocation,
    Download,
    Entity,
    Link,
    Project,
    ReferenceAsset,
    Sequence,
    ServiceAccount,
    Shot,
    ShotVersion,
    UnknownPayload,
    User,
    asset_id_for,
)

T0 = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
SHA_REF = "a" * 64
SHA_OUT = "b" * 64
SHA_UP = "c" * 64


class Log:
    """Appends events with increasing occurred_at, as one actor."""

    def __init__(self, session: Session, actor: uuid.UUID | None = None) -> None:
        self.session = session
        self.actor = actor
        self.t = T0

    def __call__(self, type_: str, payload: dict[str, Any], evidence: str = "observed") -> None:
        self.t += timedelta(seconds=1)
        (r,) = append_events(
            self.session,
            [
                EventIn(
                    event_id=uuid.uuid4(),
                    type=type_,
                    occurred_at=self.t,
                    evidence=evidence,
                    payload=payload,
                )
            ],  # type: ignore[arg-type]
            source="script",
            actor_user_id=self.actor,
        )
        assert r.status == "accepted", r.reason


def ids(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def production(log: Log) -> dict[str, str]:
    user, project, seq, shot = ids(4)
    log(
        "user.registered",
        {"user_id": user, "email": "artist1@studio", "display_name": "Artist One"},
        "originated",
    )
    log("project.created", {"project_id": project, "name": "Demo", "code": "DEMO"}, "originated")
    log(
        "sequence.created",
        {"sequence_id": seq, "project_id": project, "name": "SQ01", "order": 1},
        "originated",
    )
    log(
        "shot.created",
        {"shot_id": shot, "sequence_id": seq, "code": "SQ01_SH010", "credit_budget": "500"},
        "originated",
    )
    return {"user": user, "project": project, "sequence": seq, "shot": shot}


def test_production_tables(session: Session) -> None:
    log = Log(session)
    p = production(log)
    log("shot.updated", {"shot_id": p["shot"], "changes": {"status": "in_progress"}}, "originated")
    log("shot.active_set", {"user_id": p["user"], "shot_id": p["shot"]}, "observed")
    log("user.logged_in", {"user_id": p["user"], "client": "extension"}, "originated")

    user = session.get_one(User, uuid.UUID(p["user"]))
    assert user.email == "artist1@studio" and user.role == "artist"
    assert user.active_shot_id == uuid.UUID(p["shot"])
    assert user.last_login_at is not None
    assert session.get_one(Project, uuid.UUID(p["project"])).code == "DEMO"
    assert session.get_one(Sequence, uuid.UUID(p["sequence"])).name == "SQ01"
    shot = session.get_one(Shot, uuid.UUID(p["shot"]))
    assert (shot.code, shot.status, shot.credit_budget) == (
        "SQ01_SH010",
        "in_progress",
        Decimal("500"),
    )
    assert shot.project_id == uuid.UUID(p["project"])


def test_entities_and_references(session: Session) -> None:
    log = Log(session)
    p = production(log)
    (mira,) = ids(1)
    log(
        "entity.created",
        {"entity_id": mira, "project_id": p["project"], "entity_type": "character", "name": "Mira"},
    )
    log(
        "asset.registered",
        {"sha256": SHA_REF, "media_type": "image/png", "bytes": 10, "stored": True},
        "originated",
    )
    log("entity.reference_added", {"entity_id": mira, "sha256": SHA_REF, "label": "Mira front"})
    log("shot.entity_added", {"shot_id": p["shot"], "entity_id": mira})

    assert session.get_one(Entity, uuid.UUID(mira)).entity_type == "character"
    ref = session.scalars(select(ReferenceAsset)).one()
    assert (ref.asset_id, ref.label, ref.entity_type) == (
        asset_id_for(SHA_REF),
        "Mira front",
        "character",
    )


def test_generation_lifecycle_resolves_inputs_and_outputs(session: Session) -> None:
    log = Log(session)
    p = production(log)
    log.actor = uuid.UUID(p["user"])
    (act,) = ids(1)
    log(
        "asset.registered",
        {"sha256": SHA_REF, "media_type": "image/png", "bytes": 10, "stored": True},
        "originated",
    )
    log(
        "generation.requested",
        {
            "activity_id": act,
            "service": "higgsfield",
            "kind": "generate",
            "model_id": "nano_banana_flash",
            "service_job_id": "job-1",
            "prompt": "a teahouse in winter",
            "seed": "42",
            "parameters": {"aspect_ratio": "16:9"},
            "inputs": [
                {"role": "reference", "sha256": SHA_REF},
                {"role": "reference", "service_asset_id": "media-9"},
            ],
            "credits_cost": "150",
            "shot_id": p["shot"],
            "service_account": {"workspace_id": "ws-1", "service_user_id": "user_36m"},
        },
        "service_reported",
    )
    log(
        "generation.status",
        {"service": "higgsfield", "service_job_id": "job-1", "status": "running"},
        "service_reported",
    )
    log(
        "generation.completed",
        {
            "activity_id": act,
            "outputs": [{"index": 0, "url": "https://cdn/x.png", "media_type": "image"}],
        },
        "service_reported",
    )

    a = session.get_one(Activity, uuid.UUID(act))
    assert (a.status, a.prompt, a.seed, a.credits_cost) == (
        "succeeded",
        "a teahouse in winter",
        "42",
        Decimal("150"),
    )
    assert a.shot_id == uuid.UUID(p["shot"]) and a.captured_by_user_id == uuid.UUID(p["user"])
    assert a.evidence == "service_reported" and a.ended_at is not None
    assert session.get_one(ServiceAccount, a.service_account_id).workspace_id == "ws-1"

    inputs = list(session.scalars(select(ActivityInput).order_by(ActivityInput.position)))
    assert inputs[0].asset_id == asset_id_for(SHA_REF)  # known sha: resolved immediately
    assert inputs[1].asset_id is None and inputs[1].ref_service_asset_id == "media-9"  # pending
    (out,) = session.scalars(select(ActivityOutput))
    assert out.asset_id is None and out.url == "https://cdn/x.png"  # bytes not known yet

    # The worker hashes the output: the pending output edge resolves by location URL.
    log(
        "asset.registered",
        {
            "sha256": SHA_OUT,
            "media_type": "image/png",
            "locations": [{"service": "higgsfield", "url": "https://cdn/x.png"}],
        },
    )
    session.expire_all()
    (out,) = session.scalars(select(ActivityOutput))
    assert out.asset_id == asset_id_for(SHA_OUT) and out.sha256 == SHA_OUT

    # The uploaded reference gets hashed: the pending input edge resolves by service id.
    log(
        "asset.location_seen",
        {"sha256": SHA_UP, "service": "higgsfield", "service_asset_id": "media-9"},
    )
    session.expire_all()
    inputs = list(session.scalars(select(ActivityInput).order_by(ActivityInput.position)))
    assert inputs[1].asset_id == asset_id_for(SHA_UP)


def test_cross_service_chain_meets_at_the_same_asset(session: Session) -> None:
    """Magnific output → downloaded → uploaded to Higgsfield → used for a video. Both edges
    must land on one asset, because identity is the sha256, not the URL or service id."""
    log = Log(session)
    production(log)
    mag, hf = ids(2)
    log(
        "generation.requested",
        {
            "activity_id": mag,
            "service": "magnific",
            "service_job_id": "iGX0Hoj3uK",
            "prompt": "poster",
        },
        "service_reported",
    )
    log(
        "generation.completed",
        {
            "activity_id": mag,
            "outputs": [{"service_asset_id": "iGX0Hoj3uK", "url": "https://pikaso/1.png"}],
        },
        "service_reported",
    )
    log(
        "asset.registered",
        {
            "sha256": SHA_OUT,
            "locations": [
                {
                    "service": "magnific",
                    "url": "https://pikaso/1.png",
                    "service_asset_id": "iGX0Hoj3uK",
                }
            ],
        },
    )
    # The artist uploads that exact file to Higgsfield; the extension hashed the upload.
    log(
        "asset.location_seen",
        {"sha256": SHA_OUT, "service": "higgsfield", "service_asset_id": "media-42"},
    )
    log(
        "generation.requested",
        {
            "activity_id": hf,
            "service": "higgsfield",
            "kind": "generate",
            "service_job_id": "job-7",
            "prompt": "animate the poster",
            "inputs": [{"role": "image", "service_asset_id": "media-42"}],
        },
        "service_reported",
    )

    (mag_out,) = session.scalars(
        select(ActivityOutput).where(ActivityOutput.activity_id == uuid.UUID(mag))
    )
    (hf_in,) = session.scalars(
        select(ActivityInput).where(ActivityInput.activity_id == uuid.UUID(hf))
    )
    assert mag_out.asset_id == hf_in.asset_id == asset_id_for(SHA_OUT)
    locations = {
        (loc.service, loc.service_asset_id) for loc in session.scalars(select(AssetLocation))
    }
    assert locations == {("magnific", "iGX0Hoj3uK"), ("higgsfield", "media-42")}


def test_failed_generation(session: Session) -> None:
    log = Log(session)
    (act,) = ids(1)
    log(
        "generation.requested",
        {"activity_id": act, "service": "higgsfield", "service_job_id": "j", "credits_cost": "84"},
        "service_reported",
    )
    log(
        "generation.status",
        {"activity_id": act, "status": "failed", "detail": "provider error"},
        "service_reported",
    )
    a = session.get_one(Activity, uuid.UUID(act))
    assert a.status == "failed" and a.ended_at is not None


def test_download_resolves_when_asset_is_registered_later(session: Session) -> None:
    log = Log(session)
    (dl,) = ids(1)
    log(
        "download.observed",
        {"download_id": dl, "filename": "hf_x.png", "sha256": SHA_OUT, "hash_method": "blob"},
    )
    d = session.get_one(Download, uuid.UUID(dl))
    assert d.asset_id is None and d.hash_method == "blob"
    log("asset.registered", {"sha256": SHA_OUT})
    session.expire_all()
    assert session.get_one(Download, uuid.UUID(dl)).asset_id == asset_id_for(SHA_OUT)


def test_unhashed_download_is_kept(session: Session) -> None:
    log = Log(session)
    (dl,) = ids(1)
    log(
        "download.observed",
        {"download_id": dl, "filename": "x.mp4", "url": "blob:https://higgsfield.ai/1"},
    )
    d = session.get_one(Download, uuid.UUID(dl))
    assert d.sha256 is None and d.asset_id is None


def test_shot_versions_and_status(session: Session) -> None:
    log = Log(session)
    p = production(log)
    (v,) = ids(1)
    log("asset.registered", {"sha256": SHA_OUT}, "originated")
    log(
        "shot_version.created",
        {"version_id": v, "shot_id": p["shot"], "sha256": SHA_OUT, "version_number": 1},
        "asserted",
    )
    log("shot_version.status_changed", {"version_id": v, "status": "submitted"}, "asserted")
    log("shot_version.status_changed", {"version_id": v, "status": "approved"}, "asserted")
    sv = session.get_one(ShotVersion, uuid.UUID(v))
    assert (sv.status, sv.ever_submitted, sv.version_number) == ("approved", True, 1)


def test_version_referencing_unknown_sha_creates_an_unregistered_asset(session: Session) -> None:
    log = Log(session)
    p = production(log)
    (v,) = ids(1)
    log(
        "shot_version.created",
        {"version_id": v, "shot_id": p["shot"], "sha256": SHA_UP, "version_number": 1},
        "asserted",
    )
    asset = session.scalars(select(Asset).where(Asset.sha256 == SHA_UP)).one()
    assert asset.registered is False and asset.stored is False
    log(
        "asset.registered",
        {"sha256": SHA_UP, "media_type": "video/mp4", "bytes": 99, "stored": True},
        "originated",
    )
    session.expire_all()
    asset = session.get_one(Asset, asset.id)
    assert (asset.registered, asset.media_type, asset.bytes, asset.evidence) == (
        True,
        "video/mp4",
        99,
        "originated",
    )


def test_link_asserted(session: Session) -> None:
    log = Log(session, actor=uuid.uuid4())
    p = production(log)
    (link,) = ids(1)
    log(
        "link.asserted",
        {
            "link_id": link,
            "sha256": SHA_OUT,
            "target_type": "shot",
            "target_id": p["shot"],
            "reason": "made offline",
        },
        "asserted",
    )
    row = session.get_one(Link, uuid.UUID(link))
    assert (row.evidence, row.confidence, row.asserted_by_user_id) == ("asserted", 1.0, log.actor)


def test_unknown_payload(session: Session) -> None:
    log = Log(session)
    log(
        "adapter.unknown_payload",
        {"service": "magnific", "url": "https://x/api", "method": "POST", "sample": "{}"},
    )
    (u,) = session.scalars(select(UnknownPayload))
    assert u.service == "magnific"
