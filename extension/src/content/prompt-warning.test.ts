// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mountPromptWarning } from "./prompt-warning";

const marker = "data-faible-prompt-warning";
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe("live model-independent prompt warnings", () => {
  it("warns during typing in plain inputs and rich editors, and clears after edits", async () => {
    vi.useFakeTimers();
    const attach = vi.spyOn(Element.prototype, "attachShadow");
    document.body.innerHTML = '<input id="plain"><textarea id="negative"></textarea><div id="rich" contenteditable></div><div id="plaintext" contenteditable="plaintext-only"></div><input id="password" type="password">';
    mountPromptWarning();
    document.dispatchEvent(new Event("DOMContentLoaded"));
    await vi.advanceTimersByTimeAsync(0);
    const warningRoot = attach.mock.results[0]!.value as ShadowRoot;
    const plain = document.getElementById("plain") as HTMLInputElement;
    plain.value = "Taylor Swift";
    plain.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    expect(plain.hasAttribute(marker)).toBe(true);
    expect(warningRoot.textContent).toContain("taylor swift");
    expect((warningRoot.querySelector('[role="alert"]') as HTMLElement).style.display).toBe("block");
    plain.value = "forest";
    plain.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    expect(plain.hasAttribute(marker)).toBe(false);
    expect((warningRoot.querySelector('[role="alert"]') as HTMLElement).style.display).toBe("none");
    for (const id of ["rich", "plaintext"]) {
      const field = document.getElementById(id)!;
      field.textContent = "Spider-Man";
      field.dispatchEvent(new Event("input", { bubbles: true }));
      await vi.advanceTimersByTimeAsync(0);
      expect(field.hasAttribute(marker)).toBe(true);
      field.textContent = "";
    }
    const password = document.getElementById("password") as HTMLInputElement;
    password.value = "Taylor Swift";
    password.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    expect(password.hasAttribute(marker)).toBe(false);
    // Switching model replaces the original field: detect the new prompt immediately.
    const replacement = document.createElement("textarea");
    document.body.append(replacement);
    replacement.value = "Batman";
    replacement.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    expect(replacement.hasAttribute(marker)).toBe(true);
    replacement.remove();
    const editorHost = document.createElement("div");
    const editorRoot = editorHost.attachShadow({ mode: "open" });
    editorRoot.innerHTML = '<div contenteditable="true"><span>Tom Cruise</span></div>';
    document.body.append(editorHost);
    await vi.advanceTimersByTimeAsync(500);
    expect(editorRoot.querySelector("[contenteditable]")?.hasAttribute(marker)).toBe(true);
    expect(warningRoot.textContent).toContain("tom cruise");
    // Programmatic values without input events are covered by periodic checks too.
    (document.getElementById("negative") as HTMLTextAreaElement).value = "Elon Musk";
    await vi.advanceTimersByTimeAsync(500);
    expect(document.getElementById("negative")?.hasAttribute(marker)).toBe(true);
  });
});
