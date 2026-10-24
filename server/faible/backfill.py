"""Backfills that add knowledge through new events (never by editing old ones).

- `fingerprint_stored`: stored assets registered before fingerprinting existed get their
  pHash via a follow-up `asset.registered` ("includes phash once computed", spec §3).
- `fetch_missing`: assets known only by hash (uploads the extension located but could not
  copy) are fetched from a *public* location URL, verified against their sha256, stored and
  fingerprinted. Then generation inputs known only by a service id and a public URL (no
  bytes ever seen, e.g. captured before DECISIONS #53) are fetched and registered at that
  location, which resolves the input and lets pHash link it to its source. Signed/redacted
  URLs are skipped: they were never stored and have expired. An admin tool (DECISIONS #40);
  in normal operation the extension supplies the bytes.
"""

import hashlib
import ssl
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.fingerprint import fingerprint_file
from faible.log import EventIn, append_events
from faible.models import Activity, ActivityInput, Asset, AssetLocation
from faible.storage import asset_path, store_bytes

MAX_FETCH = 500 * 1024 * 1024


def _tls() -> ssl.SSLContext:
    # python.org builds on macOS ship without a CA bundle; certifi's is used when present.
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _public(url: str | None) -> bool:
    return url is not None and url.startswith("https://") and "[REDACTED]" not in url


def _fetch(url: str) -> tuple[bytes, str | None]:
    with urllib.request.urlopen(url, timeout=60, context=_tls()) as res:  # noqa: S310 - https only
        return res.read(MAX_FETCH + 1), res.headers.get("content-type")


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
            if _public(loc.url)
        ]
        if not urls:
            report.skipped.append(f"{asset.sha256[:12]} no public location")
            continue
        for url in urls:
            try:
                data, content_type = _fetch(url)
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
    _fetch_unresolved_inputs(session, report)
    return report


def _fetch_unresolved_inputs(session: Session, report: BackfillReport) -> None:
    rows = session.scalars(
        select(ActivityInput).where(
            ActivityInput.asset_id.is_(None), ActivityInput.ref_url.is_not(None)
        )
    )
    seen: set[str] = set()
    for inp in rows:
        url = inp.ref_url
        if url is None or url in seen:
            continue
        seen.add(url)
        label = f"input {(inp.ref_service_asset_id or url)[:12]}"
        if not _public(url):
            report.skipped.append(f"{label} no public URL")
            continue
        try:
            data, content_type = _fetch(url)
        except OSError as exc:
            report.skipped.append(f"{label} {url[:60]}: {exc}")
            continue
        if len(data) > MAX_FETCH:
            report.skipped.append(f"{label} over 500 MB")
            continue
        sha = store_bytes(data)
        service = inp.ref_service or session.get_one(Activity, inp.activity_id).service
        location = {"service": service, "url": url}
        if inp.ref_service_asset_id:
            location["service_asset_id"] = inp.ref_service_asset_id
        payload: dict[str, Any] = {"media_type": content_type, "bytes": len(data), "stored": True}
        payload |= _fp_payload(sha, content_type)
        _register(session, sha, payload | {"locations": [location]})
        report.done.append(f"{label} → {sha[:12]}")
