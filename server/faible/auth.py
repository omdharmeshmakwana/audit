"""Faible accounts (hard rule 8: every artist logs in individually, even though the studio
shares one service workspace). Passwords are Argon2-hashed into `user_credentials`, never
into the event log; registration and logins are logged as events."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.config import get_settings
from faible.events import Source
from faible.log import EventIn, append_events
from faible.models import UserCredential

_hasher = PasswordHasher()
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)
TokenType = Literal["access", "refresh"]


class AuthError(Exception):
    pass


def _secret() -> str:
    """HS256 key: FAIBLE_JWT_SECRET, else a random key persisted under data/ on first use."""
    settings = get_settings()
    if settings.jwt_secret:
        return settings.jwt_secret
    path = settings.data_dir / "jwt_secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def register_user(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    role: str = "artist",
    user_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    source: Source = "server",
) -> uuid.UUID:
    email = email.strip().lower()
    existing = session.scalar(select(UserCredential).where(UserCredential.email == email))
    if existing is not None:
        return existing.user_id
    user_id = user_id or uuid.uuid4()
    (result,) = append_events(
        session,
        [
            EventIn(
                event_id=event_id or uuid.uuid4(),
                type="user.registered",
                occurred_at=occurred_at or datetime.now(UTC),
                evidence="originated",
                payload={
                    "user_id": str(user_id),
                    "email": email,
                    "display_name": display_name,
                    "role": role,
                },
            )
        ],
        source=source,
    )
    if result.status not in ("accepted", "duplicate"):
        raise AuthError(result.reason or "registration rejected")
    session.add(UserCredential(user_id=user_id, email=email, password_hash=hash_password(password)))
    session.flush()
    return user_id


def authenticate(
    session: Session, email: str, password: str, client: Literal["web", "extension", "cli"]
) -> uuid.UUID:
    cred = session.scalar(
        select(UserCredential).where(UserCredential.email == email.strip().lower())
    )
    try:
        if cred is None:
            raise VerificationError
        _hasher.verify(cred.password_hash, password)
    except VerificationError as exc:
        raise AuthError("invalid email or password") from exc
    append_events(
        session,
        [
            EventIn(
                event_id=uuid.uuid4(),
                type="user.logged_in",
                occurred_at=datetime.now(UTC),
                evidence="originated",
                payload={"user_id": str(cred.user_id), "client": client},
            )
        ],
        source="server",
        actor_user_id=cred.user_id,
    )
    return cred.user_id


def issue_token(user_id: uuid.UUID, typ: TokenType) -> str:
    now = datetime.now(UTC)
    ttl = ACCESS_TTL if typ == "access" else REFRESH_TTL
    claims = {"sub": str(user_id), "typ": typ, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, _secret(), algorithm="HS256")


def decode_token(token: str, typ: TokenType) -> uuid.UUID:
    try:
        claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise AuthError("invalid token") from exc
    if claims.get("typ") != typ:
        raise AuthError("wrong token type")
    return uuid.UUID(claims["sub"])
