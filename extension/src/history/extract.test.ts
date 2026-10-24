import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { CaptureRecord } from "../shared/capture";
import { accountKey, collectGenerations, findAccount, isGenerationLike, serviceLogin, signature } from "./extract";
import { normalizeGeneration } from "./export";

const history = JSON.parse(readFileSync(join(__dirname, "../adapters/fixtures/higgsfield/history.json"), "utf8")) as CaptureRecord;

describe("history extraction (ported exporter rules)", () => {
  it("finds every job in a Higgsfield history response", () => {
    const body = history.response_body as { jobs: { id: string }[] };
    const found = collectGenerations(body);
    expect(found.map((g) => g.id).sort()).toEqual(body.jobs.map((j) => j.id).sort());
    expect(isGenerationLike({ id: "x", model: "m", status: "done", results: {} })).toBe(true);
    expect(isGenerationLike({ id: "x", params: {} })).toBe(false); // params without results/model
    expect(isGenerationLike({ id: 1, params: {}, results: {} })).toBe(false);
  });

  it("reads the login from Clerk-style and user payloads, never from nested creators", () => {
    const clerk = {
      client: {
        last_active_session_id: "s2",
        sessions: [
          { id: "s1", user: { id: "u1", email_addresses: [{ id: "e1", email_address: "old@x.com" }], primary_email_address_id: "e1" } },
          { id: "s2", user: { id: "u2", first_name: "Sub", last_name: "Nine", email_addresses: [{ id: "e2", email_address: "subscriptions.9@faiblemedia.com" }], primary_email_address_id: "e2" } },
        ],
      },
    };
    expect(findAccount(clerk, "https://clerk.higgsfield.ai/v1/client")).toMatchObject({ id: "u2", email: "subscriptions.9@faiblemedia.com", name: "Sub Nine" });
    expect(findAccount({ user: { id: 7, email: "a@b.co", username: "ab" } }, "u")).toMatchObject({ id: "7", email: "a@b.co" });
    expect(findAccount({ feed: [{ creator: { email: "someone@else.com" } }] }, "u")).toBeNull();
    expect(accountKey({ email: "Sub@X.com", id: null, username: null })).toBe("sub@x.com");
    expect(accountKey({ email: null, id: "u1", username: null })).toBe("id:u1");
  });

  it("describes the login as metadata (ServiceLogin), with a manual label when there is one", () => {
    const clerk = { key: "s@x.com", id: "user_1", email: "s@x.com", name: null, username: null, source: "clerk" };
    expect(serviceLogin(clerk)).toEqual({ service_user_id: "user_1", email: "s@x.com", name: null, username: null, label: null, detected_by: "clerk" });
    expect(serviceLogin({ ...clerk, source: "https://fnf-api-gw.higgsfield.ai/user" }, "Sub 9")?.detected_by).toBe("api");
    const manual = { key: "user:Team A", id: null, email: null, name: "Team A", username: "Team A", source: "manual", manual: true };
    expect(serviceLogin(manual)).toMatchObject({ label: "Team A", detected_by: "manual" });
    expect(serviceLogin({ ...clerk, key: "unknown", source: "fallback" })).toBeNull();
  });

  it("signs content stably and notices changes", () => {
    const job = { id: "j", status: "queued", results: null };
    expect(signature(job)).toBe(signature({ ...job }));
    expect(signature(job)).not.toBe(signature({ ...job, status: "completed" }));
  });

  it("exports generations.json records in the exporter's shape", () => {
    const job = (history.response_body as { jobs: Record<string, unknown>[] }).jobs[0]!;
    const n = normalizeGeneration({ accountKey: "k", id: job.id as string, data: job, sources: ["b", "a"], firstCapturedAt: "", lastCapturedAt: "" });
    expect(Object.keys(n)).toEqual([
      "generation_id", "type", "status", "model", "created_at", "prompt", "enhanced_prompt", "negative_prompt",
      "input_media", "output", "source_response_urls", "all_params", "settings", "raw",
    ]);
    expect(n.generation_id).toBe(job.id);
    expect(n.source_response_urls).toEqual(["a", "b"]);
    expect(n.raw).toBe(job);
  });
});
