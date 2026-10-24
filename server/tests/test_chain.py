"""Prompt chains: walking back through activities, inferred matches and origins."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from faible import backfill
from faible.chain import asset_chain, union_chain, word_diff
from faible.config import get_settings
from faible.fingerprint import fingerprint_file
from faible.log import EventIn, append_events
from faible.models import ShotVersion, asset_id_for
from faible.seed import seed
from tests.test_fingerprint import picture


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


def test_input_registered_after_its_generation_joins_the_chain(session: Session) -> None:
    """Magnific output → reused as a Higgsfield library input (known only by media id at
    submit) → the extension registers the input's stored copy later. The Higgsfield output's
    chain must reach the Magnific prompt."""
    t0 = datetime(2026, 10, 19, 7, 46, tzinfo=UTC)
    mag, hf = str(uuid.uuid4()), str(uuid.uuid4())
    mag_out, hf_in, hf_out = "a" * 64, "b" * 64, "c" * 64
    media = "6e459e23-b95d-4cc4-b45c-fe40ec7e87ce"
    media_url = f"https://d2ol7oe51mr4n9.cloudfront.net/user_x/{media}.png"
    _ev(session, t0, "generation.requested",
        {"activity_id": mag, "service": "magnific", "service_job_id": "3za1NelREY",
         "prompt": "a man walking on the streets of mumbai"})  # fmt: skip
    _ev(session, t0 + timedelta(seconds=13), "generation.completed",
        {"activity_id": mag, "outputs": [{"index": 0, "sha256": mag_out}]})  # fmt: skip
    _ev(session, t0 + timedelta(seconds=14), "asset.registered",
        {"sha256": mag_out, "phash": "a4b6521f748fc1b1"}, "observed")  # fmt: skip
    _ev(session, t0 + timedelta(minutes=27), "generation.requested",
        {"activity_id": hf, "service": "higgsfield", "service_job_id": "b36b9045",
         "prompt": "In <<<image_1>>> make the man's skin tone fair",
         "inputs": [{"role": "image", "service_asset_id": media, "url": media_url}]})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=27, seconds=1), "asset.registered",
        {"sha256": hf_in, "phash": "a4b6521f748fc1b1",
         "locations": [{"service": "higgsfield", "service_asset_id": media, "url": media_url}]},
        "observed")  # fmt: skip
    _ev(session, t0 + timedelta(minutes=28), "generation.completed",
        {"activity_id": hf, "outputs": [{"index": 0, "sha256": hf_out}]})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=28, seconds=1), "asset.registered",
        {"sha256": hf_out, "phash": "a0be560f748fc991"}, "observed")  # fmt: skip

    chain = asset_chain(session, asset_id_for(hf_out))
    assert chain is not None
    kinds = [(s["type"], s.get("service")) for s in chain["steps"]]
    assert kinds == [("activity", "magnific"), ("match", None), ("activity", "higgsfield")]
    assert chain["steps"][0]["prompt"] == "a man walking on the streets of mumbai"


def test_fetch_missing_resolves_an_input_never_seen_as_bytes(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run captured before inputs were fetched at submit: the backfill fetches the input's
    public URL, and the re-encoded copy links to the Magnific output by pHash."""
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    original = picture(tmp_path / "magnific.png", seed=3)
    reencoded = picture(tmp_path / "upload.jpg", seed=3, fmt="JPEG", quality=80).read_bytes()
    fp = fingerprint_file(original, "image/png")
    assert fp is not None
    url = "https://d2ol7oe51mr4n9.cloudfront.net/user_x/media-9.png"
    fetched: list[str] = []
    monkeypatch.setattr(
        backfill, "_fetch", lambda u: (fetched.append(u), (reencoded, "image/jpeg"))[1]
    )

    t0 = datetime(2026, 10, 19, 7, 46, tzinfo=UTC)
    mag, hf = str(uuid.uuid4()), str(uuid.uuid4())
    _ev(session, t0, "generation.requested",
        {"activity_id": mag, "service": "magnific", "service_job_id": "m1", "prompt": "street"})  # fmt: skip
    _ev(session, t0 + timedelta(seconds=10), "generation.completed",
        {"activity_id": mag, "outputs": [{"index": 0, "sha256": "a" * 64}]})  # fmt: skip
    _ev(session, t0 + timedelta(seconds=11), "asset.registered",
        {"sha256": "a" * 64, "phash": fp.phash}, "observed")  # fmt: skip
    _ev(session, t0 + timedelta(minutes=5), "generation.requested",
        {"activity_id": hf, "service": "higgsfield", "service_job_id": "h1", "prompt": "fair skin",
         "inputs": [{"role": "image", "service_asset_id": "media-9", "url": url}]})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=6), "generation.completed",
        {"activity_id": hf, "outputs": [{"index": 0, "sha256": "c" * 64}]})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=6, seconds=1), "asset.registered",
        {"sha256": "c" * 64, "phash": "0f0f0f0ff0f0f0f0"}, "observed")  # fmt: skip

    report = backfill.fetch_missing(session)
    assert fetched == [url] and len(report.done) == 1, report
    chain = asset_chain(session, asset_id_for("c" * 64))
    assert chain is not None
    assert [s["type"] for s in chain["steps"]] == ["activity", "match", "activity"]
    assert chain["steps"][0]["service"] == "magnific"
    assert backfill.fetch_missing(session).done == []  # resolved: nothing left to fetch


