"""Generates docs/EVENTS.md from `faible.events` (spec §3: "kept in sync with code").
A test re-renders it and fails on drift; `faible events-doc` rewrites it."""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from faible.config import REPO_ROOT
from faible.events import EVENT_TYPES

EVENTS_MD: Path = REPO_ROOT / "docs" / "EVENTS.md"

U = "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90"
SHA = "9ecfe912012a198f3028665b604913b25613213bc1caa689fa5be0c921744d69"

EXAMPLES: dict[str, tuple[str, dict[str, Any]]] = {
    "user.registered": (
        "A Faible account was created.",
        {"user_id": U, "email": "artist1@studio", "display_name": "Artist One", "role": "artist"},
    ),
    "user.logged_in": (
        "A user logged in to the web UI, the extension or the CLI.",
        {"user_id": U, "client": "extension"},
    ),
    "project.created": (
        "A production project.",
        {"project_id": U, "name": "Demo", "code": "DEMO", "status": "active"},
    ),
    "sequence.created": (
        "A sequence within a project.",
        {"sequence_id": U, "project_id": U, "name": "SQ01", "order": 1},
    ),
    "shot.created": (
        "A shot within a sequence.",
        {
            "shot_id": U,
            "sequence_id": U,
            "code": "SQ01_SH010",
            "description": "Mira at the window",
            "status": "wip",
            "credit_budget": "5000",
        },
    ),
    "shot.updated": (
        "Changed shot fields; null means unchanged.",
        {
            "shot_id": U,
            "changes": {"description": None, "status": "in_progress", "credit_budget": None},
        },
    ),
    "shot.active_set": (
        "A user set (or cleared, `null`) their active shot in the extension.",
        {"user_id": U, "shot_id": U},
    ),
    "entity.created": (
        "A character, environment or prop.",
        {
            "entity_id": U,
            "project_id": U,
            "entity_type": "character",
            "name": "Mira",
            "description": "Lead.",
        },
    ),
    "entity.reference_added": (
        "A reference image attached to an entity, by content hash.",
        {"entity_id": U, "sha256": SHA, "label": "Mira front"},
    ),
    "shot.entity_added": ("An entity appears in a shot.", {"shot_id": U, "entity_id": U}),
    "generation.requested": (
        "The extension saw a generation request (and its immediate response) go out. Inputs name assets by sha256 when known, else by a service location that resolves later.",
        {
            "activity_id": U,
            "service": "magnific",
            "kind": "generate",
            "model_id": "seedream-5-pro",
            "model_version": None,
            "service_job_id": "iGX0Hoj3uK",
            "status": "queued",
            "prompt": "in @img1 a bit less crowd",
            "negative_prompt": None,
            "seed": "249676",
            "parameters": {"aspect_ratio": "16:9", "resolution": "2k"},
            "inputs": [
                {"role": "reference", "sha256": None, "service_asset_id": "Syni0eBUb8", "url": None}
            ],
            "credits_cost": "100",
            "credits_currency": "credits",
            "shot_id": U,
            "service_account": {
                "workspace_id": "cf223cc5-b976-4479-8f8a-e279121e5648",
                "service_user_id": "173618012",
            },
        },
    ),
    "generation.status": (
        "A status change seen in polling or push traffic.",
        {
            "activity_id": None,
            "service": "higgsfield",
            "service_job_id": "253edf08-5200-4819-9677-5f98df466685",
            "status": "running",
            "detail": None,
        },
    ),
    "generation.completed": (
        "Outputs are available. Output bytes are usually hashed later (asset.registered); the edge resolves by URL or service id.",
        {
            "activity_id": U,
            "service": None,
            "service_job_id": None,
            "outputs": [
                {
                    "index": 0,
                    "sha256": None,
                    "service_asset_id": "iGX0Hoj3uK",
                    "url": "https://pikaso.cdnpk.net/private/production/5528616187/render.png?token=[REDACTED]",
                    "media_type": "image",
                }
            ],
            "credits_cost": None,
        },
    ),
    "asset.registered": (
        "The server knows an asset's bytes (sha256) and metadata; `stored` means a local copy exists. Locations are where it was seen.",
        {
            "sha256": SHA,
            "media_type": "image/png",
            "width": 2560,
            "height": 1440,
            "duration_ms": None,
            "fps": None,
            "bytes": 7168150,
            "phash": None,
            "stored": True,
            "locations": [
                {
                    "service": "magnific",
                    "url": "https://pikaso.cdnpk.net/private/production/5528616187/render.png?token=[REDACTED]",
                    "service_asset_id": "iGX0Hoj3uK",
                }
            ],
        },
    ),
    "asset.location_seen": (
        "A known asset was seen at another location (e.g. uploaded to a second service).",
        {
            "sha256": SHA,
            "service": "higgsfield",
            "url": "",
            "service_asset_id": "6a87fae8-04c4-442e-aaad-deb571f20947",
            "is_alive": None,
        },
    ),
    "download.observed": (
        "chrome.downloads reported a completed service download. `sha256` is null when no hash could be obtained.",
        {
            "download_id": U,
            "filename": "magnific_in-img1-i-want-a-bit-less_YMaBkskWeC.png",
            "service": "magnific",
            "url": "https://pikaso.cdnpk.net/private/production/5528588025/render.png?token=[REDACTED]",
            "bytes": 7168150,
            "mime": "image/png",
            "sha256": SHA,
            "hash_method": "refetch",
        },
    ),
    "shot_version.created": (
        "An asset was promoted to a numbered shot version.",
        {
            "version_id": U,
            "shot_id": U,
            "sha256": SHA,
            "version_number": 1,
            "status": "wip",
            "notes": "",
        },
    ),
    "shot_version.status_changed": (
        "A version's review status changed.",
        {"version_id": U, "status": "submitted"},
    ),
    "link.asserted": (
        "A human linked an asset to an activity or shot (evidence `asserted`, confidence 1.0).",
        {
            "link_id": U,
            "sha256": SHA,
            "target_type": "shot",
            "target_id": U,
            "reason": "made offline in Nuke",
        },
    ),
    "audit.flag_raised": (
        "The auditor raised a flag (Phase 4). Deduplicated on (rule_id, subject, evidence_hash).",
        {
            "flag_id": U,
            "rule_id": "download_unhashed",
            "severity": "warn",
            "subject_type": "download",
            "subject_id": U,
            "message": "Download has no sha256",
            "evidence": {"download_id": U},
            "evidence_hash": "3f1c…",
        },
    ),
    "audit.flag_dismissed": (
        "A flag was dismissed with a reason; it stays dismissed for the same evidence.",
        {"flag_id": U, "reason": "known offline asset"},
    ),
    "adapter.unknown_payload": (
        "The extension saw a response on a matched URL it could not parse (redacted, first 2 KB).",
        {
            "service": "magnific",
            "url": "https://www.magnific.com/app/api/new-thing",
            "method": "POST",
            "sample": '{"unexpected":true}',
        },
    ),
}


