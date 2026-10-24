"""Higgsfield exporter files ⇄ the Faible log (DECISIONS #59).

`faible import-history PATH…` reads every format the standalone exporter and its tools
write, and appends them as events:

    <account>/generations_raw.json   raw Higgsfield objects           service_generation.observed
    <account>/generations.json       normalized (raw under "raw")     service_generation.observed
    *_audited.json / *_selected.json either of the above, audited     service_generation.observed
    merged/merged_generations_*.json merge_exports.py output           service_generation.observed
    seedance-export-*.json           the custom Seedance interface     service_generation.observed
    <account>/usage_raw.json         "Save usage page" captures        usage.page_captured

Imported records are attributed to nobody (source `import`, no actor): an export names the
shared Higgsfield login, never the employee. Event ids are a function of the file, the
record and its content, and `occurred_at` of the file, so importing a file again is a no-op
(every event `duplicate`). Payloads are redacted by the event schema with Faible's rules,
which are stricter than the exporter's.

`faible export-history --out DIR` writes the merged history back out in the exporter's own
formats, one folder per service login, so the exporter's dashboard, audit page, review page
and merge script read it. It never writes into an existing non-empty folder.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.canonical import canonical_json
from faible.events import Evidence, validate_payload
from faible.log import EventIn, append_events
from faible.models import (
    UUID_NS,
    Event,
    ServiceGeneration,
    UsageCapture,
    service_generation_id_for,
)
from faible.service_history import (
    CUSTOM_INTERFACE,
    HIGGSFIELD,
    exporter_generation,
    to_datetime,
)

BATCH = 500


@dataclass
class FileReport:
    path: str
    format: str
    records: int = 0
    accepted: int = 0
    duplicate: int = 0
    conflict: int = 0
    rejected: int = 0
    skipped: int = 0
    unchanged: int = 0  # content already recorded: no event appended
    reasons: list[str] = field(default_factory=list)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _login(account: Any, detected_by: str = "import") -> dict[str, Any] | None:
    """Exporter `meta.account` / merged `account` → ServiceLogin. The shared service login."""
    if not isinstance(account, dict):
        return None
    login = {
        "service_user_id": account.get("id") if isinstance(account.get("id"), str) else None,
        "email": account.get("email") if isinstance(account.get("email"), str) else None,
        "name": account.get("name") if isinstance(account.get("name"), str) else None,
        "username": account.get("username") if isinstance(account.get("username"), str) else None,
        "label": account.get("label") if isinstance(account.get("label"), str) else None,
        "workspace_id": None,
        "detected_by": detected_by,
    }
    return login if any(v for k, v in login.items() if k != "detected_by") else None


def _files(paths: list[Path]) -> Iterator[tuple[Path, str | None]]:
    """(file, reason-to-skip). Inside one export folder generations.json holds the same
    records as generations_raw.json, so only the raw file is read."""
    for p in paths:
        files = sorted(p.rglob("*.json")) if p.is_dir() else [p]
        for f in files:
            if f.name == "generations.json" and (f.parent / "generations_raw.json").exists():
                yield f, "same records as generations_raw.json in this folder"
            else:
                yield f, None


def _detect(data: Any) -> tuple[str, list[Any], dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("captures"), list):
        return "usage", data["captures"], data.get("meta") or {}
    records = (
        data
        if isinstance(data, list)
        else data.get("generations")
        if isinstance(data, dict)
        else None
    )
    if not isinstance(records, list):
        raise ValueError("no 'generations' or 'captures' list")
    meta = (data.get("meta") or {}) if isinstance(data, dict) else {}
    sample = next((r for r in records if isinstance(r, dict)), {})
    if "requestId" in sample or "outputUrls" in sample:
        return (
            "custom",
            records,
            {"exported_at": data.get("exportedAt")} if isinstance(data, dict) else {},
        )
    if "source" in sample and "original" in sample:
        return "merged", records, meta
    if "generation_id" in sample:
        return "normalized", records, meta
    if "job_set_type" in sample or "params" in sample:
        return "raw", records, meta
    if not records:
        return "empty", records, meta
    raise ValueError("unrecognised export format")


def _occurred(meta: dict[str, Any], path: Path) -> datetime:
    for key in ("exported_at", "merged_at"):
        dt = to_datetime(meta.get(key))
        if dt:
            return dt
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _generation_payloads(
    fmt: str, records: list[Any], meta: dict[str, Any], source_file: str
) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """(content key, payload or None when the record has no usable id)."""
    file_login = _login(meta.get("account"))
    for rec in records:
        if not isinstance(rec, dict):
            yield "", None
            continue
        if fmt == "merged":
            service = rec.get("source")
            raw = rec.get("original")
            login = _login(rec.get("account"))
            job = rec.get("generation_id") or (raw or {}).get("id")
        elif fmt == "custom":
            service, raw, job = CUSTOM_INTERFACE, rec, rec.get("id")
            login = _login(rec.get("user"))
        else:
            service = HIGGSFIELD
            raw = rec.get("raw") if fmt == "normalized" else rec
            job = rec.get("generation_id") or (raw or {}).get("id")
            login = file_login
        if service not in (HIGGSFIELD, CUSTOM_INTERFACE) or not isinstance(raw, dict) or not job:
            yield "", None
            continue
        payload = {
            "service": service,
            "service_job_id": str(job),
            "source": "import",
            "service_login": login,
            "source_file": source_file,
            "raw": raw,
        }
        yield f"{service}:{job}:{_sha(raw)}", payload


def _usage_payloads(
    records: list[Any], meta: dict[str, Any], source_file: str
) -> Iterator[tuple[str, dict[str, Any] | None, datetime | None]]:
    login = _login(meta.get("account"))
    for cap in records:
        if not isinstance(cap, dict) or not isinstance(cap.get("page_url"), str):
            yield "", None, None
            continue
        key = f"usage:{cap.get('captured_at')}:{cap['page_url']}"
        responses = [
            {
                "url": str(r.get("url") or ""),
                "page_path": r.get("page_path"),
                "captured_at": r.get("captured_at"),
                "data": r.get("data"),
            }
            for r in cap.get("responses") or []
            if isinstance(r, dict)
        ]
        tables = [
            [[str(c) for c in row] for row in t if isinstance(row, list)]
            for t in cap.get("tables") or []
            if isinstance(t, list)
        ]
        payload = {
            "capture_id": str(uuid.uuid5(UUID_NS, f"import:{source_file}:{key}")),
            "service": HIGGSFIELD,
            "source": "import",
            "page_url": cap["page_url"],
            "page_title": cap.get("page_title"),
            "service_login": login,
            "responses": responses,
            "tables": tables,
            "text": str(cap.get("text") or "")[:500_000],
            "source_file": source_file,
        }
        yield key, payload, to_datetime(cap.get("captured_at"))


def import_history(
    session: Session, paths: list[Path], *, dry_run: bool = False
) -> list[FileReport]:
    """Append every record of the given files/folders. Commits after each file (a large
    export is several thousand events); a dry run validates without writing."""
    reports: list[FileReport] = []
    # Merged and audited files repeat records already in the per-account exports. A record
    # whose (redacted) content is already the stored state of its job, or was already read
    # in this run, adds nothing: it is counted as unchanged instead of growing the log.
    seen: set[str] = set()
    for path, skip in _files(paths):
        source_file = f"{path.parent.name}/{path.name}"
        if skip:
            reports.append(FileReport(str(path), "skipped", reasons=[skip]))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fmt, records, meta = _detect(data)
        except (OSError, ValueError) as err:
            reports.append(FileReport(str(path), "skipped", reasons=[str(err)]))
            continue
        report = FileReport(str(path), fmt, records=len(records))
        reports.append(report)
        default_at = _occurred(meta, path)
        items: list[EventIn] = []
        if fmt == "usage":
            for key, usage, at in _usage_payloads(records, meta, source_file):
                if usage is None:
                    report.skipped += 1
                    continue
                items.append(
                    _event(
                        "usage.page_captured", "observed", key, source_file, at or default_at, usage
                    )
                )
        else:
            for key, gen in _generation_payloads(fmt, records, meta, source_file):
                if gen is None:
                    report.skipped += 1
                    continue
                content = _content_key(gen)
                if content is not None:
                    if content in seen:
                        report.unchanged += 1
                        continue
                    seen.add(content)
                    if _recorded(session, gen, content):
                        report.unchanged += 1
                        continue
                items.append(
                    _event(
                        "service_generation.observed",
                        "service_reported",
                        key,
                        source_file,
                        default_at,
                        gen,
                    )
                )
        if dry_run:
            for item in items:
                try:
                    validate_payload(item.type, item.payload)
                    report.accepted += 1
                except ValidationError as err:
                    report.rejected += 1
                    if len(report.reasons) < 3:
                        report.reasons.append(str(err.errors()[0]["msg"]))
            continue
        for start in range(0, len(items), BATCH):
            for r in append_events(session, items[start : start + BATCH], source="import"):
                setattr(report, r.status, getattr(report, r.status) + 1)
                if r.reason and len(report.reasons) < 3:
                    report.reasons.append(r.reason)
        session.commit()
    return reports


def _content_key(payload: dict[str, Any]) -> str | None:
    """service:job:sha256 of the raw object as the log would store it (redacted)."""
    try:
        raw = validate_payload("service_generation.observed", payload)["raw"]
    except ValidationError:
        return None  # append_events reports it as rejected
    return f"{payload['service']}:{payload['service_job_id']}:{_sha(raw)}"


def _recorded(session: Session, payload: dict[str, Any], content: str) -> bool:
    row = session.get(
        ServiceGeneration, service_generation_id_for(payload["service"], payload["service_job_id"])
    )
    return row is not None and content.endswith(f":{_sha(row.raw)}")


def _event(
    type_: str,
    evidence: Evidence,
    key: str,
    source_file: str,
    at: datetime,
    payload: dict[str, Any],
) -> EventIn:
    return EventIn(
        event_id=uuid.uuid5(UUID_NS, f"import:{type_}:{source_file}:{key}"),
        type=type_,
        occurred_at=at,
        evidence=evidence,
        payload=payload,
    )


# ---------------------------------------------------------------- export


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.@+-]+", "_", s)[:80] or "account"


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def export_history(session: Session, out: Path) -> list[Path]:
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; export-history never overwrites files")
    exported_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    note = (
        "Exported from Faible's service history (service_generations): every Higgsfield "
        "generation the extension saw in Higgsfield's own history or that was imported from "
        "an exporter file, merged per job id. The account is the shared Higgsfield login, not "
        "an employee; employee attribution lives in Faible's activities."
    )
    written: list[Path] = []
    groups: dict[tuple[str, str], list[ServiceGeneration]] = {}
    for row in session.scalars(
        select(ServiceGeneration).order_by(
            ServiceGeneration.service_created_at.desc().nulls_last(),
            ServiceGeneration.service_job_id,
        )
    ):
        login = row.login_email or row.login_label or row.service_user_id or "unknown"
        groups.setdefault((row.service, login), []).append(row)
    for (service, login), rows in sorted(groups.items()):
        if service == CUSTOM_INTERFACE:
            path = out / CUSTOM_INTERFACE / f"seedance-export-{_safe_name(login)}.json"
            _write(
                path,
                {
                    "exportedAt": exported_at,
                    "count": len(rows),
                    "generations": [r.raw for r in rows],
                },
            )
            written.append(path)
            continue
        account = {
            "label": login,
            "email": rows[0].login_email,
            "name": None,
            "username": None,
            "id": rows[0].service_user_id,
        }
        meta = {
            "exported_at": exported_at,
            "account": account,
            "generation_count": len(rows),
            "note": note,
        }
        folder = out / _safe_name(login)
        _write(
            folder / "generations.json",
            {
                "meta": meta,
                "generations": [exporter_generation(r.raw, list(r.sources)) for r in rows],
            },
        )
        _write(
            folder / "generations_raw.json", {"meta": meta, "generations": [r.raw for r in rows]}
        )
        written += [folder / "generations.json", folder / "generations_raw.json"]

    usage: dict[str, list[dict[str, Any]]] = {}
    for cap in session.scalars(select(UsageCapture).order_by(UsageCapture.captured_at)):
        payload = session.scalar(select(Event.payload).where(Event.event_id == cap.event_id)) or {}
        usage.setdefault(cap.login_email or cap.login_label or "unknown", []).append(
            {
                "captured_at": cap.captured_at.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "page_url": cap.page_url,
                "page_title": cap.page_title,
                "responses": payload.get("responses") or [],
                "tables": cap.tables,
                "text": payload.get("text") or "",
            }
        )
    for login, captures in sorted(usage.items()):
        path = out / _safe_name(login) / "usage_raw.json"
        _write(
            path,
            {
                "meta": {
                    "exported_at": exported_at,
                    "account": {"label": login, "email": login if "@" in login else None},
                    "capture_count": len(captures),
                    "note": "Each capture is one Higgsfield page (e.g. Usage or Organization) "
                    "saved with “Save usage page”: the JSON responses it loaded, plus the tables "
                    "and text it displayed at that moment (read from the page: observed_dom).",
                },
                "captures": captures,
            },
        )
        written.append(path)
    return written
