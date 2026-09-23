import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { CaptureRecord } from "../shared/capture";
import { magnific } from "./magnific";

const fixture = (name: string): CaptureRecord =>
  JSON.parse(readFileSync(join(__dirname, "fixtures/magnific", name), "utf8")) as CaptureRecord;

describe("magnific adapter", () => {
  it("takes the creation identifier from a render submit", () => {
    const obs = magnific.observe(fixture("render-v4.json"));
    expect(obs).toEqual({ submittedJobIds: ["iGX0Hoj3uK"], outputs: [] });
  });

  it("takes the creation identifier from an upscale submit", () => {
    expect(magnific.observe(fixture("upscale.json")).submittedJobIds).toEqual(["nVvBer2YQD"]);
  });

  it("ignores a rejected (422) upscale submit", () => {
    expect(magnific.observe(fixture("upscale-rejected-422.json"))).toEqual({ submittedJobIds: [], outputs: [] });
  });

  it("takes creation identifiers from a video submit", () => {
    expect(magnific.observe(fixture("video-generate.json")).submittedJobIds).toEqual(["iGXTTxa3uK"]);
  });

  it("ignores a video still processing", () => {
    expect(magnific.observe(fixture("ws-video-processing.json")).outputs).toEqual([]);
  });

  it("extracts the output from a creation.updated completed Pusher event", () => {
    const [o] = magnific.observe(fixture("ws-creation-completed.json")).outputs;
    expect(o).toMatchObject({ service: "magnific", job_id: "iGX0Hoj3uK", media_type: "image", source: "notification" });
    // Redacted in the log; the worker swaps in the signed original via signed_urls.
    expect(o!.url).toMatch(/^https:\/\/pikaso\.cdnpk\.net\/private\/production\/\d+\/render\.png\?token=\[REDACTED\]$/);
  });

  it("handles Pusher data already parsed to an object", () => {
    const rec = fixture("ws-creation-completed.json");
    const body = rec.response_body as { data: unknown };
    body.data = JSON.parse(body.data as string);
    expect(magnific.observe(rec).outputs).toHaveLength(1);
  });

  it("ignores created/processing and deleted events", () => {
    expect(magnific.observe(fixture("ws-creation-created.json")).outputs).toEqual([]);
    expect(magnific.observe(fixture("ws-creation-deleted.json")).outputs).toEqual([]);
  });

  it("ignores cost simulation and the tti pre-flight", () => {
    for (const f of ["simulate-generation.json", "start-tti-v2.json", "soft-delete.json"]) {
      expect(magnific.observe(fixture(f))).toEqual({ submittedJobIds: [], outputs: [] });
    }
  });

  it("recovers the creation identifier from a download file name", () => {
    const dl = fixture("download.json");
    expect(magnific.jobIdFromFilename(String(dl.extra.filename))).toBe("YMaBkskWeC");
    expect(magnific.jobIdFromFilename("render.png")).toBeNull();
  });
});
