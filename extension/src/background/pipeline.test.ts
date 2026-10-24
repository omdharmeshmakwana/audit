import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { higgsfield } from "../adapters/higgsfield";
import { magnific } from "../adapters/magnific";
import type { Adapter } from "../adapters/types";
import type { CaptureRecord } from "../shared/capture";
import { completedFromDownload, emptyState, plan, planHistory, pruneState, usageEvent, type PipelineState, type PlanContext } from "./pipeline";

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
  it("Higgsfield: an input reused from the library (no upload this time) is fetched for registration", () => {
    const r = run(higgsfield, emptyState(), fixture("higgsfield", "submit-image-reused-input.json"));
    expect(r.events.map((e) => e.type)).toEqual(["generation.requested"]);
    const url = "https://d2ol7oe51mr4n9.cloudfront.net/user_36mMlkuUFLoVEvOM0icTtFFDdlE/6e459e23-b95d-4cc4-b45c-fe40ec7e87ce.png";
    expect(r.hashJobs).toEqual([
      {
        method: "upload",
        output: expect.objectContaining({ url, fetch_url: url, service: "higgsfield", service_asset_id: "6e459e23-b95d-4cc4-b45c-fe40ec7e87ce" }),
      },
    ]);
  });

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

  it("Magnific: a downloaded Higgsfield image uploaded as a reference → located by hash, then named as the render's input", () => {
    const state = emptyState();
    const sha = "b71d14358389a5547da8b5ebf040a56e29aae6c62c965060f65d0ee1800d9850";
    const upload = fixture("magnific", "upload-reference.json");
    (upload.request_body as { __formdata: { file: Record<string, unknown> } }).__formdata.file.__sha256 = sha;
    const u = run(magnific, state, upload);
    expect(u.events).toEqual([
      expect.objectContaining({ type: "asset.location_seen", payload: expect.objectContaining({ sha256: sha, service: "magnific", service_asset_id: "CqUjWZfEEy" }) }),
    ]);

    const render = fixture("magnific", "render-v4.json");
    const body = render.request_body as { image_references: { image: string }[] };
    body.image_references[0]!.image = "creation:CqUjWZfEEy";
    const r = run(magnific, state, render);
    expect(r.events[0]!.payload.inputs).toEqual([{ role: "reference", service_asset_id: "CqUjWZfEEy", url: null }]);
    expect(r.hashJobs).toEqual([]); // no URL to fetch: the upload above already carried the hash
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

describe("history (integrated exporter): one shared capture path, no double counting", () => {
  const history = fixture("higgsfield", "history.json");
  const jobs = (history.response_body as { jobs: Record<string, unknown>[] }).jobs;
  const login = { service_user_id: "user_1", email: "subscriptions.9@faiblemedia.com", name: null, username: null, label: null, detected_by: "clerk" };
  let n = 0;
  const newId = () => `h-${++n}`;
  const batch = (items: Record<string, unknown>[]) => ({ service: "higgsfield", url: history.url!, items, login, capturedAt: "2026-10-23T09:00:00.000Z" });

  it("the live path emits nothing for history jobs this browser did not submit", () => {
    const live = run(higgsfield, emptyState(), history);
    expect(live.events).toEqual([]);
  });

  it("the history path sends each job once as service history, never as a generation", () => {
    const state = emptyState();
    const first = planHistory(batch(jobs), state, newId);
    expect(first).toHaveLength(jobs.length);
    expect(new Set(first.map((e) => e.type))).toEqual(new Set(["service_generation.observed"]));
    expect(first[0]!.payload).toMatchObject({ service: "higgsfield", service_job_id: jobs[0]!.id, source: "history", service_login: login, source_url: history.url });
    expect(first[0]!.payload.raw).toBe(jobs[0]);
    expect(first[0]!.evidence).toBe("service_reported");
    // Same content again (re-scroll, second tab): nothing. A changed job: just that one.
    expect(planHistory(batch(jobs), state, newId)).toEqual([]);
    const changed = { ...jobs[0]!, status: "failed" };
    expect(planHistory(batch([changed, ...jobs.slice(1)]), state, newId).map((e) => e.payload.service_job_id)).toEqual([jobs[0]!.id]);
  });

  it("an older stored state without the history map still works", () => {
    const legacy = emptyState() as Partial<PipelineState>;
    delete legacy.history;
    expect(planHistory(batch(jobs.slice(0, 1)), legacy as PipelineState, newId)).toHaveLength(1);
  });

  it("history signatures get their own, larger prune cap", () => {
    const state = emptyState();
    for (let i = 0; i < 50; i++) state.history[`higgsfield:${i}`] = { sig: "s", at: `2026-10-23T09:00:${String(i).padStart(2, "0")}Z` };
    for (let i = 0; i < 50; i++) state.emitted[`e${i}`] = `2026-10-23T09:00:${String(i).padStart(2, "0")}Z`;
    pruneState(state, 10, 40);
    expect(Object.keys(state.emitted)).toHaveLength(10);
    expect(Object.keys(state.history)).toHaveLength(40);
    expect(state.history["higgsfield:49"]).toBeDefined();
    expect(state.history["higgsfield:0"]).toBeUndefined();
  });

  it("a saved usage page becomes one usage.page_captured event", () => {
    const capture = {
      capturedAt: "2026-10-23T09:00:00.000Z",
      pageUrl: "https://higgsfield.ai/usage",
      pageTitle: "Usage",
      responses: [{ url: "https://fnf-api-gw.higgsfield.ai/fnf/workspaces/wallet", pagePath: "/usage", capturedAt: "2026-10-23T08:59:00.000Z", data: { total_credits: 1 } }],
      tables: [[["Date", "Credits"], ["23 Oct", "108"]]],
      text: "Usage",
    };
    const e = usageEvent("higgsfield", capture, login, newId);
    expect(e).toMatchObject({ type: "usage.page_captured", occurred_at: capture.capturedAt, evidence: "observed" });
    expect(e.payload).toMatchObject({ service: "higgsfield", source: "extension", page_url: capture.pageUrl, tables: capture.tables, service_login: login });
    expect(e.payload.responses).toEqual([{ url: capture.responses[0]!.url, page_path: "/usage", captured_at: capture.responses[0]!.capturedAt, data: { total_credits: 1 } }]);
  });
});
