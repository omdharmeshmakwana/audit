// Higgsfield history collection in the page (isolated world, top frame), ported from
// higgsfield_exporter/extension/content.js (DECISIONS #59). It reads the capture records the
// relay already receives from the single page-world observer — it never patches fetch itself
// — and:
//   - tracks the page's Higgsfield login (Clerk first, then account-like responses, then a
//     label typed in the popup), holding generations until the login is known;
//   - forwards generation-like objects to the worker ("history"), which keeps a local copy
//     for JSON export and, when signed in to Faible, sends them as service_generation.observed;
//   - buffers recent non-generation responses for "Save usage page", which also snapshots the
//     page's tables and text (redacted here, labelled observed_dom on the server);
//   - runs the exporter's auto-scroll so long history pages load completely.

import type { CaptureRecord } from "../shared/capture";
import { redactString, redactUrl } from "../shared/redact";
import type { UsageCapture, UsageResponse } from "../history/db";
import { ACCOUNT_URL_RE, USAGE_URL_RE, accountKey, collectGenerations, findAccount, type AccountCandidate, type ServiceAccountInfo } from "../history/extract";

const RECENT_MAX = 300;
const TEXT_MAX = 500_000;
/** A usage capture travels through the event queue: keep its JSON responses bounded. */
export const RESPONSES_MAX_CHARS = 4 * 1024 * 1024;

interface Batch {
  url: string;
  items: Record<string, unknown>[];
}

/** Responses for a usage capture: the current page's, or usage-like URLs (exporter rule),
 *  newest first until the size budget is spent, then back in time order. */
export function usageResponses(recent: UsageResponse[], pagePath: string, maxChars = RESPONSES_MAX_CHARS): { responses: UsageResponse[]; dropped: number } {
  const wanted = recent.filter((r) => r.pagePath === pagePath || USAGE_URL_RE.test(r.url));
  const kept: UsageResponse[] = [];
  let used = 0;
  for (const r of [...wanted].reverse()) {
    const size = JSON.stringify(r.data ?? null).length + r.url.length;
    if (used + size > maxChars) continue;
    used += size;
    kept.push(r);
  }
  return { responses: kept.reverse(), dropped: wanted.length - kept.length };
}

/** Tables and visible text, redacted with Faible's rules before they leave the page. */
export function snapshotPage(doc: Document): { tables: string[][][]; text: string } {
  const clean = (s: string) => redactString(s.replace(/\s+/g, " ").trim());
  const cells = "th, td, [role=cell], [role=gridcell], [role=columnheader], [role=rowheader]";
  const tables = [...doc.querySelectorAll("table, [role=table], [role=grid]")]
    .map((t) =>
      [...t.querySelectorAll("tr, [role=row]")]
        .map((row) => [...row.querySelectorAll(cells)].map((c) => clean((c as HTMLElement).innerText ?? c.textContent ?? "")))
        .filter((row) => row.length),
    )
    .filter((rows) => rows.length);
  const body = doc.body as HTMLElement | null;
  return { tables, text: redactString((body?.innerText ?? body?.textContent ?? "").slice(0, TEXT_MAX)) };
}

