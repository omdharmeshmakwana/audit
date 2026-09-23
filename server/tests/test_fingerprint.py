"""Perceptual fingerprints: re-encodes match, different content doesn't."""

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from faible.fingerprint import distance, fingerprint_file, kind_of, match

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def picture(
    path: Path, seed: int, size: tuple[int, int] = (640, 360), fmt: str = "PNG", quality: int = 95
) -> Path:
    img = Image.new("RGB", size, (20 + seed * 40 % 200, 40, 90))
    d = ImageDraw.Draw(img)
    for i in range(8):
        x = (seed * 97 + i * 71) % size[0]
        y = (seed * 53 + i * 37) % size[1]
        d.ellipse(
            [x, y, x + 80 + i * 10, y + 60 + i * 8],
            fill=(240 - i * 20, 200 - seed * 30 % 150, 60 + i * 20),
        )
    img.save(path, format=fmt, quality=quality)
    return path


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=120)


def test_image_reencode_matches_and_different_image_does_not(tmp_path: Path) -> None:
    src = picture(tmp_path / "a.png", 1)
    original = fingerprint_file(src)
    with Image.open(src) as img:  # a real re-encode: smaller, lossy JPEG of the same picture
        img.resize((480, 270)).save(tmp_path / "a.jpg", format="JPEG", quality=60)
    jpeg = fingerprint_file(tmp_path / "a.jpg")
    other = fingerprint_file(picture(tmp_path / "b.png", 4))
    assert original and jpeg and other
    assert (original.kind, original.width, original.height, original.media_type) == (
        "image",
        640,
        360,
        "image/png",
    )
    m = match(original.phash, jpeg.phash)
    assert m is not None and m[1] > 0.8
    assert match(original.phash, other.phash) is None


@needs_ffmpeg
def test_video_reencode_matches_and_different_video_does_not(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=24:duration=3",
        "-pix_fmt",
        "yuv420p",
        str(src),
    )
    reenc = tmp_path / "clip_reencoded.mp4"
    ffmpeg("-i", str(src), "-vf", "scale=480:270", "-crf", "35", str(reenc))
    other = tmp_path / "other.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "mandelbrot=size=640x360:rate=24",
        "-t",
        "3",
        "-pix_fmt",
        "yuv420p",
        str(other),
    )

    a, b, c = (
        fingerprint_file(src, "video/mp4"),
        fingerprint_file(reenc, "video/mp4"),
        fingerprint_file(other, "video/mp4"),
    )
    assert a and b and c
    assert a.kind == "video" and kind_of(a.phash) == "video"
    assert (
        (a.width, a.height) == (640, 360)
        and a.duration_ms
        and 2900 <= a.duration_ms <= 3100
        and a.fps == 24
    )
    m = match(a.phash, b.phash)
    assert m is not None and 0.6 < m[1] < 1.0
    assert match(a.phash, c.phash) is None


def test_images_and_videos_are_never_compared() -> None:
    assert distance("f" * 16, '["' + "f" * 16 + '"]') is None


def test_non_media_has_no_fingerprint(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    assert fingerprint_file(p) is None
