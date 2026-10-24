// Event queue (spec §4.1): persisted in chrome.storage.local, flushed in batches to
// POST /api/v1/ingest/events, retried with exponential backoff. Ingest is idempotent on
// event_id, so re-sending after a crash is safe. Per-event results: accepted/duplicate are
// done; rejected/conflict are dropped and kept in a short list the popup shows (a rejected
// event is a bug or schema drift, and must not wedge the queue behind it).

import { locked } from "./lock";
import type { FaibleEvent } from "./pipeline";
import { api, getSession, NotLoggedIn } from "./session";

const QUEUE_KEY = "eventQueue";
const STATUS_KEY = "queueStatus";
const MAX_QUEUE = 20_000;
const BATCH = 100;
// History objects and saved usage pages are much larger than generation events: a batch
// also stops at this much JSON, so one POST never grows unbounded (always ≥ 1 event).
export const BATCH_MAX_CHARS = 8 * 1024 * 1024;

/** The next batch: up to BATCH events, within BATCH_MAX_CHARS of JSON. */
export function takeBatch<T>(events: T[], maxChars = BATCH_MAX_CHARS, max = BATCH): T[] {
  const out: T[] = [];
  let size = 0;
  for (const event of events) {
    if (out.length >= max) break;
    const n = JSON.stringify(event).length;
    if (out.length && size + n > maxChars) break;
    out.push(event);
    size += n;
  }
  return out;
}

export interface QueueStatus {
  sent: number;
  lastSentAt: string | null;
  lastError: string | null;
  dropped: number;
  rejected: { at: string; type: string; reason: string }[];
}

const emptyStatus = (): QueueStatus => ({ sent: 0, lastSentAt: null, lastError: null, dropped: 0, rejected: [] });

async function read(): Promise<{ queue: FaibleEvent[]; status: QueueStatus }> {
  const s = await chrome.storage.local.get([QUEUE_KEY, STATUS_KEY]);
  return {
    queue: (s[QUEUE_KEY] as FaibleEvent[] | undefined) ?? [],
    status: { ...emptyStatus(), ...((s[STATUS_KEY] as QueueStatus | undefined) ?? {}) },
  };
}

export async function queueState(): Promise<{ pending: number; status: QueueStatus }> {
  const { queue, status } = await read();
  const session = await getSession();
  return { pending: queue.filter((event) => event.owner_user_id === session?.user.id).length, status };
}

export function enqueueEvents(events: FaibleEvent[]): Promise<void> {
  if (events.length === 0) return Promise.resolve();
  return getSession().then((session) => locked(async () => {
    if (!session) return;
    const { queue, status } = await read();
    queue.push(...events.map((event) => ({ ...event, owner_user_id: event.owner_user_id ?? session.user.id })));
    while (queue.length > MAX_QUEUE) {
      queue.shift();
      status.dropped++;
    }
    await chrome.storage.local.set({ [QUEUE_KEY]: queue, [STATUS_KEY]: status });
    scheduleFlush(800);
  }));
}

let timer: ReturnType<typeof setTimeout> | null = null;
let backoff = 2000;

export function scheduleFlush(ms: number): void {
  if (timer) return;
  timer = setTimeout(() => {
    timer = null;
    void flush();
  }, ms);
}

interface IngestResult {
  event_id: string;
  status: "accepted" | "duplicate" | "conflict" | "rejected";
  reason: string | null;
}

export async function flush(): Promise<void> {
  const { queue } = await read();
  if (queue.length === 0) return;
  const session = await getSession();
  if (!session) return;
  // Legacy unowned events cannot safely be attributed after an account switch.
  const batch = takeBatch(queue.filter((event) => event.owner_user_id === session.user.id));
  if (!batch.length) return;
  let results: IngestResult[];
  try {
    const res = await api("/ingest/events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ events: batch.map((event) => { const clean = { ...event }; delete clean.owner_user_id; return clean; }) }),
    }, session.user.id);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
    results = ((await res.json()) as { results: IngestResult[] }).results;
  } catch (err) {
    await locked(async () => {
      const { status } = await read();
      status.lastError = err instanceof NotLoggedIn ? "not logged in — events are waiting" : String(err);
      await chrome.storage.local.set({ [STATUS_KEY]: status });
    });
    backoff = Math.min(backoff * 2, 60_000);
    scheduleFlush(err instanceof NotLoggedIn ? 30_000 : backoff);
    return;
  }
  backoff = 2000;
  await locked(async () => {
    const { queue: current, status } = await read();
    const done = new Set(batch.map((e) => e.event_id));
    const byId = new Map(batch.map((e) => [e.event_id, e]));
    for (const r of results) {
      if (r.status === "rejected" || r.status === "conflict") {
        status.rejected = [
          { at: new Date().toISOString(), type: byId.get(r.event_id)?.type ?? "?", reason: `${r.status}: ${r.reason ?? ""}` },
          ...status.rejected,
        ].slice(0, 20);
      }
    }
    status.sent += batch.length;
    status.lastSentAt = new Date().toISOString();
    status.lastError = null;
    await chrome.storage.local.set({
      [QUEUE_KEY]: current.filter((e) => !done.has(e.event_id)),
      [STATUS_KEY]: status,
    });
    if (current.some((event) => !done.has(event.event_id) && event.owner_user_id === session.user.id)) scheduleFlush(200);
  });
}

export function clearQueue(): Promise<void> {
  return locked(() => chrome.storage.local.set({ [QUEUE_KEY]: [], [STATUS_KEY]: emptyStatus() }));
}
