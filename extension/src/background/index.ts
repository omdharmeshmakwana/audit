// Background service worker (spec §4.1).
//
// capture record (page) → adapter.parse → pipeline.plan → Faible events → queue → server
//                                                       ↘ hash jobs → /ingest/asset
// Plus: chrome.downloads (download.observed + fingerprint), the Faible session and active
// shot, and the optional Phase 0 discovery mirror (raw redacted traffic, off by default).
// Never reads the local filesystem; keeps only a download's file name, never its path.

import { adapterFor } from "../adapters";
import { bodyFileSha, normalizeUrl } from "../adapters/types";
import { SERVER_URL, serviceForHost, type CaptureRecord } from "../shared/capture";
import { redactUrl } from "../shared/redact";
import { clearAssetState, fingerprintDownload, hashOutput, recordBlob, refetchDownload } from "./assets";
import { locked } from "./lock";
import { completedFromDownload, emptyState, plan, pruneState, type FaibleEvent, type PipelineState } from "./pipeline";
import { clearQueue, enqueueEvents, flush, queueState, scheduleFlush } from "./queue";
import { apiJson, getSession, login, logout, NotLoggedIn, setActiveShot, type ActiveShot } from "./session";

const PIPELINE_KEY = "pipelineState";
const ACTIVITY_KEY = "recentActivity";
const DISCOVERY_KEY = "discoveryEnabled";
const DISCOVERY_QUEUE_KEY = "discoveryQueue";

// ---------------------------------------------------------------- activity log (popup)

function log(label: string): void {
  console.log("[faible]", label);
  void locked(async () => {
    const s = await chrome.storage.local.get(ACTIVITY_KEY);
    const list = ((s[ACTIVITY_KEY] as { at: string; label: string }[] | undefined) ?? []).slice(0, 29);
    await chrome.storage.local.set({ [ACTIVITY_KEY]: [{ at: new Date().toISOString(), label }, ...list] });
  });
}

function describe(e: FaibleEvent): string | null {
  const p = e.payload;
  const job = typeof p.service_job_id === "string" ? p.service_job_id.slice(0, 10) : "—";
  switch (e.type) {
    case "generation.requested":
      return `${p.service}: ${p.kind} ${p.model_id ?? ""} ${job} (${p.status})${p.credits_cost ? `, ${p.credits_cost} credits` : ""}${p.shot_id ? "" : " — UNASSIGNED"}`;
    case "generation.status":
      return `${p.service}: ${job} → ${p.status}`;
    case "generation.completed":
      return `${p.service}: ${job} completed`;
    case "asset.location_seen":
      return `${p.service}: upload ${String(p.service_asset_id).slice(0, 10)} = sha256 ${String(p.sha256).slice(0, 10)}…`;
    case "download.observed":
      return `download ${p.filename} ${p.sha256 ? `sha256 ${String(p.sha256).slice(0, 10)}… via ${p.hash_method}` : "UNHASHED"}`;
    case "adapter.unknown_payload":
      return `${p.service}: unknown payload at ${p.url}`;
    default:
      return null;
  }
}

// ---------------------------------------------------------------- pipeline

async function withPipeline<T>(fn: (state: PipelineState) => T): Promise<T> {
  return locked(async () => {
    const s = await chrome.storage.local.get(PIPELINE_KEY);
    const state = { ...emptyState(), ...((s[PIPELINE_KEY] as PipelineState | undefined) ?? {}) };
    const result = fn(state);
    pruneState(state);
    await chrome.storage.local.set({ [PIPELINE_KEY]: state });
    return result;
  });
}

async function emit(events: FaibleEvent[]): Promise<void> {
  for (const e of events) {
    const label = describe(e);
    if (label) log(label);
  }
  await enqueueEvents(events);
}

