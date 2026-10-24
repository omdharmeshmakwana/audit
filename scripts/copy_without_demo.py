"""Copy the Faible log into a new, migrated, empty database without the seed's demo data.

    server/.venv/bin/python scripts/copy_without_demo.py faible faible_new [--move-demo-assets]

One-off, run on 2026-10-23 at the user's request (DECISIONS #69). The log is append-only, so
demo rows cannot be deleted in place; this builds a new log from the old one, in the original
order, and leaves the source database untouched as the backup.

Dropped: the seed (`faible seed`, source `script`): demo users director@studio and
artist2@studio, the DEMO project with its sequences and ten shots, the three characters and
their nine placeholder reference images, and the synthetic reference → generate → variation →
upscale → download chain with its shot version.

Kept: every real event (source `server` / `extension`) and the admin role change (source
`script`, 13 Oct). artist1@studio's registration is kept too, by the user's choice, because a
real generation was captured while signed in as that demo account on 13 Oct; it stays
attributed to it and the account stays disabled.

Real events that name a demo shot: `shot.active_set` is dropped; `generation.requested` is kept
with `shot_id` cleared, so those generations arrive unassigned. Kept events keep their event_id,
type, occurred_at, evidence, source, actor and payload (except that shot_id); recorded_at, seq
and hashes are new, because this is a new chain. Password hashes and disabled flags are copied
for the users the new log registers.

--move-demo-assets moves the stored files only the demo data used (placeholder images, the
synthetic chain) to data/backups/demo-assets-<date>/, same layout, so they can be put back.
"""

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from faible.config import get_settings
from faible.log import EventIn, append_events, verify_log
from faible.models import (
    Asset,
    Entity,
    Event,
    Project,
    Sequence,
    Shot,
    User,
    UserCredential,
)
from faible.rebuild import derived_checksum, rebuild
from faible.storage import asset_path

DEMO_PROJECT = "DEMO"
KEEP_DEMO_USER = "artist1@studio"  # a real capture was made under it (user's choice: keep)


def url(db: str) -> str:
    return f"postgresql+psycopg:///{db}"


def plan(src: Session) -> tuple[list[Event], list[dict[str, Any]], dict[str, int]]:
    project = src.scalars(select(Project).where(Project.code == DEMO_PROJECT)).one()
    demo_ids = {str(project.id)}
    demo_ids |= {
        str(s.id) for s in src.scalars(select(Sequence).where(Sequence.project_id == project.id))
    }
    demo_shots = {str(s.id) for s in src.scalars(select(Shot).where(Shot.project_id == project.id))}
    demo_ids |= demo_shots
    demo_ids |= {
        str(e.id) for e in src.scalars(select(Entity).where(Entity.project_id == project.id))
    }
    keep_user = src.scalars(select(User).where(User.email == KEEP_DEMO_USER)).one()

    kept: list[Event] = []
    payloads: list[dict[str, Any]] = []
    counts = {"dropped_seed": 0, "dropped_active_shot": 0, "unassigned": 0}
    for e in src.scalars(select(Event).order_by(Event.seq)):
        p = dict(e.payload)
        if e.source == "script":
            keep = e.type == "user.role_changed" or (
                e.type == "user.registered" and p["user_id"] == str(keep_user.id)
            )
            if not keep:
                counts["dropped_seed"] += 1
                continue
        if e.type == "shot.active_set" and p.get("shot_id") in demo_shots:
            counts["dropped_active_shot"] += 1
            continue
        if e.type == "generation.requested" and p.get("shot_id") in demo_shots:
            p["shot_id"] = None
            counts["unassigned"] += 1
        text = str(p)
        leftover = [i for i in demo_ids if i in text]
        if leftover:
            raise SystemExit(f"event seq {e.seq} ({e.type}) still names demo record(s) {leftover}")
        kept.append(e)
        payloads.append(p)
    return kept, payloads, counts


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("--move-demo-assets", action="store_true")
    args = ap.parse_args()
    if args.source == args.target:
        raise SystemExit("source and target must differ")

    with (
        Session(create_engine(url(args.source))) as src,
        Session(create_engine(url(args.target))) as dst,
    ):
        if dst.scalar(select(func.count()).select_from(Event)):
            raise SystemExit(f"{args.target} already has events; it must be migrated and empty")
        kept, payloads, counts = plan(src)
        for e, p in zip(kept, payloads, strict=True):
            (r,) = append_events(
                dst,
                [
                    EventIn(
                        event_id=e.event_id,
                        type=e.type,
                        occurred_at=e.occurred_at,
                        evidence=e.evidence,  # type: ignore[arg-type]
                        payload=p,
                    )
                ],
                source=e.source,  # type: ignore[arg-type]
                actor_user_id=e.actor_user_id,
            )
            if r.status != "accepted":
                raise SystemExit(f"seq {e.seq} {e.type}: {r.status} {r.reason}")
        users = {u.id for u in dst.scalars(select(User))}
        for c in src.scalars(select(UserCredential)):
            if c.user_id in users:
                dst.add(
                    UserCredential(
                        user_id=c.user_id,
                        email=c.email,
                        password_hash=c.password_hash,
                        disabled_at=c.disabled_at,
                    )
                )
        dst.commit()

        assert verify_log(dst).ok, "new log does not verify"
        dst.rollback()  # end the read; the check below runs in its own transaction
        with dst.begin():
            before = derived_checksum(dst)
            rebuild(dst)
            assert derived_checksum(dst).total == before.total, "rebuild differs"

        source_events = src.scalar(select(func.count()).select_from(Event))
        print(f"source events: {source_events}, copied: {len(kept)}")
        for k, v in counts.items():
            print(f"  {k}: {v}")
        for u in dst.scalars(select(User).order_by(User.email)):
            cred = dst.get(UserCredential, u.id)
            state = "disabled" if cred and cred.disabled_at else "active"
            print(f"  user {u.email} ({u.role}, {state})")
        print("new log verifies; rebuild reproduces it")

        kept_assets = {a.sha256 for a in dst.scalars(select(Asset))}
        demo_files = [
            a.sha256
            for a in src.scalars(select(Asset).where(Asset.stored))
            if a.sha256 not in kept_assets and asset_path(a.sha256).exists()
        ]
        target = get_settings().data_dir / "backups" / f"demo-assets-{date.today().isoformat()}"
        for sha in demo_files:
            if args.move_demo_assets:
                dest = target / sha[:2] / sha
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(asset_path(sha), dest)
        verb = "moved to " + str(target) if args.move_demo_assets else "used only by demo data"
        print(f"{len(demo_files)} stored file(s) {verb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
