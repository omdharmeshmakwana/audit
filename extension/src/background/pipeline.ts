// Captures → Faible events. Pure: no chrome.*, no network, no clock except what's passed in.
// The worker persists `PipelineState` between calls.
//
// Attribution rule (hard rule 8 + shared workspace): status and completion events are only
// emitted for jobs *this browser submitted*. The same workspace's history, notifications and
// project channels also show other artists' jobs; their own extensions record those.

import type { Capture, Completed, OutputRef, Requested } from "../adapters/types";
import type { UsageCapture } from "../history/db";
import { signature } from "../history/extract";
import type { CaptureRecord } from "../shared/capture";

export interface FaibleEvent {
  owner_user_id?: string; // Local queue metadata, stripped before API ingest.
  event_id: string;
  type: string;
  occurred_at: string;
  evidence: string;
  payload: Record<string, unknown>;
}

export interface StoredOutput extends OutputRef {
  owner_user_id?: string;
  service: string;
  job_id: string;
  /** Fetchable URL: the original signed URL when `url` is redacted. Never leaves the extension. */
  fetch_url: string;
  at: string;
}

export interface PipelineState {
  submitted: Record<string, { activity_id: string; at: string }>; // "service:job" →
  emitted: Record<string, string>; // dedupe key → at
  outputs: Record<string, StoredOutput>; // "service:job" (and aliases) →
  uploadSlots: Record<string, { service: string; service_asset_id: string; public_url: string | null; at: string }>;
  unknownSeen: Record<string, string>; // "service:path" → at
  /** "service:job" → signature of the history object last sent (DECISIONS #59). */
  history: Record<string, { sig: string; at: string }>;
}

export const emptyState = (): PipelineState => ({ submitted: {}, emitted: {}, outputs: {}, uploadSlots: {}, unknownSeen: {}, history: {} });

export interface HashJob {
  /** completion: an own job's output. upload: the stored copy of a file the artist uploaded
   *  (same bytes as the page hashed), fetched so the server can keep and fingerprint it. */
  method: "completion" | "upload";
  output: StoredOutput;
}

export interface PlanContext {
  activeShotId: string | null;
  signedUrls: Record<string, string>;
  newId: () => string;
}

export interface Plan {
  events: FaibleEvent[];
  hashJobs: HashJob[];
}

const key = (service: string, job: string) => `${service}:${job}`;
const UNKNOWN_REPEAT_MS = 6 * 60 * 60 * 1000;

function event(ctx: PlanContext, record: CaptureRecord, type: string, evidence: string, payload: Record<string, unknown>): FaibleEvent {
  return { event_id: ctx.newId(), type, occurred_at: record.captured_at, evidence, payload };
}

function requested(c: Requested, record: CaptureRecord, state: PipelineState, ctx: PlanContext, plan: Plan): void {
  if (c.job_id && state.submitted[key(c.service, c.job_id)]) return; // same submit seen twice
  const activity_id = ctx.newId();
  if (c.job_id) state.submitted[key(c.service, c.job_id)] = { activity_id, at: record.captured_at };
  // An input picked from the service's library (or reused) is never re-uploaded, so no
  // upload_bytes arrives for it. Fetch its stored copy so the server can register and
  // fingerprint it; hashOutput skips URLs it has already hashed.
  for (const i of c.inputs) {
    if (!i.url || !i.service_asset_id || !/^https:/.test(i.url)) continue;
    plan.hashJobs.push({
      method: "upload",
      output: { index: 0, url: i.url, fetch_url: ctx.signedUrls[i.url] ?? i.url, service: c.service, job_id: i.service_asset_id, service_asset_id: i.service_asset_id, media_type: null, at: record.captured_at },
    });
  }
  plan.events.push(
    event(ctx, record, "generation.requested", c.evidence, {
      activity_id,
      service: c.service,
      kind: c.activity_kind,
      model_id: c.model_id,
      service_job_id: c.job_id,
      status: c.status,
      prompt: c.prompt,
      negative_prompt: c.negative_prompt,
      seed: c.seed,
      parameters: c.parameters,
      inputs: c.inputs.map((i) => ({ role: i.role, service_asset_id: i.service_asset_id ?? null, url: i.url ?? null })),
      credits_cost: c.credits_cost,
      credits_currency: c.credits_currency,
      shot_id: ctx.activeShotId,
      service_account: c.service_account,
    }),
  );
}

