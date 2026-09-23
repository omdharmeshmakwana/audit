// Background service worker — Phase 0 (discovery / capture-all mode).
//
// - Receives capture records from content-script relays.
// - Persists them in chrome.storage.local (survives worker restarts) and flushes them
//   in batches to the local server's /discovery/capture sink, retrying with backoff.
// - Fingerprints assets (fingerprint.ts): completion hash, blob hash, filename → job link.
// - Observes completed downloads, tries a direct re-fetch, and attaches the best hash.
// - Never reads the local filesystem; keeps only the basename of a download's path.

import { MAX_HASH_BYTES, SERVER_URL, serviceForHost, type CaptureRecord } from "../shared/capture";
import { sha256Hex } from "../shared/hash";
import { redactUrl } from "../shared/redact";
import { clearFingerprintState, fingerprintDownload, onRecord } from "./fingerprint";
import { locked } from "./lock";
import type { DownloadFingerprint } from "./resolve";

const QUEUE_KEY = "discoveryQueue";
const STATS_KEY = "discoveryStats";
const ENABLED_KEY = "captureAllEnabled";
const MAX_QUEUE = 5000;
const BATCH = 50;

interface Stats {
  captured: number;
  sent: number;
  dropped: number;
  lastError: string | null;
  lastSentAt: string | null;
  hosts: Record<string, number>;
  // Writes (POST/PUT/PATCH/DELETE) and downloads, newest first: what the artist *did*,
  // as opposed to the background polling that dominates the counters.
  actions?: { at: string; label: string }[];
}

const emptyStats = (): Stats => ({
  captured: 0,
  sent: 0,
  dropped: 0,
  lastError: null,
  lastSentAt: null,
  hosts: {},
  actions: [],
});

// Writes that are polling/housekeeping, not artist actions.
const ROUTINE_WRITE = /status-batch|liked-by|workspaces\/context|\/api\/i\/|cdn-cgi|dd\.higgsfield/;

const short = (h: unknown) => (typeof h === "string" ? `${h.slice(0, 10)}…` : "none");

function actionLabel(r: CaptureRecord): string | null {
  if (r.transport === "download") {
    const fp = r.extra.fingerprint as DownloadFingerprint | undefined;
    const agree = fp?.agree == null ? "" : fp.agree ? ", hashes agree" : ", HASHES DISAGREE";
    return fp?.sha256
      ? `download ${String(r.extra.filename ?? "")} sha256 ${short(fp.sha256)} via ${fp.method} (${fp.evidence}${agree})`
      : `download ${String(r.extra.filename ?? "")} UNHASHED`;
  }
  if (r.transport === "fingerprint") {
    const job = String(r.extra.job_id ?? "").slice(0, 8);
    return r.extra.sha256
      ? `hashed output of job ${job} (${String(r.extra.method)}) sha256 ${short(r.extra.sha256)}`
      : `could not hash output of job ${job}: ${String(r.extra.error)}`;
  }
  if (!r.method || r.method === "GET" || !r.url || ROUTINE_WRITE.test(r.url)) return null;
  try {
    const u = new URL(r.url);
    return `${r.method} ${u.pathname} → ${r.status ?? "?"}`;
  } catch {
    return null;
  }
}

async function getState(): Promise<{ queue: CaptureRecord[]; stats: Stats; enabled: boolean }> {
  const s = await chrome.storage.local.get([QUEUE_KEY, STATS_KEY, ENABLED_KEY]);
  return {
    queue: (s[QUEUE_KEY] as CaptureRecord[] | undefined) ?? [],
    stats: (s[STATS_KEY] as Stats | undefined) ?? emptyStats(),
    enabled: (s[ENABLED_KEY] as boolean | undefined) ?? true,
  };
}

/** Queue a record; resolves false when capture is switched off (record dropped). */
function enqueue(record: CaptureRecord): Promise<boolean> {
  return locked(async () => {
    const { queue, stats, enabled } = await getState();
    if (!enabled) return false;
    queue.push(record);
    stats.captured++;
    try {
      const host = new URL(record.url ?? "").hostname;
      stats.hosts[host] = (stats.hosts[host] ?? 0) + 1;
    } catch {
      /* ignore */
    }
    const label = actionLabel(record);
    if (label) {
      stats.actions = [{ at: record.captured_at, label: `${record.service}: ${label}` }, ...(stats.actions ?? [])].slice(0, 15);
    }
    while (queue.length > MAX_QUEUE) {
      queue.shift();
      stats.dropped++;
    }
    await chrome.storage.local.set({ [QUEUE_KEY]: queue, [STATS_KEY]: stats });
    console.log("[faible]", record.service, record.transport, record.method ?? "", record.url, record);
    scheduleFlush(1500);
    return true;
  });
}

// ---------------------------------------------------------------- flushing

let flushTimer: ReturnType<typeof setTimeout> | null = null;
let backoffMs = 2000;

function scheduleFlush(ms: number): void {
  if (flushTimer) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    void flush();
  }, ms);
}

