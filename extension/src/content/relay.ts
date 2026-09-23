// Isolated-world content script: relays page-world capture records to the worker, and
// hosts the Faible side panel. The MAIN world has no chrome.* access, so records arrive
// via window.postMessage.
//
// After the extension is reloaded or updated, this script keeps running in already-open
// tabs but can no longer reach the worker ("extension context invalidated"). That is
// detected and shown in the panel: the tab must be reloaded to capture again.

import { MSG_TAG, type CaptureRecord } from "../shared/capture";
import { mountPanel } from "./panel";

let invalidated = false;

function contextAlive(): boolean {
  try {
    return !!chrome.runtime?.id;
  } catch {
    return false;
  }
}

async function send<T>(msg: Record<string, unknown>): Promise<T> {
  if (!contextAlive()) {
    invalidated = true;
    throw new Error("extension reloaded");
  }
  try {
    const r = (await chrome.runtime.sendMessage(msg)) as { ok: boolean; value?: T; error?: string } | undefined;
    if (!r) throw new Error("no response from Faible");
    if (!r.ok) throw new Error(r.error ?? "failed");
    return r.value as T;
  } catch (err) {
    if (!contextAlive() || String(err).includes("context invalidated")) invalidated = true;
    throw err;
  }
}

window.addEventListener("message", (e: MessageEvent) => {
  if (e.source !== window || e.origin !== location.origin) return;
  const data = e.data as { [MSG_TAG]?: boolean; record?: CaptureRecord } | null;
  if (!data || data[MSG_TAG] !== true || !data.record) return;
  if (!contextAlive()) {
    invalidated = true;
    return;
  }
  const r = data.record;
  console.debug("[faible]", r.transport, r.method ?? "", r.status ?? "", r.url);
  chrome.runtime.sendMessage({ type: "capture", record: r }).catch(() => {
    if (!contextAlive()) invalidated = true;
  });
});

if (window.top === window) mountPanel(send, () => invalidated || !contextAlive());
