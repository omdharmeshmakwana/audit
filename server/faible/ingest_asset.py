"""`POST /ingest/asset`: bytes in, asset out (spec §4.2, §5).

The client sends the bytes and the sha256 it computed. The server hashes the stream itself
and rejects a mismatch (never trusts the client's hash), stores the copy content-addressed,
reads basic image metadata, and appends `asset.registered` — or, for bytes it already has,
only `asset.location_seen` for any location it hasn't seen.
"""

import hashlib
import io
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.events import Evidence
from faible.log import EventIn, append_events
from faible.models import Asset, AssetLocation, asset_id_for
from faible.storage import asset_path

MAX_BYTES = 500 * 1024 * 1024
CHUNK = 1024 * 1024


class AssetRejected(Exception):
    pass


@dataclass(frozen=True)
class IngestedAsset:
    asset_id: uuid.UUID
    sha256: str
    created: bool
    new_locations: int


def _image_meta(path: str) -> dict[str, Any]:
    try:
        with Image.open(path) as img:
            return {
                "width": img.width,
                "height": img.height,
                "media_type": Image.MIME.get(img.format or ""),
            }
    except (UnidentifiedImageError, OSError):
        return {}


def ingest_asset(
    session: Session,
    stream: BinaryIO,
    *,
    claimed_sha256: str,
    media_type: str | None,
    locations: list[dict[str, Any]],
    evidence: Evidence,
    actor_user_id: uuid.UUID,
) -> IngestedAsset:
    claimed = claimed_sha256.strip().lower()
    h = hashlib.sha256()
    size = 0
    fd, tmp = tempfile.mkstemp(prefix="faible-upload-")
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise AssetRejected("over 500 MB: register the location only")
                h.update(chunk)
                out.write(chunk)
        sha = h.hexdigest()
        if sha != claimed:
            raise AssetRejected(f"sha256 mismatch: client said {claimed}, bytes are {sha}")

        existing = session.get(Asset, asset_id_for(sha))
        now = datetime.now(UTC)
        if existing is not None and existing.registered and existing.stored:
            known = {
                (loc.service, loc.url, loc.service_asset_id)
                for loc in session.scalars(
                    select(AssetLocation).where(AssetLocation.asset_id == existing.id)
                )
            }
            fresh = [
                loc
                for loc in locations
                if (loc["service"], loc.get("url") or "", loc.get("service_asset_id")) not in known
            ]
            if fresh:
                append_events(
                    session,
                    [
                        EventIn(
                            event_id=uuid.uuid4(),
                            type="asset.location_seen",
                            occurred_at=now,
                            evidence=evidence,
                            payload={"sha256": sha, **loc},
                        )
                        for loc in fresh
                    ],
                    source="extension",
                    actor_user_id=actor_user_id,
                )
            return IngestedAsset(existing.id, sha, created=False, new_locations=len(fresh))

        path = asset_path(sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.replace(tmp, path)
        meta = _image_meta(str(path))
        (result,) = append_events(
            session,
            [
                EventIn(
                    event_id=uuid.uuid4(),
                    type="asset.registered",
                    occurred_at=now,
                    evidence=evidence,
                    payload={
                        "sha256": sha,
                        "media_type": media_type or meta.get("media_type"),
                        "width": meta.get("width"),
                        "height": meta.get("height"),
                        "bytes": size,
                        "stored": True,
                        "locations": locations,
                    },
                )
            ],
            source="extension",
            actor_user_id=actor_user_id,
        )
        if result.status != "accepted":
            raise AssetRejected(result.reason or result.status)
        return IngestedAsset(asset_id_for(sha), sha, created=True, new_locations=len(locations))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ingest_bytes(session: Session, data: bytes, **kwargs: Any) -> IngestedAsset:
    """Convenience for tests and scripts."""
    return ingest_asset(session, io.BytesIO(data), **kwargs)
