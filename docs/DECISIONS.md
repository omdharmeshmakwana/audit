# Decisions and deviations from the spec

Every deviation from `faible-tracking-v1-prompt.md`, dated. Newest last.

## 2026-09-23 — Phase 0

1. **No Docker for now.** Docker is not installed on the dev machine. Postgres 16 runs
   natively via Homebrew (`postgresql@16`, databases `faible` and `faible_test`), the server
   runs from a local venv. `docker-compose.yml` will be added when we move to Docker.
   *Decided by the user.*
2. **Python 3.14 instead of 3.12.** The machine has 3.14; all dependencies (numpy, scipy,
   imagehash, argon2-cffi, psycopg) install and import cleanly on it. *Decided by the user.*
3. **Repo lives at the root of `audit-link/`**, not in a `faible/` subfolder. *Decided by the user.*
4. **Extension built with plain esbuild, not Vite + crxjs.** crxjs 2.7.1 wraps content scripts
   in an async `import()` loader. For the MAIN-world observer this means the fetch/XHR patch
   lands after an `await` (the service bundle can capture the original `fetch` first) and
   the relative import resolves against the page origin. `build.mjs` bundles each entry as
   a self-contained IIFE; the manifest is hand-written in `extension/static/manifest.json`.
   *Asked the user per §12; they chose esbuild.*
5. **Capture-all "local file" goes through the local server.** An MV3 service worker cannot
   write files. The worker persists records in `chrome.storage.local` and POSTs them to
   `POST /discovery/capture`, which appends to `data/discovery/<service>-<date>.jsonl`.
   The server redacts a second time before writing. The endpoint is Phase 0 tooling and
   is disabled with `FAIBLE_DISCOVERY_ENABLED=false`.
6. **`host_permissions: ["<all_urls>"]` in Phase 0** so the worker can test re-fetching
   downloads from CDNs we don't know yet. To be narrowed to the discovered hosts in Phase 2.
7. **Magnific matches both `*.magnific.com` and `*.magnific.ai`**, since the spec names
   `.com` but the product has historically lived on `.ai`. Traffic: `www.magnific.com` is live. `*.freepik.com` was added too (same product and
   account, per the user) and is attributed to the `magnific` service.
8. **Downloads keep only the file basename.** `DownloadItem.filename` is an absolute local
   path (it would leak the macOS username/folder layout); only the last path segment is stored.
9. **Downloads are recorded only when they come from a service** (URL, blob origin, final
   URL or referrer on a service host). Other downloads are ignored.
10. **Third-party telemetry is not captured** (Sentry, PostHog, Segment, GA, Clerk, Stripe…,
    see `NOISE_HOST` in `extension/src/content/main-world.ts`). Non-JSON GETs of static
    content (HTML/CSS/JS/fonts/images) are skipped; media GETs are recorded as metadata only.
11. **Fingerprinting pulled forward into Phase 0 (user request).** Three methods, recorded
    into the discovery log only (no `/ingest/asset` yet — that is Phase 1/2):
    - *Method 1, completion hash:* when a job **submitted from this browser** completes, the
      worker fetches its output URL (`credentials: "omit"`) and records sha256. Restricted
      to own jobs because the studio shares one service workspace: history and notifications
      also carry other artists' jobs, which their own extensions attribute.
    - *Method 2, blob hash:* the page-world script wraps `URL.createObjectURL`; media Blobs
      (image/video/audio/octet-stream/untyped, ≤ 500 MB) are hashed in the page with
      `crypto.subtle`. The page receives the original URL unchanged. Evidence `observed`.
      Higgsfield downloads are blob URLs, so this is the exact-bytes path for them.
    - *Method 3, filename → job:* `hf_<date>_<time>_<job_id>.<ext>` → the job's output URL →
      its hash. Evidence `inferred`, confidence 0.9.
    Download records carry `extra.fingerprint` with the chosen hash, method, evidence and
    `agree` (whether independent hashes match; a mismatch is surfaced, never hidden).
12. **Spec adapter signature deferred.** The fingerprint adapter is a narrower pure function
    (`observe(record) → {submittedJobIds, outputs}`) plus `jobIdFromFilename`. The full
    `(request, response) => CaptureEvent[]` adapter arrives with the event schema in Phase 2.