async function onCapture(record: CaptureRecord, signedUrls: Record<string, string>): Promise<void> {
  void mirrorToDiscovery(record);
  if (record.transport === "blob") {
    await recordBlob(record);
    return;
  }
  const adapter = adapterFor(record.service);
  if (!adapter) return;
  const url = normalizeUrl(record.url);
  let captures = adapter.parse(record);
  // Only matched endpoints can be "unknown"; everything else unparsed is simply irrelevant.
  if (captures === null && !adapter.patterns.some((p) => p.test(url))) captures = [];
  // Safety net: the page hashed a file this request uploaded, but no adapter turned it into
  // an upload capture. Report it — a missed upload is a broken link in the chain.
  const uploads = captures?.some((c) => c.kind === "upload_bytes" || c.kind === "asset_location" || c.kind === "upload_slot");
  if (bodyFileSha(record.request_body) && !uploads && (record.status ?? 0) < 400) captures = null;
  if (captures !== null && captures.length === 0) return;

  const session = await getSession();
  const result = await withPipeline((state) =>
    plan(record, captures, state, {
      activeShotId: session?.activeShot?.id ?? null,
      signedUrls,
      newId: () => crypto.randomUUID(),
    }),
  );
  await emit(result.events);
  for (const job of result.hashJobs) void hashOutput(job.output, "completion", log);
}

// ---------------------------------------------------------------- downloads

function serviceForDownload(item: chrome.downloads.DownloadItem): string {
  for (const u of [item.url, item.finalUrl, item.referrer]) {
    if (!u) continue;
    try {
      const svc = serviceForHost(new URL(u.startsWith("blob:") ? u.slice(5) : u).hostname);
      if (svc !== "unknown") return svc;
    } catch {
      /* ignore */
    }
  }
  return "unknown";
}

chrome.downloads.onChanged.addListener((delta) => {
  if (delta.state?.current !== "complete") return;
  void (async () => {
    const [item] = await chrome.downloads.search({ id: delta.id });
    if (!item || item.byExtensionId === chrome.runtime.id) return;
    const service = serviceForDownload(item);
    if (service === "unknown") return; // not a service download: none of our business
    const refetch = await refetchDownload(item);
    const pipeline = await withPipeline((s) => s);
    const fp = await fingerprintDownload(item, service, refetch, pipeline, log);
    if (fp.agree === false) log(`download ${item.filename.split(/[\\/]/).pop()}: HASHES DISAGREE`);
    const occurredAt = new Date(item.endTime ?? Date.now()).toISOString();
    if (fp.job_id) {
      const jobId = fp.job_id;
      const missed = await withPipeline((s) =>
        completedFromDownload(s, service, jobId, redactUrl(item.finalUrl || item.url), item.mime || null, occurredAt, () => crypto.randomUUID()),
      );
      if (missed) {
        log(`${service}: ${jobId.slice(0, 10)} completion recorded from its download (push message was missed)`);
        await emit([missed]);
      }
    }
    await emit([
      {
        event_id: crypto.randomUUID(),
        type: "download.observed",
        occurred_at: occurredAt,
        evidence: fp.evidence ?? "observed",
        payload: {
          download_id: crypto.randomUUID(),
          filename: item.filename.split(/[\\/]/).pop() ?? "",
          service,
          url: redactUrl(item.finalUrl || item.url),
          bytes: item.fileSize > 0 ? item.fileSize : null,
          mime: item.mime || null,
          sha256: fp.sha256,
          hash_method: fp.method,
        },
      },
    ]);
  })();
});

// ---------------------------------------------------------------- discovery mirror (Phase 0 tool)

async function mirrorToDiscovery(record: CaptureRecord): Promise<void> {
  const size = await locked(async () => {
    const s = await chrome.storage.local.get([DISCOVERY_KEY, DISCOVERY_QUEUE_KEY]);
    if (!s[DISCOVERY_KEY]) return 0;
    const q = ((s[DISCOVERY_QUEUE_KEY] as CaptureRecord[] | undefined) ?? []).concat(record).slice(-2000);
    await chrome.storage.local.set({ [DISCOVERY_QUEUE_KEY]: q });
    return q.length;
  });
  if (size >= 20) void flushDiscovery();
}

