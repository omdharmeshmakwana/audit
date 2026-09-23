import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { higgsfield } from "../adapters/higgsfield";
import { magnific } from "../adapters/magnific";
import type { Adapter } from "../adapters/types";
import type { CaptureRecord } from "../shared/capture";
import { completedFromDownload, emptyState, plan, pruneState, type PipelineState, type PlanContext } from "./pipeline";

const fixture = (service: string, name: string): CaptureRecord =>
  JSON.parse(readFileSync(join(__dirname, "../adapters/fixtures", service, name), "utf8")) as CaptureRecord;

function run(adapter: Adapter, state: PipelineState, rec: CaptureRecord, ctx: Partial<PlanContext> = {}) {
  let n = 0;
  return plan(rec, adapter.parse(rec), state, {
    activeShotId: "11111111-1111-1111-1111-111111111111",
    signedUrls: {},
    newId: () => `id-${++n}-${Math.random()}`,
    ...ctx,
  });
}

describe("pipeline", () => {
  it("Higgsfield: submit → requested (tagged with the active shot); completion → completed + hash job", () => {
    const state = emptyState();
    const a = run(higgsfield, state, fixture("higgsfield", "submit-image.json"));
    expect(a.events.map((e) => e.type)).toEqual(["generation.requested"]);
    const req = a.events[0]!;
    expect(req.payload).toMatchObject({
      service: "higgsfield",
      service_job_id: "5fb1b25a-318a-4242-b4cb-6dc5061ca17b",
      shot_id: "11111111-1111-1111-1111-111111111111",
      credits_cost: "150",
    });
    expect(req.evidence).toBe("service_reported");
    expect(req.occurred_at).toBe(fixture("higgsfield", "submit-image.json").captured_at);

    const b = run(higgsfield, state, fixture("higgsfield", "job-detail-image-completed.json"));
    expect(b.events.map((e) => e.type)).toEqual(["generation.completed"]);
    expect(b.events[0]!.payload).toMatchObject({ service_job_id: "5fb1b25a-318a-4242-b4cb-6dc5061ca17b" });
    expect(b.hashJobs).toHaveLength(1);
    expect(b.hashJobs[0]!.output.fetch_url).toMatch(/cloudfront\.net/);

    // The same completion seen again (history, notification) is not re-emitted.
    const c = run(higgsfield, state, fixture("higgsfield", "job-detail-image-completed.json"));
    expect(c.events).toEqual([]);
    expect(c.hashJobs).toEqual([]);
  });

  it("does not record other artists' jobs (shared workspace), but remembers their outputs", () => {
    const state = emptyState();
    const status = run(higgsfield, state, fixture("higgsfield", "status-batch.json"));
    expect(status.events).toEqual([]);
    const done = run(higgsfield, state, fixture("higgsfield", "job-detail-image-completed.json"));
    expect(done.events).toEqual([]);
    expect(done.hashJobs).toEqual([]);
    expect(state.outputs["higgsfield:5fb1b25a-318a-4242-b4cb-6dc5061ca17b"]).toBeDefined(); // for filename matching
  });

  it("repeated statuses are emitted once; a failed job carries its refund", () => {
    const state = emptyState();
    state.submitted["higgsfield:78945fb1-2e56-44a2-8f4f-5e621545ee78"] = { activity_id: "a", at: "" };
    const first = run(higgsfield, state, fixture("higgsfield", "notification-failed.json"));
    expect(first.events.map((e) => e.payload)).toEqual([
      expect.objectContaining({ status: "failed", credits_cost: "0", service_job_id: "78945fb1-2e56-44a2-8f4f-5e621545ee78" }),
    ]);
    expect(run(higgsfield, state, fixture("higgsfield", "notification-failed.json")).events).toEqual([]);
  });

  it("Higgsfield upload: slot + hashed PUT → asset.location_seen for the media id", () => {
    const state = emptyState();
    run(higgsfield, state, fixture("higgsfield", "media-batch.json"));
    const put = fixture("higgsfield", "s3-upload-put.json");
    put.request_body = { __blob: true, size: 1, type: "image/png", __sha256: "b".repeat(64) };
    const r = run(higgsfield, state, put);
    expect(r.events).toHaveLength(1);
    expect(r.hashJobs).toEqual([
      expect.objectContaining({ method: "upload", output: expect.objectContaining({ service_asset_id: "314e36aa-f94a-471d-81e3-053a93595cdb" }) }),
    ]);
    expect(r.events[0]).toMatchObject({
      type: "asset.location_seen",
      evidence: "observed",
      payload: { sha256: "b".repeat(64), service: "higgsfield", service_asset_id: "314e36aa-f94a-471d-81e3-053a93595cdb" },
    });
  });

  it("Magnific: render → requested; completion hashes via the signed URL, logs only the redacted one", () => {
    const state = emptyState();
    run(magnific, state, fixture("magnific", "render-v4.json"));
    const ws = fixture("magnific", "ws-creation-completed.json");
    const redacted = "https://pikaso.cdnpk.net/private/production/5528616187/render.png?token=[REDACTED]";
    const signed = "https://pikaso.cdnpk.net/private/production/5528616187/render.png?token=exp=1~hmac=abc";
    const r = run(magnific, state, ws, { signedUrls: { [redacted]: signed } });
    expect(r.events.map((e) => e.type)).toEqual(["generation.completed"]);
    expect(JSON.stringify(r.events)).not.toContain("hmac=abc");
    expect(r.hashJobs[0]!.output).toMatchObject({ url: redacted, fetch_url: signed, service_asset_id: "iGX0Hoj3uK" });
  });

  it("a rejected submit is recorded as a failed activity with no job id", () => {
    const r = run(magnific, emptyState(), fixture("magnific", "upscale-rejected-422.json"));
    expect(r.events).toHaveLength(1);
    expect(r.events[0]!.payload).toMatchObject({ status: "failed", service_job_id: null, kind: "upscale" });
  });

  it("an unparseable response on a known endpoint → adapter.unknown_payload, once per 6 h", () => {
    const state = emptyState();
    const rec = fixture("magnific", "render-v4.json");
    rec.response_body = { brand: "new shape" };
    const first = run(magnific, state, rec);
    expect(first.events[0]).toMatchObject({ type: "adapter.unknown_payload", payload: { service: "magnific", method: "POST" } });
    expect((first.events[0]!.payload.sample as string).length).toBeLessThanOrEqual(2000);
    expect(run(magnific, state, rec).events).toEqual([]);
  });

  it("with no active shot the capture is stored unassigned", () => {
    const r = run(higgsfield, emptyState(), fixture("higgsfield", "submit-image.json"), { activeShotId: null });
    expect(r.events[0]!.payload.shot_id).toBeNull();
  });

  it("a download of an own job records a missed completion once; never for others' jobs", () => {
    const state = emptyState();
    run(magnific, state, fixture("magnific", "upscale.json")); // submitted nVvBer2YQD
    const id = () => "x";
    const e = completedFromDownload(state, "magnific", "nVvBer2YQD", "https://pikaso/r.png?token=[REDACTED]", "image/png", "2026-09-23T11:33:34Z", id);
    expect(e).toMatchObject({
      type: "generation.completed",
      evidence: "observed",
      payload: { service_job_id: "nVvBer2YQD", outputs: [{ service_asset_id: "nVvBer2YQD", media_type: "image" }] },
    });
    expect(completedFromDownload(state, "magnific", "nVvBer2YQD", "u", null, "t", id)).toBeNull(); // once
    expect(completedFromDownload(state, "magnific", "someoneElse", "u", null, "t", id)).toBeNull();
  });

  it("pruneState bounds every map", () => {
    const state = emptyState();
    for (let i = 0; i < 10; i++) state.emitted[`k${i}`] = `2026-01-0${i}`;
    pruneState(state, 3);
    expect(Object.keys(state.emitted)).toEqual(["k7", "k8", "k9"]);
  });
});
