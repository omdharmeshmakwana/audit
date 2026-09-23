"""Seed data (spec §8): users, project DEMO, sequences, shots, characters with placeholder
reference images, and one full synthetic chain reference → generate → variation → upscale
→ download, so the shot page and auditor have something to show before any real capture.

Everything is deterministic — ids, event ids, timestamps, image bytes — so seeding twice is
a no-op (every event comes back `duplicate`). All events are written with source="script".
"""

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from faible.auth import register_user
from faible.config import get_settings
from faible.events import Evidence
from faible.log import EventIn, append_events
from faible.models import UUID_NS
from faible.storage import store_bytes

BASE_TIME = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
PASSWORD = "changeme"
USERS = [
    ("director@studio", "Director", "director"),
    ("artist1@studio", "Artist One", "artist"),
    ("artist2@studio", "Artist Two", "artist"),
]
CHARACTERS = [
    ("Mira", "Lead. Early thirties, short dark hair, grey wool coat.", (70, 110, 160)),
    ("Kesh", "Mira's brother. Tall, beard, green parka.", (80, 140, 90)),
    ("The Warden", "Antagonist. Older, scarred, long black coat.", (150, 60, 60)),
]
REF_LABELS = ["front", "profile", "full body"]


def sid(name: str) -> uuid.UUID:
    return uuid.uuid5(UUID_NS, f"seed:{name}")


