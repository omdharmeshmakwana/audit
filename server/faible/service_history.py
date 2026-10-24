"""Reading service history records (DECISIONS #59).

`service_generation.observed` events carry a service's own generation object (`raw`) as the
service returned it to its UI. This module is the single interpretation of those objects,
used by the projector (derived `service_generations` rows), by `faible import-history` and by
`faible export-history`. The rules are ported from the Higgsfield exporter
(`higgsfield_exporter/merge_exports.py` and `extension/exporter.js`) so imported exports and
live captures read identically.

Pure functions only: the projector depends on them, so `rebuild` stays exact.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
# Same month style the exporter's audit page writes ("17 Sept 2026, 11:18:16 AM IST").
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]

HIGGSFIELD = "higgsfield"
CUSTOM_INTERFACE = "custom_interface"  # the studio's in-house Seedance tool (merged exports)

# Params that reference UI presets (camera bodies, looks), not uploaded inputs.
PRESET_KEYS = {
    "preset", "presets", "camera_model", "camera_lens", "camera_aperture",
    "camera_focal_length", "genre_control", "era_control", "light_preset",
    "style_id", "multi_prompt",
}  # fmt: skip

# The exporter's generations.json `settings` keys.
KNOWN_PARAM_KEYS = [
    "prompt", "enhanced_prompt", "negative_prompt", "aspect_ratio", "duration",
    "resolution", "width", "height", "seed", "batch_size", "generate_audio", "fps",
    "steps", "guidance_scale", "strength", "camera_motion", "motion_strength",
]  # fmt: skip

STATUS_RANK = {"unknown": 0, "queued": 1, "running": 2, "succeeded": 3, "failed": 3, "cancelled": 3}


def map_status(value: Any) -> str:
    """Service status → Faible's vocabulary (extension/src/adapters/higgsfield.ts mapStatus),
    plus `unknown` for anything else. The raw value is kept as `service_status`."""
    s = str(value or "").lower()
    if s in ("queued", "pending"):
        return "queued"
    if s in ("in_progress", "processing", "running"):
        return "running"
    if s in ("completed", "succeeded", "success"):
        return "succeeded"
    if s in ("failed", "nsfw", "error"):
        return "failed"
    if s in ("canceled", "cancelled"):
        return "cancelled"
    return "unknown"


def kind_for(model_id: str | None) -> str:
    return "upscale" if model_id and re.search(r"upscale|topaz", model_id, re.I) else "generate"


def to_unix(value: Any) -> float | None:
    """Unix seconds/ms, ISO strings, or the audit page's readable IST strings."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value / 1000 if value > 1e12 else float(value)
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return to_unix(float(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    except ValueError:
        pass
    m = re.fullmatch(
        r"(\d{1,2}) (\w+) (\d{4}), (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)(?: IST)?", s, re.IGNORECASE
    )
    if m:
        day, mon, year, hh, mm, ss, ampm = m.groups()
        month = next(
            (i + 1 for i, n in enumerate(MONTHS) if mon.lower().startswith(n[:3].lower())), None
        )
        if month:
            hour = int(hh) % 12 + (12 if ampm.upper() == "PM" else 0)
            local = datetime(int(year), month, int(day), hour, int(mm), int(ss), tzinfo=IST)
            return local.timestamp()
    return None


def to_datetime(value: Any) -> datetime | None:
    unix = to_unix(value)
    if unix is None:
        return None
    try:
        return datetime.fromtimestamp(unix, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def human_time(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    d = dt.astimezone(IST)
    return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year}, {d.strftime('%I:%M:%S %p')} IST"


def media_kind(url: str, hint: str | None = None) -> str:
    h = (hint or "").lower()
    if "video" in h:
        return "video"
    if "audio" in h:
        return "audio"
    path = url.split("?")[0].lower()
    if re.search(r"\.(mp4|mov|webm|m4v|mkv)$", path):
        return "video"
    if re.search(r"\.(mp3|wav|m4a|aac|ogg|flac)$", path):
        return "audio"
    return "image"


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _decimal(v: Any) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, int | float | str):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


# ---------------------------------------------------------------- Higgsfield


def higgsfield_inputs(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every media URL in the params (exporter rule), with the service media id when the
    entry has one (`medias[].data.id`), so an input can join Faible's asset locations.
    Keys are walked in sorted order: JSONB does not keep key order, and the projector must
    give the same result for the payload as ingested and as replayed by `rebuild`."""
    inputs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: Any, key: str, role: str | None, media_id: str | None) -> None:
        if not isinstance(url, str) or not url.startswith("http") or url in seen:
            return
        seen.add(url)
        inputs.append(
            _compact(
                {
                    "kind": media_kind(url, role),
                    "role": role or key,
                    "url": url,
                    "service_asset_id": media_id,
                }
            )
        )

    def walk(node: Any, key: str, role: str | None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, key, role)
        elif isinstance(node, dict):
            r = node.get("role") if isinstance(node.get("role"), str) else role
            add(node.get("url"), key, r, _str(node.get("id")))
            for k, v in sorted(node.items()):
                if k not in ("url", "thumbnail_url"):
                    walk(v, key, r)
        else:
            add(node, key, role, None)

    for key, value in sorted(params.items()):
        if key not in PRESET_KEYS:
            walk(value, key, None)
    return inputs


def higgsfield_outputs(results: Any, result: Any) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(entry: Any, label: str) -> None:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            return
        if entry["url"] in seen:
            return
        seen.add(entry["url"])
        outputs.append(
            _compact(
                {
                    "index": len(outputs),
                    "kind": media_kind(entry["url"], entry.get("type")),
                    "label": label,
                    "url": entry["url"],
                    "thumbnail_url": entry.get("thumbnail_url"),
                }
            )
        )

    if isinstance(results, dict):
        add(results.get("raw") or results.get("min"), "output")
        add(results.get("split_screen"), "split_screen")
    if not outputs:
        add(result, "output")
    return outputs


def _higgsfield(raw: dict[str, Any]) -> dict[str, Any]:
    params = _dict(raw.get("params"))
    meta = _dict(raw.get("meta"))
    user = _dict(raw.get("user"))
    model = _str(raw.get("job_set_type")) or _str(raw.get("model")) or _str(raw.get("type"))
    created = raw.get("created_at_unix") or raw.get("created_at") or raw.get("createdAt")
    duration = params.get("duration") or meta.get("duration")
    fail = raw.get("fail_reason")
    return {
        "model_id": model,
        "kind": kind_for(model),
        "service_status": _str(raw.get("status")),
        "status": map_status(raw.get("status")),
        "prompt": _str(params.get("prompt")),
        "negative_prompt": _str(params.get("negative_prompt")),
        "parameters": {k: v for k, v in params.items() if k not in ("prompt", "negative_prompt")}
        | ({"duration": duration} if duration is not None and "duration" not in params else {}),
        "inputs": higgsfield_inputs(params),
        "outputs": higgsfield_outputs(raw.get("results"), raw.get("result")),
        # Higgsfield `cost`: hundredths of a UI credit per the exporter (units UNVERIFIED in
        # docs/adapters/higgsfield.md); stored as reported, in the adapter's currency.
        "credits_cost": _decimal(raw.get("cost")),
        "credits_currency": HIGGSFIELD,
        "fail_reason": fail if isinstance(fail, str) else None,
        "service_created_at": to_datetime(created),
        "service_user_id": _str(raw.get("user_id")) or _str(user.get("id")),
        "service_workspace": _str(raw.get("workspace_name")),
    }


# ---------------------------------------------------------------- custom Seedance interface


def _custom_media(values: Any, kind: str, role: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for v in values if isinstance(values, list) else []:
        s = str(v)
        if s.startswith("asset://"):
            # Asset references live inside the custom tool and aren't directly loadable.
            items.append({"kind": kind, "role": role, "service_asset_id": s[len("asset://") :]})
        else:
            items.append({"kind": media_kind(s, kind), "role": role, "url": s})
    return items


def _custom(raw: dict[str, Any]) -> dict[str, Any]:
    user = _dict(raw.get("user"))
    model = _str(raw.get("model"))
    return {
        "model_id": model,
        "kind": kind_for(model),
        "service_status": _str(raw.get("status")),
        "status": map_status(raw.get("status")),
        "prompt": _str(raw.get("prompt")),
        "negative_prompt": None,
        "parameters": _compact(
            {
                "aspect_ratio": raw.get("aspectRatio"),
                "resolution": raw.get("resolution"),
                "duration": raw.get("duration"),
                "bitrate_mode": raw.get("bitrateMode"),
                "task_mode": raw.get("taskMode"),
            }
        ),
        "inputs": _custom_media(raw.get("imagesList"), "image", "image")
        + _custom_media(raw.get("videoFiles"), "video", "video")
        + _custom_media(raw.get("audioFiles"), "audio", "audio"),
        "outputs": [
            {"index": i, "kind": media_kind(u), "label": "output", "url": u}
            for i, u in enumerate(u for u in raw.get("outputUrls") or [] if isinstance(u, str))
        ],
        "credits_cost": None,
        "credits_currency": "unknown",
        "fail_reason": _str(raw.get("error")),
        "service_created_at": to_datetime(raw.get("createdAt")),
        "service_user_id": _str(user.get("id")),
        "service_workspace": None,
    }


def normalize(service: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The derived fields of one service generation object."""
    return _custom(raw) if service == CUSTOM_INTERFACE else _higgsfield(raw)


# ---------------------------------------------------------------- exporter file formats


def exporter_generation(raw: dict[str, Any], sources: list[str]) -> dict[str, Any]:
    """One record of the exporter's `generations.json` (extension/exporter.js
    normalizeGeneration), so files written by `faible export-history` read in the
    exporter's audit page, review page, merge script and usage dashboard."""
    params = _dict(raw.get("params"))
    settings = {k: params[k] for k in KNOWN_PARAM_KEYS if k in params}
    media = next(
        (params[k] for k in ("medias", "media", "inputs", "references") if k in params), None
    )
    input_media = []
    for item in media if isinstance(media, list) else []:
        if not isinstance(item, dict):
            input_media.append({"value": item})
            continue
        data = _dict(item.get("data"))
        input_media.append(
            {
                "role": item.get("role"),
                "id": data.get("id") or item.get("id"),
                "type": data.get("type") or item.get("type"),
                "url": data.get("url") or item.get("url"),
                "raw": item,
            }
        )
    created = next((raw[k] for k in ("createdAt", "created_at", "created") if k in raw), None)
    return {
        "generation_id": raw.get("id"),
        "type": raw.get("type"),
        "status": raw.get("status"),
        "model": raw.get("model"),
        "created_at": created,
        "prompt": params.get("prompt"),
        "enhanced_prompt": params.get("enhanced_prompt"),
        "negative_prompt": params.get("negative_prompt"),
        "input_media": input_media,
        "output": raw.get("results"),
        "source_response_urls": sorted(sources),
        "all_params": params,
        "settings": settings,
        "raw": raw,
    }
