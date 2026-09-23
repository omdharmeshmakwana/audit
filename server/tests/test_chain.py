"""Prompt chains: walking back through activities, inferred matches and origins."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from faible.chain import asset_chain, union_chain, word_diff
from faible.log import EventIn, append_events
from faible.models import ShotVersion, asset_id_for
from faible.seed import seed


def test_word_diff_marks_changed_words() -> None:
    d = word_diff("Mira at the window at dusk", "Mira at the window at night, tears")
    assert {"op": "delete", "text": "dusk"} in d
    assert any(x["op"] == "insert" and "night," in x["text"] for x in d)
    assert (
        "".join(x["text"] for x in d if x["op"] != "delete") == "Mira at the window at night, tears"
    )


def test_seed_version_chain(session: Session, tmp_path: Path) -> None:
    seed(session, tmp_path)
    version = session.scalars(select(ShotVersion)).one()
    chain = asset_chain(session, version.asset_id)
    assert chain is not None
    acts = [s for s in chain["steps"] if s["type"] == "activity"]
    assert [a["kind"] for a in acts] == ["generate", "variation", "upscale"]
    assert [a["service"] for a in acts] == ["higgsfield", "higgsfield", "magnific"]
    assert acts[0]["prompt_diff"] is None  # nothing to compare the first prompt with
    ops = {(x["op"], x["text"].strip()) for x in acts[1]["prompt_diff"]}
    assert ("delete", "dusk,") in ops and ("insert", "night,") in ops
    assert acts[1]["inputs"][0]["asset"]["sha256"] == acts[0]["outputs"][0]["asset"]["sha256"]
    (origin,) = chain["origins"]
    assert origin["label"] == "Mira front"
    assert chain["weakest_confidence"] == 1.0


def _ev(
    session: Session,
    t: datetime,
    type_: str,
    payload: dict[str, Any],
    evidence: str = "service_reported",
) -> None:
    (r,) = append_events(
        session,
        [
            EventIn(
                event_id=uuid.uuid4(), type=type_, occurred_at=t, evidence=evidence, payload=payload
            )
        ],  # type: ignore[arg-type]
        source="script",
    )
    assert r.status == "accepted", r.reason


def test_reencoded_upload_links_back_through_an_inferred_match(session: Session) -> None:
    t0 = datetime(2026, 9, 23, 10, tzinfo=UTC)
    act = str(uuid.uuid4())
    out, upload, newer = "a" * 64, "b" * 64, "c" * 64
    _ev(
        session,
        t0,
        "generation.requested",
        {
            "activity_id": act,
            "service": "magnific",
            "service_job_id": "up1",
            "kind": "upscale",
            "prompt": "subtle",
        },
    )
    _ev(
        session,
        t0 + timedelta(seconds=5),
        "generation.completed",
        {"activity_id": act, "outputs": [{"index": 0, "sha256": out}]},
    )
    _ev(
        session,
        t0 + timedelta(seconds=6),
        "asset.registered",
        {"sha256": out, "phash": "c3c3c3c33c3c3c3c"},
        "observed",
    )
    # Higgsfield re-encoded the upload: different bytes, same picture.
    _ev(
        session,
        t0 + timedelta(minutes=2),
        "asset.registered",
        {"sha256": upload, "phash": "c3c3c3c33c3c3c3e"},
        "observed",
    )
    # A later look-alike must not become the source of an earlier asset.
    _ev(
        session,
        t0 + timedelta(minutes=9),
        "asset.registered",
        {"sha256": newer, "phash": "c3c3c3c33c3c3c3c"},
        "observed",
    )

    chain = asset_chain(session, asset_id_for(upload))
    assert chain is not None
    activity, match = chain["steps"]  # oldest first: the generation, then the re-encode
    assert match["type"] == "match" and match["source"]["sha256"] == out
    assert match["derived"]["sha256"] == upload
    assert match["evidence"] == "inferred" and 0.9 < match["confidence"] < 1.0
    assert activity["type"] == "activity" and activity["service_job_id"] == "up1"
    assert chain["weakest_confidence"] == match["confidence"]
    assert chain["origins"] == []


def test_union_chain_merges_versions(session: Session, tmp_path: Path) -> None:
    seed(session, tmp_path)
    version = session.scalars(select(ShotVersion)).one()
    merged = union_chain(session, [version.asset_id, version.asset_id])
    assert len([s for s in merged["steps"] if s["type"] == "activity"]) == 3
