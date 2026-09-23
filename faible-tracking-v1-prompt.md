# FAIBLE — Tracking Layer v1 (Build Spec for Claude Code)

You are building the first internal version of **Faible**, a provenance and production-tracking
system for an AI video studio. It records every generation made on **Higgsfield** and **Magnific**
by studio artists, links each output to a shot in a production, preserves the full prompt chain
(every iteration from first draft to final), and runs a rule-based auditor that flags gaps and
anomalies in the record.

This is NOT a dashboard app. It is a **system of record**. The event log is the product; the UI
is a viewer over it. If you find yourself building UI before the capture path and the event log
are solid, stop and reorder.

Read this whole file before writing any code. Then restate the deliverable and the phase plan in
five lines and start Phase 0.

---

## 0. HARD RULES (each stated once; a violation means rewrite the affected part)

1. **Source of truth is an append-only, hash-chained event log.** Nothing is ever updated or
   deleted in the log. Assets, versions, chains and graph edges are *derived* from events and
   can be rebuilt from the log at any time (`faible rebuild` command must exist).
2. **Capture from network responses, never from page text.** The extension reads the JSON the
   services send to their own UI. DOM scraping is a fallback only, and any field obtained by
   scraping is labeled `evidence: "observed_dom"`.
3. **Every asset is identified by content hash (SHA-256), never by URL or filename.** URLs and
   filenames are *locations* of an asset, stored separately, and expected to expire.
