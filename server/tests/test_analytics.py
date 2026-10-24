"""Employee and studio analytics (DECISIONS #59): attribution to the Faible user, one count
per generation across live capture and service history, role isolation, time zones,
drill-down into the live feed, prompt-chain links and usage-capture privacy."""

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_api import login
from test_history import hf_job, observe

from faible.chain import asset_chain
from faible.models import User, asset_id_for

OCT = {"from": "2026-10-01", "to": "2026-10-31", "tz": "UTC"}
SHA_A, SHA_B = "1" * 64, "2" * 64


def ev(
    type_: str, at: str, payload: dict[str, Any], evidence: str = "service_reported"
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "type": type_,
        "occurred_at": at,
        "evidence": evidence,
        "payload": payload,
    }


def requested(
    job: str, *, model: str = "m-oct", cost: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "activity_id": str(uuid.uuid4()),
        "service": "higgsfield",
        "service_job_id": job,
        "model_id": model,
        "prompt": f"prompt {job}",
        "credits_cost": cost,
        "credits_currency": "higgsfield",
        **extra,
    }


def ingest(client: TestClient, headers: dict[str, str], *events: dict[str, Any]) -> None:
    r = client.post("/api/v1/ingest/events", headers=headers, json={"events": list(events)})
    assert r.status_code == 200, r.text
    assert all(x["status"] == "accepted" for x in r.json()["results"]), r.json()


def user(session: Session, email: str) -> User:
    return session.scalars(select(User).where(User.email == email)).one()


def studio(client: TestClient, session: Session) -> dict[str, dict[str, str]]:
    """artist1: J1 (no cost live; 10800 in history) and J2 (150, failed); artist2: J3.
    History lists J1 (matched) and K1 (nobody's live capture)."""
    a1, a2 = login(client), login(client, "artist2@studio")
    ingest(
        client,
        a1,
        ev("generation.requested", "2026-10-13T20:00:00Z", requested("J1")),
        ev("generation.requested", "2026-10-14T09:00:00Z", requested("J2", cost="150")),
        ev(
            "generation.status",
            "2026-10-14T09:01:00Z",
            {"service": "higgsfield", "service_job_id": "J2", "status": "failed"},
        ),
    )
    ingest(
        client,
        a2,
        ev("generation.requested", "2026-10-15T09:00:00Z", requested("J3", model="m-two")),
    )
    observe(session, hf_job("J1"))
    observe(session, hf_job("K1", job_set_type="m-hist"))
    session.commit()
    return {"a1": a1, "a2": a2, "director": login(client, "director@studio")}


