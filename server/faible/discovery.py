"""Phase 0 discovery sink.

The extension's capture-all mode POSTs raw (already redacted) traffic records here and
they are appended to `data/discovery/<service>-<YYYY-MM-DD>.jsonl`. This is tooling
for writing `docs/adapters/*.md`, not part of the event log, and is disabled with
FAIBLE_DISCOVERY_ENABLED=false.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from faible.config import get_settings
from faible.redact import redact, redact_headers

router = APIRouter(prefix="/discovery", tags=["discovery"])

SAFE_SERVICE = re.compile(r"^[a-z0-9_-]{1,32}$")


class CaptureRecord(BaseModel):
    service: str
    transport: str  # fetch | xhr | ws | sse | fetch-stream | download
    captured_at: str
    page_url: str | None = None
    method: str | None = None
    url: str | None = None
    status: int | None = None
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: Any = None
    extra: dict[str, Any] = Field(default_factory=dict)


class CaptureBatch(BaseModel):
    records: list[CaptureRecord]


@router.post("/capture")
def capture(batch: CaptureBatch) -> dict[str, int]:
    settings = get_settings()
    if not settings.discovery_enabled:
        raise HTTPException(status_code=404)
    out_dir = settings.data_dir / "discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    written = 0
    for rec in batch.records:
        service = rec.service if SAFE_SERVICE.match(rec.service) else "unknown"
        row = rec.model_dump()
        row["request_headers"] = redact_headers(row["request_headers"])
        row["response_headers"] = redact_headers(row["response_headers"])
        row = redact(row)
        with (out_dir / f"{service}-{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        written += 1
    return {"written": written}
