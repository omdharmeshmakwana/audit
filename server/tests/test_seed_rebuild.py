"""Seed data, rebuild determinism, and the events doc."""

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faible.events import EVENT_TYPES, validate_payload
from faible.events_doc import EVENTS_MD, EXAMPLES, render
from faible.log import verify_log
from faible.models import (
    Activity,
    ActivityInput,
    ActivityOutput,
    Asset,
    Download,
    Entity,
    ReferenceAsset,
    Sequence,
    Shot,
    ShotVersion,
    User,
    UserCredential,
)
from faible.rebuild import derived_checksum, rebuild
from faible.seed import seed
from faible.storage import asset_path


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_seed_builds_the_demo_project(session: Session, tmp_path: Path) -> None:
    results = seed(session, tmp_path)
    assert results["accepted"] > 0 and results["duplicate"] == 0
    assert count(session, User) == 3 and count(session, UserCredential) == 3
    assert count(session, Sequence) == 2 and count(session, Shot) == 10
    assert count(session, Entity) == 3 and count(session, ReferenceAsset) == 9
    assert count(session, Activity) == 3 and count(session, ShotVersion) == 1

    # The synthetic chain is fully linked: every input and output resolves to an asset,
    # each step's output is the next step's input, and the download is the final asset.
    acts = list(session.scalars(select(Activity).order_by(Activity.started_at)))
    assert [a.kind for a in acts] == ["generate", "variation", "upscale"]
    assert all(a.status == "succeeded" and a.credits_cost is not None for a in acts)
    outs = {o.activity_id: o for o in session.scalars(select(ActivityOutput))}
    ins = {i.activity_id: i for i in session.scalars(select(ActivityInput))}
    assert all(o.asset_id is not None for o in outs.values())
    assert all(i.asset_id is not None for i in ins.values())
    assert ins[acts[1].id].asset_id == outs[acts[0].id].asset_id
    assert ins[acts[2].id].asset_id == outs[acts[1].id].asset_id
    (dl,) = session.scalars(select(Download))
    assert dl.asset_id == outs[acts[2].id].asset_id
    (version,) = session.scalars(select(ShotVersion))
    assert version.asset_id == outs[acts[2].id].asset_id

    # Every registered asset has its bytes on disk under its own hash.
    for asset in session.scalars(select(Asset).where(Asset.stored)):
        assert asset_path(asset.sha256, tmp_path).exists()
    assert verify_log(session).ok


def test_seed_is_idempotent(session: Session, tmp_path: Path) -> None:
    seed(session, tmp_path)
    again = seed(session, tmp_path)
    assert again["accepted"] == 0 and again["duplicate"] > 0
    assert count(session, Shot) == 10


def test_rebuild_reproduces_identical_derived_tables(session: Session, tmp_path: Path) -> None:
    seed(session, tmp_path)
    before = derived_checksum(session)
    replayed = rebuild(session)
    after = derived_checksum(session)
    assert replayed > 0
    assert before.tables == after.tables
    assert before.total == after.total
    assert count(session, UserCredential) == 3  # not derived: untouched by rebuild


def test_every_event_type_has_a_valid_documented_example() -> None:
    assert set(EXAMPLES) == set(EVENT_TYPES)
    for type_, (_, example) in EXAMPLES.items():
        validate_payload(type_, example)


def test_events_md_is_in_sync_with_code() -> None:
    assert EVENTS_MD.read_text(encoding="utf-8") == render(), "run `faible events-doc`"
