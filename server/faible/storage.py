"""Local asset copies: `data/assets/<sha256[:2]>/<sha256>` (spec §1). Content-addressed, so
writing the same bytes twice is a no-op and a file's name is its integrity check."""

import hashlib
import os
from pathlib import Path

from faible.config import get_settings


def asset_path(sha256: str, data_dir: Path | None = None) -> Path:
    base = (data_dir or get_settings().data_dir) / "assets"
    return base / sha256[:2] / sha256


def store_bytes(data: bytes, data_dir: Path | None = None) -> str:
    sha256 = hashlib.sha256(data).hexdigest()
    path = asset_path(sha256, data_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return sha256
