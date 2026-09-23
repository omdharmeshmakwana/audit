// Asset fingerprinting in the worker (see docs/DECISIONS.md, "fingerprinting methods").
//
//   method 1  completion hash  — a job *this browser submitted* completes → fetch its output
//                                URL → sha256. Only our own jobs: the studio shares one
//                                service workspace, so history/notifications also show
//                                other artists' jobs, which are theirs to capture.
//   method 2  blob hash        — the page-world script hashes Blobs behind blob: URLs; the
//                                worker keeps blob_url → sha256 to match downloads.
//   method 3  filename → job   — a download named hf_…_<job_id>.ext is linked to that job's
//                                output and its hash (hashed on demand if not yet).
//
// State is persisted in chrome.storage.local so it survives worker restarts.

import { adapterFor } from "../adapters";
import type { JobOutput } from "../adapters/types";
import { MAX_HASH_BYTES, type CaptureRecord } from "../shared/capture";
import { sha256Hex } from "../shared/hash";
import { redactUrl } from "../shared/redact";
import { locked } from "./lock";
import { resolveDownloadFingerprint, type DownloadFingerprint } from "./resolve";

const STATE_KEY = "fingerprintState";
const CAPS = { submitted: 1000, outputs: 3000, hashes: 3000, blobs: 300 };
const BLOB_WAIT_MS = 15_000;

interface HashResult {
  sha256: string | null;
  bytes: number | null;
  content_type: string | null;
  etag: string | null;
  error: string | null;
  at: string;
}
interface StoredOutput extends JobOutput {
  /** Fetchable URL: the original signed URL when `url` is redacted. Never leaves the extension. */
  fetch_url: string;
  at: string;
}
interface BlobHash {
  sha256: string | null;
  size: number;
  type: string;
  at: string;
}
interface FpState {
  submitted: Record<string, { service: string; at: string }>; // job_id →
  outputs: Record<string, StoredOutput>; // job_id →
  hashes: Record<string, HashResult>; // output url →
  blobs: Record<string, BlobHash>; // blob url →
}

type Emit = (record: CaptureRecord) => Promise<unknown>;

const empty = (): FpState => ({ submitted: {}, outputs: {}, hashes: {}, blobs: {} });

async function load(): Promise<FpState> {
  const s = await chrome.storage.local.get(STATE_KEY);
  return { ...empty(), ...((s[STATE_KEY] as FpState | undefined) ?? {}) };
}

function prune<T extends { at: string }>(map: Record<string, T>, cap: number): void {
  const keys = Object.keys(map);
  if (keys.length <= cap) return;
  keys
    .sort((a, b) => map[a]!.at.localeCompare(map[b]!.at))
    .slice(0, keys.length - cap)
    .forEach((k) => delete map[k]);
}

function update(fn: (s: FpState) => void): Promise<FpState> {
  return locked(async () => {
    const s = await load();
    fn(s);
    prune(s.submitted, CAPS.submitted);
    prune(s.outputs, CAPS.outputs);
    prune(s.hashes, CAPS.hashes);
    prune(s.blobs, CAPS.blobs);
    await chrome.storage.local.set({ [STATE_KEY]: s });
    return s;
  });
}

// ---------------------------------------------------------------- hashing URLs

const inFlight = new Map<string, Promise<HashResult>>();

async function fetchAndHash(url: string): Promise<HashResult> {
  const at = new Date().toISOString();
  try {
    // Output CDNs are public; never send the artist's cookies along.
    const res = await fetch(url, { credentials: "omit" });
    const content_type = res.headers.get("content-type");
    const etag = res.headers.get("etag");
    if (!res.ok) return { sha256: null, bytes: null, content_type, etag, error: `HTTP ${res.status}`, at };
    const len = Number(res.headers.get("content-length") ?? "0");
    if (len > MAX_HASH_BYTES) {
      await res.body?.cancel();
      return { sha256: null, bytes: len, content_type, etag, error: "over 500 MB: location only", at };
    }
    const buf = await res.arrayBuffer();
    return { sha256: await sha256Hex(buf), bytes: buf.byteLength, content_type, etag, error: null, at };
  } catch (err) {
    return { sha256: null, bytes: null, content_type: null, etag: null, error: String(err), at };
  }
}

