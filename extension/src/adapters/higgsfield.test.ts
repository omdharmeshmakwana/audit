import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { CaptureRecord } from "../shared/capture";
import { higgsfield, parseSse } from "./higgsfield";

const fixture = (name: string): CaptureRecord =>
  JSON.parse(readFileSync(join(__dirname, "fixtures/higgsfield", name), "utf8")) as CaptureRecord;

describe("higgsfield adapter", () => {
  it("takes submitted job ids from an image submit response", () => {
    const obs = higgsfield.observe(fixture("submit-image.json"));
    expect(obs.submittedJobIds).toEqual(["5fb1b25a-318a-4242-b4cb-6dc5061ca17b"]);
    expect(obs.outputs).toEqual([]);
  });

  it("takes submitted job ids from a video submit response", () => {
    expect(higgsfield.observe(fixture("submit-video.json")).submittedJobIds).toHaveLength(1);
  });

  it("extracts the raw output from a completed job detail", () => {
    const [o] = higgsfield.observe(fixture("job-detail-image-completed.json")).outputs;
    expect(o).toMatchObject({
      job_id: "5fb1b25a-318a-4242-b4cb-6dc5061ca17b",
      media_type: "image",
      source: "job_detail",
    });
    expect(o!.url).toMatch(/^https:\/\/d8j0ntlcm91z4\.cloudfront\.net\/.+5fb1b25a-318a-4242-b4cb-6dc5061ca17b\.png$/);
  });

  it("extracts outputs from job history", () => {
    const outs = higgsfield.observe(fixture("history.json")).outputs;
    expect(outs.length).toBeGreaterThan(0);
    for (const o of outs) expect(o.url).toContain(o.job_id);
  });

  it("extracts result_url from a job_completed notification, once despite repeats", () => {
    const outs = higgsfield.observe(fixture("notification-completed.json")).outputs;
    expect(outs).toEqual([
      expect.objectContaining({
        job_id: "5d6cda69-d39a-4872-80ab-c2b76788d82f",
        media_type: "video",
        source: "notification",
      }),
    ]);
  });

  it("ignores failed jobs (no output)", () => {
    expect(higgsfield.observe(fixture("notification-failed.json")).outputs).toEqual([]);
  });

  it("ignores status polling", () => {
    const obs = higgsfield.observe(fixture("status-batch.json"));
    expect(obs.outputs).toEqual([]);
    expect(obs.submittedJobIds).toEqual([]);
  });

  it("recovers the job id from a downloaded file name", () => {
    const dl = fixture("download-blob.json");
    expect(higgsfield.jobIdFromFilename(String(dl.extra.filename))).toBe("253edf08-5200-4819-9677-5f98df466685");
    expect(higgsfield.jobIdFromFilename("my-render.mp4")).toBeNull();
  });

  it("parses multi-event SSE chunks", () => {
    const events = parseSse('event: ping\ndata: {"t":1}\n\nevent: x\ndata: {"a":2}\n\n');
    expect(events).toEqual([
      { event: "ping", data: { t: 1 } },
      { event: "x", data: { a: 2 } },
    ]);
  });
});
