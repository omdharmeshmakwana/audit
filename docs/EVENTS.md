# Events

Generated from `server/faible/events.py` by `faible events-doc`. Do not edit by hand;
a test fails when this file and the code disagree.

Every event row: `seq`, `event_id`, `type`, `occurred_at` (client/service time),
`recorded_at` (server time, never from the client), `actor_user_id`, `source`
(`extension|server|script|import`), `evidence`
(`originated|service_reported|observed|inferred|asserted`), `payload`, `prev_hash`, `hash`.
`hash` = sha256 of the canonical JSON (sorted keys, no whitespace) of the row minus
`seq` and `hash`. Payloads are strict: unknown fields are rejected.

## `user.registered`

A Faible account was created.

| field | type | |
|---|---|---|
| `user_id` | `UUID` | required |
| `email` | `str` | required |
| `display_name` | `str` | required |
| `role` | `Literal` | optional |

```json
{
  "user_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "email": "artist1@studio",
  "display_name": "Artist One",
  "role": "artist"
}
```

## `user.logged_in`

A user logged in to the web UI, the extension or the CLI.

| field | type | |
|---|---|---|
| `user_id` | `UUID` | required |
| `client` | `Literal` | required |

```json
{
  "user_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "client": "extension"
}
```

## `project.created`

A production project.

| field | type | |
|---|---|---|
| `project_id` | `UUID` | required |
| `name` | `str` | required |
| `code` | `str` | required |
| `status` | `str` | optional |

```json
{
  "project_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "name": "Demo",
  "code": "DEMO",
  "status": "active"
}
```

## `sequence.created`

A sequence within a project.

| field | type | |
|---|---|---|
| `sequence_id` | `UUID` | required |
| `project_id` | `UUID` | required |
| `name` | `str` | required |
| `order` | `int` | required |

```json
{
  "sequence_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "project_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "name": "SQ01",
  "order": 1
}
```

## `shot.created`

A shot within a sequence.

| field | type | |
|---|---|---|
| `shot_id` | `UUID` | required |
| `sequence_id` | `UUID` | required |
| `code` | `str` | required |
| `description` | `str` | optional |
| `status` | `str` | optional |
| `credit_budget` | `Union` | optional |

```json
{
  "shot_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "sequence_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "code": "SQ01_SH010",
  "description": "Mira at the window",
  "status": "wip",
  "credit_budget": "5000"
}
```

## `shot.updated`

Changed shot fields; null means unchanged.

| field | type | |
|---|---|---|
| `shot_id` | `UUID` | required |
| `changes` | `ShotChanges` | required |

```json
{
  "shot_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "changes": {
    "description": null,
    "status": "in_progress",
    "credit_budget": null
  }
}
```

## `shot.active_set`

A user set (or cleared, `null`) their active shot in the extension.

| field | type | |
|---|---|---|
| `user_id` | `UUID` | required |
| `shot_id` | `Union` | required |

```json
{
  "user_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "shot_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90"
}
```

## `entity.created`

A character, environment or prop.

| field | type | |
|---|---|---|
| `entity_id` | `UUID` | required |
| `project_id` | `UUID` | required |
| `entity_type` | `Literal` | required |
| `name` | `str` | required |
| `description` | `str` | optional |

```json
{
  "entity_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "project_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "entity_type": "character",
  "name": "Mira",
  "description": "Lead."
}
```

## `entity.reference_added`

A reference image attached to an entity, by content hash.

| field | type | |
|---|---|---|
| `entity_id` | `UUID` | required |
| `sha256` | `str` | required |
| `label` | `str` | required |

```json
{
  "entity_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "sha256": "9ecfe912012a198f3028665b604913b25613213bc1caa689fa5be0c921744d69",
  "label": "Mira front"
}
```

## `shot.entity_added`

An entity appears in a shot.

| field | type | |
|---|---|---|
| `shot_id` | `UUID` | required |
| `entity_id` | `UUID` | required |

```json
{
  "shot_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "entity_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90"
}
```

## `generation.requested`

The extension saw a generation request (and its immediate response) go out. Inputs name assets by sha256 when known, else by a service location that resolves later.