/**
 * Hash an output once; concurrent and repeated callers share the result. `url` (redacted,
 * stable) is the cache key; `fetchUrl` is what is actually requested.
 */
async function hashUrl(url: string, fetchUrl: string): Promise<HashResult> {
  const known = (await load()).hashes[url];
  if (known?.sha256) return known;
  if (fetchUrl.includes("[REDACTED]")) {
    return { sha256: null, bytes: null, content_type: null, etag: null, error: "signed URL not available", at: new Date().toISOString() };
  }
  let p = inFlight.get(url);
  if (!p) {
    p = fetchAndHash(fetchUrl).then(async (r) => {
      await update((s) => void (s.hashes[url] = r));
      inFlight.delete(url);
      return r;
    });
    inFlight.set(url, p);
  }
  return p;
}

async function hashOutput(out: StoredOutput, method: "completion" | "filename_job", emit: Emit): Promise<HashResult> {
  const r = await hashUrl(out.url, out.fetch_url);
  await emit({
    service: out.service,
    transport: "fingerprint",
    captured_at: new Date().toISOString(),
    method: "GET",
    url: redactUrl(out.url),
    request_headers: {},
    response_headers: {},
    extra: {
      method,
      job_id: out.job_id,
      output_source: out.source,
      media_type: out.media_type,
      sha256: r.sha256,
      bytes: r.bytes,
      content_type: r.content_type,
      etag: r.etag,
      error: r.error,
      evidence: r.sha256 ? "observed" : null,
    },
  });
  return r;
}

// ---------------------------------------------------------------- record hook

/** Feed every captured record through here (after it has been queued). */
export async function onRecord(record: CaptureRecord, emit: Emit, signedUrls: Record<string, string> = {}): Promise<void> {
  if (record.transport === "blob") {
    const url = record.extra.blob_url;
    if (typeof url === "string") {
      await update((s) => {
        s.blobs[url] = {
          sha256: typeof record.extra.sha256 === "string" ? record.extra.sha256 : null,
          size: Number(record.extra.size ?? 0),
          type: String(record.extra.type ?? ""),
          at: record.captured_at,
        };
      });
    }
    return;
  }

  const adapter = adapterFor(record.service);
  if (!adapter) return;
  const obs = adapter.observe(record);
  if (obs.submittedJobIds.length === 0 && obs.outputs.length === 0) return;

  const at = record.captured_at;
  const outputs: StoredOutput[] = obs.outputs.map((o) => ({ ...o, fetch_url: signedUrls[o.url] ?? o.url, at }));
  const s = await update((st) => {
    for (const id of obs.submittedJobIds) st.submitted[id] = { service: adapter.service, at };
    for (const o of outputs) {
      // Keep a known fetchable URL rather than overwrite it with a redacted one.
      const prev = st.outputs[o.job_id];
      st.outputs[o.job_id] = o.fetch_url.includes("[REDACTED]") && prev ? { ...o, fetch_url: prev.fetch_url } : o;
    }
  });

  // Method 1: eagerly hash outputs of jobs this browser submitted.
  for (const o of outputs) {
    if (s.submitted[o.job_id] && !s.hashes[o.url]?.sha256 && !inFlight.has(o.url)) {
      void hashOutput(s.outputs[o.job_id]!, "completion", emit);
    }
  }
}

// ---------------------------------------------------------------- downloads

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
  refetchSha256: string | null,
  emit: Emit,
): Promise<DownloadFingerprint> {
  const url = item.finalUrl || item.url;
  const blobSha256 = url.startsWith("blob:") ? await waitForBlobHash(url) : null;

  // Method 3: filename → job id → job output hash.
  const filename = item.filename.split(/[\\/]/).pop() ?? "";
  const jobId = adapterFor(service)?.jobIdFromFilename(filename) ?? null;
  let jobOutputSha256: string | null = null;
  if (jobId) {
    const out = (await load()).outputs[jobId];
    if (out) jobOutputSha256 = (await hashOutput(out, "filename_job", emit)).sha256;
  }

  return resolveDownloadFingerprint({ blobSha256, refetchSha256, jobId, jobOutputSha256 });
}

export function clearFingerprintState(): Promise<void> {
  return locked(() => chrome.storage.local.set({ [STATE_KEY]: empty() }));
}
