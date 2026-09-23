import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from faible.config import get_settings
from faible.main import app
from faible.redact import REDACTED, redact, redact_headers, redact_url

client = TestClient(app)


def test_health_reports_db() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_redact_headers() -> None:
    h = redact_headers(
        {"Cookie": "a=b", "Authorization": "Bearer x", "X-Csrf-Token": "t", "Content-Type": "j"}
    )
    assert h == {
        "Cookie": REDACTED,
        "Authorization": REDACTED,
        "X-Csrf-Token": REDACTED,
        "Content-Type": "j",
    }


def test_redact_body_keeps_prompt_strips_secrets() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3OCJ9.abcdefghijklmnop"
    body = {
        "prompt": "a red fox",
        "access_token": "abc",
        "refreshToken": "def",
        "prompt_tokens": 12,
        "nested": [{"jwt": "x", "note": f"Bearer {jwt}"}],
    }
    out = redact(body)
    assert out["prompt"] == "a red fox"
    assert out["access_token"] == REDACTED
    assert out["refreshToken"] == REDACTED
    assert out["prompt_tokens"] == 12
    assert out["nested"][0]["jwt"] == REDACTED
    assert jwt not in json.dumps(out)


def test_redact_signed_url_keeps_shape() -> None:
    url = "https://cdn.example.com/a.mp4?Expires=123&Signature=zzz&X-Amz-Signature=yyy&w=1"
    out = redact_url(url)
    assert "zzz" not in out and "yyy" not in out
    assert "Expires=123" in out and "w=1" in out


def test_redact_escaped_json_url() -> None:
    pusher = r'{"url":"https:\/\/cdn.x\/p\/render.png?token=exp=179~hmac=deadbeef","x":1}'
    out = redact({"data": pusher})["data"]
    assert "deadbeef" not in out
    assert "render.png?token=[REDACTED]" in out
    assert json.loads(out)["x"] == 1


def test_discovery_capture_writes_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    rec = {
        "service": "higgsfield",
        "transport": "fetch",
        "captured_at": "2026-09-23T00:00:00Z",
        "url": "https://api.example/jobs",
        "request_headers": {"authorization": "Bearer secretsecret"},
        "response_body": {"id": "j1", "token": "leak"},
    }
    r = client.post("/discovery/capture", json={"records": [rec]})
    assert r.json() == {"written": 1}
    files = list((tmp_path / "discovery").glob("higgsfield-*.jsonl"))
    content = files[0].read_text()
    assert "secretsecret" not in content and "leak" not in content
    assert '"j1"' in content
