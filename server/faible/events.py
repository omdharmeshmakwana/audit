"""Event types and payload schemas (spec §3). docs/EVENTS.md is generated from this module
(`faible events-doc`) and a test fails if the two drift.

Rules:
- Payloads are strict (`extra="forbid"`): an unknown field is a rejected event, not a
  silently dropped one.
- Every id a derived table will use is carried in the payload, so projections are a pure
  function of the log and `rebuild` is exact.
- Assets are referenced by sha256 (hard rule 3). URLs and service ids are locations.
"""

import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Evidence = Literal["originated", "service_reported", "observed", "inferred", "asserted"]
Source = Literal["extension", "server", "script", "import"]
Sha256 = str  # validated by pattern on each field
SHA = Field(pattern=r"^[0-9a-f]{64}$")
SHA_OPT = Field(default=None, pattern=r"^[0-9a-f]{64}$")

ActivityKind = Literal["generate", "upscale", "edit", "variation", "download", "upload", "import"]
ActivityStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
EntityType = Literal["character", "environment", "prop"]
VersionStatus = Literal["wip", "submitted", "approved", "rejected"]
Severity = Literal["info", "warn", "error"]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- users


class UserRegistered(Payload):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Literal["director", "artist", "admin"] = "artist"


class UserRoleChanged(Payload):
    user_id: uuid.UUID
    role: Literal["director", "artist", "admin"]


class UserLoggedIn(Payload):
    user_id: uuid.UUID
    client: Literal["web", "extension", "cli"]


# ---------------------------------------------------------------- production


class ProjectCreated(Payload):
    project_id: uuid.UUID
    name: str
    code: str
    status: str = "active"


class SequenceCreated(Payload):
    sequence_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    order: int


class ShotCreated(Payload):
    shot_id: uuid.UUID
    sequence_id: uuid.UUID
    code: str
    description: str = ""
    status: str = "wip"
    credit_budget: Decimal | None = None


class ShotChanges(Payload):
    description: str | None = None
    status: str | None = None
    credit_budget: Decimal | None = None


class ShotUpdated(Payload):
    shot_id: uuid.UUID
    changes: ShotChanges


class ShotActiveSet(Payload):
    user_id: uuid.UUID
    shot_id: uuid.UUID | None  # null = cleared


class EntityCreated(Payload):
    entity_id: uuid.UUID
    project_id: uuid.UUID
    entity_type: EntityType
    name: str
    description: str = ""


class EntityReferenceAdded(Payload):
    entity_id: uuid.UUID
    sha256: Sha256 = SHA
    label: str


class ShotEntityAdded(Payload):
    shot_id: uuid.UUID
    entity_id: uuid.UUID


# ---------------------------------------------------------------- generations


class ServiceAccountRef(Payload):
    workspace_id: str | None = None
    service_user_id: str | None = None


class InputRef(Payload):
    """An input asset: by sha256 when the bytes are known, else by a service location
    (e.g. Magnific `creation:<identifier>` → service_asset_id, Higgsfield media id)."""

    role: str = "reference"
    sha256: Sha256 | None = SHA_OPT
    service_asset_id: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _has_some_ref(self) -> "InputRef":
        if not (self.sha256 or self.service_asset_id or self.url):
            raise ValueError("input needs sha256, service_asset_id or url")
        return self


class OutputRef(Payload):
    index: int = 0
    sha256: Sha256 | None = SHA_OPT
    service_asset_id: str | None = None
    url: str | None = None
    media_type: str | None = None


class ActivityRef(Payload):
    """Status/completion events name their activity by id, or by the service's job id."""

    activity_id: uuid.UUID | None = None
    service: str | None = None
    service_job_id: str | None = None

    @model_validator(mode="after")
    def _identifies_activity(self) -> "ActivityRef":
        if not (self.activity_id or (self.service and self.service_job_id)):
            raise ValueError("needs activity_id, or service + service_job_id")
        return self


class GenerationRequested(Payload):
    activity_id: uuid.UUID
    service: str
    kind: ActivityKind = "generate"
    model_id: str | None = None
    model_version: str | None = None
    service_job_id: str | None = None  # null when the request was rejected before a job existed
    status: ActivityStatus = "queued"
    prompt: str | None = None
    negative_prompt: str | None = None
    seed: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    inputs: list[InputRef] = Field(default_factory=list)
    credits_cost: Decimal | None = None
    credits_currency: str = "credits"
    shot_id: uuid.UUID | None = None  # active shot at capture time
    service_account: ServiceAccountRef | None = None


