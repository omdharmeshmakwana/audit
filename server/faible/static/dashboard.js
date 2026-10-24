// Faible dashboard (DECISIONS #59): employee analytics, the studio comparison, the live
// feed, saved usage pages and Higgsfield history. One filter bar drives every tab; every
// aggregate drills down into the generations behind it. All values are inserted with
// textContent (prompts are user content and are never parsed as HTML), and external CDN
// media is linked, never loaded automatically.
"use strict";

const $ = (id) => document.getElementById(id);
const NONE = "(none)";
const FILTERS = ["employee_id", "service", "model", "status", "project_id", "shot", "kind"];
const BROWSER_TZ = ((tz) => (tz === "Asia/Calcutta" ? "Asia/Kolkata" : tz))(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
const pageSize = 50;
const historyPageSize = 48;
// The page renders the studio-only history tab for directors/admins alone (dashboard.html);
// the server enforces the same role model on every endpoint.
const IS_MANAGER = !!document.querySelector("[data-tab=history]");

function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function anchor(text, href, cls) {
  const a = node("a", text, cls);
  a.href = href;
  return a;
}
function button(text, onClick, cls) {
  const b = node("button", text, cls);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

// ---------------------------------------------------------------- state and URL

const state = { tab: "overview", range: "all", from: "", to: "", tz: BROWSER_TZ };
for (const k of FILTERS) state[k] = "";
(() => {
  const q = new URL(location.href).searchParams;
  for (const k of [...FILTERS, "range", "from", "to", "tz", "tab"]) if (q.get(k)) state[k] = q.get(k);
  if (q.get("artist_id") && !state.employee_id) state.employee_id = q.get("artist_id"); // old links
  if ((state.from || state.to) && !q.get("range")) state.range = "custom";
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: state.tz });
  } catch {
    state.tz = BROWSER_TZ; // an unknown zone in a pasted link
  }
})();

function syncUrl() {
  const url = new URL(location.href);
  for (const k of [...FILTERS, "range", "from", "to", "tz", "tab", "artist_id"]) url.searchParams.delete(k);
  for (const k of FILTERS) if (state[k]) url.searchParams.set(k, state[k]);
  if (state.range !== "all") url.searchParams.set("range", state.range);
  if (state.range === "custom") {
    if (state.from) url.searchParams.set("from", state.from);
    if (state.to) url.searchParams.set("to", state.to);
  }
  if (state.tz !== BROWSER_TZ) url.searchParams.set("tz", state.tz);
  if (state.tab !== "overview") url.searchParams.set("tab", state.tab);
  history.replaceState(null, "", url);
}

