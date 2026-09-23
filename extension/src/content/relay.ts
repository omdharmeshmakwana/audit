// Isolated-world relay: page-world observer → background worker.
// The MAIN world has no chrome.* access, so records arrive via window.postMessage.

import { MSG_TAG, type CaptureRecord } from "../shared/capture";

window.addEventListener("message", (e: MessageEvent) => {
  if (e.source !== window || e.origin !== location.origin) return;
  const data = e.data as { [MSG_TAG]?: boolean; record?: CaptureRecord } | null;
  if (!data || data[MSG_TAG] !== true || !data.record) return;
  const r = data.record;
  console.debug("[faible]", r.transport, r.method ?? "", r.status ?? "", r.url);
  chrome.runtime.sendMessage({ type: "capture", record: r }).catch(() => undefined);
});
