# Magnific adapter — discovered payload shapes

Source: capture-all session 2026-09-23 (`data/discovery/magnific-2026-09-23.jsonl`, ~460 records).
Anything not seen in that traffic is marked `UNVERIFIED`. Fixtures: `extension/src/adapters/fixtures/magnific/`.

## Hosts

| Host | Role |
|---|---|
| `www.magnific.com` | Web app (**`.com`** is live; `.ai` not seen). REST under `/app/api/...`; all calls carry `?lang=en_US&user_id=<n>` |
| `*.freepik.com` | Same product and account (Magnific is Freepik's). Matched and attributed to `magnific`; **no traffic captured from it yet** — its endpoints are UNVERIFIED |
| `ak-data.magnific.com` | **Render API** (`/app/api/render/v4`) and **Pusher WebSocket** (`wss://…/app/<app-key>?protocol=7`) |
| `pikaso.cdnpk.net` | **Outputs** `/private/production/<n>/render.<ext>?token=exp=<unix>~hmac=<sig>` — **signed**, expiring |
| `fpkph.cdnpk.net` | PostHog proxy (`/i/v0/e/`, `/flags/`) — noise, filtered |
| `sdk.fra-02.braze.eu`, `www.google.com/ccm`, `analytics.google.com` | Marketing telemetry — noise, filtered |

Magnific runs on Freepik infrastructure (`cdnpk.net`, "pikaso" naming). Job progress is pushed
over a **Pusher WebSocket**, not polled. Pusher frames double-encode: `{"event", "channel", "data": "<JSON string>"}`
(the extension now parses the inner JSON; older captures store it as a string).

## Endpoints

| Purpose | Method + path | Seen |
|---|---|---|
| Cost estimate | `POST /app/api/v2/ai/simulate-generation` `{items: [{model, quantity, config}]}` → `{items: [{costPerImage, subtotal}], total: {credits, remaining}}` | yes (called on every settings change) |
| TTI pre-flight | `POST /app/api/start-tti-v2` `{prompt, mode, references, aspect_ratio, num_images}` → `{family, request_tokens[], available_slots}` | yes |
| **Submit (image)** | `POST ak-data.magnific.com/app/api/render/v4` → `{creation: {...}}` | yes (`seedream-5-pro`, text-to-image with reference) |
| **Progress / completion** | WS `creation.created` → `creation.updated` (`status: processing` → `completed`) on channel `private-project.<project_uuid>` | yes |
| Delete event | WS `creation.deleted` | yes |
| **Download** | `POST /app/api/creations/download` `{creations: [<numeric id>], extension}` (empty 200), then the browser downloads the signed CDN URL with `&download=<filename>` | yes |
| **Delete** | `PUT /app/api/projects/bulk/trash/soft-delete` `{files_external_ids: ["<numeric id>"]}` | yes |
| Pusher auth | `POST /app/broadcasting/auth` | yes (response redacted) |
| **Upscale** | `POST ak-data.magnific.com/app/api/render/v4/upscale` `{input_image: "creation:<identifier>", mode: "enhance-magnific-creative", scale, creativity, resemblance, hdr, fractality, engine, optimised, prompt, metadata: {previous: <numeric id>}}` → `{creation: {...}}` (tool `enhance`) | yes (1080 credits) |
| Rejected submit | same endpoint → **422** `{errors: {prompt: ["…must not be greater than 2000 characters."]}}`. No creation, no credits | yes |
| Video cost estimate | `POST /app/api/video/simulate/generate` → `clips[].creditsCost`, `totalCreditsCost` | yes |
| Video settings helper | `POST /app/api/video/generate/auto` | yes (not a submit) |
| **Submit (video)** | `POST /app/api/video/generate?return_creations=true` `{video: {clips: [{model, mode, prompt, negativePrompt, duration, resolution, aspectRatio, references: [{name, type, url: "creation:<identifier>"}], withSoundEffects}]}}` → `{data: {creations: [...]}}` (tool `video-generator`) | yes (Kling Omni 3, 575 credits) |
| Video completion | WS `creation.updated` `queued` → `processing` seen; **`completed` shape UNVERIFIED** (log ended while processing) | partial |
| Failed job (after start) | UNVERIFIED — not seen. `rollbackCredits` in video metadata suggests credits are returned on failure | |
| Cancel | no cancel endpoint seen; deleting a processing creation (`soft-delete`) was observed | |

## Field map

| Faible field | Where | Evidence |
|---|---|---|
| `service_job_id` | `creation.identifier` (e.g. `iGX0Hoj3uK`) — also in `webUrl`, references and filenames. Numeric `creation.id` is the file id used by download/delete | service_reported |
| grouping | `creation.family` (uuid, shared by a submit's variations); `start-tti-v2` returns it first | service_reported |
| `kind` | `creation.tool` / request `tool` (`text-to-image`) | service_reported |
| `model_id` | request `mode` / `metadata.mode` (`seedream-5-pro`); display `metadata.modelName`; backend `metadata.service` (`seedream-5-0-pro-edit`) | service_reported |
| `prompt` | request `prompt`; `metadata.prompt`, `metadata.inputPrompt` | service_reported |
| `negative_prompt` | request `negative_prompt` (null here) | service_reported |
| `seed` | request `seed`; `metadata.seed` (string) | service_reported |
| `parameters` | request `aspect_ratio`, `resolution`, `width`, `height`, `num_images`, `thinking_level`, `smart_prompt`; completion adds `outputWidth`, `outputHeight`, `elapsedTime` | service_reported |
| inputs | image: `image_references[]` / `metadata.imageReferences[]` = `{image: "creation:<identifier>", label}`; upscale: `input_image: "creation:<identifier>"`; video: `references[].url` = `creation:<identifier>`, resolved in WS metadata to `{creationId, media_id, url: …/upload.png}`. Uploads are creations too | service_reported |
| status | WS `data.status`: `queued` (video) → `processing` → `completed`. Failure value UNVERIFIED | service_reported |
| outputs | WS `creation.url` (signed PNG), `preview`, `large_preview`, `webUrl` | service_reported |
| `credits_cost` | `metadata.creditLedger[]` `{type: "consume", credits, creditsEstimated, transactionId}`; `creditLedgerTotals.credits` (**100** seedream-5-pro 2k image, **600** GPT 2.5 edit, **1080** 2× creative upscale, **575** 5 s Kling video). Units match the UI's credits (simulate shows the same numbers) | service_reported |
| credits balance | `simulate-generation` `total.remaining` | service_reported |
| service user | query `user_id=225502283`; Pusher channel `private-user.173618012` (two different ids — which is which is UNVERIFIED) | service_reported |
| project | `folder_reference` / `projects_project_reference` (uuid) = Pusher project channel | service_reported |

## Downloads and fingerprinting

- Downloads are direct **signed** CDN URLs (not blobs); the filename is `magnific_<prompt-slug>_<identifier>.png`.
- The worker **re-fetched the download URL immediately** and got the same bytes
  (`matches_download_size: true`, sha256 `9ecfe912…`) → method *refetch*, evidence `observed`.
- Method 1 needs the signed `creation.url`. The page-world script passes signed output-CDN URLs
  to the worker in `signed_urls` (redacted → original); the worker detaches them before queueing,
  so they never reach storage-sent records or the server (DECISIONS #14).
- Method 3: filename → `identifier` → output hash.
- Tokens carry `exp=<unix>`; seen expiring ~5 days out. Re-fetching after expiry is UNVERIFIED.

## Flows captured

- [x] image generation (`seedream-5-pro`, `gpt-2-mini`, `imagen-nano-banana-2`; completed via WS)
- [~] video generation (Kling Omni 3 submitted and processing; completion not captured)
- [x] upscale (`enhance-magnific-creative`, submitted; deleted while processing)
- [~] failed / cancelled: a 422 rejected submit and a delete-while-processing; no after-start failure
- note: 12 creations arrived over the project channel with **no submit from this browser**
  (another tab/device or a teammate on the shared workspace). Attribution must come from the
  capturing browser's own submits, never from project-channel events.
- [x] download (re-fetched and hashed)
- [x] delete (soft delete)
