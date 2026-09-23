"""Web UI: pages render from the derived tables, and every form writes through the log."""

import io
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.backfill import fingerprint_stored
from faible.models import Activity, Event, Shot, ShotVersion
from faible.storage import asset_path


@pytest.fixture
def web(client: TestClient) -> TestClient:
    r = client.post(
        "/login", data={"email": "artist1@studio", "password": "changeme"}, follow_redirects=False
    )
    assert r.status_code == 303 and "faible_session" in r.cookies
    return client


def shot_id(session: Session, code: str) -> uuid.UUID:
    return session.scalars(select(Shot).where(Shot.code == code)).one().id


def test_login_required_and_bad_password(client: TestClient) -> None:
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    r = client.post(
        "/login", data={"email": "artist1@studio", "password": "nope"}, follow_redirects=False
    )
    assert "error=" in r.headers["location"]


def test_project_page_lists_shots(web: TestClient) -> None:
    html = web.get("/").text  # one project → redirected to it
    assert "SQ01_SH010" in html and "SQ02_SH050" in html and "Mira" in html


def test_shot_page_shows_versions_chain_and_prompt_diff(web: TestClient, session: Session) -> None:
    html = web.get(f"/shots/{shot_id(session, 'SQ01_SH010')}").text
    assert "v1" in html and "Chain for v1" in html
    assert "rain-streaked window at dusk" in html  # first prompt shown plain
    assert "<ins>night, </ins>" in html or "<ins>night," in html  # diffed against the previous step
    assert "<del>dusk,</del>" in html or "<del>dusk, </del>" in html
    assert "Mira front" in html  # the reference origin, by label
    assert "enhance-magnific-creative" in html and "1080" in html


def test_asset_page_and_assert_link(web: TestClient, session: Session) -> None:
    version = session.scalars(select(ShotVersion)).one()
    html = web.get(f"/assets/{version.asset_id}").text
    assert version.asset_id and "How it was made" in html and "Assert a link" in html
    target = shot_id(session, "SQ02_SH030")
    r = web.post(
        f"/assets/{version.asset_id}/link",
        data={"target": f"shot:{target}", "reason": "reused"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?linked=1")
    last = session.scalars(select(Event).order_by(Event.seq.desc())).first()
    assert (
        last is not None
        and last.type == "link.asserted"
        and last.evidence == "asserted"
        and last.source == "server"
    )


def test_promote_output_to_version(web: TestClient, session: Session) -> None:
    sid = shot_id(session, "SQ01_SH010")
    version = session.scalars(select(ShotVersion)).one()
    sha = session.execute(
        select(Event.payload).where(Event.type == "shot_version.created")
    ).scalar_one()["sha256"]
    r = web.post(f"/shots/{sid}/promote", data={"sha256": sha}, follow_redirects=False)
    assert r.status_code == 303
    numbers = sorted(
        v.version_number
        for v in session.scalars(select(ShotVersion).where(ShotVersion.shot_id == sid))
    )
    assert numbers == [1, 2] and version.version_number == 1


def test_cross_origin_post_is_refused(web: TestClient, session: Session) -> None:
    version = session.scalars(select(ShotVersion)).one()
    r = web.post(
        f"/assets/{version.asset_id}/link",
        data={"target": f"shot:{shot_id(session, 'SQ02_SH030')}"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_unassigned_queue_assigns_in_bulk(web: TestClient, session: Session) -> None:
    # Make the seed's first generation unassigned by linking nothing: create a fresh one.
    act = session.scalars(select(Activity)).first()
    assert act is not None
    act.shot_id = None  # derived-table tweak for the test only; the form writes a real event
    session.flush()
    html = web.get("/unassigned").text
    sha = re.search(r'name="sha256" value="([0-9a-f]{64})"', html)
    assert sha, html[:500]
    target = shot_id(session, "SQ02_SH020")
    web.post(
        "/unassigned/assign",
        data={"shot_id": str(target), "sha256": sha.group(1)},
        follow_redirects=False,
    )
    session.expire_all()
    assert session.get_one(Activity, act.id).shot_id == target


def test_imported_reencode_links_to_its_source_as_inferred(
    web: TestClient, session: Session
) -> None:
    """The Phase 3 demo in miniature: a re-encoded copy of an output is imported and shows up
    linked to the original by fingerprint, with a confidence."""
    fingerprint_stored(session)
    version = session.scalars(select(ShotVersion)).one()
    original = asset_path(
        session.execute(
            select(Event.payload).where(Event.type == "shot_version.created")
        ).scalar_one()["sha256"]
    )
    buf = io.BytesIO()
    with Image.open(original) as img:
        img.convert("RGB").resize((1024, 576)).save(buf, format="JPEG", quality=70)
    r = web.post(
        "/import",
        files={"file": ("reencoded.jpg", buf.getvalue(), "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    html = web.get(r.headers["location"]).text
    assert "Looks like" in html and "ev-inferred" in html
    assert "Same picture, different file" in html  # the chain crosses the inferred edge
    assert "enhance-magnific-creative" in html  # …back to the generation that made the original
    assert str(version.asset_id) in html
