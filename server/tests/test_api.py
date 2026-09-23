"""HTTP: login and batch ingest."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.config import get_settings
from faible.db import get_session
from faible.main import app
from faible.models import Event, Project
from faible.seed import seed


@pytest.fixture
def client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    seed(session, tmp_path)
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def login(
    client: TestClient, email: str = "artist1@studio", password: str = "changeme"
) -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password, "client": "extension"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_me_and_bad_password(client: TestClient) -> None:
    me = client.get("/api/v1/me", headers=login(client)).json()
    assert me["email"] == "artist1@studio" and me["active_shot_id"] is not None
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "artist1@studio", "password": "nope"}
        ).status_code
        == 401
    )
    assert client.get("/api/v1/me").status_code == 401


def test_refresh_issues_a_new_access_token(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/login", json={"email": "artist2@studio", "password": "changeme"}
    ).json()
    new = client.post("/api/v1/auth/refresh", json={"refresh_token": r["refresh_token"]}).json()
    assert (
        client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {new['access_token']}"}
        ).status_code
        == 200
    )
    # An access token is not a refresh token.
    assert (
        client.post("/api/v1/auth/refresh", json={"refresh_token": r["access_token"]}).status_code
        == 401
    )


def test_ingest_batch_is_idempotent_and_attributed(client: TestClient, session: Session) -> None:
    headers = login(client)
    project_id = str(uuid.uuid4())
    event = {
        "event_id": str(uuid.uuid4()),
        "type": "project.created",
        "occurred_at": "2026-09-23T08:00:00Z",
        "evidence": "originated",
        "payload": {"project_id": project_id, "name": "Other", "code": "OTHER"},
    }
    bad = {**event, "event_id": str(uuid.uuid4()), "type": "no.such.type"}
    r = client.post("/api/v1/ingest/events", json={"events": [event, bad]}, headers=headers)
    assert [x["status"] for x in r.json()["results"]] == ["accepted", "rejected"]
    again = client.post("/api/v1/ingest/events", json={"events": [event]}, headers=headers)
    assert again.json()["results"][0]["status"] == "duplicate"

    row = session.scalars(select(Event).where(Event.event_id == uuid.UUID(event["event_id"]))).one()
    me = client.get("/api/v1/me", headers=headers).json()
    assert row.source == "extension" and str(row.actor_user_id) == me["id"]
    assert session.get(Project, uuid.UUID(project_id)) is not None


def test_ingest_rejects_client_recorded_at_and_requires_auth(client: TestClient) -> None:
    event = {
        "event_id": str(uuid.uuid4()),
        "type": "project.created",
        "occurred_at": "2026-09-23T08:00:00Z",
        "recorded_at": "2020-01-01T00:00:00Z",
        "evidence": "originated",
        "payload": {"project_id": str(uuid.uuid4()), "name": "X", "code": "X"},
    }
    assert client.post("/api/v1/ingest/events", json={"events": [event]}).status_code == 401
    r = client.post("/api/v1/ingest/events", json={"events": [event]}, headers=login(client))
    assert r.status_code == 422