function todayIn(tz) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}
function addDays(day, n) {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
/** [from, to] as dates in the selected time zone ("" = open). */
function rangeDates() {
  const today = todayIn(state.tz);
  switch (state.range) {
    case "7": case "30": case "90": return [addDays(today, 1 - Number(state.range)), today];
    case "month": return [`${today.slice(0, 7)}-01`, today];
    case "lastmonth": {
      const last = addDays(`${today.slice(0, 7)}-01`, -1);
      return [`${last.slice(0, 7)}-01`, last];
    }
    case "custom": return [state.from, state.to];
    default: return ["", ""];
  }
}
function apiParams(extra = {}) {
  const p = new URLSearchParams();
  const [from, to] = rangeDates();
  if (from) p.set("from", from);
  if (to) p.set("to", to);
  p.set("tz", state.tz);
  for (const k of FILTERS) if (state[k]) p.set(k, state[k]);
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  return p;
}

async function getJson(path, params) {
  const response = await fetch(`${path}?${params}`, { cache: "no-store" });
  if (response.status === 401) {
    location.href = "/login";
    throw new Error("Signed out");
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch { /* not JSON */ }
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return response.json();
}

// ---------------------------------------------------------------- formatting

const fmtNum = (v) => (v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
const credits = (values) => Object.entries(values || {}).map(([currency, amount]) => `${fmtNum(amount)} ${currency}`).join(" · ") || "No reported cost";
function when(value, withTime = true) {
  if (!value) return "—";
  const options = withTime ? { dateStyle: "medium", timeStyle: "short", timeZone: state.tz } : { dateStyle: "medium", timeZone: state.tz };
  return new Intl.DateTimeFormat(undefined, options).format(new Date(value));
}
const date = (value) => (value ? when(value) : "No activity yet");
const tzLabel = () => (state.tz === "Asia/Kolkata" ? "IST (Asia/Kolkata)" : state.tz);

function table(headers, rows) {
  const result = node("table");
  const head = node("tr");
  for (const header of headers) head.append(node("th", header));
  const body = node("tbody");
  for (const values of rows) {
    const row = node("tr");
    for (const value of values) {
      const cell = node("td");
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value ?? "—";
      row.append(cell);
    }
    body.append(row);
  }
  result.append(node("thead"), body);
  result.firstChild.append(head);
  const wrap = node("div", null, "table-wrap");
  wrap.append(result);
  return wrap;
}
function bar(value, max) {
  const outer = node("span", null, "bar");
  const fill = node("span");
  fill.style.width = `${max ? Math.max(2, (value / max) * 100) : 0}%`;
  outer.append(fill);
  return outer;
}
function badge(text, cls) { return node("span", text, `badge ${cls}`); }
function emptyState(text) { return node("p", text, "empty-state"); }
function errorState(message, retry) {
  const box = node("div", `Could not load: ${message}`, "error");
  if (retry) box.append(" ", button("Retry", retry));
  return box;
}
function card(title, sub, ...content) {
  const box = node("article", null, "panel card");
  box.append(node("h3", title));
  if (sub) box.append(node("p", sub, "muted small"));
  box.append(...content);
  return box;
}
function preview(asset, alt) {
  if (asset && asset.stored && asset.media_type) {
    const video = asset.media_type.startsWith("video");
    const media = node(video ? "video" : "img", null, "thumb");
    media.src = `/api/v1/assets/${encodeURIComponent(asset.id)}/file`;
    if (video) { media.muted = true; media.preload = "metadata"; } else { media.loading = "lazy"; media.alt = alt; }
    const link = anchor("", `/assets/${encodeURIComponent(asset.id)}`);
    link.append(media);
    return link;
  }
  if (asset) return anchor(`hash ${asset.sha256.slice(0, 8)}`, `/assets/${encodeURIComponent(asset.id)}`, "thumb hashonly");
  return node("span", "unresolved", "thumb hashonly");
}

// ---------------------------------------------------------------- filter controls

const controls = {
  employee_id: $("f-employee"), service: $("f-service"), model: $("f-model"), status: $("f-status"),
  project_id: $("f-project"), shot: $("f-shot"), kind: $("f-kind"),
};
let options = null;

function fill(select, allLabel, items) {
  const current = state[Object.keys(controls).find((k) => controls[k] === select)] || "";
  const opts = [["", allLabel], ...items];
  if (current && !opts.some(([v]) => v === current)) opts.push([current, current]);
  select.replaceChildren(...opts.map(([value, label]) => { const o = node("option", label); o.value = value; return o; }));
  select.value = current;
}
function syncControls() {
  $("f-range").value = state.range;
  $("f-from").value = state.from;
  $("f-to").value = state.to;
  for (const el of document.querySelectorAll(".custom-range")) el.hidden = state.range !== "custom";
  const zones = [...new Set([BROWSER_TZ, "Asia/Kolkata", "UTC", state.tz])];
  $("f-tz").replaceChildren(...zones.map((z) => { const o = node("option", z === BROWSER_TZ ? `${z} (this browser)` : z === "Asia/Kolkata" ? "IST (Asia/Kolkata)" : z); o.value = z; return o; }));
  $("f-tz").value = state.tz;
  if (options) {
    const manager = IS_MANAGER;
    fill(controls.employee_id, manager ? "All employees" : "My account", manager ? options.employees.map((e) => [e.id, e.email]) : []);
    controls.employee_id.disabled = !manager;
    fill(controls.service, "All services", options.services.map((s) => [s, s]));
    fill(controls.model, "All models", options.models.map((m) => [m, m === NONE ? "Unknown model" : m]));
    fill(controls.status, "All statuses", options.statuses.map((s) => [s, s]));
    fill(controls.project_id, "All projects", options.projects.map((p) => [p.id, p.code]));
    fill(controls.shot, "All shots", [[NONE, "Unassigned"], ...options.shots.filter((s) => !state.project_id || s.project_id === state.project_id).map((s) => [s.id, `${s.project} / ${s.code}`])]);
    fill(controls.kind, "All types", options.kinds.map((k) => [k, k]));
  }
  const active = [];
  const [from, to] = rangeDates();
  if (from || to) active.push(`${from || "…"} – ${to || "…"}`);
  for (const k of FILTERS) {
    if (!state[k]) continue;
    const select = controls[k];
    const label = select ? [...select.options].find((o) => o.value === state[k])?.textContent : state[k];
    active.push(`${k.replace("_id", "")}: ${label || state[k]}`);
  }
  $("filter-summary").textContent = `Times in ${tzLabel()}. ${active.length ? `Filtered by ${active.join(" · ")}.` : "No filters: complete recorded history."}`;
}

function filtersChanged() {
  syncControls();
  syncUrl();
  lastAnalytics = null;
  offset = 0;
  historyOffset = 0;
  void load(state.tab);
}
for (const [key, select] of Object.entries(controls)) {
  select.addEventListener("change", () => {
    state[key] = select.value;
    if (key === "project_id") state.shot = "";
    filtersChanged();
  });
}
$("f-range").addEventListener("change", () => { state.range = $("f-range").value; filtersChanged(); });
$("f-from").addEventListener("change", () => { state.from = $("f-from").value; filtersChanged(); });
$("f-to").addEventListener("change", () => { state.to = $("f-to").value; filtersChanged(); });
$("f-tz").addEventListener("change", () => { state.tz = $("f-tz").value; filtersChanged(); });
$("f-clear").addEventListener("click", () => {
  for (const k of FILTERS) if (k !== "employee_id" || IS_MANAGER) state[k] = "";
  Object.assign(state, { range: "all", from: "", to: "" });
  filtersChanged();
});

/** Apply filters from an aggregate and open the tab that shows what is behind it. */
function drill(changes, tab = "live") {
  Object.assign(state, changes);
  if (changes.from || changes.to) state.range = "custom";
  state.tab = tab;
  filtersChanged();
  showTab(tab, false);
}

// ---------------------------------------------------------------- tabs

const tabs = [...document.querySelectorAll("[data-tab]")];
const panels = [...document.querySelectorAll("[data-panel]")];
function showTab(tab, reload = true) {
  if (!tabs.some((t) => t.dataset.tab === tab)) tab = "overview";
  state.tab = tab;
  for (const t of tabs) t.setAttribute("aria-selected", String(t.dataset.tab === tab));
  for (const p of panels) p.hidden = p.dataset.panel !== tab;
  syncUrl();
  if (reload) void load(tab);
}
for (const t of tabs) t.addEventListener("click", () => showTab(t.dataset.tab));

async function load(tab) {
  if (tab === "overview") return loadOverview();
  if (tab === "live") return refresh();
  if (tab === "usage") return loadUsage();
  if (tab === "history") return loadHistory();
}

// ---------------------------------------------------------------- overview (analytics)

let lastAnalytics = null;
let analyticsInFlight = null; // { key, promise }: concurrent callers share one request

/** Analytics for the current filters; null when the filters changed while it loaded. */
function fetchAnalytics() {
  const params = apiParams();
  const key = params.toString();
  if (analyticsInFlight?.key === key) return analyticsInFlight.promise;
  const promise = getJson("/api/v1/analytics", params)
    .then((data) => {
      if (apiParams().toString() !== key) return null;
      lastAnalytics = data;
      options = data.options;
      syncControls();
      return data;
    })
    .finally(() => {
      if (analyticsInFlight?.key === key) analyticsInFlight = null;
    });
  analyticsInFlight = { key, promise };
  return promise;
}

async function loadOverview(quiet = false) {
  const status = $("ov-status");
  if (!quiet) status.textContent = "Loading…";
  try {
    const data = await fetchAnalytics();
    if (!data) return;
    status.textContent = `Updated ${new Date().toLocaleTimeString()} · ${data.scope.employee ? `Employee: ${data.scope.employee.label}` : "All employees"}`;
    renderOverview(data);
  } catch (err) {
    status.replaceChildren(errorState(err.message, () => loadOverview()));
  }
}

function kpi(label, value, note, cls) {
  const box = node("div", null, `kpi ${cls || ""}`);
  box.append(node("span", label, "muted"), node("strong", value));
  if (note) box.append(note instanceof Node ? note : node("small", note, "muted"));
  return box;
}

function renderOverview(d) {
  const t = d.totals;
  const estimate = t.estimated_ui_credits;
  $("kpis").replaceChildren(
    kpi("Generations", fmtNum(t.count), `${fmtNum(t.succeeded)} completed · ${fmtNum(t.failed)} failed/cancelled · ${fmtNum(t.running)} queued/running`),
    kpi("Reported cost", credits(t.credits), badge("service reported", "ev-service_reported")),
    kpi("Estimated UI credits", estimate === null ? "—" : fmtNum(estimate), badge("estimate · Higgsfield ÷ 100", "ev-inferred")),
    kpi("Cost unknown", fmtNum(t.cost_unknown), t.cost_from_history ? `${fmtNum(t.cost_from_history)} costs taken from Higgsfield history` : "generations with no reported cost"),
    kpi("Prompts", fmtNum(t.prompts.total), `${fmtNum(t.prompts.distinct)} distinct`),
    kpi("Inputs / outputs", `${fmtNum(t.inputs.total)} / ${fmtNum(t.outputs.total)}`, `${fmtNum(t.inputs.resolved)} / ${fmtNum(t.outputs.resolved)} linked to assets`),
    kpi("Downloads", fmtNum(t.downloads), `${fmtNum(d.downloads.hashed)} hashed`),
    kpi("Usage pages saved", fmtNum(d.usage_captures.total)),
  );
  $("rules").replaceChildren(
    node("li", `Times, days and the period are in ${tzLabel()}.`),
    ...Object.values(d.rules).map((r) => node("li", r)),
    node("li", "Downloads and usage pages filter by employee, period and service only."),
  );

  const body = [];
  if (!t.count && !(d.history && d.history.totals.count)) {
    body.push(emptyState("No generations match these filters. Clear filters, pick a longer period, or capture on Higgsfield/Magnific with the Faible extension."));
  }
  body.push(card("Generations over time", `Per day, ${tzLabel()}. Select a day to see its generations.`, dayChart(d.by_day)));

  const grid = node("div", null, "grid-cards");
  const dims = [
    ["Service", "service", "service"],
    ["Model", "model", "model"],
    ["Status", "status", "status"],
    ["Generation type", "kind", "kind"],
    ["Project", "project", "project_id"],
    ["Shot", "shot", "shot"],
  ];
  for (const [title, dim, filter] of dims) grid.append(card(`By ${title.toLowerCase()}`, null, breakdown(d.breakdowns[dim], dim, filter)));
  grid.append(card("By hour of day", `Hour in ${tzLabel()}.`, hourChart(d.by_hour)));
  body.push(grid);

  if (d.employees) {
    body.push(card("Employee comparison", "Ranked by generation volume. Select an employee for their dashboard, or a count for their generations.", employeeTable(d.employees)));
    body.push(card("Model usage by employee", "Generations per employee and model.", modelMatrix(d.employees, d.employee_models)));
  }
  body.push(card("Prompt history", "Most recent prompts for these filters. Upstream generations produced this one's inputs; downstream ones used its outputs. “Open chain” shows the full prompt chain with word-level diffs.", promptList(d.prompts)));
  body.push(card("Downloads", "Files downloaded from the services, with the asset they match.", downloadTable(d.downloads)));
  if (d.history) body.push(card("Higgsfield history (unattributed)", d.history.note || "History records no employee's extension captured live. Counted here only, never in an employee's totals.", historySummary(d.history)));
  $("ov-body").replaceChildren(...body);
}

function dayChart(rows) {
  if (!rows.length) return emptyState("No generations in this period.");
  const byDay = new Map(rows.map((r) => [r.day, r]));
  let days = rows.map((r) => r.day);
  const span = (Date.parse(days[days.length - 1]) - Date.parse(days[0])) / 86400000;
  if (span <= 120) { days = []; for (let d = rows[0].day; d <= rows[rows.length - 1].day; d = addDays(d, 1)) days.push(d); }
  const max = Math.max(...rows.map((r) => r.count));
  const bars = node("div", null, "bars");
  for (const day of days) {
    const r = byDay.get(day) || { count: 0, failed: 0, credits: {} };
    const col = button("", () => r.count && drill({ range: "custom", from: day, to: day }), "bar-col");
    col.title = `${day}: ${r.count} generation(s), ${r.failed} failed/cancelled · ${credits(r.credits)}`;
    col.disabled = !r.count;
    const fill = node("span");
    fill.style.height = `${r.count ? Math.max(3, (r.count / max) * 100) : 0}%`;
    col.append(fill, node("small", day.slice(5)));
    bars.append(col);
  }
  const wrap = node("div", null, "chart-scroll");
  wrap.append(bars);
  return wrap;
}

function hourChart(rows) {
  if (!rows.length) return emptyState("No generations.");
  const counts = Array(24).fill(0);
  for (const r of rows) counts[r.hour] = r.count;
  const max = Math.max(...counts);
  const bars = node("div", null, "bars hours");
  counts.forEach((count, hour) => {
    const col = node("div", null, "bar-col");
    col.title = `${String(hour).padStart(2, "0")}:00 – ${count} generation(s)`;
    const fill = node("span");
    fill.style.height = `${count ? Math.max(3, (count / max) * 100) : 0}%`;
    col.append(fill, node("small", hour % 3 === 0 ? String(hour) : ""));
    bars.append(col);
  });
  return bars;
}

function breakdown(rows, dim, filter) {
  if (!rows.length) return emptyState("Nothing to break down.");
  const max = Math.max(...rows.map((r) => r.count));
  const apply = (r) => (dim === "project" && r.key === NONE ? { shot: NONE } : { [filter]: r.key });
  return table(
    ["", "Generations", "Completed", "Failed", "Reported cost", "Unknown cost"],
    rows.map((r) => {
      const label = button(dim === "model" && r.service ? `${r.label} · ${r.service}` : r.label, () => drill(apply(r), "overview"), "linklike");
      label.title = "Filter the dashboard by this value";
      const count = node("span", null, "count-cell");
      const open = button(fmtNum(r.count), () => drill(apply(r)), "linklike");
      open.title = "Show these generations in the live feed";
      count.append(bar(r.count, max), open);
      return [label, count, fmtNum(r.succeeded), fmtNum(r.failed), credits(r.credits), fmtNum(r.cost_unknown)];
    }),
  );
}

function employeeTable(rows) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return table(
    ["Rank", "Employee", "Generations", "Completed", "Failed", "Reported cost", "Est. UI credits", "Unknown cost", "Last generation"],
    rows.map((r) => {
      const who = button(r.label, () => drill({ employee_id: r.key === NONE ? "" : r.key }, "overview"), "linklike");
      const count = node("span", null, "count-cell");
      count.append(bar(r.count, max), button(fmtNum(r.count), () => drill({ employee_id: r.key === NONE ? "" : r.key }), "linklike"));
      return [r.rank ? `#${r.rank}` : "—", who, count, fmtNum(r.succeeded), fmtNum(r.failed), credits(r.credits), r.estimated_ui_credits === null ? "—" : fmtNum(r.estimated_ui_credits), fmtNum(r.cost_unknown), r.last_at ? when(r.last_at) : "—"];
    }),
  );
}

function modelMatrix(employees, cells) {
  if (!cells.length) return emptyState("No generations.");
  const totals = new Map();
  for (const c of cells) totals.set(c.model, (totals.get(c.model) || 0) + c.count);
  const models = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([m]) => m);
  const top = models.slice(0, 6);
  const count = (employee, model) => cells.filter((c) => c.employee === employee && (model ? c.model === model : !top.includes(c.model))).reduce((n, c) => n + c.count, 0);
  const headers = ["Employee", ...top.map((m) => (m === NONE ? "Unknown model" : m)), ...(models.length > top.length ? [`Other (${models.length - top.length})`] : [])];
  const rows = employees.filter((e) => e.count).map((e) => [
    e.label,
    ...top.map((m) => {
      const n = count(e.key, m);
      return n ? button(fmtNum(n), () => drill({ employee_id: e.key === NONE ? "" : e.key, model: m })) : "—";
    }),
    ...(models.length > top.length ? [fmtNum(count(e.key, null))] : []),
  ]);
  return table(headers, rows);
}

function neighbours(label, items) {
  if (!items.length) return null;
  const line = node("div", null, "muted small");
  line.append(`${label}: `);
  items.forEach((n, i) => {
    if (i) line.append(", ");
    const text = `${n.model_id || n.service} ${when(n.started_at)}${n.email ? ` · ${n.email}` : ""}`;
    line.append(n.chain_url ? anchor(text, n.chain_url) : node("span", text));
  });
  return line;
}

function promptList(rows) {
  if (!rows.length) return emptyState("No prompts for these filters.");
  const list = node("div", null, "prompt-list");
  for (const r of rows) {
    const item = node("div", null, "prompt-item");
    const head = node("div", null, "head");
    head.append(node("strong", `${r.service} · ${r.model_id || "Unknown model"}`), node("span", r.status, `pill status-${r.status}`), badge(r.evidence.replace("_", " "), `ev-${r.evidence}`));
    item.append(head);
    item.append(node("div", `${when(r.started_at)}${r.email ? ` · ${r.email}` : ""} · ${r.shot_code ? `${r.project_code} / ${r.shot_code}` : "Unassigned"}${r.credits_cost ? ` · ${fmtNum(r.credits_cost)} ${r.credits_currency}` : ""}`, "muted small"));
    item.append(node("div", r.prompt, "prompt"));
    if (r.negative_prompt) item.append(node("div", `Negative: ${r.negative_prompt}`, "muted small"));
    const io = node("div", null, "thumbs");
    for (const i of r.inputs) { const f = node("figure"); f.append(preview(i.asset, "input"), node("figcaption", `in · ${i.role}`)); io.append(f); }
    for (const o of r.outputs) { const f = node("figure"); f.append(preview(o.asset, "output"), node("figcaption", `out #${o.index + 1}`)); io.append(f); }
    if (io.childElementCount) item.append(io);
    for (const line of [neighbours("Upstream", r.upstream), neighbours("Downstream", r.downstream)]) if (line) item.append(line);
    const links = node("div", null, "links");
    if (r.chain_url) links.append(anchor("Open chain", r.chain_url));
    if (r.shot_id) links.append(anchor("Shot page", `/shots/${encodeURIComponent(r.shot_id)}`));
    if (links.childElementCount) item.append(links);
    list.append(item);
  }
  return list;
}

function downloadTable(downloads) {
  if (!downloads.recent.length) return emptyState("No downloads for these filters.");
  return table(
    ["When", "File", "Service", "Employee", "Asset", "Hash"],
    downloads.recent.map((d) => [
      when(d.at), d.filename, d.service || "—", d.email || "—",
      d.asset_id ? anchor("Open asset", `/assets/${encodeURIComponent(d.asset_id)}`) : "Not matched",
      d.hashed ? `${d.hash_method} (${d.evidence})` : "Unhashed",
    ]),
  );
}

function historySummary(h) {
  const t = h.totals;
  const wrap = node("div");
  wrap.append(node("p", `${fmtNum(t.count)} history records match · ${fmtNum(t.unattributed)} unattributed · unattributed reported cost ${credits(t.credits)}${t.estimated_ui_credits ? ` (≈ ${fmtNum(t.estimated_ui_credits)} UI credits, estimate)` : ""}. ${h.credit_note}`));
  if (h.by_login.length) {
    wrap.append(node("h4", "By Higgsfield login"));
    wrap.append(table(["Login (shared subscription)", "Records", "Unattributed", "Unattributed cost"], h.by_login.map((r) => [
      button(r.key[0] || "Unknown login", () => { historyLogin = r.key[0] || NONE; showTab("history"); }, "linklike"),
      fmtNum(r.count), fmtNum(r.unattributed), credits(r.credits),
    ])));
  }
  if (h.by_model.length) {
    wrap.append(node("h4", "By model"));
    wrap.append(table(["Model", "Records", "Unattributed"], h.by_model.slice(0, 12).map((r) => [
      button(r.key[0] || "Unknown model", () => drill({ model: r.key[0] || NONE }, "history"), "linklike"), fmtNum(r.count), fmtNum(r.unattributed),
    ])));
  }
  wrap.append(button("Open Higgsfield history", () => showTab("history")));
  return wrap;
}

// ---------------------------------------------------------------- live feed (filtered)

const feed = $("feed");
const overview = $("overview");
const sync = $("sync");
const previous = $("previous");
const next = $("next");
const pageStatus = $("page-status");
let offset = 0;
let busy = false;
let selectionVersion = 0;
const historyCards = new Map();
let openSummaries = new Set();

function renderSummary(artist) {
  const summaryCard = node("article", null, "panel artist-card");
  summaryCard.dataset.artistId = artist.id;
  const link = button(artist.email, () => drill({ employee_id: artist.id }, "overview"), "linklike");
  const heading = node("h2");
  heading.append(link);
  summaryCard.append(heading, node("p", `${artist.display_name} · ${artist.role}`, "muted"));
  const metrics = node("div", null, "artist-metrics");
  for (const [label, value] of [
    ["Active shot", artist.active_shot || "No shot selected"],
    ["Generations", artist.generation_count],
    ["Reported spend", credits(artist.credits)],
    ["Unknown costs", artist.unknown_cost_count],
    ["Running", artist.statuses.running || 0],
    ["Queued", artist.statuses.queued || 0],
    ["Completed", artist.statuses.succeeded || 0],
    ["Failed / cancelled", `${artist.statuses.failed || 0} / ${artist.statuses.cancelled || 0}`],
  ]) {
    const metric = node("div");
    metric.append(node("span", label, "muted"), node("strong", String(value)));
    metrics.append(metric);
  }
  summaryCard.append(metrics, node("p", `Last generation: ${date(artist.last_generation_at)}`, "muted"));
  summaryCard.append(node("p", `Last login: ${date(artist.last_login_at)} · Shot selected: ${date(artist.active_shot_set_at)}`, "muted"));
  const details = node("details");
  details.open = openSummaries.has(artist.id);
  details.append(node("summary", "Models and shots used"));
  details.append(node("h3", "Models"), table(["Service / model", "Requests", "Reported spend"], artist.models.map((model) => [`${model.service} / ${model.model_id || "Unknown model"}`, model.count, credits(model.credits)])));
  details.append(node("h3", "Shots"), table(["Shot", "Requests", "Reported spend"], artist.shots.map((shot) => [shot.shot_code, shot.count, credits(shot.credits)])));
  summaryCard.append(details);
  return summaryCard;
}

async function refresh() {
  if (busy) return;
  busy = true;
  const version = selectionVersion;
  try {
    const params = apiParams({ offset, limit: pageSize, artist_id: state.employee_id });
    params.delete("employee_id");
    const data = await getJson("/api/v1/dashboard", params);
    if (version !== selectionVersion) return;
    openSummaries = new Set([...overview.querySelectorAll(".artist-card")].filter((c) => c.querySelector("details")?.open).map((c) => c.dataset.artistId));
    overview.replaceChildren(...data.summaries.map(renderSummary));
    const sections = [];
    for (const activity of data.activities) {
      const signature = JSON.stringify(activity);
      const previousCard = historyCards.get(activity.id);
      if (previousCard?.signature === signature) {
        sections.push(previousCard.card);
        continue;
      }
      const historyCard = node("article", null, "panel history-card");
      historyCard.append(node("strong", `${activity.email} · ${activity.shot_code} · ${activity.service} · ${activity.model_id || "Unknown model"}`));
      historyCard.append(node("p", `${activity.status} · ${date(activity.started_at)} · ${activity.credits_cost ?? "Unknown"} ${activity.credits_currency}`));
      if (activity.warnings.length) historyCard.append(node("div", `🔴 Prompt warning: ${activity.warnings.join(", ")}`, "error"));
      historyCard.append(node("div", activity.prompt || "No prompt reported", "prompt"));
      const outputs = node("div", null, "dashboard-outputs");
      for (const output of activity.outputs) {
        if (!output.asset_id) continue;
        const link = anchor(`Open output #${output.index + 1} and its chain`, `/assets/${encodeURIComponent(output.asset_id)}`);
        const figure = node("figure");
        if (output.stored && output.media_type) {
          const video = output.media_type.startsWith("video");
          const media = node(video ? "video" : "img");
          media.className = "dashboard-preview";
          media.src = `/api/v1/assets/${encodeURIComponent(output.asset_id)}/file`;
          if (video) { media.controls = true; media.preload = "metadata"; } else { media.loading = "lazy"; media.alt = "Generated output"; }
          figure.append(media);
        }
        figure.append(link);
        outputs.append(figure);
      }
      historyCard.append(outputs);
      const details = node("details");
      details.open = previousCard?.card.querySelector("details")?.open || false;
      details.append(node("summary", "Request settings, references and outputs"), node("pre", JSON.stringify({ job: activity.service_job_id, completed_at: activity.ended_at, seed: activity.seed, negative_prompt: activity.negative_prompt, parameters: activity.parameters, inputs: activity.inputs, outputs: activity.outputs }, null, 2)));
      historyCard.append(details);
      historyCards.set(activity.id, { signature, card: historyCard });
      sections.push(historyCard);
    }
    const visibleIds = new Set(data.activities.map((activity) => activity.id));
    for (const id of historyCards.keys()) if (!visibleIds.has(id)) historyCards.delete(id);
    const desired = sections.length ? sections : [emptyState("No captured requests match these filters on this page.")];
    const keep = new Set(desired);
    for (const child of [...feed.children]) if (!keep.has(child)) child.remove();
    desired.forEach((c, index) => {
      if (feed.children[index] !== c) feed.insertBefore(c, feed.children[index] || null);
    });
    previous.disabled = offset === 0;
    next.disabled = !data.history.has_more;
    pageStatus.textContent = data.history.total ? `${offset + 1}–${offset + data.activities.length} of ${data.history.total}` : "No generations";
    sync.textContent = `● Live · updated ${new Date().toLocaleTimeString()} · summaries cover every generation matching the filters`;
  } catch (err) {
    sync.textContent = `Connection interrupted (${err.message}) — reconnecting automatically. Last data remains visible.`;
  } finally {
    busy = false;
    if (version !== selectionVersion) void refresh();
  }
}
previous.addEventListener("click", () => { offset = Math.max(0, offset - pageSize); selectionVersion++; void refresh(); });
next.addEventListener("click", () => { offset += pageSize; selectionVersion++; void refresh(); });

// ---------------------------------------------------------------- usage pages

async function loadUsage() {
  const status = $("usage-status");
  const list = $("usage-list");
  status.textContent = "Loading…";
  try {
    const params = apiParams({ limit: 50 });
    const data = await getJson("/api/v1/usage-captures", params);
    status.textContent = `${data.total} saved page(s)${data.total > data.items.length ? `, showing the latest ${data.items.length}` : ""}.`;
    if (!data.items.length) {
      list.replaceChildren(emptyState("No usage pages saved for these filters. On Higgsfield's Usage or Organization page, open the Faible toolbar popup and click “Save usage page”. Old exporter usage_raw.json files can be loaded with faible import-history."));
      return;
    }
    list.replaceChildren(...data.items.map(usageItem));
  } catch (err) {
    status.replaceChildren(errorState(err.message, loadUsage));
  }
}

function usageItem(u) {
  const box = node("article", null, "panel usage-item");
  box.append(node("h3", u.page_title || "Higgsfield page"));
  box.append(node("p", `${when(u.captured_at)} · saved by ${u.email || (u.source === "import" ? "an exporter file (no employee)" : "unknown")} · login ${u.login || "not detected"} · ${u.response_count} data responses · ${u.table_count} tables`, "muted small"));
  box.append(node("p", u.page_url, "muted small mono"));
  const target = node("div");
  const show = button("Show tables", async () => {
    show.disabled = true;
    target.replaceChildren(node("p", "Loading…", "muted"));
    try {
      const d = await getJson(`/api/v1/usage-captures/${encodeURIComponent(u.id)}`, new URLSearchParams());
      const parts = [];
      if (!d.tables.length) parts.push(emptyState("No tables on this page. The data responses and page text are kept with the capture."));
      for (const t of d.tables) {
        const [first = [], ...rest] = t;
        parts.push(table(first, rest.slice(0, 200)));
        if (rest.length > 200) parts.push(node("p", `+${rest.length - 200} more rows (export with faible export-history)`, "muted small"));
      }
      const more = node("details");
      more.append(node("summary", `Data responses (${d.responses.length}) and page text (${d.text_chars} characters)`));
      for (const r of d.responses) more.append(node("div", r.url, "mono small"));
      if (d.text_excerpt) more.append(node("pre", d.text_excerpt + (d.text_chars > d.text_excerpt.length ? "\n…" : "")));
      parts.push(more);
      target.replaceChildren(...parts);
      show.remove();
    } catch (err) {
      target.replaceChildren(errorState(err.message));
      show.disabled = false;
    }
  });
  box.append(show, target);
  return box;
}

// ---------------------------------------------------------------- Higgsfield history

let historyOffset = 0;
let historyLogin = "";

async function loadHistory() {
  const summary = $("history-summary");
  if (!summary) return;
  const status = $("h-status");
  status.textContent = "Loading…";
  try {
    const analytics = lastAnalytics || (await fetchAnalytics());
    if (analytics?.history) {
      const h = analytics.history;
      const t = h.totals;
      summary.replaceChildren(node("p", `${fmtNum(t.count)} records match the filters · ${fmtNum(t.unattributed)} unattributed (no employee captured them live) · unattributed reported cost ${credits(t.credits)}.${h.note ? ` ${h.note}` : ""}`, "notice"));
      const select = $("h-login");
      const logins = h.by_login.map((r) => r.key[0] || NONE);
      if (historyLogin && !logins.includes(historyLogin)) logins.push(historyLogin);
      select.replaceChildren(...[["", "All logins"], ...logins.map((l) => [l, l === NONE ? "Unknown login" : l])].map(([v, label]) => { const o = node("option", label); o.value = v; return o; }));
      select.value = historyLogin;
    }
    const data = await getJson("/api/v1/service-history", apiParams({ unattributed: $("h-unattributed").checked ? "true" : "", login: historyLogin, limit: historyPageSize, offset: historyOffset }));
    status.textContent = data.total ? `${historyOffset + 1}–${historyOffset + data.items.length} of ${data.total}` : "";
    $("h-page-status").textContent = status.textContent;
    $("h-previous").disabled = historyOffset === 0;
    $("h-next").disabled = historyOffset + data.items.length >= data.total;
    $("history-list").replaceChildren(...(data.items.length ? data.items.map(historyTile) : [emptyState("No Higgsfield history matches. The extension records history as you browse Higgsfield (use “Auto-scroll & capture” in its popup); old exporter files are loaded with faible import-history.")]));
  } catch (err) {
    status.replaceChildren(errorState(err.message, loadHistory));
  }
}

function historyTile(h) {
  const tile = node("article", null, "panel tile");
  const head = node("div", null, "head");
  head.append(node("strong", h.model_id || "Unknown model"), node("span", h.service_status || h.status, `pill status-${h.status}`), badge("service reported", "ev-service_reported"));
  tile.append(head);
  const out = h.outputs.find((o) => o.asset_id);
  tile.append(node("div", `${h.created_at ? when(h.created_at) : `first seen ${when(h.first_observed_at)}`} · login ${h.login || "unknown"}`, "muted small"));
  tile.append(node("div", h.credits_cost === null ? "Cost not reported" : `${fmtNum(h.credits_cost)} ${h.credits_currency}${h.credits_currency === "higgsfield" ? ` (≈ ${fmtNum(Number(h.credits_cost) / 100)} UI credits, estimate)` : ""}`, "small"));
  tile.append(node("div", h.prompt || "No prompt in the record", "prompt clamp"));
  if (h.fail_reason) tile.append(node("div", h.fail_reason, "bad small"));
  const who = h.attributed_to;
  tile.append(node("div", who ? `Captured live by ${who.email || "a Faible user"}${who.shot_code ? ` · ${who.shot_code}` : ""}` : "Unattributed: no employee's extension captured this job live", who ? "small" : "warn small"));
  const links = node("div", null, "links");
  if (out) links.append(anchor("Faible asset and chain", `/assets/${encodeURIComponent(out.asset_id)}`));
  for (const o of h.outputs) if (o.url) { const a = anchor(`Open ${o.kind || "output"} (Higgsfield CDN)`, o.url); a.target = "_blank"; a.rel = "noopener noreferrer"; links.append(a); }
  tile.append(links);
  tile.append(node("div", `job ${h.service_job_id} · seen ${h.observation_count}×`, "muted small mono"));
  return tile;
}

if ($("h-unattributed")) {
  $("h-unattributed").addEventListener("change", () => { historyOffset = 0; void loadHistory(); });
  $("h-login").addEventListener("change", () => { historyLogin = $("h-login").value; historyOffset = 0; void loadHistory(); });
  $("h-previous").addEventListener("click", () => { historyOffset = Math.max(0, historyOffset - historyPageSize); void loadHistory(); });
  $("h-next").addEventListener("click", () => { historyOffset += historyPageSize; void loadHistory(); });
}

// ---------------------------------------------------------------- start

syncControls();
showTab(state.tab);
if (state.tab !== "overview") void fetchAnalytics().catch(() => undefined); // fills the dropdowns
setInterval(() => { if (state.tab === "live" && !document.hidden) void refresh(); }, 2000);
setInterval(() => { if (state.tab === "overview" && !document.hidden) void loadOverview(true); }, 30000);
