// @vitest-environment happy-dom
// @vitest-environment-options { "url": "https://higgsfield.ai/usage" }
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CaptureRecord } from "../shared/capture";
import { mountHistory, snapshotPage, usageResponses } from "./history";

const JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuv";
const history = JSON.parse(readFileSync(join(__dirname, "../adapters/fixtures/higgsfield/history.json"), "utf8")) as CaptureRecord;
const record = (over: Partial<CaptureRecord>): CaptureRecord => ({ service: "higgsfield", transport: "fetch", captured_at: "2026-10-23T09:00:00.000Z", request_headers: {}, response_headers: {}, extra: {}, ...over });

let sent: Record<string, unknown>[];
let listener: (msg: Record<string, unknown>, sender: unknown, reply: (v: unknown) => void) => boolean;
beforeEach(() => {
  sent = [];
  vi.stubGlobal("chrome", {
    runtime: {
      sendMessage: vi.fn(async (msg: Record<string, unknown>) => {
        sent.push(msg);
        return { ok: true, value: { sent: true } };
      }),
      onMessage: { addListener: (fn: typeof listener) => (listener = fn) },
    },
  });
});
const ask = (msg: Record<string, unknown>) => new Promise<Record<string, unknown>>((resolve) => listener(msg, {}, resolve as (v: unknown) => void));

describe("in-page history collection (ported exporter content script)", () => {
  it("holds generations until the Higgsfield login is known, then sends them under it", async () => {
    const h = mountHistory();
    h.observe(history);
    expect(sent.filter((m) => m.type === "history")).toEqual([]);
    expect((await ask({ type: "historyStatus" })).pendingCount).toBeGreaterThan(0);
    h.observe(record({ transport: "account", extra: { account: { id: "user_1", email: "Subscriptions.9@faiblemedia.com", name: null, username: null, source: "clerk" } } }));
    const batch = sent.find((m) => m.type === "history")!;
    expect(batch.accountKey).toBe("subscriptions.9@faiblemedia.com");
    expect((batch.items as unknown[]).length).toBe((history.response_body as { jobs: unknown[] }).jobs.length);
    expect(sent.some((m) => m.type === "hfAccount")).toBe(true);
    // An API guess never overrides the Clerk login; a manual label does.
    h.observe(record({ url: "https://fnf-api-gw.higgsfield.ai/user", response_body: { user: { id: "u9", email: "other@x.com" } } }));
    expect(((await ask({ type: "historyStatus" })).account as { key: string }).key).toBe("subscriptions.9@faiblemedia.com");
    await ask({ type: "historySetAccount", label: "Team A" });
    expect(((await ask({ type: "historyStatus" })).account as { key: string }).key).toBe("user:Team A");
  });

  it("saves a usage page: this page's responses plus its tables and text, redacted", async () => {
    document.title = "Usage";
    document.body.innerHTML = `<table><tr><th>Date</th><th>Credits</th></tr><tr><td>23 Oct</td><td>108 ${JWT}</td></tr></table><p>Balance 73088</p>`;
    const h = mountHistory();
    h.observe(record({ transport: "account", extra: { account: { id: "u1", email: "s@x.com", name: null, username: null, source: "clerk" } } }));
    h.observe(record({ url: "https://fnf-api-gw.higgsfield.ai/fnf/workspaces/wallet", response_body: { total_credits: 73088 } }));
    const res = await ask({ type: "historyCaptureUsage" });
    expect(res).toMatchObject({ ok: true, responses: 1, tables: 1, sent: true });
    const usage = sent.find((m) => m.type === "usage")! as { capture: { tables: string[][][]; text: string; responses: { url: string }[]; pageUrl: string } };
    expect(usage.capture.tables).toEqual([[["Date", "Credits"], ["23 Oct", "108 [REDACTED]"]]]);
    expect(usage.capture.text).not.toContain(JWT);
    expect(usage.capture.responses[0]!.url).toContain("/wallet");
  });

  it("refuses a usage capture before any login is known", async () => {
    mountHistory();
    expect(await ask({ type: "historyCaptureUsage" })).toMatchObject({ ok: false });
  });

  it("keeps the newest usage responses within the size budget", () => {
    const r = (i: number, path: string) => ({ url: `https://fnf-api-gw.higgsfield.ai/x${i}`, pagePath: path, capturedAt: "", data: { pad: "x".repeat(100) } });
    const recent = [r(1, "/usage"), r(2, "/other"), r(3, "/usage"), r(4, "/usage")];
    const { responses, dropped } = usageResponses(recent, "/usage", 300);
    expect(responses.map((x) => x.url.slice(-2))).toEqual(["x3", "x4"]); // newest kept, time order
    expect(dropped).toBe(1); // x1 over budget; x2 is another page's (not wanted at all)
    expect(snapshotPage(document).text.length).toBeLessThanOrEqual(500_000);
  });
});
