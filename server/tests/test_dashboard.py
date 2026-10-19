"""Dashboard artist isolation and the company password login."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_api import login

from faible.auth import register_user
from faible.dashboard import warnings
from faible.models import User


def test_company_password_login(client: TestClient, session: Session) -> None:
    register_user(session, email="om@faiblemedia.com", password="changeme", display_name="Om")
    session.commit()
    headers = login(client, "om@faiblemedia.com", "changeme")
    assert client.get("/api/v1/me", headers=headers).json()["email"] == "om@faiblemedia.com"
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "om@faiblemedia.com", "password": "wrong"}
        ).status_code
        == 401
    )
    response = client.post(
        "/login",
        data={"email": "om@faiblemedia.com", "password": "changeme"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/dashboard"
    assert "Max-Age=2592000" in response.headers["set-cookie"]
    assert (
        client.post("/api/v1/auth/otp/request", json={"email": "om@faiblemedia.com"}).status_code
        == 404
    )


def test_dashboard_isolation_and_manager_filter(client: TestClient, session: Session) -> None:
    headers = login(client)
    data = client.get("/api/v1/dashboard", headers=headers).json()
    assert [u["email"] for u in data["artists"]] == ["artist1@studio"]
    assert all(row["email"] == "artist1@studio" for row in data["activities"])
    assert all("stored" in output for row in data["activities"] for output in row["outputs"])
    other = session.scalar(select(User).where(User.email == "artist2@studio"))
    assert other is not None
    assert client.get(f"/api/v1/dashboard?artist_id={other.id}", headers=headers).status_code == 403
    first = session.scalar(select(User).where(User.email == "artist1@studio"))
    assert first is not None
    first.role = "director"
    session.commit()
    data = client.get(f"/api/v1/dashboard?artist_id={other.id}", headers=headers).json()
    assert len(data["artists"]) > 1
    assert all(row["email"] == "artist2@studio" for row in data["activities"])
    assert client.get("/api/v1/dashboard").status_code == 401


def test_warning_normalization() -> None:
    assert "spider-man" in warnings("SPIDER-MAN in a scene")
    assert "taylor swift" in warnings("Taylor   Swift")
    assert warnings("A quiet forest") == []


def test_complete_history_totals_and_active_shot(client: TestClient, session: Session) -> None:
    import uuid

    from faible.models import Shot

    register_user(
        session, email="report@faiblemedia.com", password="changeme", display_name="Report"
    )
    session.commit()
    headers = login(client, "report@faiblemedia.com", "changeme")
    shot = session.scalar(select(Shot))
    assert shot is not None
    assert (
        client.put(
            "/api/v1/me/active-shot", headers=headers, json={"shot_id": str(shot.id)}
        ).status_code
        == 200
    )
    events = [
        {
            "event_id": str(uuid.uuid4()),
            "type": "generation.requested",
            "occurred_at": "2026-10-13T10:00:00Z",
            "evidence": "observed",
            "payload": {
                "activity_id": str(uuid.uuid4()),
                "service": "higgsfield",
                "model_id": "model-a" if i < 100 else "model-b",
                "prompt": "forest",
                "status": "running" if i == 204 else "succeeded",
                "credits_cost": "2" if i < 204 else None,
                "shot_id": str(shot.id),
            },
        }
        for i in range(205)
    ]
    response = client.post("/api/v1/ingest/events", headers=headers, json={"events": events})
    assert all(item["status"] == "accepted" for item in response.json()["results"])
    data = client.get("/api/v1/dashboard?limit=50", headers=headers).json()
    summary = data["summaries"][0]
    assert summary["generation_count"] == 205
    from decimal import Decimal

    assert Decimal(summary["credits"]["credits"]) == Decimal("408")
    assert summary["unknown_cost_count"] == 1
    assert summary["active_shot"] == shot.code
    assert summary["statuses"] == {"running": 1, "succeeded": 204}
    assert {model["model_id"]: model["count"] for model in summary["models"]} == {
        "model-a": 100,
        "model-b": 105,
    }
    assert len(data["activities"]) == 50
    assert data["history"]["total"] == 205
    assert data["history"]["has_more"]
    tail = client.get("/api/v1/dashboard?offset=200&limit=50", headers=headers).json()
    assert len(tail["activities"]) == 5
    assert not tail["history"]["has_more"]
    assert not (
        {item["id"] for item in data["activities"]} & {item["id"] for item in tail["activities"]}
    )
    assert client.get("/api/v1/dashboard?offset=-1", headers=headers).status_code == 422


def test_artist_cannot_create_admin_through_ingest(client: TestClient) -> None:
    import uuid

    response = client.post(
        "/api/v1/ingest/events",
        headers=login(client),
        json={
            "events": [
                {
                    "event_id": str(uuid.uuid4()),
                    "type": "user.registered",
                    "occurred_at": "2026-10-13T10:00:00Z",
                    "evidence": "observed",
                    "payload": {
                        "user_id": str(uuid.uuid4()),
                        "email": "evil@faiblemedia.com",
                        "display_name": "Evil",
                        "role": "admin",
                    },
                }
            ]
        },
    )
    assert response.status_code == 403


def test_admin_role_revocation_survives_rebuild(client: TestClient, session: Session) -> None:
    import uuid
    from datetime import UTC, datetime

    from faible.log import EventIn, append_events
    from faible.rebuild import rebuild

    user_id = register_user(
        session,
        email="manager@faiblemedia.com",
        password="changeme",
        display_name="Manager",
        role="admin",
    )
    session.commit()
    headers = login(client, "manager@faiblemedia.com", "changeme")
    assert client.get("/api/v1/dashboard", headers=headers).json()["is_admin"]
    event = EventIn(
        event_id=uuid.uuid4(),
        type="user.role_changed",
        occurred_at=datetime.now(UTC),
        evidence="asserted",
        payload={"user_id": str(user_id), "role": "artist"},
    )
    result = append_events(session, [event], source="script")[0]
    assert result.status == "accepted"
    session.commit()
    assert not client.get("/api/v1/dashboard", headers=headers).json()["is_admin"]
    rebuild(session)
    session.commit()
    session.expire_all()
    assert not client.get("/api/v1/dashboard", headers=headers).json()["is_admin"]
    assert (
        client.post(
            "/api/v1/ingest/events",
            headers=headers,
            json={
                "events": [
                    {
                        "event_id": str(uuid.uuid4()),
                        "type": "user.role_changed",
                        "occurred_at": "2026-10-13T10:00:00Z",
                        "evidence": "asserted",
                        "payload": {"user_id": str(user_id), "role": "admin"},
                    }
                ]
            },
        ).status_code
        == 403
    )


def test_disabled_account_loses_login_and_existing_tokens(
    client: TestClient, session: Session
) -> None:
    """Demo accounts (artist1@studio) must stop absorbing captures once disabled: existing
    30-day tokens are refused on the next request, not just new logins."""
    from faible.auth import disable_account

    headers = login(client, "artist1@studio", "changeme")
    assert client.get("/api/v1/me", headers=headers).status_code == 200
    web = client.post(
        "/login", data={"email": "artist1@studio", "password": "changeme"}, follow_redirects=False
    )
    assert web.status_code == 303

    assert disable_account(session, "artist1@studio")
    assert not disable_account(session, "nobody@faiblemedia.com")
    session.commit()

    assert client.get("/api/v1/me", headers=headers).status_code == 401
    assert (
        client.post("/api/v1/ingest/events", json={"events": []}, headers=headers).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "artist1@studio", "password": "changeme", "client": "extension"},
        ).status_code
        == 401
    )
    assert client.get("/dashboard", follow_redirects=False).status_code in (302, 303)
