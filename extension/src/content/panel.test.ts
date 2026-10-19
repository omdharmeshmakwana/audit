// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountPanel } from "./panel";

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); document.body.inert = false; });

describe("shot selection gate", () => {
  it("requires confirming a shot on a new visit even with a remembered login", async () => {
    vi.useFakeTimers();
    const attach = vi.spyOn(Element.prototype, "attachShadow");
    const state = { loggedIn: true, user: { email: "om@faiblemedia.com" }, activeShot: { id: "shot-1", code: "SH010" } };
    const send = vi.fn(async (message: Record<string, unknown>) => {
      switch (message.type) {
        case "state": case "setActiveShot": return state;
        case "projects": return [{ id: "project", code: "P", name: "Project" }];
        case "tree": return { sequences: [{ shots: [{ id: "shot-1", code: "SH010", description: "Test" }] }] };
        case "captures": return { recent: [], unassigned_count: 0 };
      }
    });
    mountPanel(send as <T>(message: Record<string, unknown>) => Promise<T>, () => false);
    document.dispatchEvent(new Event("DOMContentLoaded"));
    await vi.advanceTimersByTimeAsync(0);
    const root = attach.mock.results[0]!.value as ShadowRoot;
    expect(document.body.inert).toBe(true);
    expect(root.querySelector(".panel")?.classList.contains("hidden")).toBe(false);
    (root.querySelector(".shots button") as HTMLButtonElement).click();
    await vi.advanceTimersByTimeAsync(0);
    expect(send).toHaveBeenCalledWith({ type: "setActiveShot", shot: { id: "shot-1", code: "SH010" } });
    expect(document.body.inert).toBe(false);
  });
});

describe("company login gate", () => {
  it("automatically asks a logged-out artist to sign in and keeps the page gated", async () => {
    vi.useFakeTimers();
    const send = vi.fn(async (message: Record<string, unknown>) => {
      if (message.type === "state") return { loggedIn: false, user: null, activeShot: null };
      return null;
    });
    mountPanel(send as <T>(message: Record<string, unknown>) => Promise<T>, () => false);
    document.dispatchEvent(new Event("DOMContentLoaded"));
    await vi.advanceTimersByTimeAsync(0);
    expect(send).toHaveBeenCalledWith({ type: "openLogin" });
    expect(document.body.inert).toBe(true);
    await vi.advanceTimersByTimeAsync(8000);
    expect(send.mock.calls.filter(([message]) => message.type === "openLogin")).toHaveLength(1);
  });
});
