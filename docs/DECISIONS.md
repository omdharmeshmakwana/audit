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
