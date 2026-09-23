// Magnific (Freepik) capture adapter. Field locations: docs/adapters/magnific.md.
// Pure functions of captured records; tested against fixtures/magnific/*.json.
//
// Magnific's stable creation handle is `identifier` (e.g. "iGX0Hoj3uK"): it appears in the
// render response, Pusher events, `webUrl`, references ("creation:<identifier>") and
// download filenames. It is used as the service job id and as the output's service id.

import type { CaptureRecord } from "../shared/capture";
import {
  bodyFileSha,
  isObj,
  normalizeUrl,
  num,
  str,
  type ActivityStatus,
  type Adapter,
  type Capture,
  type InputRef,
  type Requested,
} from "./types";

// Image generation and upscale (`render/v4`, `render/v4/upscale`) → {creation: {...}}.
const RENDER = /^https:\/\/ak-data\.magnific\.com\/app\/api\/render\/v\d+(\/upscale)?(\?|$)/;
// Video generation → {data: {creations: [...]}}. Not `video/generate/auto` or `video/simulate/…`.
const VIDEO = /^https:\/\/www\.magnific\.com\/app\/api\/video\/generate(\?|$)/;
// Reference upload (multipart) → {creation: {identifier, tool: "upload-reference"}}.
const UPLOAD = /^https:\/\/ak-data\.magnific\.com\/app\/api\/creations(\?|$)/;
const PUSHER = /^wss:\/\/ak-data\.magnific\.com\/app\//;
// magnific_<prompt-slug>_<identifier>.<ext>
const FILENAME = /^magnific_.*_([A-Za-z0-9]{8,12})\.[a-z0-9]+$/i;
const SERVICE = "magnific";
const CURRENCY = "magnific";

const ok = (status: number | undefined) => status !== undefined && status >= 200 && status < 300;

function mapStatus(s: unknown): ActivityStatus | null {
  switch (s) {
    case "queued":
      return "queued";
    case "processing":
      return "running";
    case "completed":
      return "succeeded";
    case "failed":
    case "error":
      return "failed";
    default:
      return null;
  }
}

const creationRef = (v: unknown): string | null => {
  const s = str(v);
  return s?.startsWith("creation:") ? s.slice("creation:".length) : null;
};

function mediaTypeFromUrl(url: string): string | null {
  const path = url.split("?")[0] ?? "";
  if (/\.(png|jpe?g|webp|gif|avif)$/i.test(path)) return "image";
  if (/\.(mp4|webm|mov)$/i.test(path)) return "video";
  return null;
}

function userIdFromUrl(url: string): string | undefined {
  try {
    return new URL(url).searchParams.get("user_id") ?? undefined;
  } catch {
    return undefined;
  }
}

function imageInputs(req: Record<string, unknown>): InputRef[] {
  const out: InputRef[] = [];
  const refs = Array.isArray(req.image_references) ? req.image_references : Array.isArray(req.references) ? req.references : [];
  for (const r of refs) {
    const id = isObj(r) ? creationRef(r.image) ?? creationRef(r.url) : null;
    if (id) out.push({ role: "reference", service_asset_id: id });
  }
  const source = creationRef(req.input_image);
  if (source) out.push({ role: "source", service_asset_id: source });
  return out;
}

const DROP = ["prompt", "negative_prompt", "negativePrompt", "seed", "image_references", "references", "input_image", "request_token", "metadata"];
const params = (o: Record<string, unknown>) => Object.fromEntries(Object.entries(o).filter(([k]) => !DROP.includes(k)));

function creditsOf(creation: Record<string, unknown>): string | null {
  const m = isObj(creation.metadata) ? creation.metadata : {};
  return isObj(m.creditLedgerTotals) ? num(m.creditLedgerTotals.credits) : null;
}

function parseRender(rec: CaptureRecord): Capture[] | null {
  const req = isObj(rec.request_body) ? rec.request_body : {};
  const upscale = /\/upscale(\?|$)/.test(rec.url ?? "");
  const base: Requested = {
    kind: "requested",
    service: SERVICE,
    job_id: null,
    activity_kind: upscale ? "upscale" : "generate",
    status: "failed",
    model_id: str(req.mode),
    prompt: str(req.prompt),
    negative_prompt: str(req.negative_prompt),
    seed: num(req.seed),
    parameters: params(req),
    inputs: imageInputs(req),
    credits_cost: null,
    credits_currency: CURRENCY,
    service_account: null,
    evidence: "observed",
  };
  if (!ok(rec.status)) return [base]; // rejected (e.g. 422 prompt too long): no job, no credits
  const body = rec.response_body;
  const c = isObj(body) && isObj(body.creation) ? body.creation : null;
  if (!c || !str(c.identifier)) return null;
  const m = isObj(c.metadata) ? c.metadata : {};
  return [
    {
      ...base,
      job_id: c.identifier as string,
      activity_kind: c.tool === "enhance" || upscale ? "upscale" : "generate",
      status: mapStatus(c.status) ?? "queued",
      model_id: str(m.mode) ?? base.model_id,
      prompt: base.prompt ?? str(m.prompt),
      seed: base.seed ?? num(m.seed),
      credits_cost: creditsOf(c),
      service_account: { workspace_id: str(c.folder_reference) ?? undefined, service_user_id: userIdFromUrl(rec.url ?? "") },
      evidence: "service_reported",
    },
  ];
}

