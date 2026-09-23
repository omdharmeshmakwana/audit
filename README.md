# Faible

Provenance and production tracking for an AI video studio. Spec: `faible-tracking-v1-prompt.md`.
Deviations: `docs/DECISIONS.md`.

## Local setup (no Docker yet)

Requires Homebrew `postgresql@16` running, Python 3.14, Node 24.

```sh
createdb faible && createdb faible_test

cd server
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn faible.main:app --port 8000     # http://localhost:8000/health

cd ../extension
npm install && npm run build                      # → extension/dist
```

Load the extension: `chrome://extensions` → Developer mode → **Load unpacked** → `extension/dist`.

## Database and CLI (from `server/`)

```sh
.venv/bin/python -m alembic upgrade head     # create / migrate tables
.venv/bin/python -m faible.cli seed          # demo users (password: changeme), project, chain
.venv/bin/python -m faible.cli verify-log    # walk the hash chain
.venv/bin/python -m faible.cli rebuild       # replay the log; checksum must match
.venv/bin/python -m faible.cli checksum
.venv/bin/python -m faible.cli events-doc    # regenerate docs/EVENTS.md
../scripts/demo_phase1.sh                    # Phase 1 demo on a throwaway database
```

Web UI: http://localhost:8000 (log in as `artist1@studio` / `changeme`). Projects → shot page
(versions, chain with prompt diffs, evidence, credits) → asset page (preview, hash, locations,
similar assets, assert-link form). Also: unassigned queue, file import.

Backfills (append events, never edit): `faible fingerprint`, `faible fetch-missing`.
Readable chain in the terminal: `faible show-shot SQ01_SH010`.

API: `POST /api/v1/auth/login`, `/auth/refresh`, `/auth/register`, `GET /api/v1/me`,
`POST /api/v1/ingest/events` (bearer token). Event schema: `docs/EVENTS.md`.

## Phase 0 — discovery

The extension runs in capture-all mode. Every JSON response (and WebSocket/SSE message)
seen by Higgsfield / Magnific pages is redacted and written to
`data/discovery/<service>-<date>.jsonl` by the local server. The popup shows counts,
queue and hosts seen; page DevTools console shows `[faible]` lines per capture.

## Checks

```sh
cd server && .venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/mypy faible
cd extension && npm test && npx tsc --noEmit && npx eslint .
```
