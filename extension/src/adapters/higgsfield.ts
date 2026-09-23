// Higgsfield fingerprinting adapter. Field locations: docs/adapters/higgsfield.md.
// Pure functions over captured records; tested against fixtures/higgsfield/*.json.

import type { CaptureRecord } from "../shared/capture";
import { EMPTY, type FingerprintAdapter, type JobOutput, type Observation } from "./types";

const API = /^https:\/\/fnf-api-gw\.higgsfield\.ai\//;
// Image upscale (topaz_image) runs on the layer editor service, not the main API.
const LAYER_GENERATIONS = /^https:\/\/layer-worker\.higgsfield\.ai\/api\/layer-projects\/[^/]+\/generations/;
const SUBMIT = /\/fnf\/jobs\/v2\/[^/?]+(\?|$)/;
const JOB_DETAIL = /\/fnf\/jobs\/[0-9a-f-]{36}(\?|$)/;
const HISTORY = /\/fnf\/jobs\/accessible/;
const STREAM = /\/fnf-notification\/notifications\/stream/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// hf_<YYYYMMDD>_<HHMMSS>_<job_id>.<ext>
const FILENAME = /^hf_\d{8}_\d{6}_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.[a-z0-9]+$/i;

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => v !== null && typeof v === "object" && !Array.isArray(v);

/** A job object from detail/history: {id, status, results: {raw: {url, type}}}. */
function outputFromJob(job: unknown, source: JobOutput["source"]): JobOutput | null {
  if (!isObj(job) || typeof job.id !== "string" || !UUID.test(job.id)) return null;
  if (job.status !== "completed") return null;
  const raw = isObj(job.results) && isObj(job.results.raw) ? job.results.raw : null;
  if (!raw || typeof raw.url !== "string") return null;
  return {
    service: "higgsfield",
    job_id: job.id,
    url: raw.url,
    media_type: typeof raw.type === "string" ? raw.type : null,
    status: "completed",
    source,
  };
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

/**
 * Layer generation {id, jobId, status, result: {url}}. The submit response has only `id`
 * (jobId is null until it starts); the output file is named after `jobId`. So a completed
 * generation yields the output under both ids: `id` for method 1 (matches the submit),
 * `jobId` for method 3 (matches the download filename). Same URL, hashed once.
 */
function outputsFromLayerGeneration(g: unknown): JobOutput[] {
  if (!isObj(g) || g.status !== "completed" || !isObj(g.result) || typeof g.result.url !== "string") return [];
  const url = g.result.url;
  const ids = [g.id, g.jobId].filter((v): v is string => typeof v === "string" && UUID.test(v));
  return ids.map((job_id) => ({ service: "higgsfield", job_id, url, media_type: "image", status: "completed", source: "job_detail" }));
}

function observeLayer(rec: CaptureRecord): Observation {
  const body = rec.response_body;
  if (rec.method === "POST" && rec.status === 202 && isObj(body) && typeof body.id === "string") {
    return { submittedJobIds: [body.id], outputs: [] };
  }
  if (rec.method === "GET") {
    const items = isObj(body) && Array.isArray(body.items) ? body.items : [body];
    return { submittedJobIds: [], outputs: items.flatMap(outputsFromLayerGeneration) };
  }
  return EMPTY;
}

function observe(rec: CaptureRecord): Observation {
  const url = rec.url ?? "";
  if (LAYER_GENERATIONS.test(url)) return observeLayer(rec);
  if (!API.test(url)) return EMPTY;
  const body = rec.response_body;

  if (rec.method === "POST" && SUBMIT.test(url) && isObj(body) && Array.isArray(body.job_sets)) {
    const ids: string[] = [];
    for (const set of body.job_sets) {
      if (!isObj(set) || !Array.isArray(set.jobs)) continue;
      for (const job of set.jobs) if (isObj(job) && typeof job.id === "string") ids.push(job.id);
    }
    return { submittedJobIds: ids, outputs: [] };
  }

  if (rec.method === "GET" && JOB_DETAIL.test(url)) {
    const o = outputFromJob(body, "job_detail");
    return { submittedJobIds: [], outputs: o ? [o] : [] };
  }

  if (rec.method === "GET" && HISTORY.test(url) && isObj(body) && Array.isArray(body.jobs)) {
    const outputs = body.jobs.map((j) => outputFromJob(j, "history")).filter((o): o is JobOutput => o !== null);
    return { submittedJobIds: [], outputs };
  }

  if (STREAM.test(url) && typeof body === "string") {
    const outputs: JobOutput[] = [];
    for (const { event, data } of parseSse(body)) {
      if (event !== "folder:job_completed" || !isObj(data)) continue;
      if (data.status !== "completed" || typeof data.job_id !== "string" || typeof data.result_url !== "string") continue;
      if (outputs.some((o) => o.job_id === data.job_id)) continue; // events are sometimes repeated
      outputs.push({
        service: "higgsfield",
        job_id: data.job_id,
        url: data.result_url,
        media_type: typeof data.job_type === "string" ? data.job_type : null,
        status: "completed",
        source: "notification",
      });
    }
    return { submittedJobIds: [], outputs };
  }

  return EMPTY;
}

export const higgsfield: FingerprintAdapter = {
  service: "higgsfield",
  observe,
  jobIdFromFilename(filename: string): string | null {
    return FILENAME.exec(filename)?.[1]?.toLowerCase() ?? null;
  },
};
