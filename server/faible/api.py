"""HTTP API, `/api/v1` (spec §5). Phase 1: auth and event ingest."""

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faible.auth import AuthError, authenticate, decode_token, issue_token, register_user
from faible.chain import asset_chain
from faible.db import get_session
from faible.events import Evidence
from faible.ingest_asset import AssetRejected, ingest_asset
from faible.log import EventIn, append_events
from faible.models import Asset, Project, Shot, ShotVersion, User
from faible.storage import asset_path
from faible.views import asset_detail, graph, my_captures, project_tree, shot_detail

router = APIRouter(prefix="/api/v1")
DbSession = Annotated[Session, Depends(get_session)]


SESSION_COOKIE = "faible_session"


def current_user(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    faible_session: Annotated[str | None, Cookie()] = None,
) -> User:
    """Bearer access token (extension, CLI) or the web UI's httpOnly session cookie."""
    try:
        if authorization and authorization.lower().startswith("bearer "):
            user_id = decode_token(authorization.split(" ", 1)[1], "access")
        elif faible_session:
            user_id = decode_token(faible_session, "web")
        else:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


# ---------------------------------------------------------------- auth


class RegisterIn(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1)


class LoginIn(BaseModel):
    email: str
    password: str
    client: Literal["web", "extension", "cli"] = "extension"


class RefreshIn(BaseModel):
    refresh_token: str


class TokensOut(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    user_id: uuid.UUID


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    active_shot_id: uuid.UUID | None


@router.post("/auth/register", status_code=201)
def register(body: RegisterIn, session: DbSession) -> dict[str, uuid.UUID]:
    user_id = register_user(
        session, email=body.email, password=body.password, display_name=body.display_name
    )
    session.commit()
    return {"user_id": user_id}


@router.post("/auth/login")
def login(body: LoginIn, session: DbSession) -> TokensOut:
    try:
        user_id = authenticate(session, body.email, body.password, body.client)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    session.commit()
    return TokensOut(
        access_token=issue_token(user_id, "access"),
        refresh_token=issue_token(user_id, "refresh"),
        user_id=user_id,
    )


@router.post("/auth/refresh")
def refresh(body: RefreshIn, session: DbSession) -> TokensOut:
    try:
        user_id = decode_token(body.refresh_token, "refresh")
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    if session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return TokensOut(access_token=issue_token(user_id, "access"), user_id=user_id)


@router.get("/me")
def me(user: CurrentUser) -> MeOut:
    return MeOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        active_shot_id=user.active_shot_id,
    )


# ---------------------------------------------------------------- ingest


class IngestIn(BaseModel):
    events: list[EventIn] = Field(max_length=500)


class IngestResultOut(BaseModel):
    event_id: uuid.UUID
    status: str
    seq: int | None
    reason: str | None


@router.post("/ingest/events")
def ingest_events(
    body: IngestIn, session: DbSession, user: CurrentUser
) -> dict[str, list[IngestResultOut]]:
    """Batch ingest from the extension. Idempotent on event_id; each event is accepted,
    duplicate, conflict or rejected individually; accepted ones commit together."""
    results = append_events(session, body.events, source="extension", actor_user_id=user.id)
    session.commit()
    return {
        "results": [
            IngestResultOut(event_id=r.event_id, status=r.status, seq=r.seq, reason=r.reason)
            for r in results
        ]
    }


def _emit(
    session: Session, user: User, type_: str, payload: dict[str, Any], evidence: Evidence
) -> None:
    """Append one server-originated event on the user's behalf; raise 422 if rejected."""
    (r,) = append_events(
        session,
        [
            EventIn(
                event_id=uuid.uuid4(),
                type=type_,
                occurred_at=datetime.now(UTC),
                evidence=evidence,
                payload=payload,
            )
        ],
        source="server",
        actor_user_id=user.id,
    )
    if r.status != "accepted":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, r.reason or r.status)


# ---------------------------------------------------------------- projects and shots


