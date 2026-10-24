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
.venv/bin/python -m faible.cli seed          # DEMO data only: never on the live database (DECISIONS #69)
.venv/bin/python -m faible.cli verify-log    # walk the hash chain
.venv/bin/python -m faible.cli rebuild       # replay the log; checksum must match
.venv/bin/python -m faible.cli checksum
.venv/bin/python -m faible.cli events-doc    # regenerate docs/EVENTS.md
.venv/bin/python -m faible.cli import-history PATH… [--dry-run]   # Higgsfield exporter files → log
.venv/bin/python -m faible.cli export-history [--out DIR]         # history → exporter files
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
cd server && .venv/bin/python -m pytest && .venv/bin/python -m ruff check . \
  && .venv/bin/python -m ruff format --check . && .venv/bin/python -m mypy faible
cd extension && npm test && npx tsc --noEmit && npx eslint .
```

(`python -m …` works even when the venv's script shebangs point at an old checkout path.)

## Company login and live dashboard

The test account is `om@faiblemedia.com` / `changeme`. Provision it once from `server/`:

```sh
.venv/bin/python -m faible.cli setup-test-login
```

This command creates the account or resets its password to the requested test password.
Normal app startup never resets passwords. Both web and extension logins use email/password
and remember the browser for 30 days. OTP routes and delivery are removed.

Rebuild with `npm run build` from `extension/`, reload Faible in `chrome://extensions`, and
refresh every already-open Higgsfield/Magnific tab. Load `extension/dist` for a new install.
The panel requires a shot selection on each page visit; login opens an extension-owned tab.

Open http://localhost:8000/dashboard. The feed polls every two seconds, groups captures by
artist email and shows up to 200 recent requests with shot, model, prompt, settings, status,
inputs, outputs and reported credits. Artists see their own dashboard; director/admin roles
can filter all artists. Existing project/asset pages remain shared studio views.

The prompt warning operates independently of the selected model or generation type. It
checks text inputs, textareas and rich-text editors as you type, including empty/implicit
contenteditable attributes, plaintext-only editors, dynamically replaced fields, open shadow
roots and supported service frames. Matching fields get a red outline plus a red warning
banner listing terms from the supplied promptFilter.js. Warnings clear when matches are
removed, and never block generation. Both vocabulary and text are normalized for case,
spacing and punctuation. Substring matching follows the supplied filter.

Queued captures retain their original artist identity across account switches. Legacy
unowned queue entries are not attributed to a new login automatically.

## Dashboard: employee analytics and studio overview

`http://localhost:8000/dashboard` has one filter bar and four tabs. Filters: employee
(directors/admins), period (presets or custom dates), time zone (this browser, IST or UTC),
service, model, status, project, shot and generation type. The URL keeps them, so a filtered
view can be shared.

- **Overview**: totals (generations by status, reported cost, estimated UI credits, unknown
  costs, prompts, inputs/outputs linked to assets, downloads, usage pages), generations per
  day and per hour, and breakdowns by service, model, status, type, project and shot. Click a
  label to filter the dashboard by it; click a count to open those generations in the live
  feed. The prompt history lists recent prompts with their inputs and outputs, the
  generations upstream and downstream of each in the prompt chain, and **Open chain**, the
  shot/asset page with the full chain and word-level prompt diffs. Directors and admins also
  get the employee comparison (ranked), model usage per employee, and the unattributed
  Higgsfield history summary.
- **Live feed**: the existing live dashboard (summaries and paginated history, refreshed
  every two seconds), now narrowed by the filters.
- **Usage pages**: pages saved with "Save usage page", with their tables. Artists see the
  pages they saved; directors/admins see all.
- **Higgsfield history** (directors/admins): generations as Higgsfield's own history lists
  them, from any employee's extension or imported exporter files, marked attributed (an
  employee's extension captured the same job live) or unattributed.

How things are counted is shown under "How these numbers are counted" on the page:
attribution is always the Faible user signed in to the extension, never the shared
Higgsfield login; a job seen both live and in history counts once; costs are summed per
currency as the service reported them and unknown costs are counted, not zeroed; "UI
credits" = Higgsfield cost ÷ 100 is an estimate (the exporter's convention; the unit is not
verified). API: `GET /api/v1/analytics`, `/api/v1/usage-captures[/{id}]`,
`/api/v1/service-history`, and `/api/v1/dashboard` with the same filter parameters.

## Higgsfield history, usage pages and JSON export (integrated exporter)

