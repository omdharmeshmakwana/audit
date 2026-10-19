import { promptWarnings } from "../shared/prompt-filter";

// Model-independent: observe editable fields rather than model names or generation endpoints.
const EDITORS = 'textarea, input, [contenteditable]:not([contenteditable="false"]), [role="textbox"]';
const MARKER = "data-faible-prompt-warning";
const FIELD_CSS = `[${MARKER}] { outline: 3px solid #ef4444 !important; outline-offset: 3px !important; }`;

export function mountPromptWarning(): void {
  if (document.querySelector("faible-prompt-warning")) return;
  const host = document.createElement("faible-prompt-warning");
  const root = host.attachShadow({ mode: "closed" });
  const notice = document.createElement("div");
  notice.setAttribute("role", "alert");
  notice.setAttribute("aria-live", "assertive");
  notice.style.cssText = "position:fixed;left:50%;top:16px;transform:translateX(-50%);z-index:2147483647;max-width:min(620px,90vw);background:#7f1d1d;color:#fff;border:3px solid #ef4444;border-radius:12px;padding:14px 20px;font:bold 15px system-ui;display:none;pointer-events:none;box-shadow:0 4px 24px #0008";
  root.append(notice);
  const observed = new WeakSet<Document | ShadowRoot>();
  const marked = new Set<HTMLElement>();
  let queued = false;
  function schedule(): void {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; check(); });
  }
  function ownNode(node: Node): boolean {
    return node === host || node instanceof HTMLElement && node.tagName.startsWith("FAIBLE-");
  }
  function watch(scope: Document | ShadowRoot): void {
    if (observed.has(scope)) return;
    observed.add(scope);
    const style = document.createElement("style");
    style.textContent = FIELD_CSS;
    if (scope === document) (document.head ?? document.documentElement).append(style);
    else scope.append(style);
    new MutationObserver((records) => {
      if (records.some((record) => !ownNode(record.target))) schedule();
    }).observe(scope, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["contenteditable", "role", "placeholder", "type"] });
    // These listeners also catch non-composed events emitted inside open shadow roots.
    for (const name of ["input", "change", "keyup", "compositionend"]) scope.addEventListener(name, schedule, true);
  }
  function editors(scope: Document | ShadowRoot): HTMLElement[] {
    watch(scope);
    const fields = [...scope.querySelectorAll<HTMLElement>(EDITORS)];
    for (const element of scope.querySelectorAll<HTMLElement>("*")) {
      if (ownNode(element)) continue;
      if (element.shadowRoot) fields.push(...editors(element.shadowRoot));
    }
    return fields;
  }
  function eligible(field: HTMLElement): boolean {
    if (field.closest("faible-panel, faible-prompt-warning")) return false;
    if (field instanceof HTMLInputElement && !["text", ""].includes(field.type)) return false;
    const hint = `${field.getAttribute("name") ?? ""} ${field.getAttribute("autocomplete") ?? ""} ${field.getAttribute("placeholder") ?? ""} ${field.getAttribute("aria-label") ?? ""}`;
    return !/search|email|password|username|one-time-code/i.test(hint);
  }
  function check(): void {
    const matches = new Set<string>();
    const nextMarked = new Set<HTMLElement>();
    for (const field of editors(document)) {
      if (!eligible(field)) continue;
      const value = field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement
        ? field.value : field.innerText || field.textContent || "";
      const words = promptWarnings(value);
      if (words.length) {
        nextMarked.add(field);
        if (!field.hasAttribute(MARKER)) field.setAttribute(MARKER, "true");
        for (const word of words) matches.add(word);
      }
    }
    for (const field of marked) if (!nextMarked.has(field)) field.removeAttribute(MARKER);
    marked.clear();
    for (const field of nextMarked) marked.add(field);
    const text = matches.size ? `🔴 Prompt warning: ${[...matches].join(", ")}` : "";
    if (notice.textContent !== text) notice.textContent = text;
    notice.style.display = matches.size ? "block" : "none";
  }
  const mount = () => {
    document.documentElement.append(host);
    check();
    // Also covers programmatic .value changes that emit no input event.
    setInterval(check, 500);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
}