function completed(c: Completed, record: CaptureRecord, state: PipelineState, ctx: PlanContext, plan: Plan): void {
  const outputs: StoredOutput[] = c.outputs.map((o) => ({
    ...o,
    service: c.service,
    job_id: c.job_id,
    fetch_url: ctx.signedUrls[o.url] ?? o.url,
    at: record.captured_at,
  }));
  // Remember outputs of every job (for filename → job matching on download), keeping a
  // fetchable URL rather than overwriting it with a redacted one.
  for (const id of [c.job_id, ...(c.alias_job_ids ?? [])]) {
    const prev = state.outputs[key(c.service, id)];
    const out = outputs[0];
    if (!out) continue;
    state.outputs[key(c.service, id)] =
      out.fetch_url.includes("[REDACTED]") && prev ? { ...out, fetch_url: prev.fetch_url } : out;
  }
  if (!state.submitted[key(c.service, c.job_id)]) return;
  const dedupe = `${key(c.service, c.job_id)}:completed`;
  if (state.emitted[dedupe]) return;
  state.emitted[dedupe] = record.captured_at;
  plan.events.push(
    event(ctx, record, "generation.completed", "service_reported", {
      service: c.service,
      service_job_id: c.job_id,
      outputs: c.outputs.map((o) => ({ index: o.index, url: o.url, service_asset_id: o.service_asset_id, media_type: o.media_type })),
      credits_cost: c.credits_cost ?? null,
    }),
  );
  for (const o of outputs) plan.hashJobs.push({ method: "completion", output: state.outputs[key(c.service, c.job_id)] ?? o });
}

export function plan(record: CaptureRecord, captures: Capture[] | null, state: PipelineState, ctx: PlanContext): Plan {
  const out: Plan = { events: [], hashJobs: [] };

  if (captures === null) {
    let path = record.url ?? "";
    try {
      path = new URL(path).pathname;
    } catch {
      /* keep as is */
    }
    const k = `${record.service}:${path}`;
    const last = state.unknownSeen[k];
    if (!last || Date.parse(record.captured_at) - Date.parse(last) > UNKNOWN_REPEAT_MS) {
      state.unknownSeen[k] = record.captured_at;
      out.events.push(
        event(ctx, record, "adapter.unknown_payload", "observed", {
          service: record.service,
          url: (record.url ?? "").split("?")[0],
          method: record.method ?? null,
          sample: JSON.stringify(record.response_body ?? null).slice(0, 2000), // already redacted
        }),
      );
    }
    return out;
  }

  for (const c of captures) {
    switch (c.kind) {
      case "requested":
        requested(c, record, state, ctx, out);
        break;
      case "status": {
        if (!state.submitted[key(c.service, c.job_id)]) break;
        const dedupe = `${key(c.service, c.job_id)}:status:${c.status}`;
        if (state.emitted[dedupe]) break;
        state.emitted[dedupe] = record.captured_at;
        out.events.push(
          event(ctx, record, "generation.status", "service_reported", {
            service: c.service,
            service_job_id: c.job_id,
            status: c.status,
            detail: c.detail ?? null,
            credits_cost: c.credits_cost ?? null,
          }),
        );
        break;
      }
      case "completed":
        completed(c, record, state, ctx, out);
        break;
      case "upload_slot":
        state.uploadSlots[c.upload_url] = { service: c.service, service_asset_id: c.service_asset_id, public_url: c.public_url, at: record.captured_at };
        break;
      case "upload_bytes": {
        const slot = state.uploadSlots[c.upload_url];
        if (!slot) break;
        if (slot.public_url) {
          const url = slot.public_url;
          out.hashJobs.push({
            method: "upload",
            output: { index: 0, url, fetch_url: ctx.signedUrls[url] ?? url, service: slot.service, job_id: slot.service_asset_id, service_asset_id: slot.service_asset_id, media_type: null, at: record.captured_at },
          });
        }
        out.events.push(
          event(ctx, record, "asset.location_seen", "observed", {
            sha256: c.sha256,
            service: slot.service,
            url: slot.public_url ?? "",
            service_asset_id: slot.service_asset_id,
          }),
        );
        break;
      }
      case "asset_location":
        if (c.url) {
          out.hashJobs.push({
            method: "upload",
            output: { index: 0, url: c.url, fetch_url: ctx.signedUrls[c.url] ?? c.url, service: c.service, job_id: c.service_asset_id, service_asset_id: c.service_asset_id, media_type: null, at: record.captured_at },
          });
        }
        out.events.push(
          event(ctx, record, "asset.location_seen", "observed", {
            sha256: c.sha256,
            service: c.service,
            url: c.url,
            service_asset_id: c.service_asset_id,
          }),
        );
        break;
    }
  }
  return out;
}