The standalone Higgsfield exporter (`../higgsfield_exporter`) is built into this extension
(DECISIONS #59–68). It runs on Faible's single capture path, so a response is observed once.
In the toolbar popup, on a higgsfield.ai tab:

- **Higgsfield login**: detected from Higgsfield's Clerk session or account responses; type
  a label if it isn't detected. It names the export folders and is stored as metadata only.
- **Auto-scroll & capture** scrolls a history page (Assets, Marketing Studio…) to the end.
  Every generation Higgsfield's history returns is kept in this browser and, when you are
  signed in to Faible, sent as studio history (not as your generations).
- **Save usage page** on Higgsfield's Usage or Organization page saves the JSON the page
  loaded plus its tables and text (labelled as read from the page), redacted.
- **Saved in this browser**: per-login counts, **Export** / **Export all** to a folder you
  choose once (`generations.json`, `generations_raw.json`, `usage_raw.json`, the exporter's
  formats, readable by its audit page, review page, merge script and usage dashboard),
  **Rename** and **Clear** (clears this browser's copy only).

History also arrives passively while you browse Higgsfield. Nothing is sent to the server
without a Faible login; the local copy works either way, as the exporter did.

## Upgrading from the two separate extensions

1. Server: `cd server && .venv/bin/python -m alembic upgrade head`. The migration only adds
   the `service_generations` and `usage_captures` tables; existing events and tables are
   untouched, and `verify-log` / `rebuild` keep passing.
2. Extension: `cd extension && npm run build`, then reload **Faible** in `chrome://extensions`
   (same unpacked folder, so the login, queue and settings are kept) and refresh open
   Higgsfield/Magnific tabs.
3. Old exporter extension: in its popup, **Export all** first (its saved data lives in that
   extension's storage, which Faible cannot read). Then disable or remove it, so two
   extensions don't capture the same pages.
4. Old exporter files: load them into Faible (attributed to no employee; safe to re-run):

   ```sh
   .venv/bin/python -m faible.cli import-history ../../higgsfield_exporter/exports --dry-run
   .venv/bin/python -m faible.cli import-history ../../higgsfield_exporter/exports \
       ../../higgsfield_exporter/to_be_cleaned
   ```

   Folders are read recursively. Raw, normalized, audited/selected, merged and Seedance
   files are understood; records already recorded are skipped, so adding the `merged/` files
   only adds what the per-account exports lack. The files themselves are never modified.
5. To use the exporter's own tools on the merged data: `faible export-history --out DIR`
   (a new folder), then e.g.
   `python3 ../../higgsfield_exporter/dashboard/serve.py --exports DIR`.

## Studio admin view

An admin or director who signs in at `/dashboard` sees every artist's summary and can click
an email to open that person's dashboard. Summary totals cover all stored generations,
while history is paginated in batches of 50. Models, shots and credits are aggregated from
captured requests; unknown costs are counted explicitly and currency totals remain separate.
The selected shot is the latest shot chosen in Faible, not a guarantee the artist is online.

Create a company account from `server/` (password is requested in the terminal):

```sh
.venv/bin/python -m faible.cli create-account artist@faiblemedia.com
.venv/bin/python -m faible.cli create-account admin@faiblemedia.com --role admin
```

Use the desired real email in place of the examples. Existing accounts are preserved;
the command does not change roles or reset passwords. Public registration always creates
an artist. The extension's ingest endpoint cannot create accounts or promote an admin.

On opening Higgsfield without a saved Faible login, the extension automatically opens its
company-email/password login tab and presents the page gate. Once signed in, select a shot.
Every capture is attributed to that signed-in account, independently of the shared Higgsfield
workspace login. Saved login sessions still last 30 days.

## One shared Higgsfield subscription, many people

The studio signs in to Higgsfield/Magnific with one subscription email. Faible never uses that
email for attribution: every capture is recorded under the **Faible** account signed in to the
extension on that browser. For per-person tracking, three rules:

1. **One Faible account per person**, created with `create-account` above. Nobody shares one.
2. **One Chrome profile per person.** The extension remembers its login per Chrome profile for
   30 days, so on a shared machine each person uses their own profile (or clicks
   "Not you? Switch account" in the Faible panel before working).
3. **Disable the demo accounts** on a real install, otherwise someone can sign in as
   `artist1@studio` / `changeme` from the docs and all their work lands there:

```sh
.venv/bin/python -m faible.cli disable-demo-accounts         # director/artist1/artist2@studio
.venv/bin/python -m faible.cli disable-account EMAIL         # someone leaving the studio
```

Disabling refuses new logins and every already-issued token on the next request, so an
extension still signed in as that account stops recording immediately and shows its login
page. The panel always shows "Recording as EMAIL"; the login page shows who is currently
signed in before replacing them. Already-recorded captures keep their original actor; the
event log is append-only.
