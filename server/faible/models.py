"""Database tables.

Three kinds of table:
- `events`: the append-only, hash-chained log. The source of truth (hard rule 1).
- derived tables (`DERIVED_TABLES`): written only by projectors, rebuilt from the log by
  `faible rebuild`. Every id in them comes from an event payload or is a uuid5 of stable
  content, so a rebuild reproduces them exactly.
- `user_credentials`: password hashes. Deliberately *not* in the log (an append-only log
  can never forget a credential) and not derived, so rebuild leaves it alone.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

UUID_NS = uuid.UUID("7c1f7a3e-4b0e-4f3a-9d0c-2f8e6f1a5b01")


def asset_id_for(sha256: str) -> uuid.UUID:
    """Asset ids are a pure function of content (hard rule 3)."""
    return uuid.uuid5(UUID_NS, f"asset:{sha256}")


def service_account_id_for(
    service: str, workspace_id: str | None, user_id: str | None
) -> uuid.UUID:
    return uuid.uuid5(UUID_NS, f"service_account:{service}:{workspace_id or ''}:{user_id or ''}")


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


# ---------------------------------------------------------------- the log


class Event(Base):
    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    type: Mapped[str] = mapped_column(Text, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_user_id: Mapped[uuid.UUID | None]
    source: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]]
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class UserCredential(Base):
    __tablename__ = "user_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginCode(Base):
    __tablename__ = "login_codes"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- agents


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)  # director | artist | admin
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_shot_id: Mapped[uuid.UUID | None]
    active_shot_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ServiceAccount(Base):
    __tablename__ = "service_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    service_user_id: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- provenance (PROV)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    media_type: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None]
    height: Mapped[int | None]
    duration_ms: Mapped[int | None]
    fps: Mapped[float | None] = mapped_column(Float)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    phash: Mapped[str | None] = mapped_column(Text)  # image pHash hex, or JSON list of frame hashes
    stored: Mapped[bool] = mapped_column(Boolean, default=False)  # local copy in data/assets
    # False while the asset is known only by hash (a version/link/reference named it);
    # True once an asset.registered event described it.
    registered: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[str] = mapped_column(Text)


class AssetLocation(Base):
    """Where an asset has been seen. URLs expire; identity is always the sha256."""

    __tablename__ = "asset_locations"

    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    service: Mapped[str] = mapped_column(Text, primary_key=True)
    url: Mapped[str] = mapped_column(Text, primary_key=True)  # "" when only an id is known
    service_asset_id: Mapped[str | None] = mapped_column(Text)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_alive: Mapped[bool | None]

    __table_args__ = (Index("ix_asset_locations_service_asset", "service", "service_asset_id"),)


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        Text
    )  # generate|upscale|edit|variation|download|upload|import
    service: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # queued|running|succeeded|failed|cancelled
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_job_id: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[dict[str, Any]]
    prompt: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[str | None] = mapped_column(Text)
    credits_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    credits_currency: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    captured_by_user_id: Mapped[uuid.UUID | None]
    service_account_id: Mapped[uuid.UUID | None]
    shot_id: Mapped[uuid.UUID | None]  # active shot at capture time; null = unassigned

    __table_args__ = (UniqueConstraint("service", "service_job_id"),)


class ActivityInput(Base):
    """`Activity -used-> Asset`. `asset_id` stays null until the referenced asset is known."""

    __tablename__ = "activity_inputs"

    activity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activities.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(Text)  # reference|source|mask|init_image|…
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id"))
    sha256: Mapped[str | None] = mapped_column(String(64))
    ref_service: Mapped[str | None] = mapped_column(Text)
    ref_service_asset_id: Mapped[str | None] = mapped_column(Text)
    ref_url: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)


class ActivityOutput(Base):
    """`Asset -generatedBy-> Activity`. `asset_id` stays null until the output's bytes are known."""

    __tablename__ = "activity_outputs"

    activity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activities.id"), primary_key=True)
    index: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id"))
    sha256: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text)
    service_asset_id: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)


class AssetSimilarity(Base):
    """An `inferred` edge between two assets whose bytes differ but whose perceptual
    fingerprints match (spec §2.4). Stored once per pair with `asset_a < asset_b`."""

    __tablename__ = "asset_similarities"

    asset_a: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    asset_b: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    method: Mapped[str] = mapped_column(Text)  # phash | video_phash8
    distance: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[str] = mapped_column(Text, default="inferred")
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID | None]
    service: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    asset_id: Mapped[uuid.UUID | None]
    hash_method: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[str] = mapped_column(Text)


