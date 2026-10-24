import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FaibleEvent } from "./pipeline";

const mocks = vi.hoisted(() => ({ session: { user: { id: "artist-a" } } as { user: { id: string } } | null, api: vi.fn() }));
vi.mock("./session", () => ({ getSession: async () => mocks.session, api: mocks.api, NotLoggedIn: class extends Error {} }));
import { enqueueEvents, flush, takeBatch } from "./queue";

let storage: Record<string, unknown>;
beforeEach(() => {
  vi.useFakeTimers();
  storage = {};
  mocks.session = { user: { id: "artist-a" } };
  mocks.api.mockReset();
  vi.stubGlobal("chrome", { storage: { local: {
    get: async () => storage,
    set: async (values: Record<string, unknown>) => { Object.assign(storage, values); },
  } } });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
const event = (id: string): FaibleEvent => ({ event_id: id, type: "generation.requested", occurred_at: "2026-10-13T00:00:00Z", evidence: "observed", payload: {} });

describe("queue artist attribution", () => {
  it("holds another artist's events and strips local ownership before ingest", async () => {
    await enqueueEvents([event("a")]);
    mocks.session = { user: { id: "artist-b" } };
    await enqueueEvents([event("b")]);
    mocks.api.mockResolvedValue(new Response(JSON.stringify({ results: [{ event_id: "b", status: "accepted" }] })));
    await flush();
    const body = JSON.parse(mocks.api.mock.calls[0]![1].body) as { events: FaibleEvent[] };
    expect(body.events.map((item) => item.event_id)).toEqual(["b"]);
    expect(body.events[0]).not.toHaveProperty("owner_user_id");
    expect(mocks.api.mock.calls[0]![2]).toBe("artist-b");
    expect((storage.eventQueue as FaibleEvent[]).map((item) => item.event_id)).toEqual(["a"]);
    mocks.session = { user: { id: "artist-a" } };
    mocks.api.mockResolvedValue(new Response(JSON.stringify({ results: [{ event_id: "a", status: "accepted" }] })));
    await flush();
    expect(storage.eventQueue).toEqual([]);
  });
});

describe("batch size", () => {
  it("stops a batch at the JSON budget but always sends at least one event", () => {
    const big = (id: string) => ({ ...event(id), payload: { raw: "x".repeat(400) } });
    const events = ["a", "b", "c", "d"].map(big);
    expect(JSON.stringify(events[0]).length).toBeLessThan(550); // ~510 chars each
    expect(takeBatch(events, 1100).map((e) => e.event_id)).toEqual(["a", "b"]);
    expect(takeBatch(events, 10).map((e) => e.event_id)).toEqual(["a"]);
    expect(takeBatch(events, 1_000_000, 3)).toHaveLength(3);
  });
});