@router.get("/projects")
def list_projects(session: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    return [
        {"id": p.id, "name": p.name, "code": p.code, "status": p.status}
        for p in session.scalars(select(Project).order_by(Project.code))
    ]


@router.get("/projects/{project_id}/tree")
def get_project_tree(
    project_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> dict[str, Any]:
    tree = project_tree(session, project_id)
    if tree is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such project")
    return tree


@router.get("/shots/{shot_id}")
def get_shot(shot_id: uuid.UUID, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    detail = shot_detail(session, shot_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shot")
    return detail


class VersionIn(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    notes: str = ""


@router.post("/shots/{shot_id}/versions", status_code=201)
def promote_version(
    shot_id: uuid.UUID, body: VersionIn, session: DbSession, user: CurrentUser
) -> dict[str, Any]:
    """Promote an asset to the shot's next numbered version."""
    if session.get(Shot, shot_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shot")
    current = session.scalar(
        select(func.max(ShotVersion.version_number)).where(ShotVersion.shot_id == shot_id)
    )
    number = (current or 0) + 1
    version_id = uuid.uuid4()
    _emit(
        session,
        user,
        "shot_version.created",
        {
            "version_id": str(version_id),
            "shot_id": str(shot_id),
            "sha256": body.sha256,
            "version_number": number,
            "notes": body.notes,
        },
        "asserted",
    )
    session.commit()
    return {"version_id": version_id, "version_number": number}


# ---------------------------------------------------------------- the artist's own state


class ActiveShotIn(BaseModel):
    shot_id: uuid.UUID | None


@router.put("/me/active-shot")
def set_active_shot(body: ActiveShotIn, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    if body.shot_id is not None and session.get(Shot, body.shot_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shot")
    if user.active_shot_id != body.shot_id:
        _emit(
            session,
            user,
            "shot.active_set",
            {"user_id": str(user.id), "shot_id": str(body.shot_id) if body.shot_id else None},
            "observed",
        )
        session.commit()
    return {"active_shot_id": body.shot_id}


@router.get("/me/captures")
def get_my_captures(session: DbSession, user: CurrentUser, limit: int = 10) -> dict[str, Any]:
    return my_captures(session, user.id, min(max(limit, 1), 100))


# ---------------------------------------------------------------- links


class LinkIn(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_type: Literal["activity", "shot"]
    target_id: uuid.UUID
    reason: str | None = None


class LinksIn(BaseModel):
    links: list[LinkIn] = Field(min_length=1, max_length=200)


@router.post("/links", status_code=201)
def assert_links(
    body: LinksIn, session: DbSession, user: CurrentUser
) -> dict[str, list[uuid.UUID]]:
    """Manual assertions (evidence `asserted`). A link to a shot assigns the generations
    that produced the asset to that shot — the extension's "Assign to shot"."""
    ids = []
    for link in body.links:
        if link.target_type == "shot" and session.get(Shot, link.target_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such shot {link.target_id}")
        link_id = uuid.uuid4()
        _emit(
            session,
            user,
            "link.asserted",
            {
                "link_id": str(link_id),
                "sha256": link.sha256,
                "target_type": link.target_type,
                "target_id": str(link.target_id),
                "reason": link.reason,
            },
            "asserted",
        )
        ids.append(link_id)
    session.commit()
    return {"link_ids": ids}


# ---------------------------------------------------------------- asset bytes


@router.post("/ingest/asset")
def post_asset(
    session: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    x_faible_sha256: Annotated[str, Header()],
    locations: Annotated[str, Form()] = "[]",
    media_type: Annotated[str | None, Form()] = None,
    evidence: Annotated[Evidence, Form()] = "observed",
) -> dict[str, Any]:
    """Bytes + the client's sha256 (header `X-Faible-Sha256`). Rejected on mismatch."""
    try:
        locs = json.loads(locations)
        if not isinstance(locs, list):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "locations must be a JSON list"
        ) from exc
    try:
        result = ingest_asset(
            session,
            file.file,
            claimed_sha256=x_faible_sha256,
            media_type=media_type,
            locations=locs,
            evidence=evidence,
            actor_user_id=user.id,
        )
    except AssetRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    session.commit()
    asset = session.get_one(Asset, result.asset_id)
    return {
        "asset_id": result.asset_id,
        "sha256": result.sha256,
        "created": result.created,
        "new_locations": result.new_locations,
        "bytes": asset.bytes,
    }


# ---------------------------------------------------------------- assets and graph


@router.get("/assets/{asset_id}")
def get_asset(asset_id: uuid.UUID, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    detail = asset_detail(session, asset_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such asset")
    return detail


@router.get("/assets/{asset_id}/chain")
def get_asset_chain(asset_id: uuid.UUID, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    chain = asset_chain(session, asset_id)
    if chain is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such asset")
    return chain


@router.get("/assets/{asset_id}/file")
def get_asset_file(asset_id: uuid.UUID, session: DbSession, user: CurrentUser) -> FileResponse:
    asset = session.get(Asset, asset_id)
    if asset is None or not asset.stored or not asset_path(asset.sha256).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no local copy of this asset")
    return FileResponse(
        asset_path(asset.sha256),
        media_type=asset.media_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},  # content-addressed
    )


@router.get("/graph")
def get_graph(
    project_id: uuid.UUID, session: DbSession, user: CurrentUser, limit: int = 200, offset: int = 0
) -> dict[str, Any]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such project")
    return graph(session, project_id, limit=min(max(limit, 1), 1000), offset=max(offset, 0))
