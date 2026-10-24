# Higgsfield adapter — discovered payload shapes

Source: capture-all session 2026-09-23 (`data/discovery/higgsfield-2026-09-23.jsonl`, ~1100 records).
Anything not seen in that traffic is marked `UNVERIFIED`.

## Hosts

| Host | Role |
|---|---|
| `higgsfield.ai` | Web app (TanStack-style `/_serverFn/*`, `/_i18n/*`); page origin for blob downloads |
| `fnf-api-gw.higgsfield.ai` | **Main API.** Everything under `/fnf/...` |
| `fnf-api-gw.higgsfield.ai/fnf-notification/...` | Notification stream (SSE over `fetch`) |
| `cms.higgsfield.ai` | CMS content (camera movements, creative controls, tips) — not provenance-relevant |
| `pricing.higgsfield.ai` | `/v2/pricing-config` — price catalogue |
| `layer-worker.higgsfield.ai` | Layer editor service: **image upscale** (`topaz_image`) generations |
| `fast-and-furious-input-prod-…s3.amazonaws.com` | Presigned S3 `PUT` target for uploads (XHR), until ~Oct 2026 |
| `upload.higgsfield.ai` | Presigned `PUT` target for uploads since ~2026-10-19 (same path shape, `media/batch` `upload_url`) |
| `d8j0ntlcm91z4.cloudfront.net` | **Generated outputs** (`/user_<service_user_id>/hf_<YYYYMMDD>_<HHMMSS>_<job_id>.<ext>`) |
| `d2ol7oe51mr4n9.cloudfront.net` | **Uploaded inputs** (references, `media_input`) |
| `cdn.higgsfield.ai` | Thumbnails (`thumbnail_url`) |
| `dd.higgsfield.ai`, `higgsfield.ai/api/i/*` | Datadog / PostHog proxies — noise, filtered |

All API traffic is `fetch` + JSON; uploads are XHR `PUT` to S3. No WebSocket seen. Auth is a bearer token (redacted).

## Endpoints

| Purpose | Method + path | Seen |
|---|---|---|
| **Submit generation** | `POST /fnf/jobs/v2/{job_set_type}` | image (`nano_banana_flash`), video (`cinematic_studio_video_4_0`) |
| **Poll status** | `POST /fnf/jobs/status-batch` body `{"ids": [job_id…]}` | every few seconds while jobs are open |
| **Job detail** | `GET /fnf/jobs/{job_id}` | fetched by the UI after `completed` |
| **Job history** | `GET /fnf/jobs/accessible?job_set_type=…&size=50` | on page load; >256 KB |
| **Cancel** | `PUT /fnf/jobs/{job_id}/cancel` → `{"success": true}` | yes |
| Download telemetry | `POST /fnf/jobs/{job_id}/track` (empty body/response) | just before every download |
| Wallet | `GET /fnf/workspaces/wallet` | yes |
| Price table | `GET /fnf/job-sets/costs` | yes |
| **Notifications** | `GET /fnf-notification/notifications/stream` (SSE over fetch) | `ping`, `folder:job_create`, `folder:job_completed`, `folder:job_remove` |
| Upload (1/3) | `POST /fnf/media/batch` `{mimetypes, source}` → `[{id, upload_url (presigned S3), url (uploads CDN)}]` | yes |
| Upload (2/3) | XHR `PUT <presigned S3 url>` with the file. **The page re-encodes images before uploading** (a 5,151,850-byte PNG became 5,069,466 bytes, same 2752×1536, pixels not identical, pHash distance 0), so an upload's sha256 never equals the file the artist picked. Linking needs pHash (Phase 3, `inferred`) | yes |
| Upload (3/3) | `POST /fnf/media/{id}/upload` `{filename}` → `{id, status: "uploaded"}` | yes |
| **Image upscale** | `POST layer-worker.higgsfield.ai/api/layer-projects/{project}/generations` `{type: "topaz_image", input_images: [{id, type: "media_input"}], params: {model, denoise, sharpen, output_width, output_height, …}}` → 202 `{id, status: "queued", jobId: null}` | yes |
| Image upscale status | `GET layer-worker…/generations/{id}` and `…/generations?limit=N` → `{id, jobId, status: in_progress \| completed, completedAt, result: {url}}`. `result.url` is on the outputs CDN named `hf_…_<jobId>.png` | yes (completed item seen) |
| Video upscale | `POST /fnf/jobs/topaz-video?dry_run=true` → `{cost: 8}` (price quote only). Real submit presumably the same path without `dry_run` | quote only — submit UNVERIFIED |
| **Delete** | `DELETE /fnf/assets` `{"asset_ids": [job_id…]}` → empty 200 | yes |
| View tracking | `POST /fnf/jobs/{id}/view`, `/viewed` | noise |

## Field map

