// Magnific fingerprinting adapter. Field locations: docs/adapters/magnific.md.
// Pure functions over captured records; tested against fixtures/magnific/*.json.
//
// Magnific's stable creation handle is `identifier` (e.g. "iGX0Hoj3uK"): it appears in the
// render response, Pusher events, `webUrl`, references ("creation:<identifier>") and
// download filenames. It is used as the service job id.

import type { CaptureRecord } from "../shared/capture";
import { EMPTY, type FingerprintAdapter, type JobOutput, type Observation } from "./types";

// Image generation and upscale (`render/v4`, `render/v4/upscale`) → {creation: {...}}.
const RENDER = /^https:\/\/ak-data\.magnific\.com\/app\/api\/render\/v\d+/;
// Video generation → {data: {creations: [{...}]}}. Not `video/generate/auto` or `video/simulate/…`.
const VIDEO = /^https:\/\/www\.magnific\.com\/app\/api\/video\/generate(\?|$)/;
const PUSHER = /^wss:\/\/ak-data\.magnific\.com\/app\//;
// magnific_<prompt-slug>_<identifier>.<ext>
const FILENAME = /^magnific_.*_([A-Za-z0-9]{8,12})\.[a-z0-9]+$/i;

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => v !== null && typeof v === "object" && !Array.isArray(v);

function mediaTypeFromUrl(url: string): string | null {
  const path = url.split("?")[0] ?? "";
  if (/\.(png|jpe?g|webp|gif|avif)$/i.test(path)) return "image";
  if (/\.(mp4|webm|mov)$/i.test(path)) return "video";
  return null;
}

/** Pusher frames are {event, channel, data}; `data` is a JSON string on older captures. */
function pusherData(body: unknown): { event: string; data: Json } | null {
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

function observe(rec: CaptureRecord): Observation {
  const url = rec.url ?? "";

  if (rec.method === "POST" && RENDER.test(url)) {
    const body = rec.response_body;
    const creation = isObj(body) && isObj(body.creation) ? body.creation : null;
    const id = creation && typeof creation.identifier === "string" ? creation.identifier : null;
    return id ? { submittedJobIds: [id], outputs: [] } : EMPTY;
  }

  if (rec.method === "POST" && VIDEO.test(url)) {
    const body = rec.response_body;
    const creations = isObj(body) && isObj(body.data) && Array.isArray(body.data.creations) ? body.data.creations : [];
    const ids = creations
      .map((c) => (isObj(c) && typeof c.identifier === "string" ? c.identifier : null))
      .filter((id): id is string => id !== null);
    return ids.length ? { submittedJobIds: ids, outputs: [] } : EMPTY;
  }

  if (rec.transport === "ws" && PUSHER.test(url) && rec.extra.direction === "in") {
    const frame = pusherData(rec.response_body);
    if (!frame || frame.event !== "creation.updated" || frame.data.status !== "completed") return EMPTY;
    const c = frame.data.creation;
    if (!isObj(c) || typeof c.identifier !== "string" || typeof c.url !== "string") return EMPTY;
    const out: JobOutput = {
      service: "magnific",
      job_id: c.identifier,
      url: c.url,
      media_type: mediaTypeFromUrl(c.url),
      status: "completed",
      source: "notification",
    };
    return { submittedJobIds: [], outputs: [out] };
  }

  return EMPTY;
}

export const magnific: FingerprintAdapter = {
  service: "magnific",
  observe,
  jobIdFromFilename(filename: string): string | null {
    return FILENAME.exec(filename)?.[1] ?? null;
  },
};