@dataclass
class Seeder:
    session: Session
    data_dir: Path
    step: int = 0
    results: dict[str, int] = field(default_factory=lambda: {"accepted": 0, "duplicate": 0})

    def at(self) -> datetime:
        self.step += 1
        return BASE_TIME + timedelta(minutes=self.step)

    def emit(
        self,
        type_: str,
        payload: dict[str, Any],
        evidence: Evidence,
        actor: uuid.UUID | None = None,
    ) -> None:
        occurred_at = self.at()
        (r,) = append_events(
            self.session,
            [
                EventIn(
                    event_id=sid(f"event:{self.step}:{type_}"),
                    type=type_,
                    occurred_at=occurred_at,
                    evidence=evidence,
                    payload=payload,
                )
            ],
            source="script",
            actor_user_id=actor,
        )
        if r.status not in self.results:
            raise RuntimeError(f"seed event {type_} {r.status}: {r.reason}")
        self.results[r.status] += 1

    def image(
        self, text: str, size: tuple[int, int], color: tuple[int, int, int]
    ) -> tuple[str, int]:
        """A labelled placeholder PNG, stored under data/assets. Returns (sha256, bytes)."""
        img = Image.new("RGB", size, color)
        draw = ImageDraw.Draw(img)
        w, h = size
        for i in range(0, w, max(w // 16, 1)):
            draw.line([(i, 0), (i, h)], fill=tuple(min(c + 25, 255) for c in color), width=1)
        draw.rectangle([(16, h - 72), (w - 16, h - 16)], fill=(20, 20, 20))
        draw.text((28, h - 60), text, fill=(240, 240, 240), font_size=max(h // 24, 14))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        return store_bytes(data, self.data_dir), len(data)

    def register_image(
        self,
        text: str,
        size: tuple[int, int],
        color: tuple[int, int, int],
        evidence: Evidence,
        locations: list[dict[str, Any]],
    ) -> str:
        sha, n = self.image(text, size, color)
        # No phash here: seed events must stay byte-identical for idempotency. Fingerprints
        # arrive as later asset.registered events from `faible fingerprint`.
        self.emit(
            "asset.registered",
            {
                "sha256": sha,
                "media_type": "image/png",
                "width": size[0],
                "height": size[1],
                "bytes": n,
                "stored": True,
                "locations": locations,
            },
            evidence,
        )
        return sha


def _shot_list(data_dir: Path) -> list[tuple[str, str, str]]:
    """(sequence, shot_code, description) from data/shotlist.csv, else the synthetic ten."""
    path = data_dir / "shotlist.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            return [
                (
                    r["sequence"].strip(),
                    r["shot_code"].strip(),
                    (r.get("description") or "").strip(),
                )
                for r in csv.DictReader(f)
            ]
    return [
        (sq, f"{sq}_SH{n:03d}", f"{sq} shot {n}")
        for sq in ("SQ01", "SQ02")
        for n in range(10, 60, 10)
    ]


def seed(session: Session, data_dir: Path | None = None) -> dict[str, int]:
    data_dir = data_dir or get_settings().data_dir
    s = Seeder(session, data_dir)

    users = {}
    for i, (email, name, role) in enumerate(USERS):
        users[email] = register_user(
            session,
            email=email,
            password=PASSWORD,
            display_name=name,
            role=role,
            user_id=sid(f"user:{email}"),
            event_id=sid(f"event:user:{email}"),
            occurred_at=BASE_TIME + timedelta(seconds=i),
            source="script",
        )
    director, artist1 = users["director@studio"], users["artist1@studio"]

    project = sid("project:DEMO")
    s.emit(
        "project.created",
        {"project_id": str(project), "name": "Demo", "code": "DEMO"},
        "originated",
        director,
    )

    shots: dict[str, uuid.UUID] = {}
    sequences: dict[str, uuid.UUID] = {}
    for seq_name, code, desc in _shot_list(data_dir):
        if seq_name not in sequences:
            sequences[seq_name] = sid(f"sequence:{seq_name}")
            s.emit(
                "sequence.created",
                {
                    "sequence_id": str(sequences[seq_name]),
                    "project_id": str(project),
                    "name": seq_name,
                    "order": len(sequences),
                },
                "originated",
                director,
            )
        shots[code] = sid(f"shot:{code}")
        s.emit(
            "shot.created",
            {
                "shot_id": str(shots[code]),
                "sequence_id": str(sequences[seq_name]),
                "code": code,
                "description": desc,
                "credit_budget": "5000",
            },
            "originated",
            director,
        )

    first_ref: dict[str, str] = {}
    for name, desc, color in CHARACTERS:
        entity = sid(f"character:{name}")
        s.emit(
            "entity.created",
            {
                "entity_id": str(entity),
                "project_id": str(project),
                "entity_type": "character",
                "name": name,
                "description": desc,
            },
            "originated",
            director,
        )
        for label in REF_LABELS:
            sha = s.register_image(
                f"{name} — {label} (reference)", (512, 512), color, "originated", []
            )
            s.emit(
                "entity.reference_added",
                {"entity_id": str(entity), "sha256": sha, "label": f"{name} {label}"},
                "asserted",
                director,
            )
            first_ref.setdefault(name, sha)

    # ---------------------------------------------------------------- synthetic chain
    shot = shots.get("SQ01_SH010") or next(iter(shots.values()))
    s.emit(
        "shot.entity_added",
        {"shot_id": str(shot), "entity_id": str(sid("character:Mira"))},
        "asserted",
        director,
    )
    s.emit("shot.active_set", {"user_id": str(artist1), "shot_id": str(shot)}, "observed", artist1)
    account = {"workspace_id": "seed-workspace", "service_user_id": "seed-user"}

    # (kind, service, model, job id, inputs as (role, sha | None = previous output),
    #  prompt, credits, image label, size)
    ChainStep = tuple[
        str, str, str, str, list[tuple[str, str | None]], str, str, str, tuple[int, int]
    ]
    steps: list[ChainStep] = [
        (
            "generate",
            "higgsfield",
            "nano_banana_flash",
            "seed-hf-1",
            [("reference", first_ref["Mira"])],
            "Mira stands at a rain-streaked window at dusk, grey wool coat, city lights behind, 35mm, shallow depth of field",
            "150",
            "Mira at the window — v1",
            (1024, 576),
        ),
        (
            "variation",
            "higgsfield",
            "nano_banana_flash",
            "seed-hf-2",
            [("source", None)],
            "Mira stands at a rain-streaked window at night, grey wool coat, neon city lights behind, 35mm, shallow depth of field, tears",
            "150",
            "Mira at the window — variation",
            (1024, 576),
        ),
        (
            "upscale",
            "magnific",
            "enhance-magnific-creative",
            "seed-mg-1",
            [("source", None)],
            "subtle",
            "1080",
            "Mira at the window — 2x upscale",
            (2048, 1152),
        ),
    ]
    previous_sha: str | None = None
    for i, (kind, service, model, job, inputs, prompt, credits, label, size) in enumerate(steps):
        activity = sid(f"activity:{job}")
        s.emit(
            "generation.requested",
            {
                "activity_id": str(activity),
                "service": service,
                "kind": kind,
                "model_id": model,
                "service_job_id": job,
                "prompt": prompt,
                "seed": str(1000 + i),
                "parameters": {"aspect_ratio": "16:9"}
                | ({"scale": "2x", "creativity": -3} if kind == "upscale" else {}),
                "inputs": [{"role": role, "sha256": sha or previous_sha} for role, sha in inputs],
                "credits_cost": credits,
                "shot_id": str(shot),
                "service_account": account,
            },
            "service_reported",
            artist1,
        )
        s.emit(
            "generation.status",
            {"activity_id": str(activity), "status": "running"},
            "service_reported",
            artist1,
        )
        url = f"https://cdn.example.invalid/{service}/{job}.png"
        s.emit(
            "generation.completed",
            {
                "activity_id": str(activity),
                "outputs": [
                    {"index": 0, "url": url, "service_asset_id": job, "media_type": "image"}
                ],
            },
            "service_reported",
            artist1,
        )
        previous_sha = s.register_image(
            label,
            size,
            (60 + 30 * i, 70, 90),
            "observed",
            [{"service": service, "url": url, "service_asset_id": job}],
        )

    assert previous_sha is not None
    s.emit(
        "download.observed",
        {
            "download_id": str(sid("download:1")),
            "filename": "magnific_mira-window_seed-mg-1.png",
            "service": "magnific",
            "url": "https://cdn.example.invalid/magnific/seed-mg-1.png",
            "sha256": previous_sha,
            "hash_method": "refetch",
        },
        "observed",
        artist1,
    )
    s.emit(
        "shot_version.created",
        {
            "version_id": str(sid("version:SQ01_SH010:1")),
            "shot_id": str(shot),
            "sha256": previous_sha,
            "version_number": 1,
            "notes": "First pass from the synthetic seed chain.",
        },
        "asserted",
        artist1,
    )
    session.flush()
    return s.results
