// Captures → Faible events. Pure: no chrome.*, no network, no clock except what's passed in.
// The worker persists `PipelineState` between calls.
//
// Attribution rule (hard rule 8 + shared workspace): status and completion events are only
// emitted for jobs *this browser submitted*. The same workspace's history, notifications and
// project channels also show other artists' jobs; their own extensions record those.

import type { Capture, Completed, OutputRef, Requested } from "../adapters/types";
import type { CaptureRecord } from "../shared/capture";

export interface FaibleEvent {
  event_id: string;
  type: string;
  occurred_at: string;
  evidence: string;
  payload: Record<string, unknown>;
}

export interface StoredOutput extends OutputRef {
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
}

export const emptyState = (): PipelineState => ({ submitted: {}, emitted: {}, outputs: {}, uploadSlots: {}, unknownSeen: {} });

export interface HashJob {
  method: "completion";
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

function requested(c: Requested, record: CaptureRecord, state: PipelineState, ctx: PlanContext): FaibleEvent[] {
  if (c.job_id && state.submitted[key(c.service, c.job_id)]) return []; // same submit seen twice
  const activity_id = ctx.newId();
  if (c.job_id) state.submitted[key(c.service, c.job_id)] = { activity_id, at: record.captured_at };
  return [
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
  ];
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
        out.events.push(...requested(c, record, state, ctx));
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

/** Drop the oldest entries of each map so persisted state stays bounded. */
export function pruneState(state: PipelineState, cap = 3000): void {
  for (const map of [state.submitted, state.emitted, state.outputs, state.uploadSlots, state.unknownSeen] as Record<string, unknown>[]) {
    const keys = Object.keys(map);
    if (keys.length <= cap) continue;
    const at = (k: string) => {
      const v = map[k];
      return typeof v === "string" ? v : ((v as { at?: string }).at ?? "");
    };
    keys.sort((a, b) => at(a).localeCompare(at(b)));
    for (const k of keys.slice(0, keys.length - cap)) delete map[k];
  }
}
