// @vitest-environment happy-dom
// @vitest-environment-options { "url": "https://higgsfield.ai/create" }

import { createHash } from "node:crypto";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { MSG_TAG, type CaptureRecord } from "../shared/capture";

const posted: CaptureRecord[] = [];
const JOB = { id: "job_1", status: "queued", access_token: "should-not-leak" };
const SIGNED = "https://pikaso.cdnpk.net/private/production/1/render.png?token=exp=179~hmac=deadbeef";
const SIGNED_BODY = { creation: { identifier: "iGX0Hoj3uK", url: SIGNED } };

beforeAll(async () => {
  // Stand-in for the network, installed before the observer patches fetch.
  window.fetch = vi.fn(async (input: RequestInfo | URL) =>
    new Response(JSON.stringify(String(input).includes("/creation") ? SIGNED_BODY : JOB), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  ) as typeof fetch;
  vi.spyOn(window, "postMessage").mockImplementation(((msg: { [MSG_TAG]: boolean; record: CaptureRecord }) => {
    if (msg[MSG_TAG]) posted.push(msg.record);
  }) as typeof window.postMessage);
  await import("./main-world");
});

const settle = () => new Promise((r) => setTimeout(r, 20));

describe("page-world fetch observer", () => {
  it("returns the untouched response to the page", async () => {
    const res = await window.fetch("https://api.higgsfield.ai/jobs", {
      method: "POST",
      headers: { authorization: "Bearer supersecrettoken", "content-type": "application/json" },
      body: JSON.stringify({ prompt: "a red fox", seed: 7 }),
    });
    expect(await res.json()).toEqual(JOB);
  });

  it("relays a redacted capture record", async () => {
    await settle();
    const rec = posted.find((r) => r.url === "https://api.higgsfield.ai/jobs");
    expect(rec).toBeDefined();
    expect(rec!.service).toBe("higgsfield");
    expect(rec!.method).toBe("POST");
    expect(rec!.status).toBe(200);
    expect(rec!.request_body).toEqual({ prompt: "a red fox", seed: 7 });
    expect(rec!.request_headers.authorization).toBe("[REDACTED]");
    expect((rec!.response_body as Record<string, unknown>).id).toBe("job_1");
    expect(JSON.stringify(rec)).not.toContain("should-not-leak");
    expect(JSON.stringify(rec)).not.toContain("supersecrettoken");
  });

  it("hashes media blobs behind object URLs and returns the URL untouched (method 2)", async () => {
    posted.length = 0;
    const bytes = new TextEncoder().encode("fake mp4 bytes");
    const blobUrl = URL.createObjectURL(new Blob([bytes], { type: "video/mp4" }));
    expect(blobUrl).toMatch(/^blob:/);
    await settle();
    const rec = posted.find((r) => r.transport === "blob");
    const expected = createHash("sha256").update(bytes).digest("hex");
    expect(rec?.extra).toMatchObject({ blob_url: blobUrl, sha256: expected, type: "video/mp4", size: bytes.length });
  });

  it("does not hash non-media blobs", async () => {
    posted.length = 0;
    URL.createObjectURL(new Blob(["self.onmessage=()=>{}"], { type: "text/javascript" }));
    await settle();
    expect(posted.filter((r) => r.transport === "blob")).toHaveLength(0);
  });

  it("keeps signed output URLs out of the body but relays them to the worker", async () => {
    posted.length = 0;
    await window.fetch("https://www.magnific.com/app/api/creation");
    await settle();
    const rec = posted.find((r) => r.url?.endsWith("/creation"));
    const redacted = "https://pikaso.cdnpk.net/private/production/1/render.png?token=[REDACTED]";
    expect((rec!.response_body as typeof SIGNED_BODY).creation.url).toBe(redacted);
    expect(rec!.signed_urls).toEqual({ [redacted]: SIGNED });
    expect(JSON.stringify({ ...rec, signed_urls: undefined })).not.toContain("deadbeef");
  });

  it("ignores third-party telemetry", async () => {
    posted.length = 0;
    await window.fetch("https://o123.ingest.sentry.io/api/1/envelope/");
    await settle();
    expect(posted).toHaveLength(0);
  });
});