def _fields(model: type[BaseModel]) -> list[str]:
    rows = []
    for name, f in model.model_fields.items():
        ann = f.annotation
        typ = getattr(ann, "__name__", None) or str(ann).replace("typing.", "")
        req = "required" if f.is_required() else "optional"
        rows.append(f"| `{name}` | `{typ}` | {req} |")
    return rows


def render() -> str:
    out = [
        "# Events",
        "",
        "Generated from `server/faible/events.py` by `faible events-doc`. Do not edit by hand;",
        "a test fails when this file and the code disagree.",
        "",
        "Every event row: `seq`, `event_id`, `type`, `occurred_at` (client/service time),",
        "`recorded_at` (server time, never from the client), `actor_user_id`, `source`",
        "(`extension|server|script|import`), `evidence`",
        "(`originated|service_reported|observed|inferred|asserted`), `payload`, `prev_hash`, `hash`.",
        "`hash` = sha256 of the canonical JSON (sorted keys, no whitespace) of the row minus",
        "`seq` and `hash`. Payloads are strict: unknown fields are rejected.",
        "",
    ]
    for type_, model in EVENT_TYPES.items():
        desc, example = EXAMPLES[type_]
        out += [
            f"## `{type_}`",
            "",
            desc,
            "",
            "| field | type | |",
            "|---|---|---|",
            *_fields(model),
            "",
        ]
        out += ["```json", json.dumps(example, indent=2, ensure_ascii=False), "```", ""]
    return "\n".join(out)
