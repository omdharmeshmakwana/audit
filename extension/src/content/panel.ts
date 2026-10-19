// In-page Faible side panel (spec §4.1): active project/shot selector (searchable), the last
// 10 captures with shot assignment, an unassigned counter and a bulk "Assign to shot".
//
// Rendered in a closed Shadow DOM so it neither touches nor inherits the host page's styles.
// Login is deliberately NOT here: key presses on a page are visible to that page's scripts,
// so credentials are typed in the toolbar popup instead (DECISIONS #29). All data is inserted
// with textContent — prompts are user content and never parsed as HTML.

type Send = <T>(msg: Record<string, unknown>) => Promise<T>;

interface State {
  loggedIn: boolean;
  user: { display_name: string; email: string } | null;
  activeShot: { id: string; code: string } | null;
}
interface Project {
  id: string;
  code: string;
  name: string;
}
interface Tree {
  sequences: { name: string; shots: { id: string; code: string; description: string }[] }[];
}
interface Capture {
  id: string;
  service: string;
  kind: string;
  model_id: string | null;
  status: string;
  prompt: string;
  credits_cost: string | null;
  shot_code: string | null;
  output_sha256: string | null;
}
interface Captures {
  unassigned_count: number;
  recent: Capture[];
}

const CSS = `
:host { all: initial; }
* { box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; }
.pill { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-radius: 999px; background: #16181d; color: #f5f6f8; font-size: 12px; cursor: pointer;
  box-shadow: 0 4px 16px rgba(0,0,0,.35); border: 1px solid #2c2f36; }
.pill .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; }
.pill .dot.warn { background: #f59e0b; }
.pill .dot.bad { background: #ef4444; }
.badge { background: #f59e0b; color: #16181d; border-radius: 999px; padding: 0 6px; font-weight: 700; }
.panel { position: fixed; right: 16px; bottom: 60px; z-index: 2147483647; width: 340px; max-height: 70vh; overflow: auto;
  background: #16181d; color: #e8eaee; border: 1px solid #2c2f36; border-radius: 12px; padding: 12px; font-size: 12px;
  box-shadow: 0 12px 40px rgba(0,0,0,.45); }
.hidden { display: none; }
h3 { margin: 0 0 6px; font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: #9aa0ab; font-weight: 600; }
section { margin-bottom: 12px; }
.shot { font-size: 15px; font-weight: 650; }
.warn { color: #fbbf24; }
.bad { color: #f87171; }
.muted { color: #9aa0ab; }
input[type=search], select { width: 100%; padding: 6px 8px; border-radius: 6px; border: 1px solid #3a3e47; background: #0f1115;
  color: #e8eaee; font-size: 12px; margin-bottom: 6px; }
.shots { max-height: 150px; overflow: auto; border: 1px solid #2c2f36; border-radius: 6px; }
.shots button { display: block; width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid #23262d;
  color: #e8eaee; padding: 5px 8px; font-size: 12px; cursor: pointer; }
.shots button:hover, .shots button.active { background: #232a3d; }
.cap { display: grid; grid-template-columns: 18px 1fr; gap: 6px; padding: 6px 0; border-bottom: 1px solid #23262d; }
.cap .meta { color: #9aa0ab; }
.cap .prompt { color: #d3d7de; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row { display: flex; gap: 8px; align-items: center; justify-content: space-between; }
button.act { background: #2f5bd3; color: #fff; border: 0; border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
button.act:disabled { opacity: .4; cursor: default; }
.who { margin-bottom: 6px; line-height: 1.5; }
button.link { background: none; border: 0; padding: 0; margin-left: 6px; color: #8fb0ff; font-size: 11px; cursor: pointer; text-decoration: underline; }
.banner { background: #3b1d1d; color: #fecaca; border-radius: 8px; padding: 8px; margin-bottom: 10px; }
`;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Record<string, unknown> = {},
  ...kids: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag);
  Object.assign(n, props);
  n.append(...kids);
  return n;
}

