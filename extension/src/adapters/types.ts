import type { CaptureRecord } from "../shared/capture";

// What an adapter extracts from one captured request/response (spec §4.1: adapters are pure
// functions of the traffic). The worker turns captures into Faible events.

export type ActivityKind = "generate" | "upscale" | "edit" | "variation";
export type ActivityStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type Evidence = "service_reported" | "observed";

export interface InputRef {
  role: string;
  service_asset_id?: string;
  url?: string;
}

export interface OutputRef {
  index: number;
  url: string; // as seen in the (redacted) record; the worker may hold the signed original
  service_asset_id: string;
  media_type: string | null;
}

export interface Requested {
  kind: "requested";
  service: string;
  job_id: string | null; // null: rejected before a job existed (e.g. HTTP 422)
  activity_kind: ActivityKind;
  status: ActivityStatus;
  model_id: string | null;
  prompt: string | null;
  negative_prompt: string | null;
  seed: string | null;
  parameters: Record<string, unknown>;
  inputs: InputRef[];
  credits_cost: string | null;
  credits_currency: string;
  service_account: { workspace_id?: string; service_user_id?: string } | null;
  evidence: Evidence;
}

export interface Status {
  kind: "status";
  service: string;
  job_id: string;
  status: ActivityStatus;
  credits_cost?: string;
  detail?: string;
}

export interface Completed {
  kind: "completed";
  service: string;
  job_id: string;
  outputs: OutputRef[];
  credits_cost?: string;
  /** Other ids the output file is known by (Higgsfield layer upscales are named by jobId). */
  alias_job_ids?: string[];
  source: "job_detail" | "history" | "notification";
}

/** Higgsfield upload step 1: the service hands out a presigned URL for a media id. */
export interface UploadSlot {
  kind: "upload_slot";
  service: string;
  upload_url: string;
  service_asset_id: string;
  public_url: string | null;
}

/** The page sent file bytes to an upload URL; the page-world script hashed them. */
export interface UploadBytes {
  kind: "upload_bytes";
  service: string;
  upload_url: string;
  sha256: string;
}

/** A hashed file is known to the service under an id (Magnific: upload → creation). */
export interface AssetLocation {
  kind: "asset_location";
  service: string;
  sha256: string;
  service_asset_id: string;
  url: string;
}

export type Capture = Requested | Status | Completed | UploadSlot | UploadBytes | AssetLocation;

export interface Adapter {
  service: string;
  /** Endpoints the adapter expects to understand. A matching record it can't parse is
   *  reported as `adapter.unknown_payload` (spec §4.2) — how drift gets noticed. */
  patterns: RegExp[];
  /** Captures for a record; `[]` = understood, nothing to record; `null` = unparseable. */
  parse(record: CaptureRecord): Capture[] | null;
  /** Job id recoverable from a downloaded file's name, if the service names files that way. */
  jobIdFromFilename(filename: string): string | null;
}

type Json = Record<string, unknown>;
export const isObj = (v: unknown): v is Json => v !== null && typeof v === "object" && !Array.isArray(v);
export const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);
export const num = (v: unknown): string | null =>
  typeof v === "number" && Number.isFinite(v) ? String(v) : typeof v === "string" && /^\d+(\.\d+)?$/.test(v) ? v : null;

/** A file (sha256) the page-world script attached to a request body, if any. */
export function bodyFileSha(body: unknown): string | null {
  if (!isObj(body)) return null;
  if (typeof body.__sha256 === "string") return body.__sha256;
  if (isObj(body.__formdata)) {
    for (const v of Object.values(body.__formdata)) if (isObj(v) && typeof v.__sha256 === "string") return v.__sha256;
  }
  return null;
}