def test_artist_sees_only_their_generations_counted_once(
    client: TestClient, session: Session
) -> None:
    h = studio(client, session)
    data = client.get("/api/v1/analytics", headers=h["a1"], params=OCT).json()
    assert data["scope"]["employee"]["label"] == "artist1@studio"
    t = data["totals"]
    assert (t["count"], t["succeeded"], t["failed"]) == (2, 0, 1)
    # J2 reported 150 live; J1 had no live cost and takes the 10800 its history reports.
    assert t["credits"] == {"higgsfield": "10950"}
    assert (t["cost_known"], t["cost_from_history"], t["cost_unknown"]) == (2, 1, 0)
    assert t["estimated_ui_credits"] == "109.5"
    assert data["employees"] is None and data["history"] is None  # studio-only sections
    assert {r["key"] for r in data["breakdowns"]["model"]} == {"m-oct"}
    # Dropdowns list the artist's own models only: never artist2's or the studio history's.
    assert "m-oct" in data["options"]["models"]
    assert not {"m-two", "m-hist"} & set(data["options"]["models"])
    assert data["rules"]["deduplication"]
    other = user(session, "artist2@studio").id
    assert (
        client.get(
            "/api/v1/analytics", headers=h["a1"], params={"employee_id": str(other)}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/service-history", headers=h["a1"]).status_code == 403
    assert client.get("/api/v1/analytics").status_code == 401


def test_director_compares_employees_and_sees_unattributed_history(
    client: TestClient, session: Session
) -> None:
    h = studio(client, session)
    data = client.get("/api/v1/analytics", headers=h["director"], params=OCT).json()
    assert data["totals"]["count"] == 3
    ranks = {r["label"]: (r["rank"], r["count"]) for r in data["employees"]}
    assert ranks["artist1@studio"] == (1, 2)
    assert ranks["artist2@studio"] == (2, 1)
    assert ranks["director@studio"] == (None, 0)
    pairs = {(m["model"], m["count"]) for m in data["employee_models"]}
    assert ("m-oct", 2) in pairs and ("m-two", 1) in pairs
    history = data["history"]
    assert history["applies"] is True
    assert (history["totals"]["count"], history["totals"]["unattributed"]) == (2, 1)
    # Only K1's cost is unattributed: J1's is counted with artist1's activity.
    assert history["totals"]["credits"] == {"higgsfield": "10800"}
    page = client.get("/api/v1/service-history", headers=h["director"], params=OCT).json()
    by_job = {i["service_job_id"]: i for i in page["items"]}
    assert by_job["J1"]["attributed_to"]["email"] == "artist1@studio"
    assert by_job["K1"]["attributed_to"] is None
    unattributed = client.get(
        "/api/v1/service-history", headers=h["director"], params={**OCT, "unattributed": "true"}
    ).json()
    assert [i["service_job_id"] for i in unattributed["items"]] == ["K1"]
    # Drill into one employee: history narrows to what that employee captured live.
    a1 = user(session, "artist1@studio").id
    one = client.get(
        "/api/v1/analytics", headers=h["director"], params={**OCT, "employee_id": str(a1)}
    ).json()
    assert one["totals"]["count"] == 2 and one["employees"] is None
    assert one["history"]["applies"] is False and one["history"]["totals"]["unattributed"] == 0


def test_time_zone_decides_days_hours_and_ranges(client: TestClient, session: Session) -> None:
    h = studio(client, session)
    j1 = {"model": "m-oct", "status": "queued"}
    utc = client.get("/api/v1/analytics", headers=h["a1"], params={**OCT, **j1}).json()
    ist = client.get(
        "/api/v1/analytics", headers=h["a1"], params={**OCT, **j1, "tz": "Asia/Kolkata"}
    ).json()
    assert [(d["day"], d["count"]) for d in utc["by_day"]] == [("2026-10-13", 1)]
    assert [(d["day"], d["count"]) for d in ist["by_day"]] == [("2026-10-14", 1)]
    assert [x["hour"] for x in utc["by_hour"]] == [20]
    assert [x["hour"] for x in ist["by_hour"]] == [1]  # 20:00 UTC = 01:30 IST
    day = {"from": "2026-10-14", "to": "2026-10-14", **j1}
    assert (
        client.get("/api/v1/analytics", headers=h["a1"], params={**day, "tz": "UTC"}).json()[
            "totals"
        ]["count"]
        == 0
    )
    assert (
        client.get(
            "/api/v1/analytics", headers=h["a1"], params={**day, "tz": "Asia/Kolkata"}
        ).json()["totals"]["count"]
        == 1
    )
    assert (
        client.get("/api/v1/analytics", headers=h["a1"], params={"tz": "Mars/Base"}).status_code
        == 422
    )
    bad_range = {"from": "2026-10-20", "to": "2026-10-01"}
    assert client.get("/api/v1/analytics", headers=h["a1"], params=bad_range).status_code == 422


def test_filters_drill_down_into_the_live_feed(client: TestClient, session: Session) -> None:
    h = studio(client, session)
    feed = client.get(
        "/api/v1/dashboard", headers=h["director"], params={**OCT, "status": "failed"}
    ).json()
    assert [a["service_job_id"] for a in feed["activities"]] == ["J2"]
    assert feed["history"]["total"] == 1
    totals = {s["email"]: s["generation_count"] for s in feed["summaries"]}
    assert totals["artist1@studio"] == 1 and totals["artist2@studio"] == 0
    unassigned = client.get(
        "/api/v1/dashboard", headers=h["a1"], params={**OCT, "shot": "(none)", "model": "m-oct"}
    ).json()
    assert {a["service_job_id"] for a in unassigned["activities"]} == {"J1", "J2"}
    # The unfiltered feed is unchanged (all-time history, as before).
    assert client.get("/api/v1/dashboard", headers=h["a1"]).json()["history"]["total"] >= 2


def test_prompt_rows_link_the_same_neighbours_as_the_chain(
    client: TestClient, session: Session
) -> None:
    a1 = login(client)
    first, second = requested("C1", model="m-chain"), requested("C2", model="m-chain")
    second["inputs"] = [{"role": "reference", "sha256": SHA_A}]
    second["prompt"] = "prompt C1 but at night"
    ingest(
        client,
        a1,
        ev("generation.requested", "2026-10-16T09:00:00Z", first),
        ev(
            "generation.completed",
            "2026-10-16T09:01:00Z",
            {"activity_id": first["activity_id"], "outputs": [{"index": 0, "sha256": SHA_A}]},
        ),
        ev("asset.registered", "2026-10-16T09:01:01Z", {"sha256": SHA_A}, "observed"),
        ev("generation.requested", "2026-10-16T09:05:00Z", second),
        ev(
            "generation.completed",
            "2026-10-16T09:06:00Z",
            {"activity_id": second["activity_id"], "outputs": [{"index": 0, "sha256": SHA_B}]},
        ),
        ev("asset.registered", "2026-10-16T09:06:01Z", {"sha256": SHA_B}, "observed"),
    )
    data = client.get("/api/v1/analytics", headers=a1, params={**OCT, "model": "m-chain"}).json()
    rows = {r["prompt"]: r for r in data["prompts"]}
    c1, c2 = rows["prompt C1"], rows["prompt C1 but at night"]
    assert [u["id"] for u in c2["upstream"]] == [c1["id"]]
    assert [d["id"] for d in c1["downstream"]] == [c2["id"]]
    assert c2["chain_url"] == f"/assets/{asset_id_for(SHA_B)}"
    assert c2["inputs"][0]["asset"]["sha256"] == SHA_A
    assert data["totals"]["prompts"] == {"total": 2, "distinct": 2}
    # The chain itself is untouched: both steps, the second with its word diff.
    chain = asset_chain(session, asset_id_for(SHA_B))
    assert chain is not None
    steps = [s for s in chain["steps"] if s["type"] == "activity"]
    assert [s["prompt"] for s in steps] == ["prompt C1", "prompt C1 but at night"]
    assert steps[1]["prompt_diff"] and any(seg["op"] == "insert" for seg in steps[1]["prompt_diff"])


def test_usage_captures_and_downloads_follow_the_role_model(
    client: TestClient, session: Session
) -> None:
    h = studio(client, session)
    capture_id = str(uuid.uuid4())
    ingest(
        client,
        h["a1"],
        ev(
            "usage.page_captured",
            "2026-10-17T09:00:00Z",
            {
                "capture_id": capture_id,
                "service": "higgsfield",
                "source": "extension",
                "page_url": "https://higgsfield.ai/usage",
                "page_title": "Usage",
                "service_login": {
                    "email": "subscriptions.9@faiblemedia.com",
                    "detected_by": "clerk",
                },
                "responses": [
                    {"url": "https://fnf-api-gw.higgsfield.ai/fnf/workspaces/wallet", "data": {}}
                ],
                "tables": [[["Date", "Credits"], ["17 Oct", "108"]]],
                "text": "Usage 108",
            },
            "observed",
        ),
        ev(
            "download.observed",
            "2026-10-17T10:00:00Z",
            {"download_id": str(uuid.uuid4()), "filename": "hf_x.mp4", "service": "higgsfield"},
            "observed",
        ),
    )
    mine = client.get("/api/v1/usage-captures", headers=h["a1"]).json()
    assert mine["total"] == 1 and mine["items"][0]["login"] == "subscriptions.9@faiblemedia.com"
    detail = client.get(f"/api/v1/usage-captures/{capture_id}", headers=h["a1"]).json()
    assert detail["tables"] == [[["Date", "Credits"], ["17 Oct", "108"]]]
    assert detail["dom_evidence"] == "observed_dom" and detail["text_excerpt"] == "Usage 108"
    assert client.get("/api/v1/usage-captures", headers=h["a2"]).json()["total"] == 0
    assert client.get(f"/api/v1/usage-captures/{capture_id}", headers=h["a2"]).status_code == 404
    assert client.get("/api/v1/usage-captures", headers=h["director"]).json()["total"] == 1
    a1 = client.get("/api/v1/analytics", headers=h["a1"], params=OCT).json()
    assert a1["totals"]["downloads"] == 1 and a1["downloads"]["recent"][0]["filename"] == "hf_x.mp4"
    assert a1["usage_captures"]["total"] == 1
    a2 = client.get("/api/v1/analytics", headers=h["a2"], params=OCT).json()
    assert a2["totals"]["downloads"] == 0 and a2["usage_captures"]["total"] == 0