class Link(Base):
    """A human assertion (evidence `asserted`, confidence 1.0) linking an asset to a target."""

    __tablename__ = "links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"))
    target_type: Mapped[str] = mapped_column(Text)  # activity | shot
    target_id: Mapped[uuid.UUID]
    asserted_by_user_id: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[str] = mapped_column(Text, default="asserted")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)


class UnknownPayload(Base):
    __tablename__ = "unknown_payloads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    sample: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- service history and usage


def service_generation_id_for(service: str, service_job_id: str) -> uuid.UUID:
    return uuid.uuid5(UUID_NS, f"service_generation:{service}:{service_job_id}")


class ServiceGeneration(Base):
    """One generation as the service's own history lists it (`service_generation.observed`),
    merged across every observation and import. Not attributed to anyone: the workspace is
    shared, so the observer is recorded, never the author. The same job captured live is an
    `Activity` with the same (service, service_job_id); analytics joins the two."""

    __tablename__ = "service_generations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(Text)
    service_job_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # Faible vocabulary, or "unknown"
    service_status: Mapped[str | None] = mapped_column(Text)  # as the service reported it
    prompt: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[dict[str, Any]]
    inputs: Mapped[list[Any]]
    outputs: Mapped[list[Any]]
    credits_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    credits_currency: Mapped[str] = mapped_column(Text)
    fail_reason: Mapped[str | None] = mapped_column(Text)
    service_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_user_id: Mapped[str | None] = mapped_column(Text)
    service_workspace: Mapped[str | None] = mapped_column(Text)
    # The service login of the browser that saw it (shared subscription), not a person.
    login_email: Mapped[str | None] = mapped_column(Text)
    login_label: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]]
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_count: Mapped[int] = mapped_column(Integer)
    first_observed_by_user_id: Mapped[uuid.UUID | None]
    sources: Mapped[list[Any]]  # response URLs / export files it was seen in (capped)
    evidence: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("service", "service_job_id"),
        Index("ix_service_generations_created", "service_created_at"),
    )


class UsageCapture(Base):
    """One "Save usage page" capture (`usage.page_captured`). The full JSON responses and
    page text stay in the event payload (`event_id`); this row holds what the dashboard shows."""

    __tablename__ = "usage_captures"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # capture_id
    event_id: Mapped[uuid.UUID]
    service: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    captured_by_user_id: Mapped[uuid.UUID | None]
    page_url: Mapped[str] = mapped_column(Text)
    page_title: Mapped[str | None] = mapped_column(Text)
    login_email: Mapped[str | None] = mapped_column(Text)
    login_label: Mapped[str | None] = mapped_column(Text)
    service_user_id: Mapped[str | None] = mapped_column(Text)
    response_count: Mapped[int] = mapped_column(Integer)
    tables: Mapped[list[Any]]
    text_chars: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)  # extension | import
    source_file: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------- production


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Sequence(Base):
    __tablename__ = "sequences"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(Text)
    order: Mapped[int] = mapped_column(Integer)


class Shot(Base):
    __tablename__ = "shots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sequence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sequences.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    code: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text)
    credit_budget: Mapped[Decimal | None] = mapped_column(Numeric)

    __table_args__ = (UniqueConstraint("project_id", "code"),)


class ShotVersion(Base):
    __tablename__ = "shot_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    shot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shots.id"))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    created_by_user_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)  # wip|submitted|approved|rejected
    ever_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (UniqueConstraint("shot_id", "version_number"),)


class Entity(Base):
    """Character / Environment / Prop — one table, discriminated by `entity_type`."""

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    entity_type: Mapped[str] = mapped_column(Text)  # character | environment | prop
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")


class ReferenceAsset(Base):
    __tablename__ = "reference_assets"

    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)


class ShotEntity(Base):
    __tablename__ = "shot_entities"

    shot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shots.id"), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"), primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text)


# Truncated and replayed by `faible rebuild`; checksummed by `faible checksum`.
# Order matters for TRUNCATE … CASCADE-free deletes: children first.
DERIVED_TABLES: list[type[Base]] = [
    ShotEntity,
    ReferenceAsset,
    ShotVersion,
    Link,
    Download,
    ActivityOutput,
    ActivityInput,
    Activity,
    AssetSimilarity,
    AssetLocation,
    Asset,
    Entity,
    Shot,
    Sequence,
    Project,
    ServiceAccount,
    UnknownPayload,
    ServiceGeneration,
    UsageCapture,
    User,
]
