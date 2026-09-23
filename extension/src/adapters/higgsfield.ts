// Higgsfield capture adapter. Field locations: docs/adapters/higgsfield.md.
// Pure functions of captured records; tested against fixtures/higgsfield/*.json.

import type { CaptureRecord } from "../shared/capture";
import {
  bodyFileSha,
  isObj,
  num,
  str,
  type ActivityKind,
  type ActivityStatus,
  type Adapter,
  type Capture,
  type Completed,
  type InputRef,
  type Requested,
  type Status,
} from "./types";

const API = "https://fnf-api-gw.higgsfield.ai";
const SUBMIT = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf\/jobs\/v2\/([^/?]+)/;
const TOPAZ_VIDEO = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf\/jobs\/topaz-video(\?|$)/;
const STATUS_BATCH = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf\/jobs\/status-batch/;
const JOB_DETAIL = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf\/jobs\/([0-9a-f-]{36})(\?|$)/;
const CANCEL = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf\/jobs\/([0-9a-f-]{36})\/cancel/;
const HISTORY = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf\/jobs\/accessible/;
const STREAM = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf-notification\/notifications\/stream/;
const MEDIA_BATCH = /^https:\/\/fnf-api-gw\.higgsfield\.ai\/fnf\/media\/batch/;
const S3_UPLOAD = /^https:\/\/fast-and-furious-input[^/]*\.s3\.amazonaws\.com\//;
// Image upscale (topaz_image) runs on the layer editor service, not the main API.
const LAYER_GENERATIONS = /^https:\/\/layer-worker\.higgsfield\.ai\/api\/layer-projects\/[^/]+\/generations/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// hf_<YYYYMMDD>_<HHMMSS>_<job_id>.<ext>
const FILENAME = /^hf_\d{8}_\d{6}_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.[a-z0-9]+$/i;
const CURRENCY = "higgsfield"; // units UNVERIFIED (docs/adapters/higgsfield.md)
const SERVICE = "higgsfield";

const ok = (status: number | undefined) => status !== undefined && status >= 200 && status < 300;

export function mapStatus(s: unknown): ActivityStatus | null {
  switch (s) {
    case "queued":
    case "pending":
      return "queued";
    case "in_progress":
    case "processing":
      return "running";
    case "completed":
      return "succeeded";
    case "failed":
    case "nsfw":
    case "error":
      return "failed";
    case "canceled":
    case "cancelled":
      return "cancelled";
    default:
      return null;
  }
}

const kindFor = (jobSetType: string): ActivityKind => (/upscale|topaz/i.test(jobSetType) ? "upscale" : "generate");

/** Upload/object URLs are compared without their (redacted) presigned query. */
export const urlKey = (u: string) => u.split("?")[0] ?? u;

function inputsFromMedias(params: unknown): InputRef[] {
  if (!isObj(params)) return [];
  const out: InputRef[] = [];
  if (Array.isArray(params.medias)) {
    for (const m of params.medias) {
      if (!isObj(m) || !isObj(m.data)) continue;
      const id = str(m.data.id);
      if (id) out.push({ role: str(m.role) ?? "reference", service_asset_id: id, url: str(m.data.url) ?? undefined });
    }
  }
  if (isObj(params.input_video) && str(params.input_video.id)) {
    out.push({ role: "source", service_asset_id: str(params.input_video.id)!, url: str(params.input_video.url) ?? undefined });
  }
  return out;
}

function withoutKeys(o: unknown, drop: string[]): Record<string, unknown> {
  if (!isObj(o)) return {};
  return Object.fromEntries(Object.entries(o).filter(([k]) => !drop.includes(k)));
}