function flush(): Promise<void> {
  return locked(async () => {
    const { queue, stats } = await getState();
    if (queue.length === 0) return;
    const batch = queue.slice(0, BATCH);
    try {
      const res = await fetch(`${SERVER_URL}/discovery/capture`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ records: batch }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
      const fresh = await getState();
      fresh.queue.splice(0, batch.length);
      fresh.stats.sent += batch.length;
      fresh.stats.lastError = null;
      fresh.stats.lastSentAt = new Date().toISOString();
      await chrome.storage.local.set({ [QUEUE_KEY]: fresh.queue, [STATS_KEY]: fresh.stats });
      backoffMs = 2000;
      if (fresh.queue.length > 0) scheduleFlush(200);
    } catch (err) {
      stats.lastError = String(err);
      await chrome.storage.local.set({ [STATS_KEY]: stats });
      backoffMs = Math.min(backoffMs * 2, 60_000);
      scheduleFlush(backoffMs);
    }
  });
}

chrome.alarms.create("faible-flush", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "faible-flush") void flush();
});

// ---------------------------------------------------------------- downloads

function serviceForDownload(item: chrome.downloads.DownloadItem): string {
  for (const u of [item.url, item.finalUrl, item.referrer]) {
    if (!u) continue;
    try {
      // blob:https://higgsfield.ai/<uuid> → URL() gives origin via pathname parse.
      const parsed = new URL(u.startsWith("blob:") ? u.slice(5) : u);
      const svc = serviceForHost(parsed.hostname);
      if (svc !== "unknown") return svc;
    } catch {
      /* ignore */
    }
  }
  return "unknown";
}

async function tryRefetch(item: chrome.downloads.DownloadItem): Promise<Record<string, unknown>> {
  const url = item.finalUrl || item.url;
  if (url.startsWith("blob:") || url.startsWith("data:")) {
    return { attempted: false, reason: `${url.split(":")[0]} URL is not re-fetchable from the worker` };
  }
  if (item.totalBytes > MAX_HASH_BYTES) return { attempted: false, reason: "over 500 MB" };
  try {
    const res = await fetch(url, { credentials: "include" });
    if (!res.ok) return { attempted: true, ok: false, status: res.status };
    const buf = await res.arrayBuffer();
    return {
      attempted: true,
      ok: true,
      status: res.status,
      bytes: buf.byteLength,
      content_type: res.headers.get("content-type"),
      sha256: await sha256Hex(buf),
      matches_download_size: item.fileSize > 0 ? item.fileSize === buf.byteLength : null,
    };
  } catch (err) {
    return { attempted: true, ok: false, error: String(err) };
  }
}

chrome.downloads.onChanged.addListener((delta) => {
  if (delta.state?.current !== "complete") return;
  void (async () => {
    const [item] = await chrome.downloads.search({ id: delta.id });
    if (!item || item.byExtensionId === chrome.runtime.id) return;
    const service = serviceForDownload(item);
    if (service === "unknown") return; // not a service download: none of our business
    const refetch = await tryRefetch(item);
    const refetchSha = typeof refetch.sha256 === "string" ? refetch.sha256 : null;
    const fingerprint = await fingerprintDownload(item, service, refetchSha, enqueue);
    await enqueue({
      service,
      transport: "download",
      captured_at: new Date().toISOString(),
      method: "GET",
      url: redactUrl(item.finalUrl || item.url),
      request_headers: {},
      response_headers: {},
      extra: {
        original_url: redactUrl(item.url),
        referrer: item.referrer ? redactUrl(item.referrer) : null,
        filename: item.filename.split(/[\\/]/).pop() ?? "",
        mime: item.mime,
        file_size: item.fileSize,
        total_bytes: item.totalBytes,
        started_at: item.startTime,
        ended_at: item.endTime ?? null,
        refetch,
        fingerprint,
      },
    });
  })();
});

// ---------------------------------------------------------------- messages

chrome.runtime.onMessage.addListener((msg: { type: string; record?: CaptureRecord; enabled?: boolean }, _s, reply) => {
  if (msg.type === "capture" && msg.record) {
    // Signed output URLs stay inside the extension: detach them before anything is queued.
    const { signed_urls: signed = {}, ...record } = msg.record;
    void enqueue(record).then((accepted) => {
      if (accepted) void onRecord(record, enqueue, signed);
    });
    return false;
  }
  if (msg.type === "status") {
    void getState().then(({ queue, stats, enabled }) => reply({ queued: queue.length, stats, enabled }));
    return true;
  }
  if (msg.type === "setEnabled") {
    void chrome.storage.local.set({ [ENABLED_KEY]: !!msg.enabled }).then(() => reply({ ok: true }));
    return true;
  }
  if (msg.type === "flush") {
    void flush().then(() => reply({ ok: true }));
    return true;
  }
  if (msg.type === "clear") {
    void locked(() => chrome.storage.local.set({ [QUEUE_KEY]: [], [STATS_KEY]: emptyStats() }))
      .then(clearFingerprintState)
      .then(() => reply({ ok: true }));
    return true;
  }
  return false;
});
