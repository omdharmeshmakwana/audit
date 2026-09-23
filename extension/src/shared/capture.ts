// Phase 0 raw traffic record, shared by page world → relay → background → server.
// Mirrors server/faible/discovery.py:CaptureRecord.

export type Transport =
  | "fetch"
  | "fetch-stream"
  | "xhr"
  | "ws"
  | "sse"
  | "download"
  | "blob" // page created an object URL for a media Blob; hashed in the page (method 2)
  | "fingerprint"; // worker fetched a job's output URL and hashed it (methods 1 and 3)

/** Files above this are located but not hashed (spec §4.2). */
export const MAX_HASH_BYTES = 500 * 1024 * 1024;

export interface CaptureRecord {
  service: string;
  transport: Transport;
  captured_at: string;
  page_url?: string;
  method?: string;
  url?: string;
  status?: number;
  request_headers: Record<string, string>;
  request_body?: unknown;
  response_headers: Record<string, string>;
  response_body?: unknown;
  extra: Record<string, unknown>;
  /**
   * Redacted URL → original signed URL, for output CDNs whose URLs carry an access token
   * (SIGNED_OUTPUT_HOST). Lets the worker hash outputs (method 1). Page → worker only:
   * the worker deletes this before a record is queued, persisted or sent (hard rule 9).
   */
  signed_urls?: Record<string, string>;
}

/** Output CDNs whose URLs are signed: Magnific/Freepik `pikaso.cdnpk.net`. */
export const SIGNED_OUTPUT_HOST = /(^|\.)cdnpk\.net$/i;

export const MSG_TAG = "__faible_capture__";

export const SERVER_URL = "http://localhost:8000";

// Response bodies larger than this are truncated in capture-all mode. Generous because
// Higgsfield's job history (/fnf/jobs/accessible) exceeds 256 KB and is the richest source.
export const MAX_BODY_CHARS = 8 * 1024 * 1024;

export function serviceForHost(host: string): string {
  if (/(^|\.)higgsfield\.ai$/.test(host)) return "higgsfield";
  // Magnific is Freepik's product; the same account and backend serve both domains.
  if (/(^|\.)(magnific\.(com|ai)|freepik\.com)$/.test(host)) return "magnific";
  return "unknown";
}