class GenerationStatus(ActivityRef):
    status: ActivityStatus
    detail: str | None = None
    # Final cost when the service revises it, e.g. a refunded failure (Higgsfield job_cost 0).
    # Added after Phase 1 with the user's approval (DECISIONS #28).
    credits_cost: Decimal | None = None


class GenerationCompleted(ActivityRef):
    outputs: list[OutputRef] = Field(default_factory=list)
    credits_cost: Decimal | None = None


# ---------------------------------------------------------------- assets


class LocationRef(Payload):
    service: str
    url: str = ""
    service_asset_id: str | None = None


class AssetRegistered(Payload):
    sha256: Sha256 = SHA
    media_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    fps: float | None = None
    bytes: int | None = None
    phash: str | None = None
    stored: bool = False
    locations: list[LocationRef] = Field(default_factory=list)


class AssetLocationSeen(Payload):
    sha256: Sha256 = SHA
    service: str
    url: str = ""
    service_asset_id: str | None = None
    is_alive: bool | None = None


class DownloadObserved(Payload):
    download_id: uuid.UUID
    filename: str
    service: str | None = None
    url: str | None = None
    bytes: int | None = None
    mime: str | None = None
    sha256: Sha256 | None = SHA_OPT
    hash_method: Literal["blob", "refetch", "filename_job"] | None = None


# ---------------------------------------------------------------- versions and links


class ShotVersionCreated(Payload):
    version_id: uuid.UUID
    shot_id: uuid.UUID
    sha256: Sha256 = SHA
    version_number: int
    status: VersionStatus = "wip"
    notes: str = ""


class ShotVersionStatusChanged(Payload):
    version_id: uuid.UUID
    status: VersionStatus


class LinkAsserted(Payload):
    link_id: uuid.UUID
    sha256: Sha256 = SHA
    target_type: Literal["activity", "shot"]
    target_id: uuid.UUID
    reason: str | None = None


# ---------------------------------------------------------------- audit and adapters


class AuditFlagRaised(Payload):
    flag_id: uuid.UUID
    rule_id: str
    severity: Severity
    subject_type: Literal["asset", "activity", "shot", "version", "download", "log"]
    subject_id: str
    message: str
    evidence: dict[str, Any]
    evidence_hash: str


class AuditFlagDismissed(Payload):
    flag_id: uuid.UUID
    reason: str


class AdapterUnknownPayload(Payload):
    service: str
    url: str
    method: str | None = None
    sample: str = Field(max_length=2048)  # redacted, first 2 KB


EVENT_TYPES: dict[str, type[Payload]] = {
    "user.registered": UserRegistered,
    "user.role_changed": UserRoleChanged,
    "user.logged_in": UserLoggedIn,
    "project.created": ProjectCreated,
    "sequence.created": SequenceCreated,
    "shot.created": ShotCreated,
    "shot.updated": ShotUpdated,
    "shot.active_set": ShotActiveSet,
    "entity.created": EntityCreated,
    "entity.reference_added": EntityReferenceAdded,
    "shot.entity_added": ShotEntityAdded,
    "generation.requested": GenerationRequested,
    "generation.status": GenerationStatus,
    "generation.completed": GenerationCompleted,
    "asset.registered": AssetRegistered,
    "asset.location_seen": AssetLocationSeen,
    "download.observed": DownloadObserved,
    "shot_version.created": ShotVersionCreated,
    "shot_version.status_changed": ShotVersionStatusChanged,
    "link.asserted": LinkAsserted,
    "audit.flag_raised": AuditFlagRaised,
    "audit.flag_dismissed": AuditFlagDismissed,
    "adapter.unknown_payload": AdapterUnknownPayload,
}


def validate_payload(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a payload; returns its JSON form (what gets stored and hashed).
    Raises KeyError for unknown types and pydantic.ValidationError for bad payloads."""
    model = EVENT_TYPES[type_]
    return model.model_validate(payload).model_dump(mode="json")