function parseSubmit(rec: CaptureRecord, jobSetType: string): Capture[] | null {
  const reqParams = isObj(rec.request_body) ? rec.request_body.params : undefined;
  const base = {
    kind: "requested" as const,
    service: SERVICE,
    activity_kind: kindFor(jobSetType),
    model_id: jobSetType,
    negative_prompt: null,
    seed: null,
    credits_currency: CURRENCY,
  };
  if (!ok(rec.status)) {
    // Rejected before a job existed (insufficient credits, validation…): still recorded.
    return [
      {
        ...base,
        job_id: null,
        status: "failed",
        prompt: isObj(reqParams) ? str(reqParams.prompt) : null,
        parameters: withoutKeys(reqParams, ["prompt", "medias"]),
        inputs: inputsFromMedias(reqParams),
        credits_cost: null,
        service_account: null,
        evidence: "observed",
      },
    ];
  }
  const body = rec.response_body;
  if (!isObj(body) || !Array.isArray(body.job_sets)) return null;
  const out: Requested[] = [];
  for (const set of body.job_sets) {
    if (!isObj(set) || !Array.isArray(set.jobs)) return null;
    const params = isObj(set.params) ? set.params : reqParams;
    for (const job of set.jobs) {
      if (!isObj(job) || !str(job.id)) return null;
      out.push({
        ...base,
        job_id: str(job.id),
        status: mapStatus(job.status) ?? "queued",
        prompt: isObj(params) ? str(params.prompt) : null,
        parameters: withoutKeys(params, ["prompt", "medias"]),
        inputs: inputsFromMedias(params),
        credits_cost: num(set.cost),
        service_account: { workspace_id: str(body.id) ?? undefined, service_user_id: str(job.user_id) ?? undefined },
        evidence: "service_reported",
      });
    }
  }
  return out;
}

/** A job object from detail/history: {id, status, results: {raw: {url, type}}}. */
function fromJob(job: unknown, source: Completed["source"]): Capture[] {
  if (!isObj(job) || !str(job.id) || !UUID.test(job.id as string)) return [];
  const id = job.id as string;
  const status = mapStatus(job.status);
  if (!status) return [];
  const raw = isObj(job.results) && isObj(job.results.raw) ? job.results.raw : null;
  if (status === "succeeded" && raw && str(raw.url)) {
    return [
      {
        kind: "completed",
        service: SERVICE,
        job_id: id,
        outputs: [{ index: 0, url: raw.url as string, service_asset_id: id, media_type: str(raw.type) }],
        source,
      },
    ];
  }
  if (status === "succeeded") return []; // completed but results not in this payload yet
  return [{ kind: "status", service: SERVICE, job_id: id, status }];
}

/** Parse a text/event-stream chunk into (event, data) pairs. A chunk may hold several events. */
export function parseSse(text: string): { event: string; data: unknown }[] {
  const out: { event: string; data: unknown }[] = [];
  for (const block of text.split(/\n\n+/)) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trim());
    }
    if (data.length === 0) continue;
    const joined = data.join("\n");
    try {
      out.push({ event, data: JSON.parse(joined) });
    } catch {
      out.push({ event, data: joined });
    }
  }
  return out;
}

function parseStream(body: unknown): Capture[] {
  if (typeof body !== "string") return [];
  const out: Capture[] = [];
  const seen = new Set<string>();
  for (const { event, data } of parseSse(body)) {
    if (event !== "folder:job_completed" || !isObj(data) || !str(data.job_id)) continue;
    const id = data.job_id as string;
    if (seen.has(id)) continue; // events are sometimes repeated within a chunk
    seen.add(id);
    const cost = num(data.job_cost) ?? undefined;
    if (data.status === "completed" && str(data.result_url)) {
      out.push({
        kind: "completed",
        service: SERVICE,
        job_id: id,
        outputs: [{ index: 0, url: data.result_url as string, service_asset_id: id, media_type: str(data.job_type) }],
        credits_cost: cost,
        source: "notification",
      });
    } else {
      const status = mapStatus(data.status);
      if (status) out.push({ kind: "status", service: SERVICE, job_id: id, status, credits_cost: cost });
    }
  }
  return out;
}

/**
 * Layer generation {id, jobId, status, result: {url}}. The submit response has only `id`
 * (jobId is null until it starts); the output file is named after `jobId`, recorded as an
 * alias so a download can be matched by filename (method 3).
 */
function fromLayerGeneration(g: unknown): Capture[] {
  if (!isObj(g) || !str(g.id)) return [];
  const id = g.id as string;
  const status = mapStatus(g.status);
  if (!status) return [];
  if (status === "succeeded" && isObj(g.result) && str(g.result.url)) {
    const jobId = str(g.jobId);
    return [
      {
        kind: "completed",
        service: SERVICE,
        job_id: id,
        outputs: [{ index: 0, url: g.result.url as string, service_asset_id: id, media_type: "image" }],
        alias_job_ids: jobId && UUID.test(jobId) ? [jobId] : [],
        source: "job_detail",
      },
    ];
  }
  if (status === "succeeded") return [];
  return [{ kind: "status", service: SERVICE, job_id: id, status }];
}