13. **Redaction leak fixed (2026-09-23).** Secret query params were redacted by parsing URLs
    found in text, which missed URLs inside JSON-encoded strings with escaped slashes
    (`https:\/\/…`, Magnific's Pusher frames). Signed CDN tokens reached the local discovery
    log. Redaction now matches secret query params directly in text (extension and server),
    the existing logs were re-redacted in place (5 tokens removed), and tests cover the case.
14. **Signed output URLs travel page → worker, never further.** Method 1 on Magnific needs the
    signed `creation.url`. The page-world script attaches `signed_urls` (redacted → original)
    for hosts matching `SIGNED_OUTPUT_HOST` (`*.cdnpk.net`) only. The worker detaches it before
    the record is queued, and keeps the original only in its local fingerprint state
    (`chrome.storage.local`), so it is never sent to the server or written to the log.
    Rule 9 is read as "secrets never leave the extension"; this keeps that.

## 2026-09-23 — Phase 1

15. **Hash covers the row minus `seq` and `hash`.** `seq` is assigned by the database after
    the hash is computed; ordering is protected by `prev_hash` instead. Appends are serialised
    with a transaction-scoped advisory lock so concurrent ingests cannot fork the chain.
    `recorded_at` is set by the server (never accepted from a client: `EventIn` forbids it).
16. **Append-only is enforced in the database.** Triggers raise on `UPDATE`, `DELETE` and
    `TRUNCATE` of `events`. Only a superuser disabling triggers (`session_replication_role`)
    can alter a row, and `verify-log` then reports it (see `scripts/demo_phase1.sh`).
17. **Three event types added to the §3 list:** `entity.created`, `entity.reference_added`,
    `shot.entity_added`. The spec's seed needs characters and reference images, and every
    derived table must come from events, but §3 had no event for them. Per §12, changes to the
    event schema *after* Phase 1 need the user's approval.
18. **Characters, environments and props share one `entities` table** with `entity_type`.
    Identical shapes; `ReferenceAsset` and `ShotEntity` already key on (type, id).
19. **`activities.shot_id`** (not in §2.1) holds the active shot at capture time (§4.2); null
    means unassigned, which the `unassigned_generation` rule needs.
20. **`assets.registered`.** A version, link or reference can name an asset by sha256 before
    its bytes are registered; the row exists with `registered = false` until an
    `asset.registered` event describes it (whose evidence then becomes the asset's).
21. **Edges resolve late, by hash or by service location.** `generation.requested` inputs and
    `generation.completed` outputs are stored immediately; if only a service id or URL is
    known (Magnific `creation:<id>`, Higgsfield media id, output URL), `asset_id` is filled in
    when an asset with that location appears. This is what joins a Magnific output to a
    Higgsfield input once both are hashed to the same sha256.
22. **Passwords are not in the log.** Argon2 hashes live in `user_credentials`, which is neither
    event-sourced nor rebuilt (an append-only log can never forget a credential).
    `user.registered` / `user.logged_in` are logged.
23. **Server auth and `POST /api/v1/ingest/events` are in Phase 1**, because ingest must know its
    actor (hard rule 8). JWT access (15 min) + refresh (7 days), HS256 with a key from
    `FAIBLE_JWT_SECRET` or a random key persisted in `data/jwt_secret`. The extension side of
    auth, the httpOnly web cookie, and `/ingest/asset` stay in Phase 2.
24. **Ingest results are per event** (`accepted | duplicate | conflict | rejected`) rather than
    all-or-nothing, so one malformed event cannot wedge the extension's retry queue. A reused
    `event_id` with different content is a `conflict`, not a silent duplicate.
25. **`shot.updated` cannot clear a field** (null = unchanged). Clearing a budget, if needed,
    gets its own event later.
26. **Status vocabulary.** The log uses the spec's `queued|running|succeeded|failed|cancelled`;
    Phase 2 adapters map service values (Higgsfield `in_progress`/`completed`, Magnific
    `processing`/`completed`) onto it.
27. **macOS marks files in `server/.venv` hidden, and Python 3.13+ skips hidden `.pth` files**,
    which silently breaks the editable install. Tests set `pythonpath = ["."]`; the CLI is run
    as `python -m faible.cli` from `server/` (or `chflags nohidden` the `__editable__*` files).

## 2026-09-23 — Phase 2

28. **`generation.status` gained an optional `credits_cost`** (approved by the user, §12). A
    failed Higgsfield job is refunded (`job_cost: 0`); without this the submit-time cost
    would overstate spend.
29. **Login is in the toolbar popup, not the in-page side panel.** The spec puts login in the
    panel, but key presses on higgsfield.ai / magnific.com are observable by those sites'
    scripts even inside a closed Shadow DOM. The panel shows "log in from the toolbar"
    instead; everything else in §4.1 (shot selector, last 10, unassigned counter, bulk
    assign) is in the panel.
30. **"Assign to shot" reuses `link.asserted`** (user's choice over a new
    `activity.shot_assigned` event). Assigning a capture links its output asset to the shot;
    the projector then sets `shot_id` on the activities that produced that asset. A
    generation with no output (failed, cancelled, still hashing) cannot be assigned; the
    panel disables it and says why.
31. **Host permissions narrowed** from `<all_urls>` (Phase 0, #6) to the service sites, their
    output and upload CDNs (`d8j0ntlcm91z4` / `d2ol7oe51mr4n9.cloudfront.net`,
    `*.cdnpk.net`) and `localhost:8000`. If a service moves its CDN, hashing fails visibly
    ("could not hash output …") rather than silently.
32. **Uploaded inputs are located, not stored.** The page hashes the upload (a Blob cannot
    cross `chrome.runtime` messaging), and the worker emits `asset.location_seen`
    (sha256 + service media/creation id). Outputs and re-fetchable downloads are stored via
    `/ingest/asset`. An input known only by hash is still a full node in the graph.
33. **Status and completion events only for jobs submitted from this browser.** Shared
    workspace: other artists' jobs appear in history, notifications and project channels;
    their own extensions attribute them. Outputs of all jobs are remembered locally so a
    download can still be matched by filename.
34. **Responses are cloned the moment they resolve** (before the page reads the body), found
    when request-body hashing added an `await` ahead of the clone.
35. **Discovery mirror kept, off by default** (popup checkbox) for adapter drift work.
36. **Phase 2 demo uses `GET /api/v1/shots/{id}` and `faible show-shot CODE`** in place of the
    shot page, which is Phase 3 UI.
37. **Adapters match on the normalised URL** (`new URL(url).href`). Magnific began opening its
    Pusher socket as `wss://ak-data.magnific.com:443/…`; the literal pattern stopped matching
    and every progress/completion message was silently dropped. Found from the discovery log.
38. **A download closes a missed completion.** If a downloaded file's name identifies a job this
    browser submitted and no completion was recorded, the worker emits `generation.completed`
    (evidence `observed`) and registers the bytes under that job id. A safety net, not the
    primary path: normally the push/poll message arrives first.
39. **A hashed upload that no adapter recognises raises `adapter.unknown_payload`**, so a new
    upload path shows up as drift instead of a silent gap in the chain.

## 2026-09-23 — Phase 3

40. **Backfill commands add knowledge by appending events.** `faible fingerprint` computes
    pHash for stored assets that lack one and appends `asset.registered` with it (spec §3:
    "includes phash once computed"). `faible fetch-missing` fetches bytes for assets known only
    by hash from a *public, unsigned* location URL, verifies the sha256, stores and
    fingerprints them. Signed/redacted URLs are skipped. Admin tools; normally the extension
    supplies bytes (#44).
41. **Fingerprints live in the log.** The server computes pHash at `/ingest/asset` and puts
    it in `asset.registered`; the projector derives `asset_similarities` (inferred edges)
    from those payloads, so `rebuild` reproduces the same edges. Seed events carry no
    phash (they must stay byte-identical for idempotency); seed images get fingerprints
    from `faible fingerprint`.
42. **Match thresholds and confidence.** Image: Hamming distance ≤ 10 of 64; video: mean
    per-frame distance ≤ 12 over 8 evenly spaced frames. Confidence = 0.99 − 0.4·d/threshold,
    never 1.0 (perceptually identical is not byte-identical). Placeholder images that share a
    layout match each other; the chain therefore follows only the *best* match (an older
    asset with a producer, then smallest distance), never every edge.
43. **Duplicate detection normalises stored payloads through the current schema**, so an
    approved optional field (e.g. #28) doesn't turn a re-sent old event into a `conflict`.
    Stored rows are never rewritten.
44. **The extension registers uploaded files' bytes** by fetching the service's stored copy
    (Higgsfield uploads CDN is public; Magnific's is signed and kept inside the extension).
    Both were verified byte-identical to what the page hashed.
45. **Web UI without HTMX.** Server-rendered Jinja2 with plain forms; no page needed partial
    updates, and vendoring a JS library for none would be a dependency without a use.
    Session: `web` JWT (12 h) in an httpOnly, SameSite=Lax cookie; cross-origin POSTs are
    refused by Origin check. The API accepts the cookie or a bearer access token.
46. **Asset import** (`/import`) registers a file made outside the services with evidence
    `asserted` and location `manual`; fingerprinting proposes inferred links. This is the
    spec's Phase 3 demo path ("re-encode a downloaded clip … upload it as a new asset").
47. **A chain step's "at"** is the activity's start, and a match step's is the derived asset's
    first sighting; steps are ordered oldest first.

## 2026-10-13 — Company OTP and dashboard

48. **Company OTP pilot.** User requested remembered email-code login rather than passwords,
    initially restricted to `om@faiblemedia.com` within `faiblemedia.com`. SMTP delivery is
    configurable; an explicit local delivery mode writes a private test code file instead
    of sending email. OTP challenges are private, expiring rows excluded from event replay.
49. **Shot selection on each page visit.** The extension opens its panel and gates interaction
    until a shot is explicitly selected. OTP entry remains on an extension-owned page, per
    #29. Login is remembered for 30 days; shot confirmation is per page visit.
50. **Live dashboard.** Two-second polling over captured activities, grouped by Faible email.
    Artists see their own feed; director/admin roles can filter all artists. Existing shared
    production pages are unchanged. Capture remains based on service request/response traffic.
51. **Prompt warnings.** Copied the user-supplied promptFilter.js word lists as data. Red
    warnings appear while typing and on dashboard captures; they flag only and do not block
    model requests. Normalize both prompt and vocabulary (fixing hyphenated-term matching).
52. **Queue attribution.** Pending events carry private original-user metadata stripped before
    ingest; events cannot be flushed under another account. Legacy unowned entries are held.

## 2026-10-13 — Password login and prompt warning correction

53. **OTP removed by user request.** Login now uses company email/password with the explicit
    pilot account om@faiblemedia.com / changeme. setup-test-login creates or resets that
    account without changing passwords at startup. Previously applied OTP migration/table
    remain in migration history; no OTP routes or delivery code are active.
54. **Broader prompt editor detection.** Previous logic skipped plain text inputs without
    prompt hints, empty contenteditable attributes and plaintext-only editors. The warning
    now checks these, dynamic editors, open shadow roots and supported service frames,
    independently of model. It adds both a red field outline and a prominent red banner.

## 2026-10-13 — Studio admin reporting

55. **Artist summaries and full history.** Admin/director dashboard covers every artist,
    current selected shot, model/shot usage, full-history reported credit totals, unknown
    costs, statuses and recent timestamps. Paginated history replaces the 200-row cap.
56. **Automatic login prompt.** A logged-out supported service page opens the extension-owned
    company login tab automatically. Saved 30-day sessions and per-visit shot selection stay.
57. **Account roles remain event-sourced.** create-account creates a company artist/admin via
    user.registered; existing accounts are preserved. Generic extension ingest cannot submit
    account events, preventing artist-to-admin escalation through the capture endpoint.

58. **Chosen admin account.** User selected rushik@faiblemedia.com. The temporary default
    admin@faiblemedia.com is demoted through user.role_changed, an append-only role event
    so replay cannot restore revoked privileges. Role changes are local CLI operations;
    extension ingest cannot submit role events or another user's active-shot event.
