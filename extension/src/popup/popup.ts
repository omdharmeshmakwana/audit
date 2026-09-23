interface Status {
  queued: number;
  enabled: boolean;
  stats: {
    captured: number;
    sent: number;
    dropped: number;
    lastError: string | null;
    lastSentAt: string | null;
    hosts: Record<string, number>;
    actions?: { at: string; label: string }[];
  };
}

const $ = (id: string) => document.getElementById(id)!;

async function refresh(): Promise<void> {
  const s = (await chrome.runtime.sendMessage({ type: "status" })) as Status;
  ($("enabled") as HTMLInputElement).checked = s.enabled;
  $("captured").textContent = String(s.stats.captured);
  $("sent").textContent = String(s.stats.sent);
  $("queued").textContent = String(s.queued);
  $("dropped").textContent = String(s.stats.dropped);
  $("lastSent").textContent = s.stats.lastSentAt ?? "–";
  $("error").textContent = s.stats.lastError ?? "–";
  const actions = s.stats.actions ?? [];
  $("actions").replaceChildren(
    ...(actions.length ? actions : [{ at: "", label: "none yet" }]).map((a) => {
      const li = document.createElement("li");
      li.textContent = a.at ? `${new Date(a.at).toLocaleTimeString()} ${a.label}` : a.label;
      return li;
    }),
  );
  const hosts = $("hosts");
  hosts.replaceChildren(
    ...Object.entries(s.stats.hosts)
      .sort((a, b) => b[1] - a[1])
      .map(([h, n]) => {
        const li = document.createElement("li");
        li.textContent = `${h} (${n})`;
        return li;
      }),
  );
}

$("enabled").addEventListener("change", async (e) => {
  await chrome.runtime.sendMessage({ type: "setEnabled", enabled: (e.target as HTMLInputElement).checked });
  await refresh();
});
$("flush").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "flush" });
  await refresh();
});
$("clear").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "clear" });
  await refresh();
});

void refresh();
setInterval(() => void refresh(), 1000);
