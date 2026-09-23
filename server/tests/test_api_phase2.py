"""Phase 2 endpoints: project tree, active shot, asset ingest, shot detail, versions, links,
and the full capture path as the extension drives it."""

import hashlib
import io
import json
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from PIL import Image

from faible.storage import asset_path


def login(client: TestClient, email: str = "artist1@studio") -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "changeme"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def png(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buf, format="PNG")
    return buf.getvalue()


def demo_shot(client: TestClient, h: dict[str, str], code: str = "SQ01_SH020") -> str:
    (project,) = client.get("/api/v1/projects", headers=h).json()
    tree = client.get(f"/api/v1/projects/{project['id']}/tree", headers=h).json()
    shots = {s["code"]: s["id"] for seq in tree["sequences"] for s in seq["shots"]}
    assert len(shots) == 10 and {e["name"] for e in tree["entities"]} == {
        "Mira",
        "Kesh",
        "The Warden",
    }
    return shots[code]


def upload(
    client: TestClient,
    h: dict[str, str],
    data: bytes,
    locations: list[dict[str, str]],
    sha: str | None = None,
):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/ingest/asset",
        headers={**h, "X-Faible-Sha256": sha or hashlib.sha256(data).hexdigest()},
        files={"file": ("out.png", data, "image/png")},
        data={"locations": json.dumps(locations)},
    )


def events(*items: tuple[str, dict[str, object], str]) -> dict[str, object]:
    return {
        "events": [
            {
                "event_id": str(uuid.uuid4()),
                "type": t,
                "occurred_at": datetime.now(UTC).isoformat(),
                "evidence": ev,
                "payload": p,
            }
            for t, p, ev in items
        ]
    }


def test_active_shot(client: TestClient) -> None:
    h = login(client)
    shot = demo_shot(client, h)
    assert (
        client.put("/api/v1/me/active-shot", json={"shot_id": shot}, headers=h).status_code == 200
    )
    assert client.get("/api/v1/me", headers=h).json()["active_shot_id"] == shot
    missing = client.put("/api/v1/me/active-shot", json={"shot_id": str(uuid.uuid4())}, headers=h)
    assert missing.status_code == 404


def test_ingest_asset_checks_the_hash_and_stores_the_bytes(client: TestClient) -> None:
    h = login(client)
    data = png((10, 20, 30))
    bad = upload(client, h, data, [], sha="0" * 64)
    assert bad.status_code == 422 and "mismatch" in bad.json()["detail"]

    ok = upload(
        client,
        h,
        data,
        [{"service": "higgsfield", "url": "https://cdn/x.png", "service_asset_id": "job-1"}],
    )
    body = ok.json()
    assert ok.status_code == 200 and body["created"] is True and body["bytes"] == len(data)
    assert asset_path(body["sha256"]).read_bytes() == data

    again = upload(
        client, h, data, [{"service": "magnific", "service_asset_id": "CqUjWZfEEy"}]
    ).json()
    assert again["created"] is False and again["new_locations"] == 1