4. **Every record carries an evidence level**: `originated` (Faible made the request),
   `service_reported` (read from the service's own API/history response), `observed`
   (extension saw it in traffic/UI), `inferred` (matched by fingerprint), `asserted`
   (a human linked it manually). Never silently upgrade a level.
5. **No field names for Higgsfield or Magnific payloads are known in advance.** You discover
   them in Phase 0 with real traffic and write them into `docs/adapters/<service>.md` before
   writing adapter code. Until then, every adapter field is marked `UNVERIFIED`.
6. **Capture failed, cancelled and discarded generations too.** They cost credits and they are
   part of the chain.
7. **The auditor only flags. It never blocks, deletes, edits, or generates.** Every flag cites
   the rule and the evidence. Every flag can be dismissed with a reason, and dismissals are
   stored.
8. **Extension users log in individually to Faible** even though the studio shares one
   Higgsfield/Magnific workspace account. Attribution of a generation = the Faible user whose
   extension captured it. Store the service-side user/workspace ID separately if the payload
   contains one.
9. **Secrets never enter the log.** Strip cookies, auth headers, API keys and session tokens
   from captured requests before they leave the extension. Prompts and references are kept.
10. **Everything runs locally**: Docker Compose with Postgres, FastAPI server, MinIO (or a
    plain filesystem volume) for asset copies. No cloud dependencies in v1.
11. **Small, real steps.** Each phase ends with a working demo and a checklist that you actually
    run. Do not build Phase N+1 features into Phase N.
12. When something in this spec conflicts with what you find in the real service traffic, the
    traffic wins. Note the conflict in `docs/DECISIONS.md` and proceed.

---

## 1. STACK (fixed)

| Layer | Choice |
|---|---|
| Server | Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, uvicorn |
| Database | PostgreSQL 16 (Docker) |
| Asset copies | Local volume `./data/assets/<sha256[:2]>/<sha256>` (MinIO optional, off by default) |
| Auth | Email + password, Argon2 hashing, short-lived JWT for the extension, httpOnly cookie for the web UI |
| Extension | Chrome, Manifest V3, TypeScript, Vite + `@crxjs/vite-plugin` (or plain esbuild if crxjs causes trouble) |
| Web UI | Keep it minimal. Server-rendered Jinja2 templates + HTMX is acceptable and preferred over a SPA for v1. If you use a SPA, plain React + Vite, TypeScript, no state library. |
| Fingerprinting | `imagehash` (pHash) for images; for video, sample 8 evenly spaced frames with `ffmpeg`, pHash each, store the vector. |
| Tests | pytest for server; Vitest for extension logic; one Playwright smoke test for the UI |
| Tooling | `ruff`, `mypy --strict` on server; `eslint` + `tsc --noEmit` on extension; `pre-commit` |

Repo layout:

```
faible/
  docker-compose.yml
  server/            FastAPI app, alembic, tests
  extension/         MV3 extension
  docs/
    DECISIONS.md     every deviation from this spec, dated
    adapters/        higgsfield.md, magnific.md — discovered payload shapes
    EVENTS.md        the event schema, kept in sync with code
  scripts/           seed, rebuild, export
```

---

## 2. DOMAIN MODEL

Two layers. The **provenance layer** follows W3C PROV (entity / activity / agent). The
**production layer** sits on top and is what artists see.

### 2.1 Provenance layer (derived from events)

```
Asset       id (uuid), sha256 (unique), media_type, width, height, duration_ms, fps,
            bytes, phash (nullable), first_seen_at, evidence
AssetLocation asset_id, service, url, service_asset_id, seen_at, is_alive (nullable)
Activity    id, kind (generate|upscale|edit|variation|download|upload|import),
            service, model_id, model_version (nullable), status
            (queued|running|succeeded|failed|cancelled), started_at, ended_at,
            service_job_id, parameters (jsonb), prompt (text, nullable),
            negative_prompt (nullable), seed (nullable), credits_cost (numeric, nullable),
            credits_currency (text), evidence, captured_by_user_id
ActivityInput   activity_id, asset_id, role (reference|source|mask|init_image|…)
ActivityOutput  activity_id, asset_id, index
Agent       users (Faible), and service_accounts (service, workspace_id, service_user_id)
```

Edges are implied: `Asset -generatedBy-> Activity -used-> Asset`, `Activity -attributedTo-> Agent`.
Expose them through a `/graph` endpoint; do not add a graph database.

### 2.2 Production layer

```
Project     id, name, code, status
Sequence    id, project_id, name, order
Shot        id, sequence_id, code (e.g. "SQ02_SH010"), description, status,
            credit_budget (nullable)
ShotVersion id, shot_id, asset_id, version_number, created_by_user_id, created_at,
            status (wip|submitted|approved|rejected), notes (text)
Character / Environment / Prop   id, project_id, name, description
ReferenceAsset  entity_type, entity_id, asset_id, label
ShotEntity      shot_id, entity_type, entity_id   (which characters etc. appear in a shot)
```

### 2.3 Prompt chain

A **chain** is the ordered list of activities that led to a given asset, walking `generatedBy`
and `used` edges backwards until reaching assets with no producing activity (uploads,
references). Compute on demand; cache per asset. A shot's chain view = union of chains of all
its versions, ordered by time, with a diff between consecutive prompts (word-level diff, shown
in the UI).

### 2.4 Evidence and confidence on every edge

Every derived edge stores `evidence` (from rule 4) and `confidence` (0–1). Exact hash match =
1.0 observed. pHash match ≤ threshold = inferred, confidence from distance. Manual link =
asserted, 1.0, with the user who asserted it.

---

## 3. EVENT LOG

Table `events`:

```
seq          bigserial primary key
event_id     uuid unique
type         text                     (see list)
occurred_at  timestamptz              (when it happened on the service/extension side)
recorded_at  timestamptz default now()
actor_user_id uuid nullable
source       text                     (extension|server|script|import)
evidence     text
payload      jsonb
prev_hash    text                     (hash of previous event row's canonical form)
hash         text                     (sha256 of canonical JSON of this row minus hash)
```

Canonical form = RFC 8785-style JSON canonicalization (sorted keys, no whitespace). Provide
`faible verify-log` that walks the chain and reports the first broken link.

Event types for v1 (define each payload in `docs/EVENTS.md` with a JSON example):

```
user.registered, user.logged_in
project.created, sequence.created, shot.created, shot.updated
shot.active_set            (user set their active shot in the extension)
generation.requested       (extension saw the request go out)
generation.status          (queued/running/failed/cancelled, from polling responses)
generation.completed       (outputs available; includes output URLs and any service IDs)
asset.registered           (server has bytes + sha256; includes phash once computed)
asset.location_seen
download.observed          (chrome.downloads event: filename, url, size)
shot_version.created, shot_version.status_changed
link.asserted              (manual link between asset and activity/shot)
audit.flag_raised, audit.flag_dismissed
adapter.unknown_payload    (extension saw a response it could not parse; store a redacted sample)
```

The extension sends events to `POST /ingest/events` in batches, with idempotency on
`event_id`. The server validates against Pydantic models per type, rejects unknown types,
and never trusts `recorded_at` from the client.

---

## 4. EXTENSION DESIGN

### 4.1 Components

- **Background service worker**: holds auth token, event queue (persisted in
  `chrome.storage.local`, retried with backoff), `chrome.downloads` listener, download hashing.
- **Content script** (per service): injects a page-world script, relays messages, renders a
  small Faible side panel (Shadow DOM, does not touch the host page's styles).
- **Page-world script**: patches `window.fetch` and `XMLHttpRequest` to observe request URL,
  method, request body and response body for URLs matching the service adapter's patterns.
  Never modify requests or responses. Strip headers listed in rule 9 before relaying.
- **Adapters** (`extension/src/adapters/higgsfield.ts`, `magnific.ts`): a pure function
  `(request, response) => CaptureEvent[] | null`. Unit-tested with recorded fixtures. Each
  adapter exports its URL patterns and a `fixtures/` folder of real (redacted) payloads.
- **Side panel**: login, active project/shot selector (searchable), "last 10 captures" list
  with shot assignment, a "not assigned" counter, and an "Assign to shot" bulk action.

### 4.2 Behaviour

- On login the panel loads the user's projects/shots and restores the last active shot.
- Every capture is tagged with the active shot at capture time. If none is set, the capture
  is stored as `unassigned` and the panel shows a persistent badge count. Unassigned captures
  are an auditor rule, not an error.
- **Downloads**: on `chrome.downloads.onChanged` with `state: complete`, fetch the download
  URL again in the background (same session), hash it with `crypto.subtle.digest`, and emit
  `download.observed` + upload the bytes to `POST /ingest/asset` (multipart, sha256 in header,
  server rejects on mismatch). If re-fetching is not possible (signed URL expired, CORS),
  emit `download.observed` without a hash and let the auditor flag it. **Do not read the
  user's local filesystem.**
- On `generation.completed`, also fetch the output URLs from the background and register the
  bytes immediately, so the asset exists even if the artist never downloads. Skip if the file
  is over 500 MB; record the location only.
- If the adapter returns `null` for a response on a matched URL, emit
  `adapter.unknown_payload` with a redacted sample (first 2 KB, secrets stripped). This is
  how you learn what you're missing.

### 4.3 What the extension must NOT do

Send cookies or auth headers to Faible. Modify service requests. Generate anything. Poll
service endpoints on its own (v1 only observes what the page does).

---

## 5. SERVER API (FastAPI, `/api/v1`)

```
POST /auth/register, /auth/login, /auth/refresh
GET  /me
GET  /projects, POST /projects, GET /projects/{id}/tree   (sequences + shots + entities)
POST /projects/{id}/shots/import                           (CSV: sequence,shot_code,description)
POST /ingest/events            batch, idempotent
POST /ingest/asset             multipart, sha256 header, returns asset id
GET  /assets/{id}              metadata, locations, producing activity, chain summary
GET  /assets/{id}/chain        full chain with prompt diffs
GET  /assets/{id}/file         streams the local copy
GET  /shots/{id}               versions, chain union, flags, credit spend
POST /shots/{id}/versions      promote an asset to a numbered version
POST /links                    manual assertion (asset ↔ activity | asset ↔ shot)
GET  /graph?project_id=        nodes + edges (PROV + production), paginated
GET  /audit/flags?project_id=&status=open
POST /audit/flags/{id}/dismiss {reason}
POST /audit/run?project_id=    runs all rules now (also runs on a schedule every 10 min)
GET  /reports/credits?project_id=   spend by shot, by user, by service, by day
```

Ingest path: validate → write event(s) inside one transaction → run projectors (functions
that update derived tables from that event) in the same transaction. Projectors must be
pure functions of (current derived state, event) so `rebuild` can replay them.

---

## 6. AUDITOR (rules only)

Each rule is a class with `id`, `severity` (info|warn|error), `description`, and
`run(project) -> list[Flag]`. A Flag has: rule_id, severity, subject (asset|activity|shot|
version id), message, evidence (jsonb: the specific ids/values that triggered it), created_at.
A flag is deduplicated on (rule_id, subject, evidence hash) so re-running doesn't spam.
Dismissed flags stay dismissed for the same evidence hash.

v1 rules:

| id | severity | triggers when |
|---|---|---|
| `unassigned_generation` | warn | activity has no shot after 24 h |
| `asset_without_provenance` | error | asset registered with no producing activity and no `asserted` link |
| `activity_without_output` | warn | succeeded activity with zero registered outputs after 1 h |
| `missing_prompt` | error | generate/edit activity with empty prompt and no input assets |
| `download_unhashed` | warn | `download.observed` with no sha256 |
| `orphan_download` | warn | hashed download that matches no known asset (exact or pHash) |
| `broken_chain` | error | version whose chain hits an asset with no origin and no reference label |
| `duplicate_asset_locations` | info | same sha256 seen under two service IDs (useful, not wrong) |
| `credit_budget_exceeded` | error | shot credit spend > `credit_budget` |
| `credit_spike` | warn | a shot's daily spend > 3× its trailing 7-day median |
| `failed_generation_burst` | warn | ≥5 failed activities on one shot in 1 h |
| `stale_wip` | info | shot with no new version in 7 days and status wip |
| `version_skipped_submit` | warn | version marked approved without ever being submitted |
| `unknown_payload_seen` | info | any `adapter.unknown_payload` in the last 24 h (points at adapter drift) |
| `log_integrity` | error | `verify-log` finds a broken hash link |

Every rule ships with a unit test containing a fixture that triggers it and one that doesn't.

---

## 7. WEB UI (minimal, in this order)

1. Login. Project list.
2. **Shot page**: versions in a column, selected version's chain on the right (each step:
   service, model, prompt with diff vs previous, inputs as thumbnails, credits, evidence badge,
   who, when). Open flags for the shot at the top.
3. **Asset page**: preview, hash, locations, chain, "assert link" form.
4. **Audit page**: flags table, filter by rule/severity, dismiss with reason.
5. **Credits page**: spend by shot / user / service, daily bars (plain table + a simple chart).
6. **Unassigned queue**: captures with no shot, assign in bulk.

No moodboards, no approvals workflow, no comments, no Frame.io. Those are later phases.

---

## 8. SEED DATA

`scripts/seed.py` creates: users `director@studio`, `artist1@studio`, `artist2@studio`
(password `changeme`); project "DEMO" with sequences SQ01, SQ02; ten shots
(`SQ01_SH010` … `SQ02_SH050`); characters "Mira", "Kesh", "The Warden"; three placeholder
reference images per character (generated locally with PIL, labeled). Also seed one full
synthetic chain: reference → generate → variation → upscale → download, with credits, so the
shot page and auditor have something to show before any real capture.

If a CSV shot list is provided at `data/shotlist.csv`, import it instead of the synthetic shots.

---

## 9. PHASES (do them in order; each ends with a demo you actually run)

### Phase 0 — Discovery (no product code)
- Docker Compose with Postgres up. Empty FastAPI with `/health`.
- Build the extension skeleton with the fetch/XHR patcher and a **capture-all mode** that
  logs every JSON response on `*.higgsfield.ai` and `*.magnific.com` (and any API subdomains
  you find) to the console and to a local file via the background worker.
- **STOP and ask the user to perform**: one image generation, one video generation, one
  upscale, one failed/cancelled job, one download, on each service. Collect the redacted
  payloads.
- Write `docs/adapters/higgsfield.md` and `magnific.md`: endpoint patterns, where prompt /
  model / job id / status / outputs / credits live, what polling looks like, what the download
  URL looks like and whether it's re-fetchable. Mark anything not seen as `UNVERIFIED`.
- Demo: the docs, plus fixture files.

### Phase 1 — Event log + model
- Alembic migrations for §2 and §3. Event ingest with validation and hash chaining.
  Projectors. `rebuild` and `verify-log` commands. Seed script. Tests.
- Demo: seed, ingest the synthetic chain as events, `verify-log` passes, tamper one row,
  `verify-log` fails on it.

### Phase 2 — Extension capture
- Auth + side panel + active shot. Adapters from Phase 0 fixtures with tests. Event queue
  with retry. Output fetching and asset registration. Download hashing.
- Demo: artist logs in, sets a shot, generates on Higgsfield, the shot page shows the version
  with prompt and credits within 10 seconds; downloads it, hash matches the registered asset.
  Repeat on Magnific with an upscale of that output; the chain shows both steps linked.

### Phase 3 — Chains, graph, UI
- Chain computation with prompt diffs, pHash + video fingerprint, inferred edges, manual
  links, shot/asset/unassigned pages.
- Demo: re-encode a downloaded clip with ffmpeg, upload it as a new asset, it links to the
  original as `inferred` with a confidence shown.

### Phase 4 — Auditor + credits
- All §6 rules, scheduler, audit page, credits report.
- Demo: every rule fires on its fixture; dismissing a flag keeps it dismissed after re-run.

### Phase 5 — Hardening
- Two-week dogfood checklist in `docs/DOGFOOD.md`: what to watch, how to report adapter
  drift, how to export the log (`faible export --project DEMO` → JSON lines + assets tarball).

---

## 10. THINGS YOU MUST NOT ASSUME

- That Higgsfield or Magnific expose prompts in their responses (they might return only IDs;
  then the request body is the source, and the evidence is `observed`).
- That download URLs can be re-fetched from the background worker.
- That a job's credit cost appears in the job response (it may only appear in a separate
  balance/transaction call; if so, capture that call and join on time + job id, and label the
  cost `inferred`).
