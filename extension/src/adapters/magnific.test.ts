import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { CaptureRecord } from "../shared/capture";
import { magnific } from "./magnific";
import type { Capture, Completed, Requested, Status } from "./types";

const fixture = (name: string): CaptureRecord =>
  JSON.parse(readFileSync(join(__dirname, "fixtures/magnific", name), "utf8")) as CaptureRecord;
const parse = (name: string) => magnific.parse(fixture(name));
const one = <T extends Capture>(caps: Capture[] | null): T => {
  expect(caps).toHaveLength(1);
  return caps![0] as T;
};

describe("magnific adapter", () => {
  it("image render → requested with prompt, seed, model, credits and the creation reference", () => {
    const r = one<Requested>(parse("render-v4.json"));
    expect(r).toMatchObject({
      job_id: "iGX0Hoj3uK",
      activity_kind: "generate",
      model_id: "seedream-5-pro",
      seed: "249676",
      credits_cost: "100",
      credits_currency: "magnific",
      evidence: "service_reported",
      inputs: [{ role: "reference", service_asset_id: "Syni0eBUb8" }],
      service_account: { workspace_id: "cf223cc5-b976-4479-8f8a-e279121e5648", service_user_id: "225502283" },
    });
    expect(r.prompt).toMatch(/^in @img1/);
    expect(r.parameters).not.toHaveProperty("request_token");
  });

  it("upscale → requested (upscale) with the source creation as input", () => {
    expect(one<Requested>(parse("upscale.json"))).toMatchObject({
      job_id: "nVvBer2YQD",
      activity_kind: "upscale",
      model_id: "enhance-magnific-creative",
      credits_cost: "1080",
      inputs: [{ role: "source", service_asset_id: "4RDY3FH9Aa" }],
    });
  });

  it("a 422-rejected upscale is recorded as failed with no job and no credits", () => {
    expect(one<Requested>(parse("upscale-rejected-422.json"))).toMatchObject({
      job_id: null,
      status: "failed",
      credits_cost: null,
      activity_kind: "upscale",
    });
  });

  it("video generate → requested with the clip prompt and reference", () => {
    expect(one<Requested>(parse("video-generate.json"))).toMatchObject({
      job_id: "iGXTTxa3uK",
      model_id: "kling-omni3",
      prompt: "an animation of the poster @img1 ",
      credits_cost: "575",
      inputs: [{ role: "reference", service_asset_id: "gOP30uFSXO" }],
    });
  });

  it("Pusher: processing → running, completed → output (URL redacted in the log)", () => {
    expect(one<Status>(parse("ws-creation-created.json"))).toMatchObject({ job_id: "iGX0Hoj3uK", status: "running" });
    expect(one<Status>(parse("ws-video-processing.json"))).toMatchObject({ job_id: "iGXTTxa3uK", status: "running" });
    const c = one<Completed>(parse("ws-creation-completed.json"));
    expect(c.outputs[0]).toMatchObject({ service_asset_id: "iGX0Hoj3uK", media_type: "image" });
    expect(c.outputs[0]!.url).toMatch(/render\.png\?token=\[REDACTED\]$/);
  });

  it("handles Pusher data already parsed to an object", () => {
    const rec = fixture("ws-creation-completed.json");
    const body = rec.response_body as { data: unknown };
    body.data = JSON.parse(body.data as string);
    expect(magnific.parse(rec)).toHaveLength(1);
  });

  it("ignores deleted events, cost simulation, the tti pre-flight and soft delete", () => {
    for (const f of ["ws-creation-deleted.json", "simulate-generation.json", "start-tti-v2.json", "soft-delete.json"]) {
      expect(parse(f)).toEqual([]);
    }
  });

  it("reference upload → the hashed file is known as that creation", () => {
    const rec = fixture("upload-reference.json");
    const form = (rec.request_body as { __formdata: { file: Record<string, unknown> } }).__formdata;
    form.file.__sha256 = "b71d14358389a5547da8b5ebf040a56e29aae6c62c965060f65d0ee1800d9850";
    expect(one<Capture>(magnific.parse(rec))).toMatchObject({
      kind: "asset_location",
      service_asset_id: "CqUjWZfEEy",
      sha256: "b71d14358389a5547da8b5ebf040a56e29aae6c62c965060f65d0ee1800d9850",
    });
    expect(parse("upload-reference.json")).toEqual([]); // captured before hashing existed
  });

  it("an unexpected render shape is unparseable (→ adapter.unknown_payload)", () => {
    const rec = fixture("render-v4.json");
    rec.response_body = { new: "shape" };
    expect(magnific.parse(rec)).toBeNull();
  });

  it("recovers the creation identifier from a download file name", () => {
    expect(magnific.jobIdFromFilename(String(fixture("download.json").extra.filename))).toBe("YMaBkskWeC");
    expect(magnific.jobIdFromFilename("render.png")).toBeNull();
  });
});