def test_capture_path_end_to_end(client: TestClient) -> None:
    """What the extension does for one generation: requested → completed → output bytes
    registered → the shot shows prompt, credits and a resolved output; then an upscale on
    another service uses it and the chain links through the asset."""
    h = login(client)
    shot = demo_shot(client, h)
    client.put("/api/v1/me/active-shot", json={"shot_id": shot}, headers=h)
    hf, mg = str(uuid.uuid4()), str(uuid.uuid4())
    out1, out2 = png((200, 50, 50)), png((50, 200, 50))

    r = client.post(
        "/api/v1/ingest/events",
        json=events(
            (
                "generation.requested",
                {
                    "activity_id": hf,
                    "service": "higgsfield",
                    "service_job_id": "c09533a2",
                    "model_id": "nano_banana_flash",
                    "prompt": "Mira at the window",
                    "credits_cost": "150",
                    "shot_id": shot,
                },
                "service_reported",
            ),
            (
                "generation.completed",
                {
                    "service": "higgsfield",
                    "service_job_id": "c09533a2",
                    "outputs": [
                        {"index": 0, "url": "https://cdn/hf.png", "service_asset_id": "c09533a2"}
                    ],
                },
                "service_reported",
            ),
        ),
        headers=h,
    )
    assert [x["status"] for x in r.json()["results"]] == ["accepted", "accepted"]
    sha1 = upload(
        client,
        h,
        out1,
        [{"service": "higgsfield", "url": "https://cdn/hf.png", "service_asset_id": "c09533a2"}],
    ).json()["sha256"]

    # The artist uploads that output to Magnific (creation CqUjWZfEEy) and upscales it.
    client.post(
        "/api/v1/ingest/events",
        json=events(
            (
                "asset.location_seen",
                {"sha256": sha1, "service": "magnific", "service_asset_id": "CqUjWZfEEy"},
                "observed",
            ),
            (
                "generation.requested",
                {
                    "activity_id": mg,
                    "service": "magnific",
                    "kind": "upscale",
                    "service_job_id": "ks6OBy616B",
                    "model_id": "enhance-magnific-creative",
                    "prompt": "subtle",
                    "credits_cost": "1080",
                    "shot_id": shot,
                    "inputs": [{"role": "source", "service_asset_id": "CqUjWZfEEy"}],
                },
                "service_reported",
            ),
            (
                "generation.completed",
                {
                    "service": "magnific",
                    "service_job_id": "ks6OBy616B",
                    "outputs": [{"index": 0, "service_asset_id": "ks6OBy616B"}],
                },
                "service_reported",
            ),
        ),
        headers=h,
    )
    sha2 = upload(
        client, h, out2, [{"service": "magnific", "service_asset_id": "ks6OBy616B"}]
    ).json()["sha256"]

    detail = client.get(f"/api/v1/shots/{shot}", headers=h).json()
    gen, ups = detail["activities"]
    assert (gen["prompt"], gen["credits_cost"], gen["captured_by"]) == (
        "Mira at the window",
        "150",
        "Artist One",
    )
    assert gen["outputs"][0]["sha256"] == sha1 and gen["outputs"][0]["resolved"]
    assert ups["inputs"][0]["sha256"] == sha1  # the chain links through the shared asset
    assert ups["outputs"][0]["sha256"] == sha2
    assert detail["credit_spend"] == "1230"

    v = client.post(
        f"/api/v1/shots/{shot}/versions", json={"sha256": sha2, "notes": "upscaled"}, headers=h
    ).json()
    assert v["version_number"] == 1
    assert client.get(f"/api/v1/shots/{shot}", headers=h).json()["versions"][0]["sha256"] == sha2

    captures = client.get("/api/v1/me/captures", headers=h).json()
    assert captures["unassigned_count"] == 0
    assert captures["recent"][0]["shot_code"] == "SQ01_SH020"


def test_assign_to_shot_via_links(client: TestClient) -> None:
    h = login(client)
    shot = demo_shot(client, h, "SQ02_SH010")
    act = str(uuid.uuid4())
    client.post(
        "/api/v1/ingest/events",
        json=events(
            (
                "generation.requested",
                {
                    "activity_id": act,
                    "service": "higgsfield",
                    "service_job_id": "loose-1",
                    "prompt": "unassigned",
                },
                "service_reported",
            ),
            (
                "generation.completed",
                {"activity_id": act, "outputs": [{"index": 0, "service_asset_id": "loose-1"}]},
                "service_reported",
            ),
        ),
        headers=h,
    )
    sha = upload(
        client, h, png((1, 2, 3)), [{"service": "higgsfield", "service_asset_id": "loose-1"}]
    ).json()["sha256"]
    before = client.get("/api/v1/me/captures", headers=h).json()
    assert before["unassigned_count"] >= 1

    r = client.post(
        "/api/v1/links",
        json={"links": [{"sha256": sha, "target_type": "shot", "target_id": shot}]},
        headers=h,
    )
    assert r.status_code == 201
    after = client.get("/api/v1/me/captures", headers=h).json()
    assert after["unassigned_count"] == before["unassigned_count"] - 1
    assert any(
        a["id"] == act for a in client.get(f"/api/v1/shots/{shot}", headers=h).json()["activities"]
    )