function parseLayer(rec: CaptureRecord): Capture[] | null {
  const body = rec.response_body;
  if (rec.method === "POST") {
    const req = isObj(rec.request_body) ? rec.request_body : {};
    const inputs: InputRef[] = Array.isArray(req.input_images)
      ? req.input_images.flatMap((i) => (isObj(i) && str(i.id) ? [{ role: "source", service_asset_id: i.id as string }] : []))
      : [];
    const requested: Requested = {
      kind: "requested",
      service: SERVICE,
      job_id: null,
      activity_kind: "upscale",
      status: "queued",
      model_id: str(req.type) ?? "topaz_image",
      prompt: str(req.prompt),
      negative_prompt: null,
      seed: null,
      parameters: isObj(req.params) ? req.params : {},
      inputs,
      credits_cost: null, // not in the response (UNVERIFIED where it appears)
      credits_currency: CURRENCY,
      service_account: null,
      evidence: "observed",
    };
    if (!ok(rec.status)) return [{ ...requested, status: "failed" }];
    if (!isObj(body) || !str(body.id)) return null;
    return [{ ...requested, job_id: body.id as string, evidence: "service_reported" }];
  }
  if (rec.method === "GET") {
    const items = isObj(body) && Array.isArray(body.items) ? body.items : [body];
    return items.flatMap(fromLayerGeneration);
  }
  return [];
}

function parse(rec: CaptureRecord): Capture[] | null {
  const url = rec.url ?? "";
  const body = rec.response_body;

  if (LAYER_GENERATIONS.test(url)) return parseLayer(rec);

  if (S3_UPLOAD.test(url) && rec.method === "PUT") {
    const sha = bodyFileSha(rec.request_body);
    return sha && ok(rec.status) ? [{ kind: "upload_bytes", service: SERVICE, upload_url: urlKey(url), sha256: sha }] : [];
  }

  if (!url.startsWith(API)) return [];

  const submit = SUBMIT.exec(url);
  if (submit && rec.method === "POST") return parseSubmit(rec, decodeURIComponent(submit[1]!));

  if (TOPAZ_VIDEO.test(url) && rec.method === "POST") {
    if (url.includes("dry_run=true")) return []; // price quote, not a job
    return parseSubmit(rec, "topaz-video");
  }

  if (STATUS_BATCH.test(url)) {
    if (!isObj(body) || !Array.isArray(body.items)) return ok(rec.status) ? null : [];
    return body.items.flatMap((it): Status[] => {
      const status = isObj(it) ? mapStatus(it.status) : null;
      return status && isObj(it) && str(it.id) ? [{ kind: "status", service: SERVICE, job_id: it.id as string, status }] : [];
    });
  }

  const cancel = CANCEL.exec(url);
  if (cancel && rec.method === "PUT") {
    return isObj(body) && body.success === true
      ? [{ kind: "status", service: SERVICE, job_id: cancel[1]!, status: "cancelled" }]
      : [];
  }

  if (rec.method === "GET" && JOB_DETAIL.test(url)) return ok(rec.status) ? fromJob(body, "job_detail") : [];

  if (rec.method === "GET" && HISTORY.test(url)) {
    return isObj(body) && Array.isArray(body.jobs) ? body.jobs.flatMap((j) => fromJob(j, "history")) : [];
  }

  if (STREAM.test(url)) return parseStream(body);

  if (MEDIA_BATCH.test(url) && rec.method === "POST") {
    if (!ok(rec.status)) return [];
    if (!Array.isArray(body)) return null;
    return body.flatMap((m): Capture[] =>
      isObj(m) && str(m.id) && str(m.upload_url)
        ? [
            {
              kind: "upload_slot",
              service: SERVICE,
              upload_url: urlKey(m.upload_url as string),
              service_asset_id: m.id as string,
              public_url: str(m.url),
            },
          ]
        : [],
    );
  }

  return [];
}

export const higgsfield: Adapter = {
  service: SERVICE,
  patterns: [SUBMIT, STATUS_BATCH, JOB_DETAIL, MEDIA_BATCH, LAYER_GENERATIONS],
  parse,
  jobIdFromFilename(filename: string): string | null {
    return FILENAME.exec(filename)?.[1]?.toLowerCase() ?? null;
  },
};
