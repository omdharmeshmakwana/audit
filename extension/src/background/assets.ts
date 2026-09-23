// Asset bytes: hashing and registration (spec §4.2, DECISIONS #11).
//
//   method 1  completion   — an own job completes → fetch its output → sha256 → upload the
//                            bytes to POST /ingest/asset (the server re-hashes and rejects
//                            a mismatch), so the asset exists even if nobody downloads it.
//   method 2  blob         — the page hashed the Blob behind a blob: download URL.
//   method 3  filename_job — hf_…_<job>.png / magnific_…_<id>.png → that job's output hash.
//
// Bytes are uploaded straight from memory and never persisted in the extension; a failed
// upload is retried a few times, then left for the auditor (activity_without_output).

import { MAX_HASH_BYTES, type CaptureRecord } from "../shared/capture";
import { sha256Hex } from "../shared/hash";
import { redactUrl } from "../shared/redact";
import { adapterFor } from "../adapters";
import { locked } from "./lock";
import type { PipelineState, StoredOutput } from "./pipeline";
import { resolveDownloadFingerprint, type DownloadFingerprint } from "./resolve";
import { api, NotLoggedIn } from "./session";

const STATE_KEY = "assetState";
const BLOB_WAIT_MS = 15_000;
const UPLOAD_ATTEMPTS = 3;

export interface HashResult {
  sha256: string | null;
  bytes: number | null;
  content_type: string | null;
  error: string | null;
  registered: boolean;
  at: string;
}

interface AssetState {
  hashes: Record<string, HashResult>; // output url (as recorded) →
  blobs: Record<string, { sha256: string | null; at: string }>; // blob url →
}

export type Log = (label: string) => void;

const empty = (): AssetState => ({ hashes: {}, blobs: {} });

async function load(): Promise<AssetState> {
  const s = await chrome.storage.local.get(STATE_KEY);
  return { ...empty(), ...((s[STATE_KEY] as AssetState | undefined) ?? {}) };
}

function update(fn: (s: AssetState) => void): Promise<void> {
  return locked(async () => {
    const s = await load();
    fn(s);
    for (const map of [s.hashes, s.blobs] as Record<string, { at: string }>[]) {
      const keys = Object.keys(map);
      if (keys.length > 2000) {
        keys.sort((a, b) => map[a]!.at.localeCompare(map[b]!.at));
        for (const k of keys.slice(0, keys.length - 2000)) delete map[k];
      }
    }
    await chrome.storage.local.set({ [STATE_KEY]: s });
  });
}

export function clearAssetState(): Promise<void> {
  return locked(() => chrome.storage.local.set({ [STATE_KEY]: empty() }));
}

// ---------------------------------------------------------------- registration

export async function registerBytes(
  buf: ArrayBuffer,
  sha256: string,
  locations: { service: string; url?: string; service_asset_id?: string }[],
  mediaType: string | null,
): Promise<boolean> {
  for (let attempt = 1; attempt <= UPLOAD_ATTEMPTS; attempt++) {
    try {
      const form = new FormData();
      form.append("file", new Blob([buf], { type: mediaType ?? "application/octet-stream" }), "asset");
      form.append("locations", JSON.stringify(locations));
      if (mediaType) form.append("media_type", mediaType);
      form.append("evidence", "observed");
      const res = await api("/ingest/asset", { method: "POST", headers: { "x-faible-sha256": sha256 }, body: form });
      if (res.ok) return true;
      if (res.status === 422) return false; // mismatch or too large: retrying won't help
    } catch (err) {
      if (err instanceof NotLoggedIn) return false;
    }
    await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
  }
  return false;
}

const inFlight = new Map<string, Promise<HashResult>>();

async function fetchHashRegister(out: StoredOutput): Promise<HashResult> {
  const at = new Date().toISOString();
  const fail = (error: string, extra: Partial<HashResult> = {}): HashResult => ({
    sha256: null,
    bytes: null,
    content_type: null,
    registered: false,
    error,
    at,
    ...extra,
  });
  if (out.fetch_url.includes("[REDACTED]")) return fail("signed URL not available");
  try {
    // Output CDNs are public or carry their own token; never send the artist's cookies.
    const res = await fetch(out.fetch_url, { credentials: "omit" });
    const content_type = res.headers.get("content-type");
    if (!res.ok) return fail(`HTTP ${res.status}`, { content_type });
    const len = Number(res.headers.get("content-length") ?? "0");
    if (len > MAX_HASH_BYTES) {
      await res.body?.cancel();
      return fail("over 500 MB: location only", { bytes: len, content_type });
    }
    const buf = await res.arrayBuffer();
    const sha256 = await sha256Hex(buf);
    const registered = await registerBytes(
      buf,
      sha256,
      [{ service: out.service, url: out.url, service_asset_id: out.service_asset_id }],
      content_type,
    );
    return { sha256, bytes: buf.byteLength, content_type, registered, error: null, at };
  } catch (err) {
    return fail(String(err));
  }
}