function parseVideo(rec: CaptureRecord): Capture[] | null {
  const req = isObj(rec.request_body) && isObj(rec.request_body.video) ? rec.request_body.video : {};
  const clips = Array.isArray(req.clips) ? req.clips.filter(isObj) : [];
  const fromClip = (clip: Record<string, unknown>): Requested => ({
    kind: "requested",
    service: SERVICE,
    job_id: null,
    activity_kind: "generate",
    status: "failed",
    model_id: str(clip.slug) ?? str(clip.mode),
    prompt: str(clip.prompt),
    negative_prompt: str(clip.negativePrompt),
    seed: null,
    parameters: params(clip),
    inputs: imageInputs(clip),
    credits_cost: null,
    credits_currency: CURRENCY,
    service_account: null,
    evidence: "observed",
  });
  if (!ok(rec.status)) return clips.length ? [fromClip(clips[0]!)] : [];
  const body = rec.response_body;
  const creations = isObj(body) && isObj(body.data) && Array.isArray(body.data.creations) ? body.data.creations : null;
  if (!creations) return null;
  return creations.flatMap((c, i): Requested[] => {
    if (!isObj(c) || !str(c.identifier)) return [];
    const clip = clips[i] ?? clips[0] ?? {};
    return [
      {
        ...fromClip(clip),
        job_id: c.identifier as string,
        status: mapStatus(c.status) ?? "queued",
        credits_cost: creditsOf(c),
        service_account: { workspace_id: str(c.folder_reference) ?? undefined, service_user_id: userIdFromUrl(rec.url ?? "") },
        evidence: "service_reported",
      },
    ];
  });
}

/** Pusher frames are {event, channel, data}; `data` is a JSON string on older captures. */
function pusherData(body: unknown): { event: string; data: Record<string, unknown> } | null {
  if (!isObj(body) || typeof body.event !== "string") return null;
  let data = body.data;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch {
      return null;
    }
  }
  return isObj(data) ? { event: body.event, data } : null;
}

function parsePusher(rec: CaptureRecord): Capture[] {
  if (rec.extra.direction !== "in") return [];
  const frame = pusherData(rec.response_body);
  if (!frame || (frame.event !== "creation.updated" && frame.event !== "creation.created")) return [];
  const c = frame.data.creation;
  if (!isObj(c) || !str(c.identifier)) return [];
  const id = c.identifier as string;
  const status = mapStatus(frame.data.status ?? c.status);
  if (!status) return [];
  if (status === "succeeded") {
    // Upscales expose a preview JPEG as `url` (render-optimized.jpg) and the real file as
    // `raw` (render-raw.png) — the one a download gives you. Prefer `raw` when present.
    const url = str(c.raw) ?? str(c.url);
    if (!url) return [];
    return [
      {
        kind: "completed",
        service: SERVICE,
        job_id: id,
        outputs: [{ index: 0, url, service_asset_id: id, media_type: mediaTypeFromUrl(url) }],
        source: "notification",
      },
    ];
  }
  return [{ kind: "status", service: SERVICE, job_id: id, status }];
}

function parseUpload(rec: CaptureRecord): Capture[] | null {
  if (rec.method !== "POST" || !ok(rec.status)) return [];
  const body = rec.response_body;
  // Unlike render, the upload endpoint returns the creation unwrapped.
  const c = isObj(body) && isObj(body.creation) ? body.creation : isObj(body) ? body : null;
  if (!c || !str(c.identifier)) return null;
  // Any creation made from an uploaded file: reference uploads ("upload-reference") and the
  // upscaler's own upload, whose tool name hasn't been captured yet (UNVERIFIED).
  const sha = bodyFileSha(rec.request_body);
  if (!sha) return []; // page could not hash the file (too large, or not a File)
  return [{ kind: "asset_location", service: SERVICE, sha256: sha, service_asset_id: c.identifier as string, url: str(c.url) ?? "" }];
}

function parse(rec: CaptureRecord): Capture[] | null {
  const url = normalizeUrl(rec.url);
  if (rec.transport === "ws" && PUSHER.test(url)) return parsePusher(rec);
  if (rec.method === "POST" && RENDER.test(url)) return parseRender(rec);
  if (rec.method === "POST" && VIDEO.test(url)) return parseVideo(rec);
  if (UPLOAD.test(url)) return parseUpload(rec);
  return [];
}

export const magnific: Adapter = {
  service: SERVICE,
  patterns: [RENDER, VIDEO, UPLOAD],
  parse,
  jobIdFromFilename(filename: string): string | null {
    return FILENAME.exec(filename)?.[1] ?? null;
  },
};