export function mountHistory(): { observe(record: CaptureRecord): void } {
  let account: ServiceAccountInfo | null = null;
  let pending: Batch[] = [];
  const seenIds = new Set<string>();
  const recent: UsageResponse[] = [];
  const scroll = { running: false, round: 0, maxRounds: 0, stopRequested: false };

  const send = (message: Record<string, unknown>) => chrome.runtime.sendMessage(message).catch(() => undefined);
  const sendBatch = (batch: Batch) => void send({ type: "history", accountKey: account!.key, account, url: batch.url, items: batch.items });

  function setAccount(next: AccountCandidate & { manual?: boolean }): void {
    const key = accountKey(next);
    if (!key) return;
    // Clerk is the most reliable signal; don't let API guesses override it.
    if (account && account.source === "clerk" && next.source !== "clerk" && !next.manual) return;
    if (account && account.key === key && !next.manual) return;
    account = { ...next, key };
    void send({ type: "hfAccount", account });
    const queued = pending;
    pending = [];
    queued.forEach(sendBatch);
  }

  function observe(record: CaptureRecord): void {
    if (record.transport === "account") {
      const a = record.extra.account as AccountCandidate | undefined;
      if (a) setAccount(a);
      return;
    }
    if (record.service !== "higgsfield" || (record.transport !== "fetch" && record.transport !== "xhr")) return;
    const url = record.url ?? "";
    const body = record.response_body;
    if (!body || typeof body !== "object" || !/higgsfield/i.test(url)) return;
    if (ACCOUNT_URL_RE.test(url)) {
      const a = findAccount(body, url);
      if (a) setAccount(a);
    }
    const items = collectGenerations(body);
    if (items.length) {
      items.forEach((g) => seenIds.add(g.id));
      const batch = { url, items };
      if (account) sendBatch(batch);
      else pending.push(batch);
    } else {
      recent.push({ url, pagePath: location.pathname, capturedAt: record.captured_at, data: body });
      if (recent.length > RECENT_MAX) recent.shift();
    }
  }

  // Don't lose records captured on a page where the login was never identified.
  window.addEventListener("pagehide", () => {
    if (account || !pending.length) return;
    account = { key: "unknown", id: null, email: null, name: null, username: null, source: "fallback" };
    void send({ type: "hfAccount", account });
    pending.forEach(sendBatch);
    pending = [];
  });

  // ---------------------------------------------------------------- auto-scroll (exporter)

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  function scrollOnce(): string {
    const all = [document.scrollingElement, ...document.querySelectorAll("*")].filter(Boolean) as Element[];
    let best = document.scrollingElement;
    let bestDelta = best ? best.scrollHeight - best.clientHeight : 0;
    for (const el of all) {
      try {
        const delta = el.scrollHeight - el.clientHeight;
        if (delta > 100 && delta > bestDelta && ["auto", "scroll"].includes(getComputedStyle(el).overflowY)) {
          best = el;
          bestDelta = delta;
        }
      } catch {
        /* ignore */
      }
    }
    if (best) best.scrollTop = Math.min(best.scrollTop + Math.max(best.clientHeight * 0.9, 900), best.scrollHeight);
    window.scrollBy(0, Math.max(window.innerHeight * 0.9, 900));
    return [location.href, document.scrollingElement?.scrollHeight ?? 0, document.scrollingElement?.scrollTop ?? 0, best?.scrollHeight ?? 0, best?.scrollTop ?? 0].join("|");
  }

  async function autoScroll(maxRounds: number, idleRounds: number): Promise<void> {
    if (scroll.running) return;
    Object.assign(scroll, { running: true, round: 0, maxRounds, stopRequested: false });
    let stable = 0;
    let last: string | null = null;
    try {
      for (let i = 0; i < maxRounds && !scroll.stopRequested; i++) {
        const sig = scrollOnce();
        await sleep(1200);
        scroll.round = i + 1;
        if (sig === last) stable++;
        else {
          stable = 0;
          last = sig;
        }
        if (stable >= idleRounds) break;
      }
      await sleep(2500);
    } finally {
      scroll.running = false;
    }
  }

  // ---------------------------------------------------------------- usage page capture

  async function captureUsage(): Promise<Record<string, unknown>> {
    if (!account) return { ok: false, error: "Higgsfield login not detected yet; set a label first" };
    const { responses, dropped } = usageResponses(recent, location.pathname);
    const capture: UsageCapture = {
      capturedAt: new Date().toISOString(),
      pageUrl: redactUrl(location.href),
      pageTitle: document.title,
      responses,
      ...snapshotPage(document),
    };
    const res = (await chrome.runtime.sendMessage({ type: "usage", accountKey: account.key, account, capture })) as { ok: boolean; value?: { sent: boolean }; error?: string } | undefined;
    if (!res?.ok) return { ok: false, error: res?.error ?? "save failed" };
    return { ok: true, responses: responses.length, dropped, tables: capture.tables.length, sent: !!res.value?.sent };
  }

  chrome.runtime.onMessage.addListener((msg: { type?: string; label?: string; maxRounds?: number; idleRounds?: number }, _sender, reply) => {
    switch (msg.type) {
      case "historyStatus":
        reply({
          account,
          pendingCount: pending.reduce((n, b) => n + b.items.length, 0),
          capturedOnPage: seenIds.size,
          url: location.href,
          scroll: { running: scroll.running, round: scroll.round, maxRounds: scroll.maxRounds },
        });
        return false;
      case "historySetAccount":
        setAccount({ email: null, id: null, username: msg.label ?? null, name: msg.label ?? null, source: "manual", manual: true });
        reply({ ok: true });
        return false;
      case "historyStartScroll":
        void autoScroll(msg.maxRounds ?? 180, msg.idleRounds ?? 8);
        reply({ ok: true });
        return false;
      case "historyStopScroll":
        scroll.stopRequested = true;
        reply({ ok: true });
        return false;
      case "historyCaptureUsage":
        captureUsage().then(reply, (err: unknown) => reply({ ok: false, error: String(err) }));
        return true;
      default:
        return false;
    }
  });

  return { observe };
}