/** Hash (and register) an output once; concurrent and repeated callers share the result. */
export async function hashOutput(out: StoredOutput, method: "completion" | "filename_job" | "upload", log: Log): Promise<HashResult> {
  const known = (await load()).hashes[out.url];
  if (known?.sha256) return known;
  let p = inFlight.get(out.url);
  if (!p) {
    p = fetchHashRegister(out).then(async (r) => {
      await update((s) => void (s.hashes[out.url] = r));
      inFlight.delete(out.url);
      log(
        r.sha256
          ? `${out.service}: hashed output of ${out.job_id.slice(0, 10)} (${method}) sha256 ${r.sha256.slice(0, 10)}…${r.registered ? ", registered" : ", NOT registered"}`
          : `${out.service}: could not hash output of ${out.job_id.slice(0, 10)}: ${r.error}`,
      );
      return r;
    });
    inFlight.set(out.url, p);
  }
  return p;
}

export async function recordBlob(record: CaptureRecord): Promise<void> {
  const url = record.extra.blob_url;
  if (typeof url !== "string") return;
  const sha = typeof record.extra.sha256 === "string" ? record.extra.sha256 : null;
  await update((s) => void (s.blobs[url] = { sha256: sha, at: record.captured_at }));
}

// ---------------------------------------------------------------- downloads

export interface Refetch {
  attempted: boolean;
  ok?: boolean;
  status?: number;
  sha256?: string;
  bytes?: number;
  content_type?: string | null;
  error?: string;
  reason?: string;
  buf?: ArrayBuffer; // in memory only, for registration
}

export async function refetchDownload(item: chrome.downloads.DownloadItem): Promise<Refetch> {
  const url = item.finalUrl || item.url;
  if (url.startsWith("blob:") || url.startsWith("data:")) return { attempted: false, reason: "not re-fetchable from the worker" };
  if (item.totalBytes > MAX_HASH_BYTES) return { attempted: false, reason: "over 500 MB" };
  try {
    const res = await fetch(url, { credentials: "omit" });
    if (!res.ok) return { attempted: true, ok: false, status: res.status };
    const buf = await res.arrayBuffer();
    return { attempted: true, ok: true, status: res.status, bytes: buf.byteLength, content_type: res.headers.get("content-type"), sha256: await sha256Hex(buf), buf };
  } catch (err) {
    return { attempted: true, ok: false, error: String(err) };
  }
}

async function waitForBlobHash(blobUrl: string): Promise<string | null> {
  const deadline = Date.now() + BLOB_WAIT_MS;
  while (Date.now() < deadline) {
    const b = (await load()).blobs[blobUrl];
    if (b) return b.sha256;
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

export async function fingerprintDownload(
  item: chrome.downloads.DownloadItem,
  service: string,
  refetch: Refetch,
  pipeline: PipelineState,
  log: Log,
): Promise<DownloadFingerprint> {
  const url = item.finalUrl || item.url;
  const blobSha256 = url.startsWith("blob:") ? await waitForBlobHash(url) : null;
  const filename = item.filename.split(/[\\/]/).pop() ?? "";
  const jobId = adapterFor(service)?.jobIdFromFilename(filename) ?? null;
  let jobOutputSha256: string | null = null;
  const out = jobId ? pipeline.outputs[`${service}:${jobId}`] : undefined;
  if (out) jobOutputSha256 = (await hashOutput(out, "filename_job", log)).sha256;
  if (refetch.ok && refetch.buf && refetch.sha256) {
    // Register what was downloaded, at the location it came from — and under the job id the
    // service put in the filename, so the job's output edge can resolve to these bytes.
    const location = { service, url: redactUrl(url), ...(jobId ? { service_asset_id: jobId } : {}) };
    void registerBytes(refetch.buf, refetch.sha256, [location], refetch.content_type ?? item.mime ?? null);
  }
  return resolveDownloadFingerprint({ blobSha256, refetchSha256: refetch.sha256 ?? null, jobId, jobOutputSha256 });
}