/**
 * A download of an own job's output proves the output exists. If the completion message was
 * missed (e.g. the push channel dropped during a long job), record the completion from the
 * download itself — evidence `observed`, output named by the job id from the filename.
 */
export function completedFromDownload(
  state: PipelineState,
  service: string,
  jobId: string,
  url: string,
  mime: string | null,
  occurredAt: string,
  newId: () => string,
): FaibleEvent | null {
  if (!state.submitted[key(service, jobId)]) return null;
  const dedupe = `${key(service, jobId)}:completed`;
  if (state.emitted[dedupe]) return null;
  state.emitted[dedupe] = occurredAt;
  const media_type = mime?.startsWith("video/") ? "video" : mime?.startsWith("image/") ? "image" : null;
  return {
    event_id: newId(),
    type: "generation.completed",
    occurred_at: occurredAt,
    evidence: "observed",
    payload: { service, service_job_id: jobId, outputs: [{ index: 0, url, service_asset_id: jobId, media_type }], credits_cost: null },
  };
}

// ---------------------------------------------------------------- service history and usage
//
// History listings show every artist's jobs (shared workspace), so they are NOT turned into
// generation.* events: they become service_generation.observed, which the server keeps apart
// from activities and never attributes to the observer. The live path above is unchanged.

export interface HistoryBatch {
  service: string;
  url: string; // the (redacted) response the objects came from
  items: Record<string, unknown>[];
  login: Record<string, string | null> | null; // ServiceLogin metadata
  capturedAt: string;
}

/** One event per history object whose content changed since this browser last sent it. */
export function planHistory(batch: HistoryBatch, state: PipelineState, newId: () => string): FaibleEvent[] {
  state.history ??= {};
  const out: FaibleEvent[] = [];
  for (const item of batch.items) {
    if (typeof item.id !== "string" || !item.id) continue;
    const k = key(batch.service, item.id);
    const sig = signature(item);
    if (state.history[k]?.sig === sig) continue;
    state.history[k] = { sig, at: batch.capturedAt };
    out.push({
      event_id: newId(),
      type: "service_generation.observed",
      occurred_at: batch.capturedAt,
      evidence: "service_reported",
      payload: { service: batch.service, service_job_id: item.id, source: "history", service_login: batch.login, source_url: batch.url, raw: item },
    });
  }
  return out;
}

/** "Save usage page" → usage.page_captured (tables/text are labelled observed_dom by the server). */
export function usageEvent(service: string, capture: UsageCapture, login: Record<string, string | null> | null, newId: () => string): FaibleEvent {
  return {
    event_id: newId(),
    type: "usage.page_captured",
    occurred_at: capture.capturedAt,
    evidence: "observed",
    payload: {
      capture_id: newId(),
      service,
      source: "extension",
      page_url: capture.pageUrl,
      page_title: capture.pageTitle || null,
      service_login: login,
      responses: capture.responses.map((r) => ({ url: r.url, page_path: r.pagePath, captured_at: r.capturedAt, data: r.data })),
      tables: capture.tables,
      text: capture.text,
    },
  };
}

/** Drop the oldest entries of each map so persisted state stays bounded. History gets a
 *  larger cap: a fully scrolled history page lists thousands of jobs, and a pruned entry is
 *  re-sent (harmlessly, the server merges it) the next time the page is scrolled. */
export function pruneState(state: PipelineState, cap = 3000, historyCap = 20_000): void {
  for (const map of [state.submitted, state.emitted, state.outputs, state.uploadSlots, state.unknownSeen, state.history ?? {}] as Record<string, unknown>[]) {
    const keys = Object.keys(map);
    const limit = map === state.history ? historyCap : cap;
    if (keys.length <= limit) continue;
    const at = (k: string) => {
      const v = map[k];
      return typeof v === "string" ? v : ((v as { at?: string }).at ?? "");
    };
    keys.sort((a, b) => at(a).localeCompare(at(b)));
    for (const k of keys.slice(0, keys.length - limit)) delete map[k];
  }
}
