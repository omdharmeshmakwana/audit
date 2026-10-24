"""Service history and usage captures (Higgsfield exporter integration, DECISIONS #59):
projection and merge, redaction, extension ingest rules, exporter-file import (every
format, idempotent), export back to exporter formats, and rebuild."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_api import login

from faible.history_io import export_history, import_history
from faible.log import EventIn, append_events, verify_log
from faible.models import Event, ServiceGeneration, UsageCapture, User
from faible.rebuild import derived_checksum, rebuild
from faible.service_history import exporter_generation, normalize, to_unix

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuv"


def hf_job(job: str, status: str = "completed", **extra: Any) -> dict[str, Any]:
    """A Higgsfield history object, shaped like fnf/jobs/accessible items."""
    raw: dict[str, Any] = {
        "id": job,
        "job_set_type": "seedance_2_5",
        "status": status,
        "params": {
            "prompt": f"prompt for {job}",
            "duration": 5,
            "medias": [
                {
                    "role": "image",
                    "data": {
                        "id": f"media-{job}",
                        "type": "media_input",
                        "url": f"https://d2ol7oe51mr4n9.cloudfront.net/{job}.png",
                    },
                }
            ],
        },
        "results": None,
        "cost": 10800,
        "created_at": T0.timestamp(),
        "user_id": "user_3ECi",
        "user": {"id": "user_3ECi", "full_name": "Faible 9"},
    }
    if status == "completed":
        raw["results"] = {
            "raw": {"type": "video", "url": f"https://d8j0ntlcm91z4.cloudfront.net/hf_{job}.mp4"}
        }
    return raw | extra


def observe(
    session: Session,
    raw: dict[str, Any],
    *,
    at: datetime = T0,
    actor: uuid.UUID | None = None,
    source: str = "history",
    login: dict[str, Any] | None = None,
) -> str:
    (r,) = append_events(
        session,
        [
            EventIn(
                event_id=uuid.uuid4(),
                type="service_generation.observed",
                occurred_at=at,
                evidence="service_reported",
                payload={
                    "service": "higgsfield",
                    "service_job_id": raw["id"],
                    "source": source,
                    "service_login": login or {"email": "subscriptions.9@faiblemedia.com"},
                    "raw": raw,
                },
            )
        ],
        source="extension",
        actor_user_id=actor,
    )
    return r.status


def test_normalize_matches_the_exporter_rules() -> None:
    n = normalize("higgsfield", hf_job("j1"))
    assert n["model_id"] == "seedance_2_5"
    assert (n["status"], n["service_status"], n["kind"]) == ("succeeded", "completed", "generate")
    assert n["prompt"] == "prompt for j1"
    assert n["credits_cost"] == 10800 and n["credits_currency"] == "higgsfield"
    assert n["inputs"] == [
        {
            "kind": "image",
            "role": "image",
            "url": "https://d2ol7oe51mr4n9.cloudfront.net/j1.png",
            "service_asset_id": "media-j1",
        }
    ]
    assert n["outputs"][0]["kind"] == "video"
    assert n["service_created_at"] == T0
    assert normalize("higgsfield", hf_job("j2", "nsfw"))["status"] == "failed"
    assert normalize("higgsfield", {"id": "u", "job_set_type": "topaz_image"})["kind"] == "upscale"
    # The audit page rewrites created_at as readable IST and keeps created_at_unix.
    assert to_unix("20 Oct 2026, 02:30:00 PM IST") == T0.timestamp()
    audited = hf_job("j3", created_at="20 Oct 2026, 02:30:00 PM IST", created_at_unix=1.0)
    assert normalize("higgsfield", audited)["service_created_at"] == datetime.fromtimestamp(1, UTC)


def multi_input_job(job: str) -> dict[str, Any]:
    """Several media params, in an order JSONB will not keep (found on real exports)."""
    raw = hf_job(job)
    raw["params"]["motion"] = {
        "id": "m-motion",
        "url": "https://d1xarpci4ikg0w.cloudfront.net/m.webp",
    }
    raw["params"]["input_image"] = {
        "id": "m-img",
        "url": "https://d2ol7oe51mr4n9.cloudfront.net/i.png",
    }
    return raw


def reversed_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: reversed_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(v) for v in value]
    return value


def test_normalize_does_not_depend_on_key_order() -> None:
    raw = multi_input_job("j-order")
    assert len(normalize("higgsfield", raw)["inputs"]) == 3
    assert normalize("higgsfield", raw) == normalize("higgsfield", reversed_keys(raw))


def test_observations_merge_into_one_row_and_never_downgrade(session: Session) -> None:
    assert observe(session, hf_job("job-a", "in_progress", cost=None)) == "accepted"
    assert observe(session, hf_job("job-a"), at=T0 + timedelta(minutes=5)) == "accepted"
    # A stale observation arriving later (an old export) fills gaps, never reverts status.
    stale = hf_job("job-a", "queued", fail_reason="stale")
    assert observe(session, stale, at=T0 - timedelta(days=1), source="import") == "accepted"
    rows = list(session.scalars(select(ServiceGeneration)))
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "succeeded" and row.service_status == "completed"
    assert row.outputs and row.raw["status"] == "completed"
    assert row.fail_reason == "stale"  # was empty: filled
    assert row.observation_count == 3
    assert row.first_observed_at == T0 - timedelta(days=1)
    assert row.last_observed_at == T0 + timedelta(minutes=5)
    assert row.login_email == "subscriptions.9@faiblemedia.com"


def test_history_payloads_are_redacted_by_the_schema(session: Session) -> None:
    raw = hf_job("job-r")
    raw["params"]["prompt"] = f"dawn {JWT}"
    raw["access_token"] = "secret-value"
    raw["params"]["url"] = "https://cdn.example/x.png?X-Amz-Signature=abc123&size=1"
    observe(session, raw)
    stored = session.scalars(
        select(Event.payload).where(Event.type == "service_generation.observed")
    ).one()
    text = json.dumps(stored)
    assert JWT not in text and "secret-value" not in text and "abc123" not in text
    assert stored["raw"]["access_token"] == "[REDACTED]"
    assert "size=1" in stored["raw"]["params"]["url"]
    assert verify_log(session).ok  # what was hashed is what was stored


def test_extension_ingest_attributes_the_observer_and_restricts_source(
    client: TestClient, session: Session
) -> None:
    headers = login(client)
    artist = session.scalars(select(User).where(User.email == "artist1@studio")).one()

    def ev(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": str(uuid.uuid4()),
            "type": type_,
            "occurred_at": "2026-10-20T09:00:00Z",
            "evidence": "service_reported",
            "payload": payload,
        }

    history = ev(
        "service_generation.observed",
        {
            "service": "higgsfield",
            "service_job_id": "job-x",
            "source": "history",
            "raw": hf_job("job-x"),
        },
    )
    forged = ev(
        "service_generation.observed",
        {
            "service": "higgsfield",
            "service_job_id": "job-y",
            "source": "import",
            "raw": hf_job("job-y"),
        },
    )
    usage = ev(
        "usage.page_captured",
        {
            "capture_id": str(uuid.uuid4()),
            "service": "higgsfield",
            "source": "extension",
            "page_url": "https://higgsfield.ai/usage",
            "tables": [[["Date", "Credits"], ["20 Oct", "108"]]],
            "text": f"Usage {JWT}",
        },
    )
    r = client.post(
        "/api/v1/ingest/events", headers=headers, json={"events": [history, forged, usage]}
    )
    assert r.status_code == 200
    assert [x["status"] for x in r.json()["results"]] == ["accepted", "rejected", "accepted"]
    row = session.scalars(select(ServiceGeneration)).one()
    assert row.service_job_id == "job-x"
    assert row.first_observed_by_user_id == artist.id  # observer, recorded as such
    cap = session.scalars(select(UsageCapture)).one()
    assert cap.captured_by_user_id == artist.id and cap.tables[0][1] == ["20 Oct", "108"]
    stored = session.scalar(select(Event.payload).where(Event.event_id == cap.event_id))
    assert stored is not None and JWT not in stored["text"]
    assert stored["dom_evidence"] == "observed_dom"


def write_exports(root: Path) -> dict[str, Path]:
    """One file of every format the standalone exporter and its tools produce."""
    acct = {
        "label": "subscriptions.9@faiblemedia.com",
        "email": "subscriptions.9@faiblemedia.com",
        "name": None,
        "username": None,
        "id": "user_3ECi",
    }
    meta = {"exported_at": "2026-10-21T06:53:51.666Z", "account": acct, "generation_count": 2}
    folder = root / "exports" / "subscriptions.9@faiblemedia.com"
    folder.mkdir(parents=True)
    raw = [hf_job("job-1"), hf_job("job-2", "failed", cost=0)]
    files = {
        "raw": folder / "generations_raw.json",
        "normalized": folder / "generations.json",
        "usage": folder / "usage_raw.json",
        "audited": root / "subscriptions.9_generations_audited.json",
        "merged": root / "merged_generations_2026-10-21.json",
    }
    files["raw"].write_text(json.dumps({"meta": meta, "generations": raw}))
    files["normalized"].write_text(
        json.dumps({"meta": meta, "generations": [exporter_generation(r, []) for r in raw]})
    )
    files["usage"].write_text(
        json.dumps(
            {
                "meta": {"account": acct},
                "captures": [
                    {
                        "captured_at": "2026-10-21T07:00:00.000Z",
                        "page_url": "https://higgsfield.ai/usage",
                        "page_title": "Usage",
                        "responses": [
                            {
                                "url": "https://fnf-api-gw.higgsfield.ai/fnf/workspaces/wallet",
                                "page_path": "/usage",
                                "captured_at": "2026-10-21T06:59:00Z",
                                "data": {"total_credits": 73088, "session": "s3cr3t"},
                            }
                        ],
                        "tables": [[["Date", "Credits"], ["21 Oct", "108"]]],
                        "text": "Usage page text",
                    }
                ],
            }
        )
    )
    # audit.js humanizeRecord rewrites created_at (top level and raw) as readable IST.
    ist = "20 Oct 2026, 02:30:00 PM IST"
    audited = exporter_generation(hf_job("job-3", created_at=ist), []) | {"created_at": ist}
    files["audited"].write_text(
        json.dumps({"meta": {**meta, "audit": {"mode": "remove"}}, "generations": [audited]})
    )
    files["merged"].write_text(
        json.dumps(
            {
                "meta": {"merged_at": "21 Oct 2026, 03:00:00 PM IST"},
                "generations": [
                    {
                        "generation_id": "job-1",
                        "source": "higgsfield",
                        "account": {"email": acct["email"]},
                        "outputs": [],
                        "original": hf_job("job-1"),
                    },
                    {
                        "generation_id": "cust-1",
                        "source": "custom_interface",
                        "account": {"email": "artist@faiblemedia.com"},
                        "outputs": [],
                        "original": {
                            "id": "cust-1",
                            "requestId": "r1",
                            "model": "dreamina-seedance-2-0-260128",
                            "status": "COMPLETED",
                            "prompt": "custom tool prompt",
                            "createdAt": "2026-10-19T10:00:00Z",
                            "outputUrls": ["https://cdn.example/out.mp4"],
                            "user": {"email": "artist@faiblemedia.com", "id": "u1"},
                        },
                    },
                ],
            }
        )
    )
    return files


def test_import_reads_every_exporter_format_once(session: Session, tmp_path: Path) -> None:
    files = write_exports(tmp_path)
    reports = {Path(r.path).name: r for r in import_history(session, [tmp_path])}
    assert reports["generations.json"].format == "skipped"  # same records as the raw file
    assert reports["generations_raw.json"].accepted == 2
    assert reports["usage_raw.json"].accepted == 1
    assert reports["subscriptions.9_generations_audited.json"].accepted == 1
    merged = reports["merged_generations_2026-10-21.json"]
    assert (merged.accepted, merged.unchanged) == (1, 1)  # job-1 already recorded from raw

    rows = {r.service_job_id: r for r in session.scalars(select(ServiceGeneration))}
    assert set(rows) == {"job-1", "job-2", "job-3", "cust-1"}
    assert rows["job-2"].status == "failed" and rows["job-2"].credits_cost == 0
    assert rows["job-3"].service_created_at == T0  # parsed from the audit page's IST string
    assert rows["cust-1"].service == "custom_interface" and rows["cust-1"].status == "succeeded"
    assert all(r.first_observed_by_user_id is None for r in rows.values())  # nobody's work
    assert rows["job-1"].login_email == "subscriptions.9@faiblemedia.com"
    usage = session.scalars(select(UsageCapture)).one()
    payload = session.scalar(select(Event.payload).where(Event.event_id == usage.event_id))
    assert payload is not None and payload["responses"][0]["data"]["session"] == "[REDACTED]"

    events = session.scalar(select(func.count()).select_from(Event))
    again = import_history(session, [files["raw"], files["usage"], files["merged"]])
    assert sum(r.accepted for r in again) == 0
    assert session.scalar(select(func.count()).select_from(Event)) == events
    assert verify_log(session).ok


def test_import_dry_run_writes_nothing(session: Session, tmp_path: Path) -> None:
    write_exports(tmp_path)
    before = session.scalar(select(func.count()).select_from(Event))
    reports = import_history(session, [tmp_path], dry_run=True)
    assert sum(r.accepted for r in reports) >= 4
    assert session.scalar(select(func.count()).select_from(Event)) == before


def test_export_round_trips_through_the_exporter_formats(session: Session, tmp_path: Path) -> None:
    write_exports(tmp_path / "in")
    import_history(session, [tmp_path / "in"])
    out = tmp_path / "out"
    written = export_history(session, out)
    names = {p.relative_to(out).as_posix() for p in written}
    assert {
        "subscriptions.9@faiblemedia.com/generations.json",
        "subscriptions.9@faiblemedia.com/generations_raw.json",
        "subscriptions.9@faiblemedia.com/usage_raw.json",
    } <= names
    assert any(n.startswith("custom_interface/seedance-export-") for n in names)
    folder = out / "subscriptions.9@faiblemedia.com"
    normalized = json.loads((folder / "generations.json").read_text())
    assert normalized["meta"]["account"]["email"] == "subscriptions.9@faiblemedia.com"
    first = normalized["generations"][0]
    assert {
        "generation_id",
        "status",
        "prompt",
        "input_media",
        "output",
        "all_params",
        "raw",
    } <= set(first)
    usage = json.loads((folder / "usage_raw.json").read_text())
    assert usage["captures"][0]["tables"] == [[["Date", "Credits"], ["21 Oct", "108"]]]
    # Exported files read back as already-recorded content: nothing new.
    reports = import_history(session, [out])
    assert sum(r.accepted for r in reports if r.format != "usage") == 0
    with pytest.raises(FileExistsError):
        export_history(session, out)  # never overwrites


def test_rebuild_reproduces_history_and_usage_tables(session: Session, tmp_path: Path) -> None:
    write_exports(tmp_path)
    import_history(session, [tmp_path])
    observe(session, hf_job("job-1", fail_reason=None), at=T0 + timedelta(days=2))
    observe(session, multi_input_job("job-4"))
    before = derived_checksum(session)
    assert before.tables["service_generations"][0] == 5
    assert before.tables["usage_captures"][0] == 1
    rebuild(session)
    assert derived_checksum(session).total == before.total
