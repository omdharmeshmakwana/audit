// Popup section for the integrated Higgsfield exporter (DECISIONS #59), ported from
// higgsfield_exporter/extension/popup.js: this tab's Higgsfield login, auto-scroll & capture,
// "Save usage page", and this browser's saved accounts with JSON export, rename and clear.
// The page side is content/history.ts; storage is history/db.ts (same extension origin).

import { deleteAccount, listAccounts, renameAccount, type AccountRecord } from "../history/db";
import type { ServiceAccountInfo } from "../history/extract";
import { displayName, exportAccount, getFolder, writableFolder } from "../history/export";

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

interface TabStatus {
  account: ServiceAccountInfo | null;
  pendingCount: number;
  capturedOnPage: number;
  scroll: { running: boolean; round: number; maxRounds: number };
}

export function mountHistoryPopup(isLoggedIn: () => boolean): () => Promise<void> {
  let activeTabId: number | null = null;

  /** Writes straight into the chosen folder when access is already granted; otherwise opens
   *  the export page, where Chrome can ask for folder access (it can't from a popup). */
  async function exportAccounts(target: string): Promise<void> {
    const folder = await writableFolder();
    if (!folder) {
      void chrome.tabs.create({ url: `export.html?account=${encodeURIComponent(target)}` });
      return;
    }
    const accounts = (await listAccounts()).filter((a) => (target === "all" ? a.count || a.usageCount : a.key === target));
    $("export-status").textContent = "Exporting…";
    try {
      for (const a of accounts) await exportAccount(folder, a);
      $("export-status").textContent = `Saved to ${folder.name}/`;
    } catch (err) {
      $("export-status").textContent = `Export failed: ${err instanceof Error ? err.message : String(err)}`;
    }
  }

  async function refreshTab(): Promise<void> {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const isHf = !!tab?.url && /^https?:\/\/([^/]+\.)?higgsfield\.ai\//.test(tab.url);
    $("not-hf").classList.toggle("hidden", isHf);
    $("hf").classList.toggle("hidden", !isHf);
    $("history-sync").textContent = isLoggedIn()
      ? "Collected history and saved pages are sent to Faible as studio history (not as your generations) and kept in this browser for export."
      : "Not signed in to Faible: history is kept in this browser only, for export.";
    if (!isHf || tab?.id === undefined) return;
    activeTabId = tab.id;
    let status: TabStatus;
    try {
      status = (await chrome.tabs.sendMessage(tab.id, { type: "historyStatus" })) as TabStatus;
      if (!status) throw new Error("no history module");
    } catch {
      $("acct").textContent = "Reload the tab to start capturing";
      return;
    }
    const accounts = await listAccounts();
    const saved = status.account && accounts.find((a) => a.key === status.account!.key);
    $("acct").textContent = status.account ? displayName(saved || { ...status.account, label: null }) : "Not detected yet";
    $("acct-manual").classList.toggle("hidden", !!status.account);
    $("page-count").textContent = String(status.capturedOnPage);
    $("pending-label").classList.toggle("hidden", !status.pendingCount);
    $("pending-count").classList.toggle("hidden", !status.pendingCount);
    $("pending-count").textContent = String(status.pendingCount);
    const s = status.scroll;
    $("scroll").classList.toggle("hidden", s.running);
    $("stop").classList.toggle("hidden", !s.running);
    $("scroll-status").textContent = s.running ? `Scrolling… round ${s.round}/${s.maxRounds}` : "";
  }

  async function refreshAccounts(): Promise<void> {
    const accounts = await listAccounts();
    const list = $("accounts");
    $("empty").classList.toggle("hidden", accounts.length > 0);
    const folder = await getFolder();
    $("folder").textContent = folder ? `${folder.name}/` : "choose folder";
    list.replaceChildren(...accounts.map(accountItem));
  }

  function accountItem(a: AccountRecord): HTMLLIElement {
    const li = document.createElement("li");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = displayName(a);
    const meta = document.createElement("div");
    meta.className = "meta";
    const extra = a.label && a.email ? ` · ${a.email}` : "";
    const usage = a.usageCount ? ` · ${a.usageCount} usage captures` : "";
    meta.textContent = `${a.count ?? 0} generations${usage}${extra} · last seen ${a.lastSeen ? new Date(a.lastSeen).toLocaleString() : "—"}`;
    const actions = document.createElement("div");
    actions.className = "actions";
    const btn = (text: string, onClick: () => void, cls?: string) => {
      const b = document.createElement("button");
      b.textContent = text;
      if (cls) b.className = cls;
      b.addEventListener("click", onClick);
      actions.append(b);
    };
    btn("Export", () => void exportAccounts(a.key));
    btn("Rename", () => {
      const label = prompt("Label for this Higgsfield login:", displayName(a));
      if (label === null) return;
      void renameAccount(a.key, label.trim()).then(refresh);
    });
    btn(
      "Clear",
      () => {
        const usageNote = a.usageCount ? ` and ${a.usageCount} usage captures` : "";
        if (!confirm(`Delete ${a.count ?? 0} generations${usageNote} saved in this browser for ${displayName(a)}?\n\nRecords already sent to Faible are not affected.`)) return;
        void deleteAccount(a.key).then(refresh);
      },
      "danger",
    );
    li.append(name, meta, actions);
    return li;
  }

  async function refresh(): Promise<void> {
    await Promise.all([refreshTab().catch(() => undefined), refreshAccounts().catch(() => undefined)]);
  }

  const tabMessage = (msg: Record<string, unknown>) =>
    activeTabId === null ? Promise.reject(new Error("no Higgsfield tab")) : chrome.tabs.sendMessage(activeTabId, msg);

  $("scroll").addEventListener("click", () => void tabMessage({ type: "historyStartScroll", maxRounds: 180, idleRounds: 8 }).then(refresh, refresh));
  $("stop").addEventListener("click", () => void tabMessage({ type: "historyStopScroll" }).then(refresh, refresh));
  $("usage").addEventListener("click", () => {
    $("usage-status").textContent = "Saving…";
    void tabMessage({ type: "historyCaptureUsage" })
      .catch((err: Error) => ({ ok: false, error: err.message }))
      .then((res: { ok?: boolean; error?: string; responses?: number; tables?: number; dropped?: number; sent?: boolean } | undefined) => {
        $("usage-status").textContent = res?.ok
          ? `Saved ${res.responses} data responses and ${res.tables} tables from this page${res.dropped ? ` (${res.dropped} large older responses left out)` : ""}. ${res.sent ? "Sent to Faible." : "Kept in this browser (not signed in to Faible)."}`
          : `Not saved: ${res?.error || "reload the tab and try again"}`;
        void refreshAccounts();
      });
  });
  $("manual-set").addEventListener("click", () => {
    const label = $<HTMLInputElement>("manual-label").value.trim();
    if (!label) return;
    void tabMessage({ type: "historySetAccount", label }).then(() => setTimeout(() => void refresh(), 300));
  });
  $("export-all").addEventListener("click", () => void exportAccounts("all"));
  $("folder").addEventListener("click", (e) => {
    e.preventDefault();
    void chrome.tabs.create({ url: "export.html" });
  });

  return refresh;
}