| field | type | |
|---|---|---|
| `activity_id` | `UUID` | required |
| `service` | `str` | required |
| `kind` | `Literal` | optional |
| `model_id` | `Union` | optional |
| `model_version` | `Union` | optional |
| `service_job_id` | `Union` | optional |
| `status` | `Literal` | optional |
| `prompt` | `Union` | optional |
| `negative_prompt` | `Union` | optional |
| `seed` | `Union` | optional |
| `parameters` | `dict` | optional |
| `inputs` | `list` | optional |
| `credits_cost` | `Union` | optional |
| `credits_currency` | `str` | optional |
| `shot_id` | `Union` | optional |
| `service_account` | `Union` | optional |

```json
{
  "activity_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "service": "magnific",
  "kind": "generate",
  "model_id": "seedream-5-pro",
  "model_version": null,
  "service_job_id": "iGX0Hoj3uK",
  "status": "queued",
  "prompt": "in @img1 a bit less crowd",
  "negative_prompt": null,
  "seed": "249676",
  "parameters": {
    "aspect_ratio": "16:9",
    "resolution": "2k"
  },
  "inputs": [
    {
      "role": "reference",
      "sha256": null,
      "service_asset_id": "Syni0eBUb8",
      "url": null
    }
  ],
  "credits_cost": "100",
  "credits_currency": "credits",
  "shot_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "service_account": {
    "workspace_id": "cf223cc5-b976-4479-8f8a-e279121e5648",
    "service_user_id": "173618012"
  }
}
```

## `generation.status`

A status change seen in polling or push traffic.

| field | type | |
|---|---|---|
| `activity_id` | `Union` | optional |
| `service` | `Union` | optional |
| `service_job_id` | `Union` | optional |
| `status` | `Literal` | required |
| `detail` | `Union` | optional |

```json
{
  "activity_id": null,
  "service": "higgsfield",
  "service_job_id": "253edf08-5200-4819-9677-5f98df466685",
  "status": "running",
  "detail": null
}
```

## `generation.completed`

Outputs are available. Output bytes are usually hashed later (asset.registered); the edge resolves by URL or service id.

| field | type | |
|---|---|---|
| `activity_id` | `Union` | optional |
| `service` | `Union` | optional |
| `service_job_id` | `Union` | optional |
| `outputs` | `list` | optional |
| `credits_cost` | `Union` | optional |

```json
{
  "activity_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "service": null,
  "service_job_id": null,
  "outputs": [
    {
      "index": 0,
      "sha256": null,
      "service_asset_id": "iGX0Hoj3uK",
      "url": "https://pikaso.cdnpk.net/private/production/5528616187/render.png?token=[REDACTED]",
      "media_type": "image"
    }
  ],
  "credits_cost": null
}
```

## `asset.registered`

The server knows an asset's bytes (sha256) and metadata; `stored` means a local copy exists. Locations are where it was seen.

| field | type | |
|---|---|---|
| `sha256` | `str` | required |
| `media_type` | `Union` | optional |
| `width` | `Union` | optional |
| `height` | `Union` | optional |
| `duration_ms` | `Union` | optional |
| `fps` | `Union` | optional |
| `bytes` | `Union` | optional |
| `phash` | `Union` | optional |
| `stored` | `bool` | optional |
| `locations` | `list` | optional |

```json
{
  "sha256": "9ecfe912012a198f3028665b604913b25613213bc1caa689fa5be0c921744d69",
  "media_type": "image/png",
  "width": 2560,
  "height": 1440,
  "duration_ms": null,
  "fps": null,
  "bytes": 7168150,
  "phash": null,
  "stored": true,
  "locations": [
    {
      "service": "magnific",
      "url": "https://pikaso.cdnpk.net/private/production/5528616187/render.png?token=[REDACTED]",
      "service_asset_id": "iGX0Hoj3uK"
    }
  ]
}
```

## `asset.location_seen`

A known asset was seen at another location (e.g. uploaded to a second service).

| field | type | |
|---|---|---|
| `sha256` | `str` | required |
| `service` | `str` | required |
| `url` | `str` | optional |
| `service_asset_id` | `Union` | optional |
| `is_alive` | `Union` | optional |