export function mountPanel(send: Send, isInvalidated: () => boolean): void {
  const host = document.createElement("faible-panel");
  const root = host.attachShadow({ mode: "closed" });
  root.append(el("style", { textContent: CSS }));

  const dot = el("span", { className: "dot" });
  const pillText = el("span", { textContent: "Faible" });
  const badge = el("span", { className: "badge hidden" });
  const pill = el("div", { className: "pill", title: "Faible" }, dot, pillText, badge);
  const panel = el("div", { className: "panel hidden" });
  const gate = el("div");
  gate.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:2147483646;display:none;pointer-events:auto";
  root.append(gate, pill, panel);

  let open = true;
  let shotConfirmed = false;
  let currentEmail = "";
  let loginRequested = false;
  let state: State | null = null;
  let projects: Project[] = [];
  let tree: Tree | null = null;
  let projectId: string | null = null;
  let filter = "";
  let captures: Captures | null = null;
  const selected = new Set<string>();
  let error = "";

  pill.addEventListener("click", () => {
    open = !open || !shotConfirmed;
    panel.classList.toggle("hidden", !open);
    void refresh(true);
  });

  async function loadTree(): Promise<void> {
    if (!projectId) return;
    tree = await send<Tree>({ type: "tree", projectId });
  }

  async function refresh(full = false): Promise<void> {
    if (isInvalidated()) return render();
    try {
      state = await send<State>({ type: "state" });
      if (currentEmail !== (state.user?.email ?? "")) {
        currentEmail = state.user?.email ?? "";
        shotConfirmed = false;
        projects = [];
        projectId = null;
        captures = null;
      }
      if (!state.loggedIn || !state.activeShot) shotConfirmed = false;
      if (!state.loggedIn && !loginRequested) {
        loginRequested = true;
        await send({ type: "openLogin" });
      }
      if (state.loggedIn) loginRequested = false;
      if (state.loggedIn) {
        if (full || projects.length === 0) {
          projects = await send<Project[]>({ type: "projects" });
          projectId ??= projects[0]?.id ?? null;
          await loadTree();
        }
        captures = await send<Captures>({ type: "captures" });
      }
      error = "";
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
    render();
  }

  async function choose(shot: { id: string; code: string } | null): Promise<void> {
    try {
      state = await send<State>({ type: "setActiveShot", shot });
      shotConfirmed = !!shot;
      if (shot) open = false;
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
    render();
  }

  async function assign(): Promise<void> {
    if (!state?.activeShot || selected.size === 0 || !captures) return;
    const shas = captures.recent.filter((c) => selected.has(c.id) && c.output_sha256).map((c) => c.output_sha256!);
    try {
      await send({ type: "assign", sha256s: [...new Set(shas)], shotId: state.activeShot.id });
      selected.clear();
      await refresh();
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      render();
    }
  }

  function renderPill(): void {
    const invalid = isInvalidated();
    const noShot = state?.loggedIn && !state.activeShot;
    dot.className = `dot${invalid || (state && !state.loggedIn) ? " bad" : noShot ? " warn" : ""}`;
    pillText.textContent = invalid
      ? "Faible: reload this tab"
      : !state?.loggedIn
        ? "Faible: log in"
        : state.activeShot
          ? `Faible · ${state.activeShot.code}`
          : "Faible · no shot";
    const n = captures?.unassigned_count ?? 0;
    badge.textContent = String(n);
    badge.classList.toggle("hidden", !n || invalid);
    badge.title = `${n} unassigned capture${n === 1 ? "" : "s"}`;
  }

  function render(): void {
    renderPill();
    const needsGate = !shotConfirmed || !state?.loggedIn;
    gate.style.display = needsGate ? "block" : "none";
    if (document.body) document.body.inert = needsGate;
    if (needsGate) open = true;
    panel.classList.toggle("hidden", !open);
    if (!open) return;
    const parts: Node[] = [];
    if (isInvalidated()) {
      parts.push(
        el("div", { className: "banner", textContent: "Faible was updated or reloaded. Reload this tab to keep capturing — until then nothing on this page is recorded." }),
      );
      panel.replaceChildren(...parts);
      return;
    }
    if (error) parts.push(el("div", { className: "banner", textContent: error }));
    if (!state?.loggedIn) {
      parts.push(
        el("section", {}, el("h3", { textContent: "Not logged in" }), el("div", { textContent: "Sign in with your company email to start tracking. This browser will remember your login for 30 days." })),
      );
      const signIn = el("button", { className: "act", textContent: "Sign in with email and password" });
      signIn.addEventListener("click", () => void send({ type: "openLogin" }).catch((err: Error) => { error = err.message; render(); }));
      parts.push(signIn);
      panel.replaceChildren(...parts);
      return;
    }
    // Who the captures are recorded under. The studio shares one Higgsfield login, so this
    // is the only place a person can see they are about to work as someone else.
    const who = el("div", { className: "who" }, el("span", { textContent: "Recording as " }), el("strong", { textContent: state.user?.email ?? "" }));
    const switchBtn = el("button", { className: "link", textContent: "Not you? Switch account" });
    switchBtn.addEventListener("click", () => void send({ type: "openLogin" }).catch((err: Error) => { error = err.message; render(); }));
    who.append(switchBtn);
    parts.push(who);
    if (!shotConfirmed) parts.push(el("div", { className: "banner", textContent: "Select the scene you are working on before using Higgsfield." }));
    const dashboard = el("a", { href: "http://localhost:8000/dashboard", target: "_blank", rel: "noopener", textContent: "Open live dashboard" });
    parts.push(dashboard);

    // Active shot
    const shotSec = el("section", {}, el("h3", { textContent: "Active scene" }));
    shotSec.append(
      el("div", {
        className: state.activeShot ? "shot" : "shot warn",
        textContent: state.activeShot ? state.activeShot.code : "None — new captures will be unassigned",
      }),
    );
    if (projects.length > 1) {
      const sel = el("select");
      for (const p of projects) sel.append(el("option", { value: p.id, textContent: `${p.code} — ${p.name}`, selected: p.id === projectId }));
      sel.addEventListener("change", () => {
        projectId = sel.value;
        void loadTree().then(render);
      });
      shotSec.append(sel);
    }
    const search = el("input", { type: "search", placeholder: "Search scenes…", value: filter });
    search.addEventListener("input", () => {
      filter = search.value;
      renderShots();
    });
    const shotList = el("div", { className: "shots" });
    const renderShots = () => {
      const f = filter.trim().toLowerCase();
      const buttons: Node[] = [];
      for (const seq of tree?.sequences ?? []) {
        for (const s of seq.shots) {
          if (f && !`${s.code} ${s.description}`.toLowerCase().includes(f)) continue;
          const b = el("button", { className: state?.activeShot?.id === s.id ? "active" : "" }, s.code, el("span", { className: "muted", textContent: s.description ? ` — ${s.description}` : "" }));
          b.addEventListener("click", () => void choose({ id: s.id, code: s.code }));
          buttons.push(b);
        }
      }
      shotList.replaceChildren(...(buttons.length ? buttons : [el("div", { className: "muted", textContent: " No scenes match." })]));
    };
    renderShots();
    shotSec.append(search, shotList);
    if (state.activeShot) {
      const clear = el("button", { className: "act", textContent: "Clear active scene" });
      clear.style.marginTop = "6px";
      clear.style.background = "#3a3e47";
      clear.addEventListener("click", () => void choose(null));
      shotSec.append(clear);
    }
    parts.push(shotSec);

    // Recent captures
    const capSec = el("section");
    const n = captures?.unassigned_count ?? 0;
    capSec.append(el("div", { className: "row" }, el("h3", { textContent: "Last 10 captures" }), el("span", { className: n ? "warn" : "muted", textContent: `${n} unassigned` })));
    for (const c of captures?.recent ?? []) {
      const assignable = !c.shot_code && !!c.output_sha256;
      const box = el("input", { type: "checkbox", disabled: !assignable, checked: selected.has(c.id), title: assignable ? "Select to assign" : c.shot_code ? "Already assigned" : "No output yet — failed, cancelled or still hashing" });
      box.addEventListener("change", () => {
        if (box.checked) selected.add(c.id);
        else selected.delete(c.id);
        render();
      });
      const meta = `${c.service} · ${c.kind} · ${c.model_id ?? "?"} · ${c.status}${c.credits_cost ? ` · ${c.credits_cost} cr` : ""}`;
      capSec.append(
        el(
          "div",
          { className: "cap" },
          box,
          el("div", {}, el("div", { className: "meta", textContent: meta }), el("div", { className: "prompt", textContent: c.prompt || "(no prompt)", title: c.prompt }), el("div", { className: c.shot_code ? "muted" : "warn", textContent: c.shot_code ?? "unassigned" })),
        ),
      );
    }
    if (!captures?.recent.length) capSec.append(el("div", { className: "muted", textContent: "Nothing captured yet." }));
    const assignBtn = el("button", {
      className: "act",
      textContent: state.activeShot ? `Assign ${selected.size || ""} to ${state.activeShot.code}` : "Pick an active scene to assign",
      disabled: !state.activeShot || selected.size === 0,
    });
    assignBtn.addEventListener("click", () => void assign());
    capSec.append(el("div", { className: "row" }, assignBtn));
    parts.push(capSec);

    panel.replaceChildren(...parts);
  }

  const mount = () => {
    document.documentElement.append(host);
    void refresh(true);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
  // Every 4 s while open; every 20 s closed (keeps the pill's badge and shot current).
  let tick = 0;
  setInterval(() => {
    tick++;
    if (open || tick % 5 === 0) void refresh();
  }, 4000);
}
