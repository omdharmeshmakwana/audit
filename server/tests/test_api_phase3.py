"""Phase 3 API: asset detail, chain, file, graph; web session cookie."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.auth import issue_token
from faible.models import ShotVersion, User


def login(client: TestClient) -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"email": "artist1@studio", "password": "changeme"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_asset_detail_chain_and_file(client: TestClient, session: Session) -> None:
    h = login(client)
    version = session.scalars(select(ShotVersion)).one()
    detail = client.get(f"/api/v1/assets/{version.asset_id}", headers=h).json()
    assert detail["asset"]["stored"] and detail["asset"]["media_type"] == "image/png"
    assert detail["produced_by"][0]["kind"] == "upscale"
    assert detail["versions"][0]["version_number"] == 1
    assert detail["downloads"][0]["hash_method"] == "refetch"
    assert detail["locations"][0]["service"] == "magnific"

    chain = client.get(f"/api/v1/assets/{version.asset_id}/chain", headers=h).json()
    assert [s["kind"] for s in chain["steps"]] == ["generate", "variation", "upscale"]

    f = client.get(f"/api/v1/assets/{version.asset_id}/file", headers=h)
    assert f.status_code == 200 and f.headers["content-type"] == "image/png"
    assert f.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_graph_has_prov_and_production_edges(client: TestClient) -> None:
    h = login(client)
    (project,) = client.get("/api/v1/projects", headers=h).json()
    g = client.get(f"/api/v1/graph?project_id={project['id']}", headers=h).json()
    types = {e["type"] for e in g["edges"]}
    assert {"used", "generatedBy", "inShot", "versionOf"} <= types
    assert g["total_activities"] == 3
    assert all("evidence" in e and "confidence" in e for e in g["edges"])
    page = client.get(
        f"/api/v1/graph?project_id={project['id']}&limit=1&offset=1", headers=h
    ).json()
    assert len([n for n in page["nodes"] if n["type"] == "activity"]) == 1


def test_web_session_cookie_authenticates_but_is_not_a_bearer_token(
    client: TestClient, session: Session
) -> None:
    user = session.scalars(select(User).where(User.email == "artist1@studio")).one()
    web = issue_token(user.id, "web")
    client.cookies.set("faible_session", web)
    assert client.get("/api/v1/me").status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {web}"}).status_code == 401
