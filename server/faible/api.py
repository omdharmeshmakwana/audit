"""HTTP API, `/api/v1` (spec §5). Phase 1: auth and event ingest."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from faible.auth import AuthError, authenticate, decode_token, issue_token, register_user
from faible.db import get_session
from faible.log import EventIn, append_events
from faible.models import User

router = APIRouter(prefix="/api/v1")
DbSession = Annotated[Session, Depends(get_session)]


def current_user(session: DbSession, authorization: Annotated[str | None, Header()] = None) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        user_id = decode_token(authorization.split(" ", 1)[1], "access")
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
