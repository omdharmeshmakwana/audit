// export.html?account=<key>  → export one Higgsfield login's saved history
// export.html?account=all    → export every login with data
// export.html                → just choose the folder
// Ported from higgsfield_exporter/extension/export.js. Folder access can only be granted
// from a regular tab, which is why the popup opens this page (DECISIONS #59).

import { listAccounts } from "../history/db";
import { displayName, ensureWritableFolder, exportAccount, getFolder } from "../history/export";

const $ = (id: string) => document.getElementById(id) as HTMLElement;
const target = new URLSearchParams(location.search).get("account");

function log(text: string): void {
  const li = document.createElement("li");
  li.textContent = text;
  $("log").append(li);
}

async function showFolder(): Promise<void> {
  const handle = await getFolder();
  $("folder").textContent = handle ? handle.name : "Not chosen";
}

async function accountsToExport() {
  const accounts = await listAccounts();
  if (target === "all") return accounts.filter((a) => a.count || a.usageCount);
  return accounts.filter((a) => a.key === target);
}

const aborted = (err: unknown) => err instanceof DOMException && err.name === "AbortError";

async function run(): Promise<void> {
  $("log").replaceChildren();
  try {
    const folder = await ensureWritableFolder();
    await showFolder();
    for (const account of await accountsToExport()) {
      log(`Exporting ${displayName(account)}…`);
      const { folder: path, count, usage } = await exportAccount(folder, account);
      log(`✓ ${count} generations${usage ? `, ${usage} usage captures` : ""} → ${path}/`);
    }
    log("Done.");
  } catch (err) {
    if (!aborted(err)) log(`✗ ${err instanceof Error ? err.message : String(err)}`);
  }
}

$("pick").addEventListener("click", () => {
  void ensureWritableFolder({ forcePick: true })
    .catch((err: unknown) => {
      if (!aborted(err)) log(`✗ ${err instanceof Error ? err.message : String(err)}`);
    })
    .finally(() => void showFolder());
});
$("run").addEventListener("click", () => void run());

void (async () => {
  await showFolder();
  if (!target) {
    $("run").hidden = true;
    return;
  }
  const accounts = await accountsToExport();
  $("run").textContent = target === "all" ? `Export all (${accounts.length} logins)` : `Export ${accounts[0] ? displayName(accounts[0]) : "login"}`;
})();
