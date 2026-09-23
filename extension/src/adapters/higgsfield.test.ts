import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { CaptureRecord } from "../shared/capture";
import { higgsfield, parseSse, urlKey } from "./higgsfield";
import type { Capture, Completed, Requested, Status } from "./types";

const fixture = (name: string): CaptureRecord =>
  JSON.parse(readFileSync(join(__dirname, "fixtures/higgsfield", name), "utf8")) as CaptureRecord;
const parse = (name: string) => higgsfield.parse(fixture(name));
const one = <T extends Capture>(caps: Capture[] | null): T => {
  expect(caps).toHaveLength(1);
  return caps![0] as T;
};

describe("higgsfield adapter", () => {
  it("image submit → requested with prompt, model, credits, service account", () => {
    const r = one<Requested>(parse("submit-image.json"));
    expect(r).toMatchObject({
      kind: "requested",
      job_id: "5fb1b25a-318a-4242-b4cb-6dc5061ca17b",
      activity_kind: "generate",
      model_id: "nano_banana_flash",
      status: "queued",
      credits_cost: "150",
      credits_currency: "higgsfield",
      evidence: "service_reported",
      service_account: { workspace_id: "24df8a59-35af-44c8-90b5-53c112f69c2e", service_user_id: "user_36mMlkuUFLoVEvOM0icTtFFDdlE" },
    });
    expect(r.prompt).toMatch(/^Master wide shot/);
    expect(r.parameters).toMatchObject({ aspect_ratio: "16:9" });
    expect(r.parameters).not.toHaveProperty("prompt");
  });

  it("video submit → requested with the reference images as inputs", () => {
    const r = one<Requested>(parse("submit-video.json"));
    expect(r).toMatchObject({ model_id: "cinematic_studio_video_4_0", credits_cost: "1200" });
    expect(r.inputs.length).toBe(10);
    expect(r.inputs[0]).toMatchObject({ role: "image", service_asset_id: "3e7ceabb-07a5-4899-94e0-b5813e9db3f8" });
  });

  it("a rejected submit is still recorded, as failed with no job", () => {
    const rec = fixture("submit-image.json");
    rec.status = 402;
    rec.response_body = { detail: "not enough credits" };
    expect(one<Requested>(higgsfield.parse(rec))).toMatchObject({ job_id: null, status: "failed", evidence: "observed" });
  });

  it("an unexpected submit shape is unparseable (→ adapter.unknown_payload)", () => {
    const rec = fixture("submit-image.json");
    rec.response_body = { something: "new" };
    expect(higgsfield.parse(rec)).toBeNull();
  });

  it("status polling → status captures with mapped values", () => {
    expect(one<Status>(parse("status-batch.json"))).toMatchObject({
      kind: "status",
      job_id: "253edf08-5200-4819-9677-5f98df466685",
      status: "running",
    });
  });

  it("cancel → cancelled", () => {
    expect(one<Status>(parse("cancel.json"))).toMatchObject({
      job_id: "0ca6024d-4528-43c9-bb34-2244cec10115",
      status: "cancelled",
    });
  });

  it("completed job detail → completed with the raw output", () => {
    const c = one<Completed>(parse("job-detail-image-completed.json"));
    expect(c).toMatchObject({ job_id: "5fb1b25a-318a-4242-b4cb-6dc5061ca17b", source: "job_detail" });
    expect(c.outputs[0]).toMatchObject({ index: 0, service_asset_id: c.job_id, media_type: "image" });
    expect(c.outputs[0]!.url).toMatch(/cloudfront\.net\/.+5fb1b25a-318a-4242-b4cb-6dc5061ca17b\.png$/);
  });

  it("history → completed captures for finished jobs", () => {
    const caps = parse("history.json")!;
    expect(caps.length).toBeGreaterThan(0);
    for (const c of caps) if (c.kind === "completed") expect(c.outputs[0]!.url).toContain(c.job_id);
  });

  it("job_completed notification → completed with the final cost, once despite repeats", () => {
    const c = one<Completed>(parse("notification-completed.json"));
    expect(c).toMatchObject({ job_id: "5d6cda69-d39a-4872-80ab-c2b76788d82f", credits_cost: "8400", source: "notification" });
  });

  it("failed notification → failed status carrying the refund (job_cost 0)", () => {
    expect(one<Status>(parse("notification-failed.json"))).toMatchObject({
      job_id: "78945fb1-2e56-44a2-8f4f-5e621545ee78",
      status: "failed",
      credits_cost: "0",
    });
  });

  it("upload: media batch gives the slot, the S3 PUT carries the page-computed hash", () => {
    const slot = one<Capture>(parse("media-batch.json"));
    expect(slot).toMatchObject({ kind: "upload_slot", service_asset_id: "314e36aa-f94a-471d-81e3-053a93595cdb" });
    const put = fixture("s3-upload-put.json");
    put.request_body = { __blob: true, size: 1674058, type: "image/png", __sha256: "b".repeat(64) };
    const bytes = one<Capture>(higgsfield.parse(put));
    expect(bytes).toMatchObject({ kind: "upload_bytes", sha256: "b".repeat(64) });
    // The two join on the URL without its presigned query.
    expect(slot.kind === "upload_slot" && bytes.kind === "upload_bytes" && slot.upload_url === bytes.upload_url).toBe(true);
    expect(urlKey("https://s3/x.png?X-Amz-Signature=1")).toBe("https://s3/x.png");
  });

  it("image upscale on the layer service → requested (upscale), then completed with a jobId alias", () => {
    expect(one<Requested>(parse("layer-upscale-submit.json"))).toMatchObject({
      job_id: "0844f6e1-fa8f-4bb0-9e82-9f9299ac1852",
      activity_kind: "upscale",
      model_id: "topaz_image",
    });
    const caps = parse("layer-generations-list.json")!;
    const done = caps.find((c): c is Completed => c.kind === "completed")!;
    expect(done).toMatchObject({ job_id: "3e2085ef-ad2e-4a21-9047-8462c66a0390", alias_job_ids: ["3c517970-d937-4b05-8cb1-6819f77e14ed"] });
    expect(caps.find((c) => c.kind === "status")).toMatchObject({ status: "running" });
  });

  it("ignores unrelated endpoints", () => {
    const rec = fixture("status-batch.json");
    rec.url = "https://fnf-api-gw.higgsfield.ai/fnf/workspaces/wallet";
    expect(higgsfield.parse(rec)).toEqual([]);
  });

  it("recovers the job id from a downloaded file name", () => {
    const dl = fixture("download-blob.json");
    expect(higgsfield.jobIdFromFilename(String(dl.extra.filename))).toBe("253edf08-5200-4819-9677-5f98df466685");
    expect(higgsfield.jobIdFromFilename("my-render.mp4")).toBeNull();
  });

  it("parses multi-event SSE chunks", () => {
    expect(parseSse('event: ping\ndata: {"t":1}\n\nevent: x\ndata: {"a":2}\n\n')).toEqual([
      { event: "ping", data: { t: 1 } },
      { event: "x", data: { a: 2 } },
    ]);
  });
});