async function flushDiscovery(): Promise<void> {
  const s = await chrome.storage.local.get(DISCOVERY_QUEUE_KEY);
  const q = (s[DISCOVERY_QUEUE_KEY] as CaptureRecord[] | undefined) ?? [];
  if (q.length === 0) return;
  try {
    const res = await fetch(`${SERVER_URL}/discovery/capture`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ records: q }),
    });
    if (res.ok) {
      // Drop only what was sent; records may have arrived meanwhile.
      await locked(async () => {
        const now = await chrome.storage.local.get(DISCOVERY_QUEUE_KEY);
        const rest = ((now[DISCOVERY_QUEUE_KEY] as CaptureRecord[] | undefined) ?? []).slice(q.length);
        await chrome.storage.local.set({ [DISCOVERY_QUEUE_KEY]: rest });
      });
    }
  } catch {
    /* retried on the next alarm */
  }
}

chrome.alarms.create("faible-flush", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "faible-flush") return;
  void flush();
  void flushDiscovery();
});

// ---------------------------------------------------------------- messages (relay, panel, popup)

interface Msg {
  type: string;
  record?: CaptureRecord;
  email?: string;
  password?: string;
  shot?: ActiveShot | null;
  projectId?: string;
  sha256s?: string[];
  shotId?: string;
  enabled?: boolean;
}

async function state(): Promise<Record<string, unknown>> {
  const session = await getSession();
  const q = await queueState();
  const s = await chrome.storage.local.get([ACTIVITY_KEY, DISCOVERY_KEY]);
  return {
    loggedIn: !!session,
    user: session?.user ?? null,
    activeShot: session?.activeShot ?? null,
    queue: q,
    activity: s[ACTIVITY_KEY] ?? [],
    discovery: !!s[DISCOVERY_KEY],
    server: SERVER_URL,
  };
}

async function handle(msg: Msg): Promise<unknown> {
  switch (msg.type) {
    case "state":
      return state();
    case "login":
      await login(msg.email ?? "", msg.password ?? "");
      scheduleFlush(100);
      return state();
    case "logout":
      await logout();
      return state();
    case "projects":
      return apiJson("/projects");
    case "tree":
      return apiJson(`/projects/${msg.projectId}/tree`);
    case "setActiveShot":
      await setActiveShot(msg.shot ?? null);
      return state();
    case "captures": {
      const c = await apiJson<{ unassigned_count: number }>("/me/captures?limit=10");
      void chrome.action.setBadgeText({ text: c.unassigned_count ? String(c.unassigned_count) : "" });
      return c;
    }
    case "assign":
      return apiJson("/links", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          links: (msg.sha256s ?? []).map((sha256) => ({
            sha256,
            target_type: "shot",
            target_id: msg.shotId,
            reason: "assigned in extension",
          })),
        }),
      });
    case "flush":
      await flush();
      return state();
    case "setDiscovery":
      await chrome.storage.local.set({ [DISCOVERY_KEY]: !!msg.enabled });
      return state();
    case "reset":
      await clearQueue();
      await clearAssetState();
      await chrome.storage.local.set({ [PIPELINE_KEY]: emptyState(), [ACTIVITY_KEY]: [] });
      return state();
    default:
      throw new Error(`unknown message ${msg.type}`);
  }
}

chrome.runtime.onMessage.addListener((msg: Msg, _sender, reply) => {
  if (msg.type === "capture" && msg.record) {
    // Signed output URLs stay inside the extension: detach them before anything is stored.
    const { signed_urls: signed = {}, ...record } = msg.record;
    void onCapture(record, signed);
    return false;
  }
  handle(msg).then(
    (value) => reply({ ok: true, value }),
    (err: unknown) =>
      reply({
        ok: false,
        error: err instanceof NotLoggedIn ? "not logged in" : err instanceof Error ? err.message : String(err),
      }),
  );
  return true;
});
