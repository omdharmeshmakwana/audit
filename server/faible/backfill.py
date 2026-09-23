"""Backfills that add knowledge through new events (never by editing old ones).

- `fingerprint_stored`: stored assets registered before fingerprinting existed get their
  pHash via a follow-up `asset.registered` ("includes phash once computed", spec §3).
- `fetch_missing`: assets known only by hash (uploads the extension located but could not
  copy) are fetched from a *public* location URL, verified against their sha256, stored and
  fingerprinted. Signed/redacted URLs are skipped: they were never stored and have expired.
  An admin tool (DECISIONS #40); in normal operation the extension supplies the bytes.
"""

import hashlib
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.fingerprint import fingerprint_file
from faible.log import EventIn, append_events
from faible.models import Asset, AssetLocation
from faible.storage import asset_path, store_bytes

MAX_FETCH = 500 * 1024 * 1024


@dataclass
class BackfillReport:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _register(session: Session, sha: str, payload: dict[str, Any]) -> None:
    append_events(
        session,
        [
            EventIn(
                event_id=uuid.uuid4(),
                type="asset.registered",
                occurred_at=datetime.now(UTC),
                evidence="observed",
                payload={"sha256": sha, **{k: v for k, v in payload.items() if v is not None}},
            )
        ],
        source="script",
    )


def _fp_payload(sha: str, media_type: str | None) -> dict[str, Any]:
    fp = fingerprint_file(asset_path(sha), media_type)
    if fp is None:
        return {}
    return {
        "media_type": media_type or fp.media_type,
        "width": fp.width,
        "height": fp.height,
        "duration_ms": fp.duration_ms,
        "fps": fp.fps,
        "phash": fp.phash,
    }


def fingerprint_stored(session: Session) -> BackfillReport:
    report = BackfillReport()
    for asset in list(session.scalars(select(Asset).where(Asset.stored, Asset.phash.is_(None)))):
        if not asset_path(asset.sha256).exists():
            report.skipped.append(f"{asset.sha256[:12]} missing on disk")
            continue
        payload = _fp_payload(asset.sha256, asset.media_type)
        if not payload.get("phash"):
            report.skipped.append(f"{asset.sha256[:12]} not an image or video")
            continue
        _register(session, asset.sha256, payload)
        report.done.append(asset.sha256[:12])
    return report


def fetch_missing(session: Session) -> BackfillReport:
    report = BackfillReport()
    unstored = list(session.scalars(select(Asset).where(Asset.stored.is_(False))))
    for asset in unstored:
        urls = [
            loc.url
            for loc in session.scalars(
                select(AssetLocation).where(AssetLocation.asset_id == asset.id)
            )
            if loc.url.startswith("https://") and "[REDACTED]" not in loc.url
        ]
        if not urls:
            report.skipped.append(f"{asset.sha256[:12]} no public location")
            continue
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=60) as res:  # noqa: S310 - https only
                    data = res.read(MAX_FETCH + 1)
                    content_type = res.headers.get("content-type")
            except OSError as exc:
                report.skipped.append(f"{asset.sha256[:12]} {url[:60]}: {exc}")
                continue
            if len(data) > MAX_FETCH:
                report.skipped.append(f"{asset.sha256[:12]} over 500 MB")
                break
            if hashlib.sha256(data).hexdigest() != asset.sha256:
                report.skipped.append(
                    f"{asset.sha256[:12]} bytes at {url[:60]} have a different sha256"
                )
                continue
            store_bytes(data)
            payload = {"media_type": content_type, "bytes": len(data), "stored": True}
            payload |= _fp_payload(asset.sha256, content_type)
            _register(session, asset.sha256, payload)
            report.done.append(asset.sha256[:12])
            break
    return report
