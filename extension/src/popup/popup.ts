// Toolbar popup: Faible login (typed here, in the extension's own page, where the service
// site's scripts can't observe it), sync status and recent activity.

interface State {
  loggedIn: boolean;
  user: { email: string; display_name: string } | null;
  activeShot: { id: string; code: string } | null;
  queue: {
    pending: number;
    status: { sent: number; lastSentAt: string | null; lastError: string | null; rejected: { at: string; type: string; reason: string }[] };
  };
  activity: { at: string; label: string }[];
  discovery: boolean;
}

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

async function send<T>(msg: Record<string, unknown>): Promise<T> {
  const r = (await chrome.runtime.sendMessage(msg)) as { ok: boolean; value?: T; error?: string };
  if (!r.ok) throw new Error(r.error ?? "failed");
  return r.value as T;
}

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function list(el: HTMLElement, items: { at: string; label: string }[], empty: string): void {
  el.replaceChildren(
    ...(items.length ? items : [{ at: "", label: empty }]).map((it) => {
      const li = document.createElement("li");
      if (it.at) {
        const t = document.createElement("time");
        t.textContent = time(it.at);
        li.append(t);
      }
      li.append(it.label);
      return li;
    }),
  );
}

function render(s: State): void {
  $("login").classList.toggle("hidden", s.loggedIn);
  $("app").classList.toggle("hidden", !s.loggedIn);
  $("who").textContent = s.user ? s.user.display_name : "";
  if (!s.loggedIn) return;
  const shot = $("shot");
  shot.textContent = s.activeShot ? s.activeShot.code : "No active shot — captures will be unassigned";
  shot.className = s.activeShot ? "shot" : "shot warn";
  $("pending").textContent = String(s.queue.pending);
  $("sent").textContent = String(s.queue.status.sent);
  $("last-sent").textContent = s.queue.status.lastSentAt ? time(s.queue.status.lastSentAt) : "–";
  $("error").textContent = s.queue.status.lastError ?? "–";
  list($("rejected"), s.queue.status.rejected.map((r) => ({ at: r.at, label: `${r.type} ${r.reason}` })), "");
  if (!s.queue.status.rejected.length) $("rejected").replaceChildren();
  list($("activity"), s.activity, "nothing captured yet");
  $<HTMLInputElement>("discovery").checked = s.discovery;
}

async function refresh(): Promise<void> {
  render(await send<State>({ type: "state" }));
}

$("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  send<State>({ type: "login", email: $<HTMLInputElement>("email").value, password: $<HTMLInputElement>("password").value })
    .then(render)
    .catch((err: Error) => ($("login-error").textContent = err.message));
});
$("logout").addEventListener("click", () => void send<State>({ type: "logout" }).then(render));
$("flush").addEventListener("click", () => void send<State>({ type: "flush" }).then(render));
$("discovery").addEventListener("change", (e) =>
  void send<State>({ type: "setDiscovery", enabled: (e.target as HTMLInputElement).checked }).then(render),
);

void refresh();
setInterval(() => void refresh(), 1500);