- That both services use fetch (one may use WebSockets or SSE for status; observe and handle).
- That the page-world patch survives the services' own bundling; test on the real site.
- Any Chrome API behaviour you are not sure of: check the MV3 docs, do not write from memory.

---

## 11. DEFINITION OF DONE (v1)

- A new artist can install the extension, log in, pick a shot, generate on either service,
  and the generation appears on the shot page with prompt, model, inputs, outputs, credits,
  evidence badge and their name, with no manual steps.
- `verify-log` passes on the production database; `rebuild` reproduces identical derived
  tables (assert with a checksum of the derived tables before/after).
- Every auditor rule has passing tests and has fired at least once on real data during
  dogfooding.
- `docs/adapters/*.md` has no `UNVERIFIED` fields left for the flows in Phase 0.
- `docs/DECISIONS.md` lists every deviation from this file.

---

## 12. WORKING STYLE

Ask before: choosing between crxjs and esbuild if crxjs fails; anything that would require
reading local files; any change to the event schema after Phase 1. Otherwise decide, record
the decision, keep moving. Prefer boring, explicit code over clever code. Write the test
before the projector. Commit at the end of every phase with the demo checklist in the message.

**Never drift:** event log first, hashes not URLs, network not DOM, evidence on everything,
auditor flags only, discover payloads before writing adapters.
