"""Perceptual fingerprints (spec §1): pHash for images; for video, pHash of 8 evenly spaced
frames sampled with ffmpeg. Identity stays the sha256 (hard rule 3) — fingerprints only
propose `inferred` links between assets whose bytes differ (re-encodes, resizes, uploads a
service re-compressed).

Stored in `assets.phash` as text: 16 hex chars for an image, a JSON list of 8 for a video.
"""

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import imagehash
from PIL import Image, UnidentifiedImageError

FRAMES = 8
IMAGE_THRESHOLD = 10  # max Hamming distance (of 64 bits) to call two images a match
VIDEO_THRESHOLD = 12  # max mean per-frame distance for videos


@dataclass(frozen=True)
class Fingerprint:
    kind: str  # "image" | "video"
    media_type: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    fps: float | None
    phash: str


def _image(path: Path) -> Fingerprint | None:
    try:
        with Image.open(path) as img:
            img.load()
            mime = Image.MIME.get(img.format or "")
            return Fingerprint(
                "image", mime, img.width, img.height, None, None, str(imagehash.phash(img))
            )
    except (UnidentifiedImageError, OSError):
        return None


def _ffprobe(path: Path) -> dict[str, object] | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    # Fixed argv, no shell; the only variable is our own temp/asset path.
    out = subprocess.run(  # noqa: S603
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if out.returncode != 0:
        return None
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return None
    s = streams[0]
    num, _, den = str(s.get("avg_frame_rate", "0/1")).partition("/")
    fps = float(num) / float(den) if den and float(den) else None
    return {
        "width": s.get("width"),
        "height": s.get("height"),
        "fps": round(fps, 3) if fps else None,
        "duration": float((data.get("format") or {}).get("duration") or 0),
    }


def _video(path: Path) -> Fingerprint | None:
    info = _ffprobe(path)
    ffmpeg = shutil.which("ffmpeg")
    if not info or not info["duration"] or not ffmpeg:
        return None
    duration = float(info["duration"])  # type: ignore[arg-type]
    hashes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="faible-frames-") as tmp:
        for i in range(FRAMES):
            t = duration * (i + 0.5) / FRAMES
            frame = Path(tmp) / f"{i}.png"
            subprocess.run(  # noqa: S603
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-ss",
                    f"{t:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-y",
                    str(frame),
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if not frame.exists():
                return None
            with Image.open(frame) as img:
                hashes.append(str(imagehash.phash(img)))
    return Fingerprint(
        "video",
        None,
        info["width"],  # type: ignore[arg-type]
        info["height"],  # type: ignore[arg-type]
        int(duration * 1000),
        info["fps"],  # type: ignore[arg-type]
        json.dumps(hashes),
    )


def fingerprint_file(path: Path, media_type: str | None = None) -> Fingerprint | None:
    """Image first (cheap), then video. None for anything else (audio, unknown)."""
    if media_type and media_type.startswith("video/"):
        return _video(path) or _image(path)
    return _image(path) or _video(path)


# ---------------------------------------------------------------- comparison


def kind_of(phash: str) -> str:
    return "video" if phash.startswith("[") else "image"


def _hamming(a: str, b: str) -> int:
    return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))


def distance(a: str, b: str) -> float | None:
    """Image: Hamming distance. Video: mean per-frame distance. None if not comparable."""
    ka, kb = kind_of(a), kind_of(b)
    if ka != kb:
        return None
    if ka == "image":
        return float(_hamming(a, b))
    fa, fb = json.loads(a), json.loads(b)
    if len(fa) != len(fb) or not fa:
        return None
    return sum(_hamming(x, y) for x, y in zip(fa, fb, strict=True)) / len(fa)


def match(a: str, b: str) -> tuple[float, float] | None:
    """(distance, confidence) when two fingerprints match, else None. Confidence falls from
    0.99 at distance 0 (perceptually identical, but not byte-identical — so never 1.0)."""
    d = distance(a, b)
    if d is None:
        return None
    threshold = VIDEO_THRESHOLD if kind_of(a) == "video" else IMAGE_THRESHOLD
    if d > threshold:
        return None
    return d, round(0.99 - 0.4 * d / threshold, 3)