| Faible field | Where | Evidence |
|---|---|---|
| `service_job_id` | submit response `job_sets[].jobs[].id` (= `trace_id`) | service_reported |
| job set id | submit response `job_sets[].id`; detail `job_set_id` | service_reported |
| `kind` / `model_id` | path segment `{job_set_type}` = `job_set_type` in responses (e.g. `nano_banana_flash`, `cinematic_studio_video_4_0`); request `params.model` is `"default"` for video | service_reported |
| `prompt` | request `params.prompt`; echoed in submit response `job_sets[].params.prompt`, detail `params.prompt`, history `params.prompt` | service_reported |
| `negative_prompt` | UNVERIFIED (not present in either model's params) | |
| `seed` | UNVERIFIED (not present) | |
| `parameters` | `params`: `aspect_ratio`, `width`, `height`, `resolution`, `batch_size`, `duration`, `generate_audio`, `enhance_prompt`, `bitrate_mode`, `prompt_language`, `is_inpaint`, `reference_elements` | service_reported |
| inputs | `params.medias[]` = `{role: "image", data: {id, type: "media_input", url, ipStatus}}`; URL on the uploads CDN | service_reported |
| status | `status`: `queued` → `in_progress` → `completed` \| `failed`; cancel via PUT. Notification `folder:job_completed.status` carries `completed` or `failed` | service_reported |
| outputs | detail/history `results.raw.url` (full) and `results.min.url` (preview); video also `thumbnail_url`, `h264`, `hls` (null so far) | service_reported |
| `credits_cost` | submit response `job_sets[].cost` (**150** image, **1200** video); notification `job_cost` / `price_credits` (**8400** for a 1080p 7 s video, **0** for a failed one) | service_reported |
| credit units | UNVERIFIED — values look like hundredths of a UI credit (wallet `on_demand_credits` 73088). Confirm against the UI | |
| failed job refund | notification `job_cost: 0` on `status: failed` → failed jobs are not charged | service_reported |
| output (from stream) | `folder:job_completed.result_url`, plus `width`, `height`, `duration_sec`, `model` | service_reported |
| provider cost (USD) | detail `meta.cost` (JSON string: input/output/thinking/total) | service_reported — informational |
| service user | `user_id` (`user_36m…`), detail `user.{id, username, full_name}` | service_reported |
| workspace | `/fnf/workspaces/wallet` `workspace_id`; submit response top-level `id` | service_reported |
| credits balance | wallet `on_demand_credits`, `credits_balance`, `subscription_balance`, `total_credits` | service_reported |
| cancel refunds credits? | UNVERIFIED | |
| reference inputs | `params.medias[].data.id` = uploaded media id from `/fnf/media/batch`; `url` on the uploads CDN | service_reported |

## Downloads

- The UI fetches the CDN file, then saves it from a **`blob:https://higgsfield.ai/<uuid>`** URL.
  `chrome.downloads` sees the blob URL, which the worker cannot re-fetch.
- The filename keeps the service naming: `hf_<date>_<time>_<job_id>.<ext>`, so the job id is
  recoverable from the download.
- The CDN output URLs are **unsigned, public, `cache-control: public, max-age=31536000, immutable`**.
  `HEAD` without cookies → 200, `content-length` equal to the downloaded `file_size`.
- **Fingerprinting (implemented, DECISIONS #11):** completion hash of own jobs' `results.raw.url`
  / `result_url` (method 1), in-page hash of the Blob behind the blob URL (method 2), and
  filename → job id → output hash (method 3). Download records carry `extra.fingerprint`.
- **Verified 2026-09-23:** a video download's in-page blob hash equalled the CDN output hash
  found via the filename's job id (`6e7fe35f…`, `agree: true`).

## Flows captured

- [x] image generation (`nano_banana_flash`, completed)
- [x] video generation (`cinematic_studio_video_4_0`, completed)
- [x] image upscale (layer-worker `topaz_image`: submit, in-progress and completed shapes)
- [~] video upscale — price quote (`dry_run`) only; real submit UNVERIFIED
- [x] cancelled job (two)
- [x] failed job (`78945fb1`, via notification, `job_cost: 0`)
- [x] download (video and image)
- [x] upload (reference image)
- [x] delete

## History listings and the login (integrated exporter, DECISIONS #59–68)

- Any Higgsfield JSON response holding generation-like objects (`id` + `params`/`model` +
  `results`/`status`, the exporter's rule — chiefly `GET /fnf/jobs/accessible`) is also read
  by `extension/src/content/history.ts`. Each object is sent as
  `service_generation.observed` with the raw object; the live adapter above still decides
  `generation.*` events, only for jobs this browser submitted (#33). The two meet on the job
  id (`id` = `trace_id` = `service_job_id`).
- History objects' `cost` is the same unit as the submit response's `job_sets[].cost`
  (e.g. 10800 for a video). The exporter treated it as hundredths of a UI credit; Faible
  stores it as reported and labels ÷100 as an estimate. Unit still UNVERIFIED.
- The page's login (shared subscription) is read from `window.Clerk.user` (id, primary email,
  full name, username) and, as a fallback, from account-like responses. It is metadata on
  history and usage events, never attribution.
- "Save usage page": the Usage / Organization pages' own JSON responses (wallet, transactions;
  shapes UNVERIFIED, stored as received) plus the page's tables and text (`observed_dom`).