```json
{
  "sha256": "9ecfe912012a198f3028665b604913b25613213bc1caa689fa5be0c921744d69",
  "service": "higgsfield",
  "url": "",
  "service_asset_id": "6a87fae8-04c4-442e-aaad-deb571f20947",
  "is_alive": null
}
```

## `download.observed`

chrome.downloads reported a completed service download. `sha256` is null when no hash could be obtained.

| field | type | |
|---|---|---|
| `download_id` | `UUID` | required |
| `filename` | `str` | required |
| `service` | `Union` | optional |
| `url` | `Union` | optional |
| `bytes` | `Union` | optional |
| `mime` | `Union` | optional |
| `sha256` | `Union` | optional |
| `hash_method` | `Union` | optional |

```json
{
  "download_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "filename": "magnific_in-img1-i-want-a-bit-less_YMaBkskWeC.png",
  "service": "magnific",
  "url": "https://pikaso.cdnpk.net/private/production/5528588025/render.png?token=[REDACTED]",
  "bytes": 7168150,
  "mime": "image/png",
  "sha256": "9ecfe912012a198f3028665b604913b25613213bc1caa689fa5be0c921744d69",
  "hash_method": "refetch"
}
```

## `shot_version.created`

An asset was promoted to a numbered shot version.

| field | type | |
|---|---|---|
| `version_id` | `UUID` | required |
| `shot_id` | `UUID` | required |
| `sha256` | `str` | required |
| `version_number` | `int` | required |
| `status` | `Literal` | optional |
| `notes` | `str` | optional |

```json
{
  "version_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "shot_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "sha256": "9ecfe912012a198f3028665b604913b25613213bc1caa689fa5be0c921744d69",
  "version_number": 1,
  "status": "wip",
  "notes": ""
}
```

## `shot_version.status_changed`

A version's review status changed.

| field | type | |
|---|---|---|
| `version_id` | `UUID` | required |
| `status` | `Literal` | required |

```json
{
  "version_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "status": "submitted"
}
```

## `link.asserted`

A human linked an asset to an activity or shot (evidence `asserted`, confidence 1.0).

| field | type | |
|---|---|---|
| `link_id` | `UUID` | required |
| `sha256` | `str` | required |
| `target_type` | `Literal` | required |
| `target_id` | `UUID` | required |
| `reason` | `Union` | optional |

```json
{
  "link_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "sha256": "9ecfe912012a198f3028665b604913b25613213bc1caa689fa5be0c921744d69",
  "target_type": "shot",
  "target_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "reason": "made offline in Nuke"
}
```

## `audit.flag_raised`

The auditor raised a flag (Phase 4). Deduplicated on (rule_id, subject, evidence_hash).

| field | type | |
|---|---|---|
| `flag_id` | `UUID` | required |
| `rule_id` | `str` | required |
| `severity` | `Literal` | required |
| `subject_type` | `Literal` | required |
| `subject_id` | `str` | required |
| `message` | `str` | required |
| `evidence` | `dict` | required |
| `evidence_hash` | `str` | required |

```json
{
  "flag_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "rule_id": "download_unhashed",
  "severity": "warn",
  "subject_type": "download",
  "subject_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "message": "Download has no sha256",
  "evidence": {
    "download_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90"
  },
  "evidence_hash": "3f1c…"
}
```

## `audit.flag_dismissed`

A flag was dismissed with a reason; it stays dismissed for the same evidence.

| field | type | |
|---|---|---|
| `flag_id` | `UUID` | required |
| `reason` | `str` | required |

```json
{
  "flag_id": "5b0c2f1e-8a0d-4c7b-9a51-3f2d7e6c1a90",
  "reason": "known offline asset"
}
```

## `adapter.unknown_payload`

The extension saw a response on a matched URL it could not parse (redacted, first 2 KB).

| field | type | |
|---|---|---|
| `service` | `str` | required |
| `url` | `str` | required |
| `method` | `Union` | optional |
| `sample` | `str` | required |

```json
{
  "service": "magnific",
  "url": "https://www.magnific.com/app/api/new-thing",
  "method": "POST",
  "sample": "{\"unexpected\":true}"
}
```