def test_higgsfield_output_used_as_magnific_reference_chains_by_hash(session: Session) -> None:
    """Higgsfield output → downloaded → uploaded to Magnific as a reference. Magnific keeps
    uploads byte-identical, so the link is exact (sha256), with no inferred match step."""
    t0 = datetime(2026, 9, 23, 9, 38, tzinfo=UTC)
    hf, mag = str(uuid.uuid4()), str(uuid.uuid4())
    hf_out, mag_out = "d" * 64, "e" * 64
    _ev(session, t0, "generation.requested",
        {"activity_id": hf, "service": "higgsfield", "service_job_id": "c09533a2",
         "prompt": "crowded market at dusk"})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=1), "generation.completed",
        {"activity_id": hf, "outputs": [{"index": 0, "sha256": hf_out}]})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=1, seconds=1), "asset.registered",
        {"sha256": hf_out, "phash": "c3c3c3c33c3c3c3c"}, "observed")  # fmt: skip
    _ev(session, t0 + timedelta(minutes=7), "asset.location_seen",
        {"sha256": hf_out, "service": "magnific", "service_asset_id": "CqUjWZfEEy"},
        "observed")  # fmt: skip
    _ev(session, t0 + timedelta(minutes=8), "generation.requested",
        {"activity_id": mag, "service": "magnific", "service_job_id": "ks6OBy616B",
         "prompt": "in @img1 a bit less crowd",
         "inputs": [{"role": "reference", "service_asset_id": "CqUjWZfEEy"}]})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=9), "generation.completed",
        {"activity_id": mag, "outputs": [{"index": 0, "sha256": mag_out}]})  # fmt: skip
    _ev(session, t0 + timedelta(minutes=9, seconds=1), "asset.registered",
        {"sha256": mag_out, "phash": "0f0f0f0ff0f0f0f0"}, "observed")  # fmt: skip

    chain = asset_chain(session, asset_id_for(mag_out))
    assert chain is not None
    assert [(s["type"], s["service"]) for s in chain["steps"]] == [
        ("activity", "higgsfield"),
        ("activity", "magnific"),
    ]
    assert chain["steps"][1]["inputs"][0]["asset"]["sha256"] == hf_out
    assert chain["weakest_confidence"] == 1.0


def test_union_chain_merges_versions(session: Session, tmp_path: Path) -> None:
    seed(session, tmp_path)
    version = session.scalars(select(ShotVersion)).one()
    merged = union_chain(session, [version.asset_id, version.asset_id])
    assert len([s for s in merged["steps"] if s["type"] == "activity"]) == 3
